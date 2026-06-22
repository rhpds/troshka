#!/usr/bin/env bash
#
# Test agnosticd-v2 lifecycle with a template deploy from agnosticv merged vars.
#
# Usage:
#   ./scripts/test-agnosticd-template.sh [guid]
#
# Prerequisites:
#   - Troshka backend running (http://localhost:8200)
#   - ansible-navigator installed
#   - ~/agnosticd-v2 repo with ansible/configs/troshka/
#   - ~/agnosticv repo with troshka/OCP4-RAN-TK/
#   - ~/troshka-ansible-collection installed
#   - ~/secrets/troshka-api-key.txt
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGD_DIR="$HOME/agnosticd-v2"
AGV_DIR="$HOME/agnosticv"
TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
GUID="${1:-$(head -c4 /dev/urandom | xxd -p | cut -c1-5)}"
shift 2>/dev/null || true
EXTRA_ARGS=("$@")
CI_PATH="troshka/OCP4-RAN-TK/dev.yaml"

echo "=== Troshka Template Deploy Test ==="
echo "  GUID:       $GUID"
echo "  API:        $TROSHKA_API_URL"
echo "  CI:         $CI_PATH"
echo ""

# --- API key ---
if [[ -f "$HOME/secrets/troshka-api-key.txt" ]]; then
    API_KEY=$(cat "$HOME/secrets/troshka-api-key.txt")
else
    echo "ERROR: ~/secrets/troshka-api-key.txt not found"
    exit 1
fi
echo "  API key:    ${API_KEY:0:15}..."

# --- Pull-through registry creds ---
PTR_CREDS_FILE="$HOME/secrets/troshka-pull-through-registry.yaml"
if [[ -f "$PTR_CREDS_FILE" ]]; then
    echo "  PTR creds:  $PTR_CREDS_FILE"
else
    echo "  WARNING: $PTR_CREDS_FILE not found — pull-through registry auth will fail"
    PTR_CREDS_FILE=""
fi

# --- Pull secret ---
PULL_SECRET_FILE="$HOME/secrets/ocp4-pull-secret.json"
if [[ -f "$PULL_SECRET_FILE" ]]; then
    echo "  Pull secret: $PULL_SECRET_FILE"
else
    echo "  WARNING: $PULL_SECRET_FILE not found — disconnected mirror will fail"
    echo "  Get yours from https://console.redhat.com/openshift/install/pull-secret"
    PULL_SECRET_FILE=""
fi

if [[ -n "$PULL_SECRET_FILE" ]]; then
    python3 -c "
import json, yaml, os
ps = open('$PULL_SECRET_FILE').read().strip()
fd = os.open('/tmp/troshka-pull-secret-vars.yaml', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    yaml.dump({'ocp4_pull_secret': ps, 'pull_secret': ps}, f)
"
fi

# --- Merge agnosticv CI ---
echo ""
echo "=== Merging agnosticv CI ==="
MERGED_FILE="/tmp/troshka-merged-${GUID}.yaml"
cd "$AGV_DIR"
agnosticv --merge "$CI_PATH" 2>/dev/null > "$MERGED_FILE"
echo "  Merged to:  $MERGED_FILE"
echo "  Lines:      $(wc -l < "$MERGED_FILE")"

# Verify key vars
for var in cloud_provider env_type troshka_deploy_mode vms networks; do
    if grep -q "^${var}:" "$MERGED_FILE"; then
        echo "  $var: $(grep "^${var}:" "$MERGED_FILE" | head -1 | cut -d: -f2- | xargs)"
    else
        echo "  WARNING: $var not found in merged output"
    fi
done

# --- Install collection from local checkout (dev override) ---
if [ -d "$HOME/troshka-ansible-collection" ]; then
    echo ""
    echo "=== Installing collection from local checkout ==="
    ansible-galaxy collection install "$HOME/troshka-ansible-collection" \
        -p "$HOME/.ansible/collections" --force 2>&1 | tail -1
fi

# --- Run ---
echo ""
echo "=== Running agnosticd-v2 lifecycle ==="
echo ""

cd "$AGD_DIR"
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    -e @ansible/configs/troshka/default_vars.yml \
    -e @"$MERGED_FILE" \
    -e config=troshka \
    -e troshka_api_url="$TROSHKA_API_URL" \
    -e troshka_api_key="$API_KEY" \
    -e guid="$GUID" \
    -e "troshka_project_name=RAN $GUID" \
    -e output_dir=/tmp/agnosticd-output \
    ${PULL_SECRET_FILE:+-e @/tmp/troshka-pull-secret-vars.yaml} \
    ${PTR_CREDS_FILE:+-e @"$PTR_CREDS_FILE"} \
    -v ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

STATUS=$?

# --- Cleanup ---
rm -f "$MERGED_FILE"

if [[ $STATUS -eq 0 ]]; then
    echo ""
    echo "=== SUCCESS ==="
    echo "  GUID: $GUID"
    echo ""
    echo "  Destroy:  $SCRIPT_DIR/test-agnosticd-flow.sh --destroy --guid $GUID"
else
    echo ""
    echo "=== FAILED (exit code: $STATUS) ==="
fi

exit $STATUS
