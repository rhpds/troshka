#!/usr/bin/env bash
# Destroy all Troshka projects via the API and wait until none remain.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

TIMEOUT_SEC="${TROSHKA_WIPE_TIMEOUT:-1800}"
INTERVAL_SEC="${TROSHKA_WIPE_INTERVAL:-10}"

require_cmd curl jq

list_project_ids() {
  api_get "/api/v1/projects/" | jq -r '.[].id // empty'
}

ids="$(list_project_ids || true)"
if [[ -z "${ids}" ]]; then
  echo "No projects to destroy."
  exit 0
fi

echo "Destroying projects:"
echo "$ids" | while read -r id; do
  [[ -z "$id" ]] && continue
  echo "  DELETE /api/v1/projects/${id}"
  api_delete "/api/v1/projects/${id}" >/dev/null || {
    echo "warning: delete request failed for ${id} (will keep polling)" >&2
  }
done

deadline=$((SECONDS + TIMEOUT_SEC))
echo "Waiting for projects to finish deleting (timeout ${TIMEOUT_SEC}s)..."
while (( SECONDS < deadline )); do
  remaining="$(list_project_ids || true)"
  if [[ -z "${remaining}" ]]; then
    echo "All projects destroyed."
    exit 0
  fi
  count="$(echo "$remaining" | grep -c . || true)"
  echo "  ${count} project(s) still present; sleeping ${INTERVAL_SEC}s..."
  sleep "$INTERVAL_SEC"
done

echo "error: wipe timed out; remaining project ids:" >&2
list_project_ids >&2 || true
echo "Refusing to uninstall the platform while workloads remain." >&2
exit 1
