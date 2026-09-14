# Flow2API MVP Stable

This repository defines the MVP stable baseline for the Flow2API service.

## Baseline

- Upstream baseline: `f6606c4` (`TheSmallHanCat/flow2api` `main`)
- MVP label: `0.1.0-mvp`
- Runtime targets: Python 3.11, Docker standard image, Docker headed image
- Persistence: SQLite and generated media stay in operator-managed mounts

The baseline contains the current Flow request path, browser and API CAPTCHA
integrations, token refresh, account load balancing, image/video model routing,
and the OpenAI/Gemini-compatible HTTP routes already present in the upstream
source. This MVP definition adds release and verification boundaries; it does
not copy any deployment database, token, cookie, CAPTCHA key, or browser
profile into the repository.

## Required gates

Every pull request and `main` push runs:

1. Python bytecode compilation.
2. The repository unit test suite.
3. Standard and headed Docker builds.
4. A container smoke that requires `GET /health` to return `200` and the
   unauthenticated `/v1/models` route to remain protected (`401`).

The smoke does not need a Flow account, CAPTCHA provider, proxy, or runtime
secret. A real image generation check remains an operator acceptance step
because it necessarily consumes an external account and CAPTCHA budget.

## Local run

For the reproducible headed MVP container:

```bash
docker compose -f docker-compose.mvp.yml up -d --build
curl --fail http://127.0.0.1:8000/health
```

Create `config/setting.toml` and populate the administrator-managed token and
CAPTCHA settings through the management UI or a secret manager. Keep `data/`,
`tmp/`, and the config file outside Git; the repository ignores them.

The standard image remains available through `docker-compose.yml`. Use the
headed MVP file when the `browser` or `personal` CAPTCHA mode is required.

## Stability boundary

The MVP contract covers source correctness, image construction, startup, the
public health contract, and API authentication protection. It does not claim a
fixed upstream quota, CAPTCHA success rate, account lifetime, or public SLA.
Those properties depend on the operator's Flow accounts, provider limits,
network egress, and external CAPTCHA service.
