#!/usr/bin/env bash
# Wait until Troshka API answers successfully.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

TIMEOUT_SEC="${TROSHKA_WAIT_TIMEOUT:-600}"
INTERVAL_SEC="${TROSHKA_WAIT_INTERVAL:-5}"
PATH_CHECK="${TROSHKA_HEALTH_PATH:-/api/v1/projects/}"

require_cmd curl

deadline=$((SECONDS + TIMEOUT_SEC))
echo "Waiting for Troshka API at ${TROSHKA_API_URL}${PATH_CHECK} (timeout ${TIMEOUT_SEC}s)..."
while (( SECONDS < deadline )); do
  if curl -fsS -o /dev/null "${TROSHKA_API_URL}${PATH_CHECK}" 2>/dev/null; then
    echo "API is ready."
    exit 0
  fi
  sleep "$INTERVAL_SEC"
done

echo "error: timed out waiting for API at ${TROSHKA_API_URL}" >&2
exit 1
