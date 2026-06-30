#!/usr/bin/env bash
#
# Test agnosticd-v2 lifecycle with a template deploy from agnosticv merged vars.
#
# Usage:
#   ./scripts/test-agnosticd-template.sh [options] [guid] [extra-ansible-args...]
#   ./scripts/test-agnosticd-template.sh                                    # RAN lab (default)
#   ./scripts/test-agnosticd-template.sh --ci troshka/OCP4-SNO-IBI-LAB/dev.yaml
#   ./scripts/test-agnosticd-template.sh --ci troshka/OCP4-SNO-IBI-LAB/dev.yaml --pattern "IBI Lab Ready"
#   ./scripts/test-agnosticd-template.sh --ci troshka/OCP4-SNO-IBI/common.yaml --skip-tags repos,packages
#
# Options:
#   --ci <path>        agnosticv catalog item path (default: troshka/OCP4-RAN-TK/dev.yaml)
#   --pattern <name>   Deploy from a saved pattern instead of building from scratch
#   --guid <guid>      Use a specific GUID (default: random 5-char hex)
#
# Prerequisites:
#   - Troshka backend running (http://localhost:8200)
#   - ansible-navigator installed
#   - ~/agnosticd-v2 repo with ansible/configs/troshka/
#   - ~/agnosticv repo
#   - ~/troshka-ansible-collection installed
#   - ~/secrets/troshka-api-key.txt
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGD_DIR="$HOME/agnosticd-v2"
AGV_DIR="$HOME/agnosticv"
TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
CI_PATH=""
PATTERN_NAME=""
GUID=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ci) CI_PATH="$2"; shift 2 ;;
        --pattern) PATTERN_NAME="$2"; shift 2 ;;
        --guid) GUID="$2"; shift 2 ;;
        --help|-h)
            head -15 "$0" | grep "^#" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            if [[ -z "$GUID" && "$1" =~ ^[a-f0-9]+$ && ${#1} -le 8 ]]; then
                GUID="$1"; shift
            else
                EXTRA_ARGS+=("$1"); shift
            fi
            ;;
    esac
done

if [[ -z "$CI_PATH" ]]; then
    echo "Error: --ci <path> is required (e.g., --ci troshka/OCP4-RAN-TK/dev.yaml)"
    exit 1
fi

GUID="${GUID:-$(head -c4 /dev/urandom | xxd -p | cut -c1-5)}"

# Derive project prefix from CI path (e.g., troshka/OCP4-SNO-IBI-LAB/dev.yaml → IBI-LAB)
CI_DIR=$(basename "$(dirname "$CI_PATH")")
PROJECT_PREFIX=$(echo "$CI_DIR" | sed 's/^OCP4-//; s/^OCP-//')

echo "=== Troshka Template Deploy Test ==="
echo "  GUID:       $GUID"
echo "  API:        $TROSHKA_API_URL"
echo "  CI:         $CI_PATH"
if [[ -n "$PATTERN_NAME" ]]; then
    echo "  Pattern:    $PATTERN_NAME"
fi
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
#PULL_SECRET_FILE="$HOME/secrets/ocp4-pull-secret.json"
#if [[ -f "$PULL_SECRET_FILE" ]]; then
#    echo "  Pull secret: $PULL_SECRET_FILE"
#else
#    echo "  WARNING: $PULL_SECRET_FILE not found — disconnected mirror will fail"
#    echo "  Get yours from https://console.redhat.com/openshift/install/pull-secret"
#    PULL_SECRET_FILE=""
#fi

#if [[ -n "$PULL_SECRET_FILE" ]]; then
#    python3 -c "
#import json, yaml, os
#ps = open('$PULL_SECRET_FILE').read().strip()
#fd = os.open('/tmp/troshka-pull-secret-vars.yaml', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
#with os.fdopen(fd, 'w') as f:
#    yaml.dump({'ocp4_pull_secret': ps, 'pull_secret': ps}, f)
#"
#fi

# --- Merge agnosticv CI ---
echo ""
echo "=== Merging agnosticv CI ==="
MERGED_FILE="/tmp/troshka-merged-${GUID}.yaml"
cd "$AGV_DIR"

# Concatenate common.yaml + CI file, resolving local #include directives
# (global includes like /includes/secrets/ are passed via -e @PTR_CREDS_FILE)
CI_FULL="$AGV_DIR/$CI_PATH"
CI_DIR="$(dirname "$CI_FULL")"
CI_COMMON="$CI_DIR/common.yaml"
{
    echo "---"
    for src in "$CI_COMMON" "$CI_FULL"; do
        [[ -f "$src" ]] || continue
        [[ "$src" == "$CI_COMMON" && "$(basename "$CI_FULL")" == "common.yaml" ]] && continue
        while IFS= read -r line; do
            if [[ "$line" =~ ^#include\ +(/.*) ]]; then
                inc="${BASH_REMATCH[1]}"
                inc_file="$AGV_DIR${inc}"
                # Resolve local includes (same CI dir), skip global ones
                if [[ -f "$inc_file" && "$inc_file" == "$CI_DIR"/* ]]; then
                    grep -v '^---' "$inc_file"
                fi
            elif [[ "$line" != "---" ]]; then
                echo "$line"
            fi
        done < "$src"
    done
} > "$MERGED_FILE"

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

# --- Pattern deploy mode ---
PATTERN_ARGS=""
if [[ -n "$PATTERN_NAME" ]]; then
    echo ""
    echo "=== Pattern deploy mode: $PATTERN_NAME ==="
    PATTERN_ARGS="-e troshka_deploy_mode=pattern -e troshka_pattern_name=$PATTERN_NAME"
fi

# --- Run ---
LOG_FILE="/tmp/troshka-$(echo "$PROJECT_PREFIX" | tr '[:upper:]' '[:lower:]')-${GUID}.log"
echo ""
echo "=== Running agnosticd-v2 lifecycle ==="
echo "  Log: $LOG_FILE"
echo ""

cd "$AGD_DIR"
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export PYTHONUNBUFFERED=1
VAULT_ARGS=""
VAULT_PW_FILE="$HOME/secrets/vault_pw.txt"
if [[ -f "$VAULT_PW_FILE" ]] && grep -q '!vault' "$MERGED_FILE" 2>/dev/null; then
    VAULT_ARGS="--vault-password-file $VAULT_PW_FILE"
fi

ANSIBLE_COLLECTIONS_PATH="$HOME/.ansible/collections" \
ansible-navigator run ansible/main.yml \
    --mode stdout \
    --ee false \
    -e @ansible/configs/troshka/default_vars.yml \
    -e @"$MERGED_FILE" \
    -e config=troshka \
    -e troshka_api_url="$TROSHKA_API_URL" \
    -e troshka_api_key="$API_KEY" \
    -e guid="$GUID" \
    -e troshka_project_name="${PROJECT_PREFIX}_${GUID}" \
    -e output_dir=/tmp/agnosticd-output \
    ${PULL_SECRET_FILE:+-e @/tmp/troshka-pull-secret-vars.yaml} \
    ${PTR_CREDS_FILE:+-e @"$PTR_CREDS_FILE"} \
    $VAULT_ARGS \
    $PATTERN_ARGS \
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
