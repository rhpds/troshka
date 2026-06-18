#!/usr/bin/env bash
#
# Simulate what AAP2 does to deploy/destroy a Troshka pattern via agnosticd-v2.
#
# Usage:
#   ./scripts/test-agnosticd-flow.sh <pattern-name> [guid]     # deploy
#   ./scripts/test-agnosticd-flow.sh --destroy [guid]           # destroy last or specified deploy
#   ./scripts/test-agnosticd-flow.sh --status [guid]            # check status
#   ./scripts/test-agnosticd-flow.sh --stop [guid]              # stop
#   ./scripts/test-agnosticd-flow.sh --start [guid]             # start
#
# Prerequisites:
#   - Troshka backend running (http://localhost:8200)
#   - ansible-navigator installed
#   - ~/agnosticd-v2 repo checked out on troshka-cloud-provider branch
#   - ~/troshka-ansible-collection repo
#
# State is saved to ~/.troshka-test-state/ so lifecycle commands work.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TROSHKA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGD_DIR="$HOME/agnosticd-v2"
COLLECTION_DIR="$HOME/troshka-ansible-collection"
TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
CONFIG="${TROSHKA_CONFIG:-troshka}"

# --- Helper: install collection ---
install_collection() {
    ansible-galaxy collection install "$COLLECTION_DIR" -p "$HOME/.ansible/collections" --force 2>&1 | tail -1
}

# --- Helper: get or create API key ---
get_api_key() {
    if [[ -n "${TROSHKA_API_KEY:-}" ]]; then
        echo "$TROSHKA_API_KEY"
        return
    fi
    local key
    key=$(curl -s -X POST "${TROSHKA_API_URL}/api/v1/api-keys/" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"agnosticd-test-$(date +%s)\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
    echo "$key"
}

# --- Helper: resolve project ID from --guid ---
resolve_project_id() {
    local guid="$1"
    local api_key
    api_key=$(get_api_key)
    curl -sL "${TROSHKA_API_URL}/api/v1/projects/?guid=${guid}" \
        -H "Authorization: Bearer ${api_key}" | python3 -c "
import sys, json
projects = json.load(sys.stdin)
if not projects:
    print('', end='')
else:
    print(projects[0]['id'], end='')
"
}

# --- Helper: run lifecycle action ---
run_lifecycle() {
    local action="$1"
    local project_id="$2"
    local guid="$3"
    local api_key
    api_key=$(get_api_key)

    echo "=== $(echo "$action" | tr '[:lower:]' '[:upper:]') (project=$project_id) ==="

    install_collection

    local playbook="ansible/lifecycle_entry_point.yml"
    if [[ "$action" == "destroy" ]]; then
        playbook="ansible/destroy.yml"
    fi

    cd "$AGD_DIR"
    ANSIBLE_COLLECTIONS_PATH="$HOME/.ansible/collections" ANSIBLE_NOCOLOR=1 \
    ansible-navigator run "$playbook" \
        --mode stdout --ee false \
        --extra-vars "cloud_provider=troshka config=${CONFIG} guid=${guid} ACTION=${action} troshka_api_url=${TROSHKA_API_URL} troshka_api_key=${api_key} troshka_project_id=${project_id}" \
        -v
    local status=$?

    if [[ $status -eq 0 ]]; then
        echo "=== $(echo "$action" | tr '[:lower:]' '[:upper:]') OK ==="
    else
        echo "=== $(echo "$action" | tr '[:lower:]' '[:upper:]') FAILED (exit code: $status) ==="
    fi
    return $status
}

# --- Parse arguments ---
ACTION=""
PROJECT_ID_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --destroy) ACTION="destroy"; shift ;;
        --stop)    ACTION="stop";    shift ;;
        --start)   ACTION="start";   shift ;;
        --status)  ACTION="status";  shift ;;
        --project-id)   PROJECT_ID_ARG="$2"; shift 2 ;;
        --pattern-name) PATTERN_NAME_ARG="$2"; shift 2 ;;
        --guid)         GUID_ARG="$2"; shift 2 ;;
        *) break ;;
    esac
done

