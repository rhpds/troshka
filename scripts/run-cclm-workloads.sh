#!/usr/bin/env bash
#
# Chain the CCLM demo_workloads roles against a deployed ocp-cclm project.
#
# Troshka runs one ad-hoc role per API call (not playbooks/cclm/main.yml).
# This script POSTs each role in order and waits for succeeded before the next.
#
# Usage:
#   ./scripts/run-cclm-workloads.sh <project-id-or-prefix>
#   ./scripts/run-cclm-workloads.sh <project> --only network
#   ./scripts/run-cclm-workloads.sh <project> --from forklift --extra-vars-file vars.yml
#
# Prerequisites:
#   - Troshka backend running (default http://localhost:8200)
#   - Project state=active, control-plane-usable on target cluster(s)
#   - Both nested clusters installed (source SNO+workers, destination SNO)
#
# Auth: set TROSHKA_API_KEY or let the script mint a dev key via POST /api/v1/api-keys/.
#
# Multi-cluster note: the runner pod mints clusters.default from one kubeconfig
# (selected by --cluster, default source). Roles that loop source+destination need
# both entries in clusters extra_vars once Troshka grows multi-cluster mint.
# Until then, pass --extra-vars-file if you have a hand-built clusters dict.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TROSHKA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$TROSHKA_DIR/src/backend"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

TROSHKA_API_URL="${TROSHKA_API_URL:-http://localhost:8200}"
DEMO_WORKLOADS_BRANCH="${CCLM_WORKLOADS_BRANCH:-feat/cclm-workloads}"
CLUSTER_ID="${CCLM_CLUSTER_ID:-source}"
POLL_INTERVAL="${CCLM_POLL_INTERVAL:-15}"
EXTRA_VARS_FILE=""
FROM_ROLE=""
ONLY_ROLE=""
DRY_RUN=0

ROLE_ORDER=(operators hco network forklift seed_vms)

role_short_to_fqcn() {
    case "$1" in
        operators) echo "troshka_workload_cclm_operators" ;;
        hco)       echo "troshka_workload_cclm_hco" ;;
        network)   echo "troshka_workload_cclm_network" ;;
        forklift)  echo "troshka_workload_cclm_forklift" ;;
        seed_vms)  echo "troshka_workload_cclm_seed_vms" ;;
        *)
            echo "ERROR: unknown role '$1' (expected: ${ROLE_ORDER[*]})" >&2
            return 1
            ;;
    esac
}

usage() {
    cat <<EOF
Usage: $0 <project-id-or-prefix> [options]

Run the CCLM workload role chain via Troshka ad-hoc workloads API.

Options:
  --api-url URL           Troshka API base (default: \$TROSHKA_API_URL or localhost:8200)
  --branch BRANCH         demo_workloads git ref (default: feat/cclm-workloads)
  --cluster ID            target_map.cluster_id for kubeconfig mint (default: source)
  --from ROLE             Start at this role (skip earlier steps)
  --only ROLE             Run a single role
  --extra-vars-file FILE  YAML extra_vars merged into each run (e.g. Ceph secret)
  --poll-interval SEC     Status poll interval (default: 15)
  --dry-run               Print payloads without POSTing
  -h, --help              Show this help

Roles (in order): ${ROLE_ORDER[*]}

Examples:
  $0 768d8c12
  $0 my-cclm-lab --only operators
  $0 my-cclm-lab --from network --extra-vars-file ~/cclm-extra-vars.yml

Environment:
  TROSHKA_API_URL, TROSHKA_API_KEY, CCLM_WORKLOADS_BRANCH, CCLM_CLUSTER_ID
EOF
}

get_api_key() {
    if [[ -n "${TROSHKA_API_KEY:-}" ]]; then
        echo "$TROSHKA_API_KEY"
        return
    fi
    curl -sf -X POST "${TROSHKA_API_URL}/api/v1/api-keys/" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"cclm-workloads-$(date +%s)\"}" \
        | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['key'])"
}

