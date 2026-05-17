#!/usr/bin/env bash
set -Eeuo pipefail

APP_SERVICE="udbhav-markets.service"
NGINX_SERVICE="nginx.service"
STATE_FILE="/var/tmp/udbhav-markets-healthcheck.failures"
FAILURE_THRESHOLD=3
LOCAL_HEALTH_URL="http://127.0.0.1:3001/api/health?ready=1"
PUBLIC_CHECK_URL="https://udbhav.uk/markets/dashboard"
PUBLIC_CHECK_HOST="udbhav.uk:443:127.0.0.1"
RESTART_SCRIPT="/usr/local/bin/udbhav-markets-clean-restart.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo or as root." >&2
  exit 1
fi

check_local_app() {
  curl --silent --fail --max-time 5 "${LOCAL_HEALTH_URL}" >/dev/null 2>&1
}

check_public_site() {
  curl --silent --fail --max-time 10 --resolve "${PUBLIC_CHECK_HOST}" "${PUBLIC_CHECK_URL}" >/dev/null 2>&1
}

reset_failures() {
  rm -f "${STATE_FILE}"
}

read_failures() {
  if [[ -f "${STATE_FILE}" ]]; then
    cat "${STATE_FILE}" 2>/dev/null || echo "0"
  else
    echo "0"
  fi
}

write_failures() {
  printf '%s\n' "$1" > "${STATE_FILE}"
}

local_ok=0
public_ok=0

if check_local_app; then
  local_ok=1
fi

if check_public_site; then
  public_ok=1
fi

if (( local_ok == 1 && public_ok == 1 )); then
  reset_failures
  exit 0
fi

failures="$(read_failures)"
failures="$((failures + 1))"
write_failures "${failures}"
echo "[udbhav-markets-healthcheck] failure ${failures}/${FAILURE_THRESHOLD} (local=${local_ok} public=${public_ok})"

if (( failures < FAILURE_THRESHOLD )); then
  exit 0
fi

if (( local_ok == 0 )); then
  echo "[udbhav-markets-healthcheck] local app failed, running clean restart"
  "${RESTART_SCRIPT}"
elif (( public_ok == 0 )); then
  echo "[udbhav-markets-healthcheck] public route failed while local app is healthy, restarting nginx"
  systemctl restart "${NGINX_SERVICE}"
fi

sleep 8

if check_local_app && check_public_site; then
  echo "[udbhav-markets-healthcheck] service recovered"
  reset_failures
  exit 0
fi

echo "[udbhav-markets-healthcheck] service still unhealthy after remediation" >&2
exit 1
