#!/usr/bin/env bash
#
# Test IBI (Image-Based Install) SNO deployment via agnosticd-v2.
#
# Usage:
#   ./scripts/test-ibi-deploy.sh [guid] [extra-ansible-args...]
#
# Prerequisites:
#   - Troshka backend running (http://localhost:8200)
#   - ansible-navigator installed
#   - ~/agnosticd-v2 repo with ansible/configs/troshka/
#   - ~/agnosticv repo with troshka/OCP4-SNO-IBI/
#   - ~/troshka-ansible-collection installed
#   - ~/secrets/troshka-api-key.txt
#   - ~/secrets/ocp4-pull-secret.json
#   - Seed image available: quay.io/redhat-gpte/sno-seed:4.22
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGD_DIR="$HOME/agnosticd-v2"
AGV_DIR="$HOME/agnosticv"
TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
GUID="${1:-$(head -c4 /dev/urandom | xxd -p | cut -c1-5)}"
shift 2>/dev/null || true
EXTRA_ARGS=("$@")
CI_PATH="troshka/OCP4-SNO-IBI/common.yaml"

echo "=== Troshka IBI SNO Deploy Test ==="
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
    echo "ERROR: $PULL_SECRET_FILE not found"
    echo "  Get yours from https://console.redhat.com/openshift/install/pull-secret"
    exit 1
fi

python3 -c "
import json, yaml, os
ps = open('$PULL_SECRET_FILE').read().strip()
fd = os.open('/tmp/troshka-pull-secret-vars.yaml', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    yaml.dump({'ocp4_pull_secret': ps, 'pull_secret': ps}, f)
"

# --- Merge agnosticv CI ---
echo ""
echo "=== Merging agnosticv CI ==="
MERGED_FILE="/tmp/troshka-ibi-merged-${GUID}.yaml"
cd "$AGV_DIR"
agnosticv --merge "$CI_PATH" 2>/dev/null > "$MERGED_FILE"
echo "  Merged to:  $MERGED_FILE"
echo "  Lines:      $(wc -l < "$MERGED_FILE")"

for var in cloud_provider env_type install_method host_ocp4_ibi_seed_image host_ocp4_installer_version; do
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
LOG_FILE="/tmp/troshka-ibi-${GUID}.log"
echo ""
echo "=== Running agnosticd-v2 lifecycle (IBI SNO) ==="
echo "  Expected time: ~20-25 min (5 min infra + 15 min IBI)"
echo "  Log: $LOG_FILE"
echo ""

cd "$AGD_DIR"
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export PYTHONUNBUFFERED=1
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    -e @ansible/configs/troshka/default_vars.yml \
    -e @"$MERGED_FILE" \
    -e config=troshka \
    -e troshka_api_url="$TROSHKA_API_URL" \
    -e troshka_api_key="$API_KEY" \
    -e guid="$GUID" \
    -e troshka_project_name="IBI_${GUID}" \
    -e output_dir=/tmp/agnosticd-output \
    -e @/tmp/troshka-pull-secret-vars.yaml \
    ${PTR_CREDS_FILE:+-e @"$PTR_CREDS_FILE"} \
    -v ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} 2>&1 | tee "$LOG_FILE"

STATUS=${PIPESTATUS[0]}

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
