import math
import json
import unittest

from starlette.requests import Request

from src.api.routes import (
    FLOW_TOP_P_COMPATIBILITY_DEFAULT,
    FlowTopPCompatibilityError,
    create_chat_completion,
    generate_content,
    _normalize_gemini_request,
    _normalize_openai_request,
    _validate_flow_top_p,
)
from src.core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
    GenerationConfigParam,
)


class TopPCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _raw_request(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "headers": [(b"host", b"videotmp.opencodex.uk")],
                "server": ("videotmp.opencodex.uk", 443),
                "client": ("127.0.0.1", 12345),
            }
        )

    def test_absent_and_default_top_p_are_accepted(self):
        _validate_flow_top_p(None)
        _validate_flow_top_p(FLOW_TOP_P_COMPATIBILITY_DEFAULT)

    def test_non_default_top_p_fails_closed(self):
        for value in (0.0, 0.8, math.nextafter(1.0, 0.0)):
            with self.subTest(value=value):
                with self.assertRaises(FlowTopPCompatibilityError) as caught:
                    _validate_flow_top_p(value)

                self.assertEqual(caught.exception.code, "unsupported_parameter")
                self.assertIn("silently ignored", str(caught.exception))

    def test_out_of_range_or_non_finite_top_p_is_invalid(self):
        for value in (-0.1, 1.1, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(FlowTopPCompatibilityError) as caught:
                    _validate_flow_top_p(value)
                self.assertEqual(caught.exception.code, "invalid_parameter_value")

    async def test_openai_top_p_is_preserved_for_compatibility_check(self):
        request = ChatCompletionRequest(
            model="gemini-3.1-flash-image-landscape",
            messages=[ChatMessage(role="user", content="schema-only test")],
            top_p=0.8,
        )

        normalized = await _normalize_openai_request(request)

        self.assertEqual(normalized.top_p, 0.8)

    async def test_gemini_top_p_is_preserved_for_compatibility_check(self):
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    role="user",
                    parts=[GeminiPart(text="schema-only test")],
                )
            ],
            generationConfig=GenerationConfigParam(topP=0.8),
        )

        normalized = await _normalize_gemini_request(
            "gemini-3.1-flash-image-landscape",
            request,
        )

        self.assertEqual(normalized.top_p, 0.8)

    def test_gemini_snake_case_alias_is_accepted(self):
        config = GenerationConfigParam.model_validate({"top_p": 0.8})
        self.assertEqual(config.topP, 0.8)

    async def test_openai_non_default_returns_structured_400_before_generation(self):
        response = await create_chat_completion(
            ChatCompletionRequest(
                model="gemini-3.1-flash-image-landscape",
                messages=[ChatMessage(role="user", content="must not generate")],
                top_p=0.8,
            ),
            self._raw_request("/v1/chat/completions"),
            api_key="test-only",
        )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["param"], "top_p")
        self.assertEqual(payload["error"]["code"], "unsupported_parameter")

    async def test_gemini_non_default_returns_structured_400_before_generation(self):
        response = await generate_content(
            "gemini-3.1-flash-image-landscape",
            GeminiGenerateContentRequest(
                contents=[
                    GeminiContent(
                        role="user",
                        parts=[GeminiPart(text="must not generate")],
                    )
                ],
                generationConfig=GenerationConfigParam(topP=0.8),
            ),
            self._raw_request(
                "/v1beta/models/gemini-3.1-flash-image-landscape:generateContent"
            ),
            api_key="test-only",
        )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["status"], "INVALID_ARGUMENT")
        self.assertIn("silently ignored", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
