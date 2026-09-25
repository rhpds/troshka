#!/usr/bin/env bash
# Wait until Troshka API answers successfully.
# Honors TROSHKA_BASIC_USER/PASSWORD, TROSHKA_FORWARDED_EMAIL, TROSHKA_CURL_INSECURE
# (via troshka_curl in common.sh). Default path is /api/v1/health (works with
# oauth2-proxy skip-auth; for nginx basic auth pass credentials).
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

TIMEOUT_SEC="${TROSHKA_WAIT_TIMEOUT:-600}"
INTERVAL_SEC="${TROSHKA_WAIT_INTERVAL:-5}"
PATH_CHECK="${TROSHKA_HEALTH_PATH:-/api/v1/health}"

require_cmd curl

if [[ -z "${TROSHKA_API_URL:-}" ]]; then
  echo "error: TROSHKA_API_URL is not set" >&2
  exit 1
fi

deadline=$((SECONDS + TIMEOUT_SEC))
echo "Waiting for Troshka API at ${TROSHKA_API_URL}${PATH_CHECK} (timeout ${TIMEOUT_SEC}s)..."
while (( SECONDS < deadline )); do
  if troshka_curl -o /dev/null "${TROSHKA_API_URL}${PATH_CHECK}" 2>/dev/null; then
    echo "API is ready."
    exit 0
  fi
  sleep "$INTERVAL_SEC"
done

echo "error: timed out waiting for API at ${TROSHKA_API_URL}" >&2
exit 1
