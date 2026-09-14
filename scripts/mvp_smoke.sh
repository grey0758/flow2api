#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:?usage: $0 IMAGE [TIMEOUT_SECONDS]}"
timeout_seconds="${2:-90}"
container_id=""

cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker rm --force "${container_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

container_id="$(docker run --detach --init --publish 127.0.0.1::8000 "${image}")"
port=""
for _ in $(seq 1 30); do
  port="$(docker port "${container_id}" 8000/tcp 2>/dev/null | sed -n 's/.*://p' | head -n 1)"
  [[ -n "${port}" ]] && break
  sleep 1
done
[[ -n "${port}" ]] || { docker logs "${container_id}"; exit 1; }

deadline=$((SECONDS + timeout_seconds))
health_status=""
while (( SECONDS < deadline )); do
  health_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/health" || true)"
  if [[ "${health_status}" == "200" ]]; then
    break
  fi
  sleep 2
done

if [[ "${health_status}" != "200" ]]; then
  echo "Flow2API health check failed with HTTP ${health_status:-unavailable}" >&2
  docker logs "${container_id}" >&2
  exit 1
fi

models_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/v1/models" || true)"
if [[ "${models_status}" != "401" ]]; then
  echo "Expected protected /v1/models to return HTTP 401, got ${models_status:-unavailable}" >&2
  exit 1
fi

echo "MVP smoke passed: /health=200 /v1/models=401"
