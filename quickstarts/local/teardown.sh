#!/usr/bin/env bash
# Full wipe + remove local Compose stack.
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

COMPOSE_DIR="${REPO_ROOT}/deploy/compose"
ENV_FILE="${COMPOSE_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a && source "${ENV_FILE}" && set +a
fi

API_PORT="${TROSHKA_API_PORT:-8200}"
export TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:${API_PORT}}"

confirm "Destroy all Troshka projects and remove the Compose stack?" "$@" || {
  echo "Aborted."
  exit 1
}

detect_compose() {
  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    echo "podman compose"
    return
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  echo "error: need Podman or Docker with Compose v2" >&2
  exit 1
}

COMPOSE_CMD="$(detect_compose)"

"${_script_dir}/../lib/wipe-workloads.sh"
"${_script_dir}/../lib/verify-clean.sh"

echo "Stopping Compose stack and removing volumes..."
# shellcheck disable=SC2086
${COMPOSE_CMD} -f "${COMPOSE_DIR}/compose.yaml" --env-file "${ENV_FILE}" down -v

echo "Local Troshka control plane removed."
echo "If you created a macOS libvirt guest or used WSL only for this demo, remove that guest/distro when finished."