resolve_project_id() {
    local prefix="$1"
    local api_key="$2"

    if [[ -n "$api_key" ]]; then
        TROSHKA_API_KEY="$api_key" TROSHKA_API_URL="$TROSHKA_API_URL" PREFIX="$prefix" \
        "$VENV_PYTHON" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

api = os.environ["TROSHKA_API_URL"].rstrip("/")
key = os.environ["TROSHKA_API_KEY"]
prefix = os.environ["PREFIX"]

def get(path):
    req = urllib.request.Request(api + path, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

try:
    data = get("/api/v1/projects/")
except urllib.error.HTTPError as exc:
    print(f"ERROR: API request failed ({exc.code})", file=sys.stderr)
    sys.exit(1)

projects = data if isinstance(data, list) else data.get("projects", data.get("items", []))
matches = [
    p for p in projects
    if p.get("id", "").startswith(prefix) or p.get("name") == prefix
]
if not matches:
    print(f'ERROR: No project matching "{prefix}"', file=sys.stderr)
    sys.exit(1)
if len(matches) > 1:
    names = ", ".join(f'{p.get("name")} ({p["id"][:8]}…)' for p in matches)
    print(f'ERROR: Multiple projects match "{prefix}": {names}', file=sys.stderr)
    sys.exit(1)
print(matches[0]["id"])
PY
    fi
}

build_payload() {
    local role_short="$1"
    local role_name
    role_name=$(role_short_to_fqcn "$role_short")

    TROSHKA_PAYLOAD_ROLE="$role_name" \
    TROSHKA_PAYLOAD_BRANCH="$DEMO_WORKLOADS_BRANCH" \
    TROSHKA_PAYLOAD_CLUSTER="$CLUSTER_ID" \
    TROSHKA_PAYLOAD_EXTRA_VARS_FILE="${EXTRA_VARS_FILE:-}" \
    "$VENV_PYTHON" <<'PY'
import json
import os
import sys

role = os.environ["TROSHKA_PAYLOAD_ROLE"]
branch = os.environ["TROSHKA_PAYLOAD_BRANCH"]
cluster = os.environ["TROSHKA_PAYLOAD_CLUSTER"]
extra_file = os.environ.get("TROSHKA_PAYLOAD_EXTRA_VARS_FILE", "")

body = {
    "kind": "ad_hoc",
    "role_fqcn": f"rhpds.demo_workloads.{role}",
    "requirements_content": {
        "collections": [{
            "name": "https://github.com/rhpds/demo_workloads.git",
            "type": "git",
            "version": branch,
        }]
    },
    "target_map": {
        "mode": "cluster",
        "cluster_id": cluster,
    },
}

if extra_file:
    with open(extra_file, encoding="utf-8") as fh:
        body["extra_vars_text"] = fh.read()

print(json.dumps(body))
PY
}

trigger_workload() {
    local project_id="$1"
    local payload="$2"
    local api_key="$3"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "$payload" | "$VENV_PYTHON" -m json.tool
        echo "(dry-run: skipping POST)"
        return 0
    fi

    local response http_code
    response=$(curl -s -w "\n%{http_code}" -X POST \
        "${TROSHKA_API_URL}/api/v1/projects/${project_id}/workloads" \
        -H "Authorization: Bearer ${api_key}" \
        -H "Content-Type: application/json" \
        -d "$payload")
    http_code=$(echo "$response" | tail -n1)
    response=$(echo "$response" | sed '$d')

    if [[ "$http_code" != "202" ]]; then
        echo "ERROR: workload trigger failed (HTTP ${http_code})" >&2
        echo "$response" | "$VENV_PYTHON" -m json.tool 2>/dev/null || echo "$response" >&2
        return 1
    fi

    echo "$response" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['id'])"
}

poll_workload() {
    local run_id="$1"
    local api_key="$2"
    local status progress_line

    while true; do
        local response
        response=$(curl -sf \
            "${TROSHKA_API_URL}/api/v1/workloads/${run_id}" \
            -H "Authorization: Bearer ${api_key}")

        status=$(echo "$response" | "$VENV_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['status'])")
        progress_line=$(echo "$response" | "$VENV_PYTHON" -c "
import sys, json
p = json.load(sys.stdin).get('progress') or {}
msg = p.get('message') or p.get('stage') or ''
if msg:
    print(msg)
")

        if [[ -n "$progress_line" ]]; then
            echo "  status=${status}  ${progress_line}"
        else
            echo "  status=${status}"
        fi

        case "$status" in
            succeeded) return 0 ;;
            error|timeout|cancelled)
                echo "ERROR: workload ${run_id} ended with status=${status}" >&2
                return 1
                ;;
        esac
        sleep "$POLL_INTERVAL"
    done
}

