import base64
import gzip
import io
import json
import random
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from src.core.config import config
from src.services.flow_client import FlowClient


class LosslessUploadCompressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_method = config.captcha_method
        self.previous_retries = config.flow_max_retries
        config.set_captcha_method('browser')
        config.set_flow_max_retries(3)

    async def asyncTearDown(self):
        config.set_captcha_method(self.previous_method)
        config.set_flow_max_retries(self.previous_retries)

    def reference(self, image_format):
        pixels = random.Random(17).randbytes(512 * 512 * 4)
        image = Image.frombytes('RGBA', (512, 512), pixels)
        output = io.BytesIO()
        if image_format == 'JPEG':
            exif = Image.Exif()
            exif[274] = 6  # EXIF orientation must survive without rotating pixels.
            image.convert('RGB').save(output, image_format, quality=100, exif=exif)
        elif image_format == 'PNG':
            info = PngInfo()
            info.add_text('Description', 'synthetic metadata')
            image.save(output, image_format, pnginfo=info)
        else:
            image.save(output, image_format, lossless=True)
        return output.getvalue()

    async def test_real_images_preserve_every_byte_including_alpha_and_metadata(self):
        for image_format, mime in (
            ('JPEG', 'image/jpeg'), ('PNG', 'image/png'), ('WEBP', 'image/webp'),
        ):
            with self.subTest(image_format=image_format):
                original = self.reference(image_format)
                self.assertGreaterEqual(len(original), FlowClient.REFERENCE_UPLOAD_GZIP_MIN_BYTES)
                client = FlowClient(None)
                client._make_request = AsyncMock(return_value={'media': {'name': 'test-media'}})
                self.assertEqual(await client.upload_image(
                    'private-at', original, project_id='owned-project',
                ), 'test-media')
                request = client._make_request.call_args.kwargs
                decoded = gzip.decompress(request['raw_body'])
                payload = json.loads(decoded)
                self.assertEqual(base64.b64decode(payload['imageBytes']), original)
                self.assertEqual(payload, request['json_data'])
                self.assertEqual(payload['mimeType'], mime)
                self.assertEqual(payload['clientContext']['projectId'], 'owned-project')
                self.assertEqual(request['headers'], {'Content-Encoding': 'gzip'})
                self.assertFalse(request['allow_urllib_fallback'])
                self.assertLess(len(request['raw_body']), len(decoded))

    async def test_small_webp_keeps_identity_body_and_matching_extension(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(return_value={'media': {'name': 'test-media'}})
        await client.upload_image('private-at', b'RIFF1234WEBP' + b'x' * 16,
                                  project_id='owned-project')
        request = client._make_request.call_args.kwargs
        self.assertIsNone(request['raw_body'])
        self.assertIsNone(request['headers'])
        self.assertTrue(request['json_data']['fileName'].endswith('.webp'))

    async def test_gzip_that_does_not_save_bytes_uses_identity(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(return_value={'media': {'name': 'test-media'}})
        with patch('src.services.flow_client.gzip.compress', return_value=b'x' * 1_000_000):
            await client.upload_image('private-at', b'x' * 192_000,
                                      project_id='owned-project')
        request = client._make_request.call_args.kwargs
        self.assertIsNone(request['raw_body'])
        self.assertIsNone(request['headers'])

    async def test_timeout_retry_reuses_body_and_owned_context(self):
        original = self.reference('PNG')
        client = FlowClient(None)
        client._make_request = AsyncMock(side_effect=[
            TimeoutError('private network timed out'), {'media': {'name': 'test-media'}},
        ])
        with patch('src.services.flow_client.asyncio.sleep', new=AsyncMock()), \
             patch.object(client, '_prepare_upload_body', wraps=client._prepare_upload_body) as prepare:
            self.assertEqual(await client.upload_image(
                'private-at', original, project_id='owned-project',
            ), 'test-media')
        self.assertEqual(prepare.call_count, 1)
        first, second = [call.kwargs for call in client._make_request.call_args_list]
        self.assertIs(first['raw_body'], second['raw_body'])
        self.assertEqual(first['json_data'], second['json_data'])
        self.assertTrue(all(call.kwargs['allow_urllib_fallback'] is False
                            for call in client._make_request.call_args_list))

    async def test_rejected_compressed_upload_never_replays_legacy_or_plain_body(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(side_effect=RuntimeError('HTTP Error 400: INVALID_ARGUMENT'))
        with self.assertRaisesRegex(RuntimeError, 'cause=upstream_http_400'):
            await client.upload_image('private-at', self.reference('PNG'),
                                      project_id='owned-project')
        self.assertEqual(client._make_request.await_count, 1)
        self.assertTrue(client._make_request.call_args.kwargs['url'].endswith('/flow/uploadImage'))

    async def test_actual_transport_sends_gzip_and_redacts_debug_payload(self):
        original = self.reference('PNG')
        sent = []

        class Response:
            status_code = 200
            text = 'private-media-response'
            headers = {'private-response-header': 'secret'}
            def json(self): return {'media': {'name': 'test-media'}}

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                sent.append(kwargs)
                return Response()

        client = FlowClient(None)
        previous_debug = config.debug_enabled
        config.set_debug_enabled(True)
        try:
            with patch('src.services.flow_client.AsyncSession', return_value=Session()), \
                 patch('src.services.flow_client.debug_logger') as logger:
                await client.upload_image('private-at', original, project_id='owned-project')
            self.assertNotIn('private-at', str(logger.mock_calls))
            self.assertNotIn('private-media-response', str(logger.mock_calls))
            self.assertIn('[redacted image upload payload]', str(logger.mock_calls))
            self.assertNotIn('json', sent[0])
            self.assertEqual(sent[0]['headers']['Content-Encoding'], 'gzip')
            self.assertEqual(base64.b64decode(json.loads(gzip.decompress(sent[0]['data']))['imageBytes']), original)
        finally:
            config.set_debug_enabled(previous_debug)

    async def test_upload_timeout_has_no_hidden_urllib_post(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                raise TimeoutError('curl: (28) private network timed out')

        client = FlowClient(None)
        with patch('src.services.flow_client.AsyncSession', return_value=Session()), \
             patch.object(client, '_sync_json_request_via_urllib') as fallback, \
             patch('src.services.flow_client.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'cause=network_timeout'):
                await client.upload_image('private-at', self.reference('PNG'),
                                          project_id='owned-project')
        fallback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
