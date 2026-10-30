"""GET one existing first-party delivery; never call Google or a solver."""
import datetime
import hashlib
import io
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


db = sqlite3.connect('file:/app/data/flow.db?mode=ro', uri=True, timeout=5)
db.execute('PRAGMA query_only=ON')
row = db.execute("""
    SELECT id,response_body FROM request_logs
    WHERE operation='generate_image' AND status_code=200 AND json_valid(response_body)
      AND json_extract(response_body,'$.generated_assets.upscaled_image.resolution')='4K'
    ORDER BY id DESC LIMIT 1
""").fetchone()
db.close()
result = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'solver_calls': 0, 'media_posts': 0, 'google_requests': 0,
          'database_writes': 0, 'delivery_gets': 0,
          'proves_captcha_optional': False}
if row:
    log_id, body = row
    value = json.loads(body)
    image = (value.get('performance') or {}).get('image_generation') or {}
    expected = ((image.get('provenance') or {}).get('output') or {}).get('sha256')
    asset = value['generated_assets']['upscaled_image']
    url = asset.get('local_url') or value.get('url') or ''
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ''
    result.update(log_id=log_id, delivery_hostname=hostname)
    own_delivery = (hostname.endswith('.opencodex.uk')
                    or hostname == 'opencodex-video-generated-assets.2437313414.workers.dev')
    if (parsed.scheme == 'https' and own_delivery
            and not parsed.username and not parsed.password and parsed.port in (None, 443)):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'opencodex-output-audit/1.0'})
            result['delivery_gets'] = 1
            with opener.open(request, timeout=45) as response:
                result['http'] = response.status
                raw = response.read(96 * 1024 * 1024 + 1)
            if len(raw) > 96 * 1024 * 1024:
                raise ValueError('delivery_exceeds_media_limit')
            digest = hashlib.sha256(raw).hexdigest()
            with Image.open(io.BytesIO(raw)) as decoded:
                decoded.load()
                width, height = decoded.size
                image_format = decoded.format
            result.update(full_decode=True, width=width, height=height,
                format=image_format, bytes=len(raw), sha256=digest,
                sha256_matches_log=bool(expected and expected == digest),
                dimensions_match_log=(width, height) == (asset.get('width'), asset.get('height')))
        except Exception as error:
            result.update(error_class=type(error).__name__, http=getattr(error, 'code', result.get('http')))
    else:
        result['stopped'] = 'first_party_https_delivery_required'
else:
    result['stopped'] = 'no_existing_4k_success'
print(json.dumps(result))