show_workload_log() {
    local run_id="$1"
    local api_key="$2"

    echo "=== workload log (${run_id}) ==="
    curl -sf \
        "${TROSHKA_API_URL}/api/v1/workloads/${run_id}/log" \
        -H "Authorization: Bearer ${api_key}" \
        | "$VENV_PYTHON" -c "
import sys, json
log = json.load(sys.stdin).get('log') or ''
lines = log.splitlines()
tail = lines[-80:] if len(lines) > 80 else lines
print('\n'.join(tail))
" || echo "(could not fetch log)"
}

role_index() {
    local needle="$1"
    local i
    for i in "${!ROLE_ORDER[@]}"; do
        if [[ "${ROLE_ORDER[$i]}" == "$needle" ]]; then
            echo "$i"
            return 0
        fi
    done
    return 1
}

select_roles() {
    local start=0
    local end=$((${#ROLE_ORDER[@]} - 1))

    if [[ -n "$ONLY_ROLE" ]]; then
        if ! role_index "$ONLY_ROLE" >/dev/null; then
            role_short_to_fqcn "$ONLY_ROLE" >/dev/null
        fi
        SELECTED_ROLES=("$ONLY_ROLE")
        return
    fi

    if [[ -n "$FROM_ROLE" ]]; then
        start=$(role_index "$FROM_ROLE")
        role_short_to_fqcn "$FROM_ROLE" >/dev/null
    fi

    SELECTED_ROLES=()
    local i
    for i in $(seq "$start" "$end"); do
        SELECTED_ROLES+=("${ROLE_ORDER[$i]}")
    done
}

# --- parse args ---
PROJECT_PREFIX=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api-url) TROSHKA_API_URL="$2"; shift 2 ;;
        --branch) DEMO_WORKLOADS_BRANCH="$2"; shift 2 ;;
        --cluster) CLUSTER_ID="$2"; shift 2 ;;
        --from) FROM_ROLE="$2"; shift 2 ;;
        --only) ONLY_ROLE="$2"; shift 2 ;;
        --extra-vars-file) EXTRA_VARS_FILE="$2"; shift 2 ;;
        --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)
            if [[ -z "$PROJECT_PREFIX" ]]; then
                PROJECT_PREFIX="$1"
            else
                echo "Unexpected argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$PROJECT_PREFIX" ]]; then
    usage >&2
    exit 1
fi

if [[ -n "$FROM_ROLE" && -n "$ONLY_ROLE" ]]; then
    echo "ERROR: --from and --only are mutually exclusive" >&2
    exit 1
fi

if [[ -n "$EXTRA_VARS_FILE" && ! -f "$EXTRA_VARS_FILE" ]]; then
    echo "ERROR: extra-vars file not found: $EXTRA_VARS_FILE" >&2
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "ERROR: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

API_KEY=$(get_api_key)
echo "API:     ${TROSHKA_API_URL}"
echo "Branch:  ${DEMO_WORKLOADS_BRANCH}"
echo "Cluster: ${CLUSTER_ID} (kubeconfig for clusters.default mint)"

echo "Resolving project: ${PROJECT_PREFIX}"
PROJECT_ID=$(resolve_project_id "$PROJECT_PREFIX" "$API_KEY")
echo "Project: ${PROJECT_ID}"

select_roles
echo "Roles:   ${SELECTED_ROLES[*]}"
echo ""

FAILED=0
for role_short in "${SELECTED_ROLES[@]}"; do
    role_fqcn=$(role_short_to_fqcn "$role_short")
    echo "=== ${role_short} (${role_fqcn}) ==="

    payload=$(build_payload "$role_short")
    if ! run_id=$(trigger_workload "$PROJECT_ID" "$payload" "$API_KEY"); then
        FAILED=1
        break
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
        continue
    fi

    echo "  run_id=${run_id}"
    if ! poll_workload "$run_id" "$API_KEY"; then
        show_workload_log "$run_id" "$API_KEY"
        FAILED=1
        break
    fi
    echo "  OK"
    echo ""
done

if [[ "$FAILED" -eq 1 ]]; then
    echo "=== FAILED ===" >&2
    exit 1
fi

echo "=== All CCLM workloads succeeded ==="
