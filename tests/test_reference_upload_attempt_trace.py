import json
import unittest
from unittest.mock import AsyncMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient


class ReferenceUploadAttemptTraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_method = config.captcha_method
        self.previous_retries = config.flow_max_retries
        config.set_captcha_method('browser')
        config.set_flow_max_retries(3)

    async def asyncTearDown(self):
        config.set_captcha_method(self.previous_method)
        config.set_flow_max_retries(self.previous_retries)

    async def test_failed_attempts_survive_success_without_private_error_text(self):
        original = b'\xff\xd8\xff' + b'x' * 192_000
        client = FlowClient(None)
        curl_error = TimeoutError('curl: (28) private-proxy password private-at')
        timeout = RuntimeError('Flow API request failed: transport=network_timeout')
        timeout.__cause__ = curl_error
        client._make_request = AsyncMock(side_effect=[
            RuntimeError('HTTP Error 503: private response private-project'),
            timeout,
            {'media': {'name': 'private-media'}},
        ])
        trace = {'index': 1}
        with patch('src.services.flow_client.asyncio.sleep', new=AsyncMock()) as sleep:
            result = await client.upload_image(
                'private-at', original, project_id='private-project',
                diagnostic_trace=trace,
            )
        self.assertEqual(result, 'private-media')
        self.assertEqual(client._make_request.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(trace['status'], 'uploaded')
        self.assertEqual(trace['attempts'], 3)
        self.assertEqual(trace['input_bytes'], len(original))
        requests = [call.kwargs for call in client._make_request.call_args_list]
        self.assertEqual(trace['gzip_body_bytes'], len(requests[0]['raw_body']))
        self.assertTrue(all(request['raw_body'] is requests[0]['raw_body'] for request in requests))
        self.assertTrue(all(request['allow_urllib_fallback'] is False for request in requests))
        attempts = trace['http_attempts']
        self.assertEqual([a['success'] for a in attempts], [False, False, True])
        self.assertEqual([a['retry_scheduled'] for a in attempts], [True, True, False])
        self.assertEqual(attempts[0]['cause'], 'upstream_http_503')
        self.assertEqual(attempts[0]['http_status_code'], 503)
        self.assertEqual(attempts[1]['cause'], 'network_timeout')
        self.assertEqual(attempts[1]['curl_code'], 28)
        self.assertTrue(all(a['duration_ms'] >= 0 for a in attempts))
        self.assertGreaterEqual(trace['duration_ms'], trace['preparation_ms'])
        encoded = json.dumps(trace)
        self.assertNotIn('private-', encoded)
        self.assertNotIn(original[:100].hex(), encoded)

    async def test_terminal_400_stays_one_call_and_is_logged_safely(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(side_effect=RuntimeError(
            'HTTP Error 400: INVALID_ARGUMENT private response private-cookie',
        ))
        trace = {}
        with patch('src.services.flow_client.asyncio.sleep', new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'cause=upstream_http_400'):
                await client.upload_image('private-at', b'\xff\xd8\xffsmall',
                                          project_id='owned', diagnostic_trace=trace)
        self.assertEqual(client._make_request.await_count, 1)
        sleep.assert_not_called()
        self.assertEqual(trace['status'], 'failed')
        self.assertIsNone(trace['gzip_body_bytes'])
        self.assertEqual(trace['http_attempts'][0]['http_status_code'], 400)
        self.assertFalse(trace['http_attempts'][0]['retry_scheduled'])
        self.assertNotIn('private', json.dumps(trace))

    async def test_exhausted_timeouts_retain_all_three_attempts(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(side_effect=TimeoutError('private timeout'))
        trace = {}
        with patch('src.services.flow_client.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'cause=network_timeout'):
                await client.upload_image('private-at', b'\xff\xd8\xffsmall',
                                          project_id='owned', diagnostic_trace=trace)
        self.assertEqual(client._make_request.await_count, 3)
        self.assertEqual(len(trace['http_attempts']), 3)
        self.assertEqual([a['retry_scheduled'] for a in trace['http_attempts']],
                         [True, True, False])
        self.assertTrue(all(a['cause'] == 'network_timeout' for a in trace['http_attempts']))
        self.assertEqual(trace['cause'], 'network_timeout')
        self.assertNotIn('private', json.dumps(trace))


if __name__ == '__main__':
    unittest.main()
