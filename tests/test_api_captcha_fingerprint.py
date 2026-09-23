"""Verify API-mode captcha injects solution user_agent into the request fingerprint.

背景: YesCaptcha / CapMonster / EzCaptcha / CapSolver 返回的 solution 包含
gRecaptchaResponse 与 userAgent。Google reCAPTCHA V3 评估会校验 token 与
提交请求的 User-Agent 一致性, 因此调用 Flow API 时必须沿用打码服务返回的
UA, 否则服务端判定 UNUSUAL_ACTIVITY 并返回 reCAPTCHA evaluation failed。
"""

import contextvars
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from src.services.flow_client import FlowClient


class _FakeProxyManager:
    async def get_request_proxy_url(self):
        return None


class _FakeAsyncSession:
    """模拟 curl_cffi 的 AsyncSession: createTask 返回 taskId, getTaskResult 返回 ready。"""

    def __init__(self):
        self._calls = 0
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self._calls += 1
        self.calls.append((args, kwargs))
        response = MagicMock()
        if self._calls == 1:
            # createTask
            response.status_code = 200
            response.json.return_value = {
                "errorId": 0,
                "taskId": "tid-xyz",
            }
        else:
            # getTaskResult
            response.status_code = 200
            response.json.return_value = {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "gRecaptchaResponse": "token-abc",
                    "userAgent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/147.0.0.0 Safari/537.36"
                    ),
                },
            }
        return response


class ApiCaptchaFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_captcha_returns_token_and_user_agent(self):
        """_get_api_captcha_token 必须返回 (token, userAgent) 元组。"""
        flow = FlowClient.__new__(FlowClient)
        flow.proxy_manager = _FakeProxyManager()
        flow._request_fingerprint_ctx = contextvars.ContextVar(
            "flow_request_fingerprint_test",
            default=None,
        )
        flow._user_agent_cache = {}
        fake_session = _FakeAsyncSession()

        with patch("src.services.flow_client.AsyncSession", lambda *a, **kw: fake_session), \
             patch("src.services.flow_client.config") as cfg, \
             patch("asyncio.sleep", new=AsyncMock()):
            cfg.yescaptcha_api_key = "key"
            cfg.yescaptcha_base_url = "https://api.yescaptcha.com"
            cfg.yescaptcha_task_type = "RecaptchaV3TaskProxylessM1"
            cfg.debug_enabled = False

            result = await flow._get_api_captcha_token(
                method="yescaptcha",
                project_id="proj-1",
                action="IMAGE_GENERATION",
            )

        self.assertIsNotNone(result, "函数不应返回 None, 因为我们 mock 了 ready 状态")
        self.assertIsInstance(result, tuple, "_get_api_captcha_token 应返回 (token, userAgent) 元组")
        token, user_agent = result
        self.assertEqual(token, "token-abc")
        self.assertIn("Windows", user_agent, "userAgent 应当来自打码服务 solution, 包含 Windows")
        self.assertIn("Chrome/147", user_agent)
        create_payload = fake_session.calls[0][1]["json"]
        self.assertEqual(
            create_payload["task"]["websiteURL"],
            "https://flow.google.com/project/proj-1",
        )

    async def test_api_captcha_terminal_error_stops_polling_immediately(self):
        class _TerminalErrorSession(_FakeAsyncSession):
            async def post(self, *args, **kwargs):
                self._calls += 1
                self.calls.append((args, kwargs))
                response = MagicMock()
                response.status_code = 200
                if self._calls == 1:
                    response.json.return_value = {
                        "errorId": 0,
                        "taskId": "tid-invalid-domain",
                    }
                else:
                    response.json.return_value = {
                        "errorId": 1,
                        "errorCode": "ERROR_RECAPTCHA_INVALID_DOMAIN",
                    }
                return response

        flow = FlowClient.__new__(FlowClient)
        flow.proxy_manager = _FakeProxyManager()
        flow._request_fingerprint_ctx = contextvars.ContextVar(
            "flow_request_fingerprint_terminal_error_test",
            default=None,
        )
        flow._user_agent_cache = {}
        fake_session = _TerminalErrorSession()

        with patch("src.services.flow_client.AsyncSession", lambda *a, **kw: fake_session), \
             patch("src.services.flow_client.config") as cfg, \
             patch("asyncio.sleep", new=AsyncMock()) as sleep:
            cfg.yescaptcha_api_key = "key"
            cfg.yescaptcha_base_url = "https://api.yescaptcha.com"
            cfg.yescaptcha_task_type = "RecaptchaV3TaskProxylessM1S9"
            cfg.debug_enabled = False

            result = await flow._get_api_captcha_token(
                method="yescaptcha",
                project_id="proj-1",
                action="IMAGE_GENERATION",
            )

        self.assertIsNone(result)
        self.assertEqual(fake_session._calls, 2)
        sleep.assert_not_awaited()

    def test_current_flow_request_context_uses_modern_project_url(self):
        flow = FlowClient.__new__(FlowClient)
        flow._request_fingerprint_ctx = contextvars.ContextVar(
            "flow_request_fingerprint_current_context_test",
            default=None,
        )
        headers = flow._build_labs_request_context_headers("proj-1")
        self.assertEqual(headers["Origin"], "https://flow.google.com")
        self.assertEqual(
            headers["Referer"],
            "https://flow.google.com/project/proj-1",
        )


if __name__ == "__main__":
    unittest.main()
