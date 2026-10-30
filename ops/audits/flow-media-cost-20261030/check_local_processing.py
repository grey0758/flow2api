"""Exercise production image validation/cache on synthetic data with networking disabled."""
import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile

from PIL import Image
from src.services.file_cache import FileCache
from src.services.generation_handler import GenerationHandler


async def main():
    results = []
    for resolution, edge in (('2K', 2048), ('4K', 4096)):
        buffer = io.BytesIO()
        Image.new('RGB', (edge, edge), (51, 102, 153)).save(buffer, format='PNG')
        raw = buffer.getvalue()
        encoded = base64.b64encode(raw).decode('ascii')
        target = 'UPSAMPLE_IMAGE_RESOLUTION_' + resolution
        aspect = 'IMAGE_ASPECT_RATIO_SQUARE'
        info = GenerationHandler._validate_upscaled_image(encoded, target, aspect)
        assert (info['width'], info['height']) == (edge, edge)
        invalid_cases = []
        for name, invalid, invalid_target in (
            ('bad_base64', '!!!', target),
            ('truncated_png', base64.b64encode(raw[:len(raw)//2]).decode('ascii'), target),
            ('wrong_resolution', encoded, 'UPSAMPLE_IMAGE_RESOLUTION_' + ('4K' if resolution == '2K' else '2K')),
        ):
            try:
                GenerationHandler._validate_upscaled_image(invalid, invalid_target, aspect)
            except RuntimeError:
                invalid_cases.append(name)
            else:
                raise AssertionError('invalid_image_accepted:' + name)
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(cache_dir=directory)
            filename = await cache.cache_base64_image(encoded, resolution)
            stored = (Path(directory) / filename).read_bytes()
            assert stored == raw
            assert cache._validate_image_content(stored) == 'image/png'
        results.append({'resolution': resolution, 'width': edge, 'height': edge,
                        'full_decode': True, 'cache_bytes_preserved': True,
                        'rejected': invalid_cases})
    print(json.dumps({'evidence_type': 'synthetic_local_processing',
        'new_upstream_requests': 0, 'solver_calls': 0,
        'proves_captcha_optional': False, 'results': results}))


asyncio.run(main())
