"""Verify existing 2K/4K cached outputs; no network, solver or media requests."""
import collections
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time

from PIL import Image
from src.api import image_dashboard as dashboard

deadline = time.monotonic() + 45
db = sqlite3.connect('file:/app/data/flow.db?mode=ro', uri=True, timeout=5)
db.execute('PRAGMA query_only=ON')
db.set_progress_handler(lambda: int(time.monotonic() > deadline), 20000)
db.execute('BEGIN')
results = []
for resolution in ('2K', '4K'):
    rows = db.execute("""
        SELECT id,response_body FROM request_logs
        WHERE operation='generate_image' AND status_code=200
          AND json_valid(response_body)
          AND json_extract(response_body,'$.generated_assets.upscaled_image.resolution')=?
        ORDER BY id DESC LIMIT 8
    """, (resolution,)).fetchall()
    candidates = []
    for path in Path('/app/tmp').iterdir():
        if (not path.is_symlink() and path.is_file()
                and re.fullmatch(r'[a-f0-9]{32}_' + resolution + r'\.(png|jpg|jpeg|webp)', path.name)):
            candidates.append(path)
    found = None
    for log_id, body in rows:
        value = json.loads(body)
        perf = value.get('performance') or {}
        image = perf.get('image_generation') or {}
        asset = value['generated_assets']['upscaled_image']
        expected = ((image.get('provenance') or {}).get('output') or {}).get('sha256')
        if not expected:
            continue
        for path in candidates:
            if time.monotonic() > deadline:
                raise TimeoutError('bounded_readonly_audit_deadline')
            if path.stat().st_size != asset.get('size_bytes'):
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected:
                continue
            with Image.open(io.BytesIO(raw)) as decoded:
                decoded.load()
                width, height = decoded.size
                image_format = decoded.format
            unknown = collections.Counter()
            retry = perf.get('cross_account_retry') or {}
            first = retry.get('first_account_image_trace') or {}
            groups = dashboard._cost_trace_groups(
                (image.get('upstream_trace') or {}).get('generation_attempts') or [],
                (image.get('upsample') or {}).get('attempts') or [],
                (first.get('upstream_trace') or {}).get('generation_attempts') or [],
                (first.get('upsample') or {}).get('attempts') or [],
                retry.get('additional_account_image_traces') or [],
                (image.get('reference_upload') or {}).get('images') or [],
                (first.get('reference_upload') or {}).get('images') or [],
                retry.get('attempted'), unknown)
            stage_tokens = collections.defaultdict(collections.Counter)
            for attempts, stage, cross in groups:
                for attempt in dashboard._attempts(attempts):
                    if isinstance(attempt, dict) and attempt.get('recaptcha_ok') is True:
                        provider = str(attempt.get('captcha_provider') or '').lower()
                        stage_tokens[stage][provider] += 1
            found = {
                'resolution': resolution, 'log_id': log_id,
                'full_decode': True, 'width': width, 'height': height,
                'format': image_format, 'bytes': len(raw), 'sha256': digest,
                'dimensions_match_log': (width, height) == (asset.get('width'), asset.get('height')),
                'sha256_matches_log': True,
                'recorded_returned_solver_tokens_by_stage': dict(stage_tokens),
                'cost_unknown': dict(unknown),
                'evidence_type': 'existing_production_output_readonly',
                'proves_captcha_optional': False,
            }
            break
        if found:
            break
    results.append(found or {'resolution': resolution, 'matching_cached_output_found': False,
                            'candidate_success_logs': len(rows)})
db.rollback()
db.close()
sources = {}
for relative in ('src/services/cookie_media.py', 'src/services/flow_client.py',
                 'src/services/generation_handler.py', 'src/services/file_cache.py',
                 'src/api/image_dashboard.py'):
    sources[relative] = hashlib.sha256((Path('/app') / relative).read_bytes()).hexdigest()
print(json.dumps({'as_of_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'new_upstream_requests': 0, 'solver_calls': 0, 'database_writes': 0,
    'results': results, 'sources': sources}, ensure_ascii=False))
