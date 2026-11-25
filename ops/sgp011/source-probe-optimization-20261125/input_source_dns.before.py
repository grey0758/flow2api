"""Bounded alternate CDN resolution for one operator-verified source host.

This changes only an existing pre-generation reference GET retry. Host/SNI and
path stay unchanged; Google RPC, output downloads and global DNS stay intact.
"""
import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession

SOURCE_HOST = "oss.zjydai.com"
RESOLVERS = (
    "https://dns.google/resolve?name=oss.zjydai.com&type=A",
    "https://cloudflare-dns.com/dns-query?name=oss.zjydai.com&type=A",
)


def supported_source(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == SOURCE_HOST
                and parsed.port in (None, 443) and parsed.username is None
                and parsed.password is None)
    except (TypeError, ValueError):
        return False


def public_answers(payload):
    """Only global IPv4 A records on the authenticated resolver's CNAME chain."""
    if not isinstance(payload, dict) or payload.get("Status") != 0:
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list) or len(answers) > 64:
        return []
    names = {SOURCE_HOST}
    for _ in range(8):
        before = len(names)
        for row in answers:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name", "")).lower().rstrip(".")
            if name in names and row.get("type") == 5:
                target = str(row.get("data", "")).lower().rstrip(".")
                if target and len(target) <= 253:
                    names.add(target)
        if len(names) == before:
            break
    result = []
    for row in answers:
        if not isinstance(row, dict) or row.get("type") != 1:
            continue
        if str(row.get("name", "")).lower().rstrip(".") not in names:
            continue
        try:
            ip = ipaddress.ip_address(row.get("data", ""))
            if ip.version == 4 and ip.is_global and not ip.is_multicast and str(ip) not in result:
                result.append(str(ip))
        except (TypeError, ValueError):
            continue
    return result[:2]


async def resolver_answers(endpoint):
    try:
        async with AsyncSession(trust_env=False) as session:
            response = await session.get(
                endpoint, timeout=2.5, verify=True, allow_redirects=False,
                proxies={"http": "", "https": ""},
                headers={"Accept": "application/dns-json"},
            )
            if response.status_code != 200 or len(response.content) > 65536:
                return []
            return public_answers(response.json())
    except asyncio.CancelledError:
        raise
    except Exception:
        return []


async def source_range_reachable(ip, url):
    """Verify original-host TLS and a bounded sample, never deliver this sample."""
    try:
        async with asyncio.timeout(2):
            options = {CurlOpt.RESOLVE: [SOURCE_HOST + ":443:" + ip]}
            async with AsyncSession(trust_env=False, curl_options=options) as session:
                async with session.stream(
                    "GET", url, timeout=2, verify=True, allow_redirects=False,
                    proxies={"http": "", "https": ""}, impersonate="chrome120",
                    headers={"Range": "bytes=0-65535", "Accept-Encoding": "identity",
                             "Referer": "https://labs.google/"},
                ) as response:
                    if response.status_code not in (200, 206):
                        return None
                    received = 0
                    async for chunk in response.aiter_content():
                        received += len(chunk)
                        if received >= 65536:
                            break
                    if received <= 0:
                        return None
        return ip
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def alternate_source_transport(url, remaining):
    """Return one fresh TLS/sample-verified public CDN candidate within 5s.

    Absence/failure preserves the ordinary retry. Cancellation always propagates.
    No endpoint/IP is cached or hardcoded; DNS TTL cannot become a stale pin.
    """
    if not supported_source(url) or remaining < 8:
        return {}, {"dns_route": "system"}
    started = time.monotonic()
    trace = {"dns_route": "system", "alternate_dns_attempted": True}
    tasks = []
    try:
        async with asyncio.timeout(min(5, remaining - 3)):
            groups = await asyncio.gather(*(resolver_answers(url) for url in RESOLVERS))
            # Interleave answers so one resolver cannot occupy every probe.
            candidates = []
            for index in range(2):
                for group in groups:
                    if index < len(group) and group[index] not in candidates:
                        candidates.append(group[index])
            trace["alternate_candidates"] = len(candidates)
            tasks = [asyncio.create_task(source_range_reachable(ip, url)) for ip in candidates]
            for result in asyncio.as_completed(tasks):
                selected = await result
                if selected:
                    trace.update(dns_route="public_doh_range", alternate_tls_verified=True,
                                 alternate_range_verified=True)
                    options = {
                        CurlOpt.RESOLVE: [SOURCE_HOST + ":443:" + selected],
                        CurlOpt.CONNECTTIMEOUT_MS: 8000,
                    }
                    return {"curl_options": options, "verify": True}, trace
    except asyncio.CancelledError:
        raise
    except Exception:
        trace["alternate_resolution_failed"] = True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        trace["alternate_dns_ms"] = round((time.monotonic() - started) * 1000, 3)
    return {}, trace
