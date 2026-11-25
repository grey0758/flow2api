# Source CDN probe diagnostics and bounded patience

Deployed 2026-11-25 09:36:01 UTC as immutable image2663726f. Only the actual
serving input_source_dns module changed; other53 Python sources retained.
This is a scoped production patch archive and does not replace legacy src/main.

On the existing second reference GET, each of at most4 fresh public CDN
candidates gets up to3s (previous2s); total alternate-resolution budget6s
(previous5s), still within original90s input budget and two full GET limit.
Same-host TLS, bounded Range sample, IP validation and cancellation retained.
Logs include candidate hash, resolution/probe time, HTTP/bytes, curl timings,
failure class and cancelled losers. No raw source URL/IP/credential is logged.

25 isolated tests and realUID54-source manifest import passed. Exact-image
SIGTERM fixture drained its accepted request; real old Flow graceful143 with
shutdown markers. Existing HTTP input loader downloaded two owned2048PNGs
in0.613/4.185s with exact SHA and full decode, GooglePOST0. No measured new
CDN fault recovery rate claimed.

Initial test fixture lacked local dependency files; retest used complete
read-only fixtures. Initial release worker referenced an old operational
directory and stopped before changes; freshv2 directory/proof completed.
No Nginx edits/reloads; scoped tt priority handoff restored341/344ON1m.
Do not replay consumed build/release/canary intents or restore business DB.
