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
STATE_DIR="$HOME/.troshka-test-state"

TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
CONFIG="${TROSHKA_CONFIG:-openshift-cluster-troshka}"

mkdir -p "$STATE_DIR"

# --- Helper: install collection into temp dir ---
install_collection() {
    COLLECTIONS_DIR=$(mktemp -d)
    ansible-galaxy collection install "$COLLECTION_DIR" -p "$COLLECTIONS_DIR" --force 2>&1 | tail -1
    echo "$COLLECTIONS_DIR"
}

# --- Helper: get or create API key ---
get_api_key() {
    if [[ -n "${TROSHKA_API_KEY:-}" ]]; then
        echo "$TROSHKA_API_KEY"
        return
    fi
    if [[ -f "$STATE_DIR/api_key" ]]; then
        cat "$STATE_DIR/api_key"
        return
    fi
    local key
    key=$(curl -s -X POST "${TROSHKA_API_URL}/api/v1/api-keys/" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"agnosticd-test\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
    echo "$key" > "$STATE_DIR/api_key"
    echo "$key"
}

# --- Helper: load state for a GUID ---
load_state() {
    local guid="$1"
    local state_file="$STATE_DIR/${guid}.json"
    if [[ ! -f "$state_file" ]]; then
        # Try "latest" symlink
        if [[ -L "$STATE_DIR/latest" ]]; then
            state_file=$(readlink "$STATE_DIR/latest")
        fi
    fi
    if [[ ! -f "$state_file" ]]; then
        echo "ERROR: No saved state for guid '$guid'. Run a deploy first." >&2
        exit 1
    fi
    cat "$state_file"
}

# --- Helper: run lifecycle action ---
run_lifecycle() {
    local action="$1"
    local guid="$2"
    local state_json
    state_json=$(load_state "$guid")
    local project_id api_key
    project_id=$(echo "$state_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
    api_key=$(get_api_key)

    echo "=== $(echo "$action" | tr '[:lower:]' '[:upper:]') (guid=$guid, project=$project_id) ==="

    local collections_dir
    collections_dir=$(install_collection)

    local playbook="ansible/lifecycle_entry_point.yml"
    if [[ "$action" == "destroy" ]]; then
        playbook="ansible/destroy.yml"
    fi

    cd "$AGD_DIR"
    ANSIBLE_COLLECTIONS_PATH="${collections_dir}:${HOME}/.ansible/collections" \
    ansible-navigator run "$playbook" \
        --mode stdout --ee false \
        --extra-vars "cloud_provider=troshka config=${CONFIG} guid=${guid} ACTION=${action} troshka_api_url=${TROSHKA_API_URL} troshka_api_key=${api_key} troshka_project_id=${project_id}" \
        -v
    local status=$?

    rm -rf "$collections_dir"

    if [[ "$action" == "destroy" && $status -eq 0 ]]; then
        rm -f "$STATE_DIR/${guid}.json"
        if [[ -L "$STATE_DIR/latest" ]]; then
            local latest_target
            latest_target=$(readlink "$STATE_DIR/latest")
            if [[ "$latest_target" == *"${guid}"* ]]; then
                rm -f "$STATE_DIR/latest"
            fi
        fi
        echo "=== DESTROYED ==="
    elif [[ $status -eq 0 ]]; then
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
        --project-id) PROJECT_ID_ARG="$2"; shift 2 ;;
        *) break ;;
    esac
done

if [[ -n "$ACTION" ]]; then
    GUID="${1:-}"
    if [[ -n "$PROJECT_ID_ARG" ]]; then
        # Create ad-hoc state from --project-id
        GUID="${GUID:-adhoc-$(date +%s)}"
        API_KEY=$(get_api_key)
        echo "{\"guid\": \"$GUID\", \"project_id\": \"$PROJECT_ID_ARG\", \"api_key\": \"$API_KEY\"}" > "$STATE_DIR/${GUID}.json"
    fi
    if [[ -z "$GUID" && -L "$STATE_DIR/latest" ]]; then
        GUID=$(readlink "$STATE_DIR/latest" | xargs basename | sed 's/.json//')
    fi
    if [[ -z "$GUID" ]]; then
        echo "Usage: $0 --${ACTION} [guid]"
        echo "       $0 --${ACTION} --project-id <uuid>"
        echo "No guid specified and no latest deploy found."
        exit 1
    fi
    run_lifecycle "$ACTION" "$GUID"
    exit $?
fi

# --- Deploy flow ---
PATTERN_NAME="${1:-}"
GUID="${2:-test-$(date +%s)}"

if [[ -z "$PATTERN_NAME" ]]; then
    echo "Usage: $0 <pattern-name> [guid]"
    echo "       $0 --destroy [guid]"
    echo "       $0 --stop [guid]"
    echo "       $0 --start [guid]"
    echo "       $0 --status [guid]"
    echo ""
    echo "Available patterns:"
    curl -s "${TROSHKA_API_URL}/api/v1/patterns/" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    print(f'  {p[\"name\"]}  (state={p[\"state\"]})')
" 2>/dev/null || echo "  (could not fetch patterns — is backend running?)"
    echo ""
    echo "Previous deploys:"
    for f in "$STATE_DIR"/*.json 2>/dev/null; do
        [[ -f "$f" ]] || continue
        local_guid=$(basename "$f" .json)
        echo "  $local_guid"
    done
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
COLLECTIONS_DIR=$(install_collection)
echo "  Installed to $COLLECTIONS_DIR"

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

ANSIBLE_COLLECTIONS_PATH="${COLLECTIONS_DIR}:${HOME}/.ansible/collections" \
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    --extra-vars "@${VARS_FILE}" \
    -v

STATUS=$?

# --- Cleanup temp files ---
rm -f "$VARS_FILE"
rm -rf "$COLLECTIONS_DIR"

if [[ $STATUS -eq 0 ]]; then
    # --- Save state for lifecycle commands ---
    PROJECT_ID=$(curl -sL "${TROSHKA_API_URL}/api/v1/projects/" \
        -H "Authorization: Bearer ${API_KEY}" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p['name'] == '${GUID}':
        print(p['id']); break
")
    if [[ -n "$PROJECT_ID" ]]; then
        cat > "$STATE_DIR/${GUID}.json" <<STATE
{"guid": "${GUID}", "project_id": "${PROJECT_ID}", "api_key": "${API_KEY}", "pattern": "${PATTERN_NAME}"}
STATE
        ln -sf "$STATE_DIR/${GUID}.json" "$STATE_DIR/latest"
        echo ""
        echo "=== SUCCESS ==="
        echo "GUID:       $GUID"
        echo "Project ID: $PROJECT_ID"
        echo "State saved to $STATE_DIR/${GUID}.json"
        echo ""
        echo "Lifecycle commands:"
        echo "  $0 --status"
        echo "  $0 --stop"
        echo "  $0 --start"
        echo "  $0 --destroy"
    else
        echo ""
        echo "=== SUCCESS (but could not find project ID to save state) ==="
        echo "GUID: $GUID"
    fi
else
    echo ""
    echo "=== FAILED (exit code: $STATUS) ==="
fi

exit $STATUS
