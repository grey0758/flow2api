# Lossless reference uploads

Project-scoped uploads retain the original image bytes. References at least
192,000 bytes are encoded in the existing base64 JSON envelope, then gzip is
applied to that HTTP request with `Content-Encoding: gzip`. If compression
does not reduce the serialized request size, the original JSON transport is
used. Small references also keep the original JSON transport.

This preserves every image byte, including PNG/WebP alpha, EXIF orientation,
color profiles and other embedded metadata. It does not resize, rotate,
flatten transparency or perform lossy image recompression. JSON serialization
and gzip run off the event loop and once per reference; upload retries reuse
the same body, session and owned project.

Each owned reference upload gets one initial POST and at most one retry for
timeout or upstream5xx (at most two POSTs), even if the general Flow retry
configuration is higher. A lower configuration remains respected. The same
losslessly compressed body is reused for the retry. The new-endpoint requests
disable the generic urllib replay.
HTTP400 is terminal, and project-scoped uploads never use the unscoped legacy
endpoint or resend an uncompressed body as a second fallback. Existing account
and proxy bindings are retained. Debug output redacts upload payloads,
credentials and raw provider responses; diagnostics retain only safe classes.

Compression reduces transport overhead and has passed same-account large-file
upload checks. It cannot guarantee availability of the network or provider.
A successful upload does not imply a generated image or recovered account.

Each reference's existing request-log performance trace also records input
bytes, gzip HTTP-body bytes when used, preparation time, the configured timeout,
and each new-endpoint POST's duration, success, fixed failure class, HTTP status
or numeric curl code when available, and whether another scoped attempt was
scheduled. These records include failed attempts even when a later attempt
succeeds. The trace contains no image content, exception text, credentials,
account email, project/media identifier or proxy URL. The containing request
log's existing token ID permits account comparisons. The effective upload
attempt ceiling is also recorded as `max_attempts`. Legacy-endpoint calls are not included in this trace;
production references use owned project contexts (`scoped=true`).

Run `python -m pytest -q tests/test_lossless_upload_compression.py` for exact
JPEG/PNG/WebP byte preservation, alpha/EXIF fixtures, the HTTP encoding,
small/non-saving identity paths, body reuse and replay/redaction boundaries.
