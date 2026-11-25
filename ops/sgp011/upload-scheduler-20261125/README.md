# Upload-only account and exit admission backoff

LIVE 2026-11-25 17:35:18 UTC, image80b7abe8/PID3010457. Exact2663726f
base with ONLYgeneration_handler+upload_exit_backoff changed;52other sources
exact. This archive is separate from the old src tree and is not a full modern
auth migration. No original dirty worktree files were replaced.

Confirmed modern maseQ curl28/unknown upload batch failures now update NEW
reference-upload admission independently from retry authorization. The original
failed request, content rules and unknownPOST no-replay boundary stay exact.

First failed batch pauses account and its verified assigned exit120s. Repeated
failures in15m use300s. Concurrent failures cannot extend the same open interval
indefinitely. State is memory-only and bounded512identities/15m. Whole-batch
complete success clears cooldown only if started after the latest failure;
failure history remains until15m. Missing route registry pauses only account.
Text-only image selection ignores this rule. Admission trace and safe structured
UPLOAD_SCHEDULER_BACKOFF log record effects without secrets.

34isolated checks including originalID64trace, shared exits, alternate selection,
expiry/memory bounds, late success, batch drain/order/cancel and unchanged retry
classifiers pass. ActualUID54manifest/import and exact-image SIGTERM/drain pass.
Current tasks/UNKNOWN/billing/auth/quotas/proxies retained; no Nginxwrite/reload.
No long-term natural timeout reduction is claimed from unit/injected evidence.

Tests mount these helpers under/tests in the accepted offline image; preserve
/tests/generation_handler.before.py and sanitized real-traces.json. Test source
may be run without provider network. Old failed fixture/import receipts retained.
Do not rerun consumed deployment/canary intents or restore old business DB.

The two large handler snapshots are byte-exact gzip archives (mtime0); unpack
without normalization to retain provenance SHA and historical whitespace.
