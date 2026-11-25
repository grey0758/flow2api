"""Bounded, request-local image diagnostics. No I/O on token-return paths."""
import asyncio
import contextvars
import copy
import json
import math
import errno
import os
import resource
import re
import time
import uuid

# DOUBLE byte counters work with the deployed curl_cffi 0.7.3 (OFF_T does not).
CURL_FIELDS = {
    0x300003: "total_seconds", 0x300004: "dns_seconds",
    0x300005: "connect_seconds", 0x300021: "tls_seconds",
    0x300006: "pretransfer_seconds", 0x300011: "first_byte_seconds",
    0x300007: "uploaded_bytes", 0x300008: "downloaded_bytes",
    0x300009: "download_bytes_per_second", 0x30000a: "upload_bytes_per_second",
    0x20002e: "http_version",
}
CURL_INFOS = list(CURL_FIELDS)
STAGES = {"google_generation", "google_upsample", "reference_upload",
          "input_reference_download", "gemini_output_download", "output_media_download",
          "output_publish", "provider_create", "provider_result", "provider_sync"}
_provider = contextvars.ContextVar("image_provider_diagnostics", default=None)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e15 and math.isfinite(value)


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value) else "UNKNOWN_ERROR"


def curl_code(error):
    for _ in range(3):
        if error is None:
            break
        code = getattr(error, "code", None)
        if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 999:
            return code
        # Parse only curl's numeric marker, never retain exception text.
        match = re.search(r"curl: \(([0-9]{1,3})\)", str(error))
        if match:
            return int(match.group(1))
        error = getattr(error, "__cause__", None)
    return None



