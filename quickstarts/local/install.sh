#!/usr/bin/env bash
# One-button local Troshka control plane (Compose).
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
source "${_script_dir}/../lib/common.sh"

COMPOSE_DIR="${REPO_ROOT}/deploy/compose"
ENV_FILE="${COMPOSE_DIR}/.env"
UI_PORT="${TROSHKA_UI_PORT:-3100}"
API_PORT="${TROSHKA_API_PORT:-8200}"

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

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 48 /dev/urandom | xxd -p | head -c 48
  fi
}

ensure_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    echo "Using existing ${ENV_FILE}"
    return
  fi
  echo "Generating ${ENV_FILE}"
  cat >"${ENV_FILE}" <<EOF
POSTGRES_PASSWORD=$(rand_hex)
JWT_SECRET=$(rand_hex)
ENCRYPTION_KEY=$(rand_hex)
S3_ACCESS_KEY=troshka
S3_SECRET_KEY=$(rand_hex)
TROSHKA_IMAGE_TAG=${TROSHKA_IMAGE_TAG:-latest}
TROSHKA_UI_PORT=${UI_PORT}
TROSHKA_API_PORT=${API_PORT}
EOF
}

ensure_env
# shellcheck disable=SC1090
set -a && source "${ENV_FILE}" && set +a

echo "Starting Troshka control plane with: ${COMPOSE_CMD}"
# shellcheck disable=SC2086
${COMPOSE_CMD} -f "${COMPOSE_DIR}/compose.yaml" --env-file "${ENV_FILE}" up -d

export TROSHKA_API_URL="http://localhost:${API_PORT}"
"${_script_dir}/../lib/wait-for-api.sh"

cat <<EOF

Troshka is up (dev auth — auto-admin).

  UI:  http://localhost:${UI_PORT}
  API: http://localhost:${API_PORT}

Next — pick compute (lab VMs need somewhere to run):
  1. Local/near-local host:  ${_script_dir}/bootstrap-host.sh   (Linux, or inside macOS guest / WSL2)
  2. Remote Linux with troshkad
  3. Cloud provider: EC2 / Azure / GCP / KubeVirt  (see docs/install-*.md)

Docs: docs/quickstarts/local.md
Teardown: ${_script_dir}/teardown.sh

EOF
