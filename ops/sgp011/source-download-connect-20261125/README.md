# Input reference GET connection budget

Reference archive for the exact 2026-11-25 sgp011 Flow release. This is not a
full application source migration. Never overwrite the legacy checkout from
this directory or rerun completed build/release/canary intents.

Input source GETs now have an 8-second DNS/connect/TLS budget. The existing
60-second body timeout, 90-second overall limit, two-GET maximum and verified
alternate CDN path remain. Google account/media egress, generation/upload
POSTs and output downloads are unchanged. The only runtime difference is
`src/api/routes.py`; all other 53 source hashes matched.

`routes.patch` applies only when the before hash matches provenance.json.
The exact before/after files and diagnostic module are included to run the
18 isolated fixtures: `python3 -m unittest test_reference_download -v` with
the accepted runtime Python/curl_cffi dependencies and no network.

Publication does not deploy or activate anything. Current live state, first
failed manifest assertion and automatic code-only rollback, fresh final
handoff, two app acceptance examples and all limits are recorded in the
owning ops docs/image-error-review-20261124.md. Production data and failed
receipts were retained, Nginx was neither changed nor reloaded.
