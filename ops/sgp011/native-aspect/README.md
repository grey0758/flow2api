# Native 2:3 / 4:5 compatibility in the deployed modern Flow runtime

This is a focused source archive of the existing sgp011 Cookie/RPC runtime
change, published on2026-11-07. It is **not** a standalone application update
to the legacy application at this repository's main branch. The modern runtime
also needs its existing Cookie transport, media parser, generation integration,
account/session management and output validation. Those independent components
are deliberately outside this aspect-only archive.

`native-aspect.patch` records the exact two-file delta from the archived
2026-11-04 modern baseline. `src/services/original_aspect.py` is the exact
resulting module. The patch uses native numeric target enums6/8 for2:3/4:5,
preserves reference bytes/order and REFERENCE roles, and checks decoded output
dimensions. It also retains the contemporaneously deployed5:4,3:2,21:9 enums.
It does not crop, pad, add a hidden reference, or loosen output validation.

This historical patch must not be blindly applied to later production files:
later upload-first/egress repairs changed surrounding imports. Compare the
before/after hashes in `manifest.json` in an isolated snapshot first. To verify
an already-updated runtime without importing the app or changing any files:

```sh
python3 verify_runtime.py --root /path/to/modern/runtime
```

The verifier checks the complete aspect-module bytes plus the affected import,
ASPECTS, context and image_argument ASTs. It permits unrelated later code to
remain intact. It never makes Google/CAPTCHA calls or imports application code.
The component contracts in `tests/test_native_aspect.py` should be run against
the complete modern runtime with network disabled; passing the verifier alone
is not a full application build or end-to-end acceptance.

## tt-api API contract

For `/v1/images/generations`, pass `model=gemini-3-pro-image-preview`,
`size=2:3` or `4:5`, `resolution=1K|2K|4K`, and a valid reference in
`image_urls`. tt341 maps these to the corresponding
`gemini-3.0-pro-image-two-three[-2k|-4k]` or
`gemini-3.0-pro-image-four-five[-2k|-4k]` model. The reference need not match
the requested aspect. Pure-text/no-reference generation remains unsupported
for these extended aliases.4K capability still depends on qualified available
accounts; mappings are not a4K acceptance claim.

Two fresh fixed341 requests passed on2026-11-07:2:3/2K JPEG1696x2528 and
4:5/2K JPEG1856x2304. Delivered OSS files fully decoded and matched Flow SHA;
each user task was settled and consumed once.4:5 used the existing one-time
cross-account upsample regeneration; it was not a first-attempt success.
See `acceptance.json` for sanitized evidence. Google uses pixel-aligned native
dimensions rather than mathematically exact nominal ratios.

The implementation was already serving when this archive was published.
Publication does not replace an image, restart the service, alter Nginx,
enable async319, replay consumed canaries, or change account/auth/quota/egress,
upload-first control, EZnew-to-Yes, billing or accepted tasks. Existing production
support is verified in place. A new runtime rollout would require a fresh
current-base review and graceful application handoff, never reuse old releases.
