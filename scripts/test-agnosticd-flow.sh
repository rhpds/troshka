#!/usr/bin/env bash
#
# Simulate what AAP2 does to deploy a Troshka pattern via agnosticd-v2.
#
# Usage:
#   ./scripts/test-agnosticd-flow.sh <pattern-name> [guid]
#
# Prerequisites:
#   - Troshka backend running (http://localhost:8200)
#   - A Troshka API key (set TROSHKA_API_KEY or it will be auto-generated)
#   - ansible-navigator installed
#   - ~/agnosticd-v2 repo checked out on troshka-cloud-provider branch
#   - ~/troshka-ansible-collection repo
#
# Examples:
#   ./scripts/test-agnosticd-flow.sh "OpenShift 4.22 Compact 3-Node (Agent Installer)-pattern"
#   ./scripts/test-agnosticd-flow.sh "My Lab Pattern" test-guid-001
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TROSHKA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGD_DIR="$HOME/agnosticd-v2"
COLLECTION_DIR="$HOME/troshka-ansible-collection"

TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
PATTERN_NAME="${1:-}"
GUID="${2:-test-$(date +%s)}"
CONFIG="${TROSHKA_CONFIG:-openshift-cluster-troshka}"

if [[ -z "$PATTERN_NAME" ]]; then
    echo "Usage: $0 <pattern-name> [guid]"
    echo ""
    echo "Available patterns:"
    curl -s "${TROSHKA_API_URL}/api/v1/patterns/" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    print(f'  {p[\"name\"]}  (state={p[\"state\"]})')
" 2>/dev/null || echo "  (could not fetch patterns — is backend running?)"
    exit 1
fi

# --- Get or create API key ---
if [[ -z "${TROSHKA_API_KEY:-}" ]]; then
    echo "=== Creating Troshka API key ==="
    API_KEY_RESP=$(curl -s -X POST "${TROSHKA_API_URL}/api/v1/api-keys/" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"agnosticd-test-$(date +%s)\"}")
    TROSHKA_API_KEY=$(echo "$API_KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
    echo "  API key: ${TROSHKA_API_KEY:0:15}..."
fi

# --- Verify pattern exists ---
echo "=== Looking up pattern: $PATTERN_NAME ==="
PATTERN_COUNT=$(curl -s "${TROSHKA_API_URL}/api/v1/patterns/?name=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PATTERN_NAME'))")" \
    -H "Authorization: Bearer ${TROSHKA_API_KEY}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [[ "$PATTERN_COUNT" == "0" ]]; then
    echo "  ERROR: Pattern '$PATTERN_NAME' not found"
    exit 1
fi
echo "  Found pattern"

# --- Install collection locally ---
echo "=== Installing Troshka collection from $COLLECTION_DIR ==="
COLLECTIONS_DIR=$(mktemp -d)
ansible-galaxy collection install "$COLLECTION_DIR" -p "$COLLECTIONS_DIR" --force 2>&1 | head -5
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
troshka_api_key: "${TROSHKA_API_KEY}"

config: "${CONFIG}"

troshka_deploy_mode: pattern
troshka_pattern_name: "${PATTERN_NAME}"
troshka_portal_access_level: console

guid: "${GUID}"
VARS

echo "=== Vars file: $VARS_FILE ==="
cat "$VARS_FILE"
echo ""

# --- Run ansible-navigator ---
echo "=== Running ansible-navigator ==="
cd "$AGD_DIR"

ANSIBLE_COLLECTIONS_PATH="${COLLECTIONS_DIR}:${HOME}/.ansible/collections" \
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    --extra-vars "@${VARS_FILE}" \
    -v

STATUS=$?

# --- Cleanup ---
rm -f "$VARS_FILE"
rm -rf "$COLLECTIONS_DIR"

if [[ $STATUS -eq 0 ]]; then
    echo ""
    echo "=== SUCCESS ==="
    echo "GUID: $GUID"
    echo "Check Troshka UI for the deployed project"
else
    echo ""
    echo "=== FAILED (exit code: $STATUS) ==="
fi

exit $STATUS
