# Flow source GET compatibility and precise 4K diagnostics

Deployed 2026-11-26 05:29:22 UTC on sgp011. This archive extends the actual immutable production image without replacing the divergent MVP checkout.

Source GETs retain the 90-second total budget. Only two proven pre-response connection failures allow one final GET with a 12-second connect limit. Persistent transient source failures now return HTTP 502 with reference_source_unavailable, execution_outcome=not_started, enabling the existing tt-api HTTP status retry path. Permanent 4xx and unknown Google POST policies remain unchanged.

Pool diagnostics distinguish unanimous bootstrap identity holds from a generic session_invalid. No hold, account, quota, egress or authentication validator was bypassed. All nine current Ultra sessions lacked authenticated bootstrap fields; two healthy Pro control sessions verified. This change does not restore their logins or constitute 4K acceptance.

39 isolated checks, UID10001 imports/55-source manifest, graceful SIGTERM143 fixture and real existing 2048x2048 source GET/full decode passed. No generation POST or customer generation replay. Real production shutdown drained accepted work. Only routes.py/load_balancer.py plus one new pure helper changed.

Large snapshots are byte-exact gzip to retain historical whitespace and source hashes. To rerun the focused tests in isolation, extract the gzip files under both baseline and candidate, then use the candidate /app sources (the tests compare the original scheduling and authentication code); the full build receipts remain in the ops workspace. Do not rerun consumed release/handoff intents or restore old DB/results/UNKNOWN/billing.
