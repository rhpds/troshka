#!/usr/bin/env bash
# Exit non-zero if any Troshka projects still exist.
set -euo pipefail

_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${_lib_dir}/common.sh"

require_cmd curl jq

remaining="$(api_get "/api/v1/projects/" | jq -r '.[].id // empty' || true)"
if [[ -n "${remaining}" ]]; then
  echo "error: projects still present — refuse platform uninstall:" >&2
  echo "$remaining" >&2
  exit 1
fi

echo "Verify clean: no projects remain."
exit 0
