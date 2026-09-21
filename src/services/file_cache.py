"""File caching service"""
import os
import asyncio
import base64
import hashlib
import hmac
import io
import time
import mimetypes
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlparse
from curl_cffi.requests import AsyncSession
from PIL import Image, UnidentifiedImageError
from ..core.config import config
from ..core.logger import debug_logger


class FileCache:
    """File caching service for videos"""

    def __init__(
        self,
        cache_dir: str = "tmp",
        default_timeout: int = 7200,
        proxy_manager=None,
        flow_client=None,
    ):
        """
        Initialize file cache

        Args:
            cache_dir: Cache directory path
            default_timeout: Default cache timeout in seconds (default: 2 hours)
            proxy_manager: ProxyManager instance for downloading files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_timeout = max(0, int(default_timeout))
        self.proxy_manager = proxy_manager
        self.flow_client = flow_client
        self._cleanup_task = None
        self._download_locks: Dict[str, asyncio.Lock] = {}

    def _is_cleanup_disabled(self) -> bool:
        return self.default_timeout <= 0

    def _get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """读取当前请求链路里绑定的浏览器指纹。"""
        if not self.flow_client or not hasattr(self.flow_client, "get_request_fingerprint"):
            return None

        try:
            fingerprint = self.flow_client.get_request_fingerprint()
            if isinstance(fingerprint, dict) and fingerprint:
                return fingerprint
        except Exception as e:
            debug_logger.log_warning(f"Get request fingerprint failed: {str(e)}")

        return None

    async def _resolve_download_proxy(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """根据媒体类型解析下载代理地址。"""
        if isinstance(fingerprint, dict):
            fingerprint_proxy = str(fingerprint.get("proxy_url") or "").strip()
            if fingerprint_proxy:
                return fingerprint_proxy

        if not self.proxy_manager:
            return None

        try:
            # 媒体下载（图片/视频）优先使用独立的上传/下载代理
            if media_type in ("image", "video") and hasattr(self.proxy_manager, "get_media_proxy_url"):
                return await self.proxy_manager.get_media_proxy_url()

            # 其他下载走请求代理
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()

            # 向后兼容旧实现
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as e:
            debug_logger.log_warning(f"Resolve download proxy failed: {str(e)}")

        return None

    async def _download_proxy_candidates(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> list[Optional[str]]:
        """Return ordered, distinct proxies so every media retry opens a new route."""
        candidates: list[Optional[str]] = []
        configured = os.getenv("FLOW2API_MEDIA_DOWNLOAD_PROXY_URLS", "")
        for value in configured.split(","):
            value = value.strip()
            if value and value not in candidates:
                candidates.append(value)

        resolved = await self._resolve_download_proxy(media_type, fingerprint=fingerprint)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
        if not candidates:
            candidates.append(None)
        return candidates

    @staticmethod
    def _validate_image_content(content: bytes, hinted_content_type: str = "") -> str:
        if not content:
            raise ValueError("downloaded image is empty")
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                image_format = (image.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("downloaded payload is not a complete image") from exc

        content_types = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
            "GIF": "image/gif",
        }
        content_type = content_types.get(image_format)
        if not content_type:
            raise ValueError(f"unsupported downloaded image format: {image_format or 'unknown'}")
        hinted = (hinted_content_type or "").split(";", 1)[0].strip().lower()
        if hinted and hinted.startswith("image/") and hinted != content_type:
            debug_logger.log_warning(
                f"Downloaded image content type mismatch: header={hinted}, decoded={content_type}"
            )
        return content_type

    async def _download_validated_image(self, url: str, file_path: Path) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("image source must be an HTTPS URL")

        fingerprint = self._get_request_fingerprint()
        headers = self._build_download_headers("image", fingerprint=fingerprint)
        candidates = await self._download_proxy_candidates("image", fingerprint=fingerprint)
        errors = []
        debug_logger.log_info(
            f"Downloading image from host={parsed.hostname} with {len(candidates)} route(s)"
        )
        for route_index, proxy_url in enumerate(candidates, start=1):
            try:
                async with AsyncSession() as session:
                    response = await session.get(
                        url,
                        timeout=60,
                        proxy=proxy_url,
                        headers=headers,
                        impersonate="chrome120",
                        verify=True,
                    )
                if response.status_code != 200:
                    raise OSError(f"media source returned HTTP {response.status_code}")
                content_type = self._validate_image_content(
                    response.content,
                    response.headers.get("content-type", ""),
                )
                self._write_cached_content(file_path, response.content)
                debug_logger.log_info(
                    f"Image cached through route={route_index} ({len(response.content)} bytes)"
                )
                return content_type
            except Exception as exc:
                errors.append(type(exc).__name__)
                debug_logger.log_warning(
                    f"Image download route={route_index} failed: {type(exc).__name__}"
                )
        raise OSError("image download failed across routes: " + ",".join(errors))

    @staticmethod
    def _r2_cache_config() -> Optional[Dict[str, Any]]:
        enabled = os.getenv("FLOW2API_R2_CACHE_ENABLED", "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None

        endpoint = os.getenv("FLOW2API_R2_GATEWAY_ENDPOINT", "").strip().rstrip("/")
        secret = os.getenv("FLOW2API_R2_GATEWAY_SECRET", "").strip()
        bucket = os.getenv("FLOW2API_R2_BUCKET", "").strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("R2 gateway endpoint must be an origin-only HTTPS URL")
        if len(secret) < 32 or not bucket or any(char in bucket for char in "/\\"):
            raise ValueError("R2 cache gateway configuration is incomplete")

        try:
            ttl = int(os.getenv("FLOW2API_R2_CACHE_TTL_SECONDS", "7200"))
            max_bytes = int(os.getenv("FLOW2API_R2_MAX_BYTES", str(64 * 1024 * 1024)))
        except ValueError as exc:
            raise ValueError("R2 cache numeric configuration is invalid") from exc
        if ttl < 60 or ttl > 7200:
            raise ValueError("R2 cache TTL must be between 60 and 7200 seconds")
        if max_bytes < 1024 or max_bytes > 256 * 1024 * 1024:
            raise ValueError("R2 cache maximum object size is invalid")
        return {
            "endpoint": endpoint,
            "secret": secret,
            "bucket": bucket,
            "ttl": ttl,
            "max_bytes": max_bytes,
        }

    @staticmethod
    def _r2_object_url(endpoint: str, object_key: str) -> str:
        escaped = "/".join(quote(segment, safe="") for segment in object_key.split("/"))
        return f"{endpoint}/objects/{escaped}"

    @staticmethod
    def _r2_signed_url(config: Dict[str, Any], object_key: str, expires_at: int) -> str:
        payload = f"GET\n{config['bucket']}\n{object_key}\n{expires_at}"
        digest = hmac.new(
            config["secret"].encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        query = urlencode({"expires": str(expires_at), "sig": signature})
        return f"{FileCache._r2_object_url(config['endpoint'], object_key)}?{query}"

    async def publish_cached_image(self, filename: str, fallback_base_url: str) -> str:
        """Upload a validated cached image to private R2 and return a two-hour URL.

        A gateway outage falls back only to the already downloaded and validated
        bytes. The supplier URL is never returned and generation is never replayed.
        """
        file_path = self.cache_dir / filename
        data = file_path.read_bytes()
        content_type = self._validate_image_content(data)
        r2_config = self._r2_cache_config()
        if r2_config is None:
            return f"{fallback_base_url.rstrip('/')}/tmp/{filename}"
        if len(data) > r2_config["max_bytes"]:
            raise ValueError("cached image exceeds R2 upload limit")

        object_key = f"images/videotmp/{filename}"
        expires_at = int(time.time()) + r2_config["ttl"]
        request_id = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:64]
        headers = {
            "Authorization": f"Bearer {r2_config['secret']}",
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "X-Opencodex-Bucket": r2_config["bucket"],
            "X-Opencodex-Meta-SHA256": hashlib.sha256(data).hexdigest(),
            "X-Opencodex-Meta-Request-Id": request_id,
            "X-Opencodex-Meta-Expires-At": str(expires_at),
        }
        try:
            async with AsyncSession() as session:
                response = await session.put(
                    self._r2_object_url(r2_config["endpoint"], object_key),
                    data=data,
                    headers=headers,
                    timeout=60,
                    verify=True,
                )
            if response.status_code != 204:
                raise OSError(f"R2 gateway returned HTTP {response.status_code}")
            debug_logger.log_info(
                f"Image published to private R2 ({len(data)} bytes, ttl={r2_config['ttl']}s)"
            )
            return self._r2_signed_url(r2_config, object_key, expires_at)
        except Exception as exc:
            debug_logger.log_error(
                error_message=f"R2 image publish failed: {type(exc).__name__}",
                status_code=0,
                response_text="",
            )
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"

    def _guess_extension(self, url: str, media_type: str) -> str:
        """尽量保留原始扩展名，未知时回退到默认值。"""
        path = urlparse(url).path or ""
        guessed, _ = mimetypes.guess_type(path)
        suffix = Path(path).suffix.lower()

        if media_type == "video":
            if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
                return suffix
            if guessed == "video/webm":
                return ".webm"
            if guessed == "video/quicktime":
                return ".mov"
            return ".mp4"

        if media_type == "image":
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
                return suffix
            if guessed == "image/png":
                return ".png"
            if guessed == "image/webp":
                return ".webp"
            if guessed == "image/gif":
                return ".gif"
            if guessed == "image/avif":
                return ".avif"
            if guessed == "image/bmp":
                return ".bmp"
            return ".jpg"

        return suffix

    def _build_download_headers(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """构建媒体下载请求头，优先复用当前打码浏览器指纹。"""
        headers = {
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                if media_type == "image"
                else "*/*"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://labs.google/",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        }

        if media_type == "image":
            headers["Sec-Fetch-Dest"] = "image"
        else:
            headers["Sec-Fetch-Dest"] = "video"

        if isinstance(fingerprint, dict):
            if fingerprint.get("user_agent"):
                headers["User-Agent"] = str(fingerprint["user_agent"])
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = str(fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])

        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        return headers

    def _write_cached_content(self, file_path: Path, content: bytes):
        """先写临时文件，再原子替换，避免并发读到半截文件。"""
        temp_path = file_path.with_suffix(f"{file_path.suffix}.part")
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
            temp_path.replace(file_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._is_cleanup_disabled():
            debug_logger.log_info("Cache cleanup disabled (timeout <= 0), skip starting cleanup task")
            return False
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            return True
        return True

    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def refresh_cleanup_task(self) -> bool:
        """Apply the latest timeout setting to the cleanup background task."""
        if self._is_cleanup_disabled():
            await self.stop_cleanup_task()
            return False
        return await self.start_cleanup_task()

    async def _cleanup_loop(self):
        """Background task to clean up expired files"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_expired_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Cleanup task error: {str(e)}",
                    status_code=0,
                    response_text=""
                )

    async def _cleanup_expired_files(self):
        """Remove expired cache files"""
        try:
            timeout = self.get_timeout()
            if timeout <= 0:
                return
            current_time = time.time()
            removed_count = 0

            for file_path in self.cache_dir.iterdir():
                timeout = self.get_timeout()
                if timeout <= 0:
                    debug_logger.log_info("Cache cleanup disabled during cleanup pass, stop deleting files")
                    break
                if file_path.is_file():
                    # Check file age
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > timeout:
                        try:
                            file_path.unlink()
                            removed_count += 1
                        except Exception:
                            pass

            if removed_count > 0:
                debug_logger.log_info(f"Cleanup: removed {removed_count} expired cache files")

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cleanup expired files: {str(e)}",
                status_code=0,
                response_text=""
            )

    def _generate_cache_filename(self, url: str, media_type: str) -> str:
        """Generate unique filename for cached file"""
        # Use URL hash as filename
        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = self._guess_extension(url, media_type)

        return f"{url_hash}{ext}"

    def _normalize_cache_error(self, error: Exception) -> str:
        """整理缓存错误，避免将底层命令异常直接暴露给用户。"""
        if isinstance(error, FileNotFoundError):
            missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
            return f"本机未安装 {missing_name}"

        message = str(error or "").strip()
        if not message:
            return "未知错误"

        if message.startswith("Failed to cache file:"):
            message = message.split(":", 1)[1].strip() or "未知错误"

        return message

    async def download_and_cache(self, url: str, media_type: str) -> str:
        """
        Download file from URL and cache it locally

        Args:
            url: File URL to download
            media_type: 'image' or 'video'

        Returns:
            Local cache filename
        """
        filename = self._generate_cache_filename(url, media_type)
        file_path = self.cache_dir / filename
        download_lock = self._download_locks.setdefault(filename, asyncio.Lock())

        async with download_lock:
            # Check if already cached and not expired
            if file_path.exists():
                if self._is_cleanup_disabled():
                    if media_type == "image":
                        self._validate_image_content(file_path.read_bytes())
                    return filename
                file_age = time.time() - file_path.stat().st_mtime
                if file_age < self.default_timeout:
                    try:
                        if media_type == "image":
                            self._validate_image_content(file_path.read_bytes())
                        debug_logger.log_info(f"Cache hit: {filename}")
                        return filename
                    except Exception:
                        debug_logger.log_warning(f"Discarding invalid cached media: {filename}")
                try:
                    file_path.unlink()
                except Exception:
                    pass

            if media_type == "image":
                await self._download_validated_image(url, file_path)
                return filename

            # Download file
            parsed = urlparse(url)
            debug_logger.log_info(f"Downloading media from host={parsed.hostname or 'unknown'}")

            fingerprint = self._get_request_fingerprint()
            proxy_url = await self._resolve_download_proxy(media_type, fingerprint=fingerprint)
            headers = self._build_download_headers(media_type, fingerprint=fingerprint)

            # Try method 1: curl_cffi with browser impersonation
            try:
                async with AsyncSession() as session:
                    response = await session.get(
                        url,
                        timeout=60,
                        proxy=proxy_url,
                        headers=headers,
                        impersonate="chrome120",
                        verify=False
                    )

                    if response.status_code == 200 and response.content:
                        self._write_cached_content(file_path, response.content)
                        debug_logger.log_info(
                            f"File cached (curl_cffi): {filename} ({len(response.content)} bytes)"
                        )
                        return filename
                    debug_logger.log_warning(
                        f"curl_cffi failed with HTTP {response.status_code}, trying wget..."
                    )

            except Exception as e:
                debug_logger.log_warning(f"curl_cffi failed: {str(e)}, trying wget...")

            # Try method 2: wget command
            try:
                import subprocess

                wget_cmd = [
                    "wget",
                    "-q",
                    "-O", str(file_path),
                    "--timeout=60",
                    "--tries=3",
                    f"--user-agent={headers.get('User-Agent', '')}",
                    f"--header=Accept: {headers.get('Accept', '*/*')}",
                    f"--header=Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                    f"--header=Connection: {headers.get('Connection', 'keep-alive')}",
                    f"--header=Referer: {headers.get('Referer', 'https://labs.google/')}",
                ]

                if "sec-ch-ua" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua: {headers['sec-ch-ua']}")
                if "sec-ch-ua-mobile" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}")
                if "sec-ch-ua-platform" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua-platform: {headers['sec-ch-ua-platform']}")

                if proxy_url:
                    env = os.environ.copy()
                    env["http_proxy"] = proxy_url
                    env["https_proxy"] = proxy_url
                else:
                    env = None

                wget_cmd.append(url)
                result = subprocess.run(wget_cmd, capture_output=True, timeout=90, env=env)

                if result.returncode == 0 and file_path.exists():
                    file_size = file_path.stat().st_size
                    if file_size > 0:
                        debug_logger.log_info(f"File cached (wget): {filename} ({file_size} bytes)")
                        return filename
                    raise Exception("Downloaded file is empty")

                error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                debug_logger.log_warning(f"wget failed: {error_msg}, trying curl...")

            except FileNotFoundError:
                debug_logger.log_warning("wget not found, trying curl...")
            except Exception as e:
                debug_logger.log_warning(f"wget failed: {str(e)}, trying curl...")

            # Try method 3: system curl command
            try:
                import subprocess

                curl_cmd = [
                    "curl",
                    "-L",
                    "-s",
                    "-o", str(file_path),
                    "--max-time", "60",
                    "-H", f"Accept: {headers.get('Accept', '*/*')}",
                    "-H", f"Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                    "-H", f"Connection: {headers.get('Connection', 'keep-alive')}",
                    "-H", f"Referer: {headers.get('Referer', 'https://labs.google/')}",
                    "-A", headers.get("User-Agent", ""),
                ]

                if "sec-ch-ua" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua: {headers['sec-ch-ua']}"])
                if "sec-ch-ua-mobile" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}"])
                if "sec-ch-ua-platform" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua-platform: {headers['sec-ch-ua-platform']}"])
                if proxy_url:
                    curl_cmd.extend(["-x", proxy_url])

                curl_cmd.append(url)
                result = subprocess.run(curl_cmd, capture_output=True, timeout=90)

                if result.returncode == 0 and file_path.exists():
                    file_size = file_path.stat().st_size
                    if file_size > 0:
                        debug_logger.log_info(f"File cached (curl): {filename} ({file_size} bytes)")
                        return filename
                    raise Exception("Downloaded file is empty")

                error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                raise Exception(f"curl command failed: {error_msg}")

            except FileNotFoundError as e:
                normalized_error = self._normalize_cache_error(e)
                debug_logger.log_error(
                    error_message=f"Failed to download file: {str(e)}",
                    status_code=0,
                    response_text=str(e)
                )
                raise Exception(normalized_error) from e
            except Exception as e:
                normalized_error = self._normalize_cache_error(e)
                debug_logger.log_error(
                    error_message=f"Failed to download file: {str(e)}",
                    status_code=0,
                    response_text=str(e)
                )
                raise Exception(normalized_error) from e

    async def cache_base64_video(self, base64_data: str) -> str:
        """Cache base64 encoded video data to local file

        Args:
            base64_data: Base64 encoded video data (without data:video/... prefix)

        Returns:
            Local cache filename
        """
        import base64 as _b64
        import uuid as _uuid

        unique_id = hashlib.md5(f"{_uuid.uuid4()}{time.time()}".encode()).hexdigest()
        filename = f"{unique_id}.mp4"
        file_path = self.cache_dir / filename

        try:
            video_data = _b64.b64decode(base64_data)
            self._write_cached_content(file_path, video_data)
            debug_logger.log_info(f"Base64 video cached: {filename} ({len(video_data)} bytes)")
            return filename
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cache base64 video: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 video: {str(e)}")

    async def cache_base64_image(self, base64_data: str, resolution: str = "") -> str:
        """
        Cache base64 encoded image data to local file

        Args:
            base64_data: Base64 encoded image data (without data:image/... prefix)
            resolution: Resolution info for filename (e.g., "4K", "2K")

        Returns:
            Local cache filename
        """
        import uuid

        # Generate unique filename
        unique_id = hashlib.md5(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()
        suffix = f"_{resolution}" if resolution else ""
        filename = f"{unique_id}{suffix}.jpg"
        file_path = self.cache_dir / filename

        try:
            # Decode base64 and save to file
            image_data = base64.b64decode(base64_data)
            self._validate_image_content(image_data)
            self._write_cached_content(file_path, image_data)
            debug_logger.log_info(f"Base64 image cached: {filename} ({len(image_data)} bytes)")
            return filename
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cache base64 image: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 image: {str(e)}")

    def get_cache_path(self, filename: str) -> Path:
        """Get full path to cached file"""
        return self.cache_dir / filename

    def set_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        self.default_timeout = max(0, int(timeout))
        debug_logger.log_info(f"Cache timeout updated to {timeout} seconds")

    def get_timeout(self) -> int:
        """Get current cache timeout"""
        return self.default_timeout

    async def clear_all(self):
        """Clear all cached files"""
        try:
            removed_count = 0
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        removed_count += 1
                    except Exception:
                        pass

            debug_logger.log_info(f"Cache cleared: removed {removed_count} files")
            return removed_count

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to clear cache: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise
