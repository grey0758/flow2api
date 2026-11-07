"""Flow's explicit aspect contract. Never resize or add a reference image.

The explicit numeric enums below are archived from the 2026-11-04 official
editor encoder. They replace the earlier unset/first-base Original workaround.
Output dimensions follow Google's published Pro image table; the 4:5 family
was also fully decoded from official-editor generation and both upsample tiers.
"""
import hashlib
import io
from PIL import Image

BASE_ROLE = 'IMAGE_INPUT_TYPE_BASE_IMAGE'
REFERENCE_ROLE = 'IMAGE_INPUT_TYPE_REFERENCE'
# Internal names only: these strings are never transmitted as protobuf enums.
ORIGINAL_ASPECTS = {
    'four-five': ('IMAGE_ASPECT_RATIO_PORTRAIT_FOUR_FIVE', 4 / 5, (928, 1152)),
    'five-four': ('IMAGE_ASPECT_RATIO_LANDSCAPE_FIVE_FOUR', 5 / 4, (1152, 928)),
    'two-three': ('IMAGE_ASPECT_RATIO_PORTRAIT_TWO_THREE', 2 / 3, (848, 1264)),
    'three-two': ('IMAGE_ASPECT_RATIO_LANDSCAPE_THREE_TWO', 3 / 2, (1264, 848)),
    'twenty-one-nine': ('IMAGE_ASPECT_RATIO_LANDSCAPE_TWENTY_ONE_NINE', 21 / 9, (1584, 672)),
}
# Official I3a/scc encoder; 4:5 validated with an unchanged 800x800 reference.
NATIVE_ASPECT_ENUMS = {
    'IMAGE_ASPECT_RATIO_PORTRAIT_FOUR_FIVE': 8,
    'IMAGE_ASPECT_RATIO_LANDSCAPE_FIVE_FOUR': 9,
    'IMAGE_ASPECT_RATIO_PORTRAIT_TWO_THREE': 6,
    'IMAGE_ASPECT_RATIO_LANDSCAPE_THREE_TWO': 7,
    'IMAGE_ASPECT_RATIO_LANDSCAPE_TWENTY_ONE_NINE': 10,
}
SPECS = {spec[0]: spec[1:] for spec in ORIGINAL_ASPECTS.values()}
DIMENSIONS = {
    tier: {aspect: tuple(v * factor for v in size) for aspect, (_, size) in SPECS.items()}
    for tier, factor in (('1K', 1), ('2K', 2), ('4K', 4))
}


def reference_geometry(images, aspect):
    if aspect not in SPECS:
        raise ValueError('original_aspect_unsupported')
    if not images or not isinstance(images[0], (bytes, bytearray)):
        raise ValueError('original_aspect_matching_reference_required')
    raw = images[0]
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            fmt = image.format
            orientation = image.getexif().get(274, 1)
            if fmt not in {'PNG', 'JPEG'} or orientation != 1:
                raise ValueError('original_aspect_reference_geometry_unverified')
        # Pillow's PNG EXIF reader may consume the stream. Verify a fresh view
        # of the same bytes instead of normalizing/re-encoding the image.
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError('original_aspect_reference_invalid') from error
    nominal, size = SPECS[aspect]
    ratio = width / height if height else 0
    # Explicit target aspect is independent of the first reference geometry.
    # Input decode/format/orientation checks remain; no crop, pad or transcode.
    if width < 1 or height < 1:
        raise ValueError('original_aspect_matching_reference_required')
    return {'mode': 'native_explicit_aspect', 'aspect': aspect,
        'rpc_aspect_enum': NATIVE_ASPECT_ENUMS[aspect],
        'reference_ratio_matches_target': min(abs(ratio / target - 1) for target in (nominal, size[0] / size[1])) <= .0015,
        'width': width,
        'height': height, 'format': fmt, 'exif_orientation': orientation,
        'sha256': hashlib.sha256(raw).hexdigest(),
        'reference_bytes_unchanged': True, 'additional_images': 0}


def original_inputs(inputs):
    if not inputs:
        raise ValueError('original_aspect_matching_reference_required')
    result = [dict(item) for item in inputs]
    for index, item in enumerate(result):
        item['imageInputType'] = REFERENCE_ROLE
    return result


def validate_output(raw, aspect, resolution='1K'):
    expected = DIMENSIONS.get(resolution, {}).get(aspect)
    if expected is None:
        raise ValueError('original_aspect_output_contract_unknown')
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if image.size != expected or image.format not in {'PNG', 'JPEG', 'WEBP'}:
            error = ValueError('original_aspect_output_dimensions_mismatch')
            error.flow2api_output_validation = {
                'failure_kind': 'dimension_mismatch' if image.size != expected else 'unsupported_format',
                'expected_width': expected[0], 'expected_height': expected[1],
                'actual_width': image.width, 'actual_height': image.height,
                'format': image.format, 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(),
                'aspect_ratio': aspect, 'target_resolution': resolution,
            }
            raise error
    return {'width': expected[0], 'height': expected[1], 'bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(), 'mode': 'native_explicit_aspect'}
