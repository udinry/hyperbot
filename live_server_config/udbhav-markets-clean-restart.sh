#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="udbhav-markets.service"
APP_PORT="3001"
HEALTH_URL="http://127.0.0.1:${APP_PORT}/api/health?ready=1"
MAX_ATTEMPTS=30
SLEEP_SECONDS=2

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo or as root." >&2
  exit 1
fi

echo "[udbhav-markets] stopping ${SERVICE_NAME}"
systemctl stop "${SERVICE_NAME}" || true

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -tiTCP:${APP_PORT} -sTCP:LISTEN || true)"
  if [[ -n "${pids}" ]]; then
    echo "[udbhav-markets] terminating stray listeners on port ${APP_PORT}: ${pids}"
    kill -TERM ${pids} || true
    sleep 2

    remaining="$(lsof -tiTCP:${APP_PORT} -sTCP:LISTEN || true)"
    if [[ -n "${remaining}" ]]; then
      echo "[udbhav-markets] force-killing remaining listeners on port ${APP_PORT}: ${remaining}"
      kill -KILL ${remaining} || true
    fi
  fi
fi

echo "[udbhav-markets] starting ${SERVICE_NAME}"
systemctl start "${SERVICE_NAME}"

for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1)); do
  if curl --silent --fail --max-time 5 "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "[udbhav-markets] health check passed on attempt ${attempt}"
    exit 0
  fi
  sleep "${SLEEP_SECONDS}"
done

echo "[udbhav-markets] health check did not recover after restart" >&2
systemctl --no-pager --full status "${SERVICE_NAME}" >&2 || true
exit 1
