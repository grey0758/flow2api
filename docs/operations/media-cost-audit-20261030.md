# Media processing cost audit, 2026-10-30

The deployed service obtains a paid CAPTCHA token before reference upload,
image generation and upstream 2K/4K upsampling. Base64/Pillow decoding,
dimension validation and local caching do not themselves call a CAPTCHA
supplier. Removing image integrity checks would not remove those supplier calls.
Historical token estimates do not establish supplier invoice reconciliation.

This change adds operational audit tools and records local checks only. It
contains no runtime patch or deployment. No new Google media POST, CAPTCHA
supplier call or production database write was made for these checks. Existing
normal-browser upsample successes do not prove CAPTCHA was absent or optional.

## Verification

The exact deployed image at audit time was
`sha256:db46f2983190d4175bf388826510bbef72901bb147240210ce4c78dddbba722a`.
A disposable, unprivileged, read-only container with networking disabled and
no production mounts exercised the production image validation/cache methods:

| Synthetic image | Full decode and exact dimensions | Cache preserves bytes | Invalid Base64, truncated PNG and wrong resolution |
| --- | --- | --- | --- |
| 2048x2048 PNG (2K) | Pass | Pass | All rejected |
| 4096x4096 PNG (4K) | Pass | Pass | All rejected |

An existing 2400x1792 2K JPEG also decoded fully and matched its logged SHA256
and dimensions. Its original task recorded one supplier token at each of the
upload, generation and upsample stages. The read-only audit did not rerun it.

A matching local cache for the selected existing 4K output was unavailable.
One GET of its first-party delivery returned HTTP403, so fresh verification of
that 4K artifact remains incomplete. No regeneration or upsample replay was
performed. These checks do not establish upstream quality or CAPTCHA omission
support, and no such optimization was deployed.

## Tools

The scripts in `ops/audits/flow-media-cost-20261030/` match the deployed Cookie
media/dashboard contract; that deployment contains modules not present in this
older source branch. Run them only in the matching authorized environment.

- `inspect_existing.py`: read-only SQLite and cache inspection; no network
  calls or media submissions. Output contains safe metadata and hashes.
- `check_delivered_4k.py`: at most one GET of an already completed first-party
  delivery, with redirects disabled and a strict owner-host allowlist. It never
  calls Google, signs a new URL or prints existing signed URLs or credentials.
- `check_local_processing.py`: synthetic image validation/cache checks. Run
  inside a disposable container with `--network none --read-only`, a writable
  tmpfs, no production mounts, and `PYTHONPATH=/app`. Do not use this as an
  upstream acceptance or image-quality test.
- `local-processing-result.json`: result of the isolated check described above.

Continuity source: Curator primary gpl001, Codex session
`01a0a5d2-6dbb-7372-b13f-a591d3e1655e`. Search terms:
`Flow EZCaptcha upload reference cost`, `EZCaptcha`, and
`flowapitmp EZCaptcha 成本 上传 参考图`. Related direct actions:
`1c6250bc-58e2-438d-b995-7e435fb13305` (initial cost audit) and
`573bfda2-aeeb-46b4-9c99-d77ebdf9c3b3` (proposed deployment not completed;
read-only follow-up and local checks completed). Operational detail and the
focused skill remain in the independent operations workspace.
