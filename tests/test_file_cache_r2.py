import asyncio
import base64
import hashlib
import hmac
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

from src.services import file_cache as file_cache_module
from src.services.file_cache import FileCache


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), color=(20, 40, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", content_type="image/png"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class FileCacheR2Tests(unittest.TestCase):
    def test_download_retries_same_image_on_next_proxy(self):
        attempts = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, **kwargs):
                attempts.append(kwargs.get("proxy"))
                if len(attempts) == 1:
                    raise OSError("TLS connection reset")
                return FakeResponse(content=png_bytes())

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            file_cache_module, "AsyncSession", FakeSession
        ), patch.dict(os.environ, {
            "FLOW2API_MEDIA_DOWNLOAD_PROXY_URLS": (
                "http://primary.invalid:8080,http://backup.invalid:8080"
            )
        }, clear=False):
            cache = FileCache(cache_dir=temp_dir, default_timeout=7200)
            filename = asyncio.run(
                cache.download_and_cache("https://flow-content.google/image/example", "image")
            )
            self.assertEqual(
                attempts,
                ["http://primary.invalid:8080", "http://backup.invalid:8080"],
            )
            self.assertEqual((Path(temp_dir) / filename).read_bytes(), png_bytes())

    def test_publish_private_r2_url_with_two_hour_expiry(self):
        uploads = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def put(self, url, **kwargs):
                uploads.append((url, kwargs))
                return FakeResponse(status_code=204)

        secret = "s" * 48
        environment = {
            "FLOW2API_R2_CACHE_ENABLED": "true",
            "FLOW2API_R2_GATEWAY_ENDPOINT": "https://assets.example.test",
            "FLOW2API_R2_GATEWAY_SECRET": secret,
            "FLOW2API_R2_BUCKET": "opencodex-videotmp-generated-assets",
            "FLOW2API_R2_CACHE_TTL_SECONDS": "7200",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            file_cache_module, "AsyncSession", FakeSession
        ), patch.dict(os.environ, environment, clear=False):
            cache = FileCache(cache_dir=temp_dir, default_timeout=7200)
            filename = "sample.png"
            (Path(temp_dir) / filename).write_bytes(png_bytes())
            signed_url = asyncio.run(
                cache.publish_cached_image(filename, "https://origin.invalid")
            )

        self.assertEqual(len(uploads), 1)
        upload_url, upload = uploads[0]
        self.assertEqual(
            upload_url,
            "https://assets.example.test/objects/images/videotmp/sample.png",
        )
        self.assertEqual(
            upload["headers"]["X-Opencodex-Bucket"],
            "opencodex-videotmp-generated-assets",
        )
        self.assertTrue(upload["headers"]["X-Opencodex-Meta-Expires-At"].isdigit())
        self.assertEqual(upload["data"], png_bytes())

        parsed = urlparse(signed_url)
        query = parse_qs(parsed.query)
        expires = query["expires"][0]
        object_key = unquote(parsed.path.removeprefix("/objects/"))
        payload = f"GET\nopencodex-videotmp-generated-assets\n{object_key}\n{expires}"
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        self.assertEqual(query["sig"], [expected])

    def test_r2_upload_failure_returns_validated_inline_image(self):
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def put(self, _url, **_kwargs):
                raise OSError("gateway unavailable")

        environment = {
            "FLOW2API_R2_CACHE_ENABLED": "true",
            "FLOW2API_R2_GATEWAY_ENDPOINT": "https://assets.example.test",
            "FLOW2API_R2_GATEWAY_SECRET": "s" * 48,
            "FLOW2API_R2_BUCKET": "opencodex-videotmp-generated-assets",
            "FLOW2API_R2_CACHE_TTL_SECONDS": "7200",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            file_cache_module, "AsyncSession", FakeSession
        ), patch.dict(os.environ, environment, clear=False):
            cache = FileCache(cache_dir=temp_dir, default_timeout=7200)
            filename = "sample.png"
            (Path(temp_dir) / filename).write_bytes(png_bytes())
            result = asyncio.run(
                cache.publish_cached_image(filename, "https://origin.invalid")
            )

        self.assertTrue(result.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(result.split(",", 1)[1]), png_bytes())


if __name__ == "__main__":
    unittest.main()