if [[ -n "$ACTION" ]]; then
    if [[ -n "$GUID_ARG" && -z "$PROJECT_ID_ARG" ]]; then
        echo "Looking up project for guid=$GUID_ARG..."
        PROJECT_ID_ARG=$(resolve_project_id "$GUID_ARG")
        if [[ -z "$PROJECT_ID_ARG" ]]; then
            echo "ERROR: No project found with guid '$GUID_ARG'"
            exit 1
        fi
        echo "  Found: $PROJECT_ID_ARG"
    fi
    if [[ -z "$PROJECT_ID_ARG" ]]; then
        echo "Usage: $0 --${ACTION} --project-id <uuid>"
        echo "       $0 --${ACTION} --guid <guid>"
        exit 1
    fi
    run_lifecycle "$ACTION" "$PROJECT_ID_ARG" "${GUID_ARG:-lifecycle}"
    exit $?
fi

# --- Deploy flow ---
PATTERN_NAME="${PATTERN_NAME_ARG:-${1:-}}"
GUID="${GUID_ARG:-${2:-test-$(date +%s)}}"

if [[ -z "$PATTERN_NAME" ]]; then
    echo "Usage: $0 --pattern-name <name> [--guid <guid>]"
    echo "       $0 --destroy --project-id <uuid>"
    echo "       $0 --stop --project-id <uuid>"
    echo "       $0 --start --project-id <uuid>"
    echo "       $0 --status --project-id <uuid>"
    echo ""
    echo "Available patterns:"
    curl -s "${TROSHKA_API_URL}/api/v1/patterns/" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    print(f'  {p[\"name\"]}  (state={p[\"state\"]})')
" 2>/dev/null || echo "  (could not fetch patterns — is backend running?)"
    exit 1
fi

API_KEY=$(get_api_key)
echo "  API key: ${API_KEY:0:15}..."

# --- Verify pattern exists ---
echo "=== Looking up pattern: $PATTERN_NAME ==="
PATTERN_COUNT=$(curl -s "${TROSHKA_API_URL}/api/v1/patterns/?name=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PATTERN_NAME'))")" \
    -H "Authorization: Bearer ${API_KEY}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [[ "$PATTERN_COUNT" == "0" ]]; then
    echo "  ERROR: Pattern '$PATTERN_NAME' not found"
    exit 1
fi
echo "  Found pattern"

# --- Install collection ---
echo "=== Installing Troshka collection ==="
install_collection

# --- Create vars file ---
VARS_FILE=$(mktemp /tmp/troshka-test-vars.XXXXXX)
mv "$VARS_FILE" "${VARS_FILE}.yml"
VARS_FILE="${VARS_FILE}.yml"
cat > "$VARS_FILE" <<VARS
---
cloud_provider: troshka
cloud_provider_repo: ${COLLECTION_DIR}

troshka_api_url: "${TROSHKA_API_URL}"
troshka_api_key: "${API_KEY}"

config: "${CONFIG}"

troshka_deploy_mode: pattern
troshka_pattern_name: "${PATTERN_NAME}"
troshka_portal_access_level: console

guid: "${GUID}"
VARS

echo "=== Deploying: guid=$GUID ==="

# --- Run ansible-navigator ---
cd "$AGD_DIR"

ANSIBLE_COLLECTIONS_PATH="$HOME/.ansible/collections" ANSIBLE_NOCOLOR=1 \
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    --extra-vars "@${VARS_FILE}" \
    -v

STATUS=$?

# --- Cleanup temp files ---
rm -f "$VARS_FILE"

if [[ $STATUS -eq 0 ]]; then
    PROJECT_ID=$(curl -sL "${TROSHKA_API_URL}/api/v1/projects/" \
        -H "Authorization: Bearer ${API_KEY}" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p['name'] == '${GUID}':
        print(p['id']); break
")
    echo ""
    echo "=== SUCCESS ==="
    echo "GUID:       $GUID"
    echo "Project ID: $PROJECT_ID"
    echo ""
    echo "Lifecycle commands:"
    echo "  $0 --status --guid $GUID"
    echo "  $0 --stop --guid $GUID"
    echo "  $0 --start --guid $GUID"
    echo "  $0 --destroy --guid $GUID"
else
    echo ""
    echo "=== FAILED (exit code: $STATUS) ==="
fi

exit $STATUS
