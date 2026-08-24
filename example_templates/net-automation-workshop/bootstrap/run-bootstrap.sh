#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "$0")" && pwd)"
COLLECTION_DIR="${TROSHKA_COLLECTION_DIR:-$HOME/troshka-ansible-collection}"
TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
PROJECT_ID="${TROSHKA_PROJECT_ID:-}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set TROSHKA_PROJECT_ID" >&2
  exit 1
fi

if [[ -z "${TROSHKA_API_KEY:-}" ]]; then
  TROSHKA_API_KEY=$(curl -sf -X POST "${TROSHKA_API_URL}/api/v1/api-keys/" \
    -H 'Content-Type: application/json' \
    -d "{\"name\": \"netlab-bootstrap-$(date +%s)\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["key"])')
  export TROSHKA_API_KEY
fi

ansible-galaxy collection install "$COLLECTION_DIR" -p "$HOME/.ansible/collections" --force >/dev/null

INV_FILE="${BOOTSTRAP_DIR}/workshop.generated.troshka.yml"
cat > "$INV_FILE" <<EOF
plugin: troshka.cloud.troshka
api_url: ${TROSHKA_API_URL}
api_key: ${TROSHKA_API_KEY}
project_id: ${PROJECT_ID}
connection_mode: troshka
EOF

export ANSIBLE_STDOUT_CALLBACK=default
cd "$BOOTSTRAP_DIR"

PLAYBOOK_ARGS=(-i "$INV_FILE" site.yml)
if [[ -n "${REG_USER:-}" ]]; then PLAYBOOK_ARGS+=(-e "reg_user=${REG_USER}"); fi
if [[ -n "${REG_PASS:-}" ]]; then PLAYBOOK_ARGS+=(-e "reg_pass=${REG_PASS}"); fi
if [[ -n "${REG_ORG:-}" ]]; then PLAYBOOK_ARGS+=(-e "reg_org=${REG_ORG}"); fi
if [[ -n "${REG_ACTIVATION_KEY:-}" ]]; then PLAYBOOK_ARGS+=(-e "reg_activation_key=${REG_ACTIVATION_KEY}"); fi

exec ansible-playbook "${PLAYBOOK_ARGS[@]}" "$@"