def resource_failure(error, phase=None):
    """Fixed error-chain enums/numbers only; never retain exception messages.

    Best effort even at EMFILE. This runs on failure, never on a sync solver's
    token-to-dispatch success path. It does not grant retry eligibility.
    """
    result = {"schema": 1, "chain": []}
    allowed = {"OSError", "FileNotFoundError", "PermissionError", "BlockingIOError",
               "MemoryError", "RuntimeError", "ValueError", "TypeError", "TimeoutError",
               "OperationalError", "DatabaseError", "InterfaceError", "CancelledError",
               "ConnectionError", "ConnectTimeout", "SSLError", "CurlError",
               "CurlCffiWarning", "RequestsError", "Exception"}
    seen = set()
    current = error
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        item = {"type": name if name in allowed else "other"}
        for attr in ("errno", "sqlite_errorcode"):
            value = getattr(current, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100000:
                item[attr] = value
        if item.get("errno") in {errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN}:
            item["resource_error"] = {errno.EMFILE: "process_fd_exhausted",
                errno.ENFILE: "system_fd_exhausted", errno.ENOMEM: "memory_unavailable",
                errno.EAGAIN: "resource_temporarily_unavailable"}[item["errno"]]
        value = curl_code(current)
        if value is not None:
            item["curl_code"] = value
        result["chain"].append(item)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    if phase in {"proxy_selection", "upload_attempt", "transport", "response_decode", "configuration"}:
        result["phase"] = phase
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        result.update(nofile_soft=soft, nofile_hard=hard)
    except (OSError, ValueError):
        pass
    try:
        result["open_fds"] = len(os.listdir("/proc/self/fd"))
    except OSError as exc:
        result["fd_snapshot_errno"] = exc.errno
    return result

def begin_transfer(trace, stage):
    if isinstance(trace, dict):
        trace.update(schema=1, stage=stage if stage in STAGES else "unknown",
                     dispatch_started=False, submission_outcome="not_started",
                     last_observed_phase="not_observed")
    return time.monotonic()


def capture_response(trace, response):
    if not isinstance(trace, dict) or response is None:
        return
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        trace["http_status_code"] = status
    infos = getattr(response, "infos", None)
    if isinstance(infos, dict):
        for key, name in CURL_FIELDS.items():
            if number(infos.get(key)):
                trace[name] = round(infos[key], 6)
    for name, phase in (("dns_seconds", "dns_completed"), ("connect_seconds", "connection_completed"),
                        ("tls_seconds", "tls_completed"), ("pretransfer_seconds", "transfer_started"),
                        ("uploaded_bytes", "request_bytes_sent"), ("first_byte_seconds", "response_started"),
                        ("downloaded_bytes", "response_bytes_received")):
        if trace.get(name, 0) > 0:
            trace["last_observed_phase"] = phase


def finish_transfer(trace, started, response=None, error=None, phase="transport"):
    if not isinstance(trace, dict):
        return
    capture_response(trace, response or getattr(error, "response", None))
    trace["duration_ms"] = max(0, round((time.monotonic() - started) * 1000, 3))
    status = trace.get("http_status_code", 0)
    if response is not None:
        trace["response_complete"] = True
        trace["submission_outcome"] = "http_response"
    elif trace.get("dispatch_started"):
        # Neither libcurl timeout nor its timings prove whether Google committed.
        trace["submission_outcome"] = "unknown"
    if error is None:
        trace["success"] = True
        return
    trace["success"] = False
    trace["resource_failure"] = resource_failure(error, phase)
    trace["failure_phase"] = phase if phase in {"transport", "response_decode", "image_decode", "cache_write", "local_read", "configuration"} else "unknown"
    code = curl_code(error)
    if code is not None:
        trace["curl_code"] = code
    if isinstance(error, asyncio.CancelledError):
        kind = "cancelled"
    elif phase != "transport":
        kind = "local_processing_error"
    elif status >= 400:
        kind = "http_error"
    elif code == 28 or isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        kind = "network_timeout"
    else:
        kind = {5: "proxy_dns_error", 6: "dns_error", 7: "connect_error", 35: "tls_error",
                51: "tls_verification_error", 60: "tls_verification_error", 55: "send_error",
                56: "receive_error"}.get(code, "transport_error")
    trace["failure_kind"] = kind
    if kind == "network_timeout":
        # Last progress is evidence; it is not the timeout's root cause/phase.
        trace["timeout_phase"] = "unknown"
    if response is not None and phase == "response_decode":
        trace["submission_outcome"] = "http_response_unparsed"


def reset_provider():
    _provider.set(None)


def provider_snapshot():
    obj = _provider.get()
    return copy.deepcopy(obj.data) if obj is not None else {}


class ProviderTrace:
    def __init__(self, provider, task_type=None):
        self.started = time.monotonic()
        self.data = {"schema": 1, "provider": provider if provider in {"ezcaptcha", "eznew", "yescaptcha", "capsolver", "capmonster"} else "unknown",
                     "create_attempts": [], "poll_count": 0, "outcome": "started"}
        if isinstance(task_type, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", task_type):
            self.data["task_type"] = task_type
        self.event = None
        self.phase = "transport"
        self.response = None
        _provider.set(self)

    def begin(self, stage):
        self.event = {}
        self.phase = "transport"
        self.response = None
        self.call_started = begin_transfer(self.event, stage)
        self.event["dispatch_started"] = True
        if stage == "provider_create":
            if len(self.data["create_attempts"]) < 5:
                self.data["create_attempts"].append(self.event)
        else:
            self.data["result"] = self.event
            if stage == "provider_result":
                self.data["poll_count"] += 1

    def received(self, response):
        self.response = response
        capture_response(self.event, response)
        self.phase = "response_decode"

    def parsed(self, body):
        finish_transfer(self.event, self.call_started, self.response)
        if not isinstance(body, dict):
            return
        error_id = body.get("errorId")
        if isinstance(error_id, int) and not isinstance(error_id, bool) and 0 <= error_id <= 1_000_000:
            self.event["provider_error_id"] = error_id
        if body.get("errorCode"):
            self.event["provider_error_code"] = safe_code(body["errorCode"])
        if body.get("status") in {"ready", "processing", "idle"}:
            self.event["provider_status"] = body["status"]
        self.phase = "response_decode"

    def failed(self, error):
        if self.event is not None:
            finish_transfer(self.event, self.call_started, self.response, error, self.phase)
        self.finish("cancelled" if isinstance(error, asyncio.CancelledError) else "transport_or_schema_error")

    def finish(self, outcome):
        self.data["outcome"] = outcome if outcome in {"token_returned", "create_failed", "result_failed", "poll_timeout", "ready_without_token", "sync_failed", "missing_configuration", "transport_or_schema_error", "cancelled"} else "unknown"
        self.data["duration_ms"] = max(0, round((time.monotonic() - self.started) * 1000, 3))


def emit_transfer_failure(trace):
    """Preselection/response-conversion failures have no Flow row; emit safe event.

    Caller-supplied URLs/IDs and image bytes are intentionally never accepted.
    """
    numeric = set(CURL_FIELDS.values()) | {"schema", "duration_ms", "http_status_code", "curl_code"}
    fixed = {"stage": STAGES | {"unknown"}, "submission_outcome": {"not_started", "unknown", "http_response", "http_response_unparsed"},
             "last_observed_phase": {"not_observed", "dns_completed", "connection_completed", "tls_completed", "transfer_started", "request_bytes_sent", "response_started", "response_bytes_received"},
             "failure_phase": {"transport", "response_decode", "image_decode", "cache_write", "local_read", "configuration", "unknown"},
             "failure_kind": {"cancelled", "local_processing_error", "http_error", "network_timeout", "transport_error", "proxy_dns_error", "dns_error", "connect_error", "tls_error", "tls_verification_error", "send_error", "receive_error"},
             "timeout_phase": {"unknown"}}
    event = {"event_id": uuid.uuid4().hex}
    for key, value in trace.items():
        if key in numeric and number(value):
            event[key] = value
        elif key in fixed and isinstance(value, str) and value in fixed[key]:
            event[key] = value
        elif key in {"dispatch_started", "success", "response_complete"} and isinstance(value, bool):
            event[key] = value
    try:
        print("[IMAGE_DIAGNOSTIC] " + json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
    except Exception:
        pass  # Diagnostic output cannot change delivery.
