#!/usr/bin/env bash
#
# Execute commands on a bastion VM via the Troshka exec API.
#
# Usage:
#   ./bastion-exec.sh                         # List OCP projects with bastion VMs
#   ./bastion-exec.sh <project-id-prefix>     # Interactive shell-like prompt
#   ./bastion-exec.sh <project-id-prefix> <cmd>  # Run a single command
#   ./bastion-exec.sh <project-id-prefix> -- <cmd with args>
#
set -euo pipefail

API="http://localhost:8200/api/v1"
BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

PROJECT_PREFIX="${1:-}"

# No args — list OCP projects with bastions
if [[ -z "$PROJECT_PREFIX" ]]; then
    cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
from app.core.database import SessionLocal
from app.models.project import Project
db = SessionLocal()
projects = db.query(Project).filter(Project.state.in_(['active', 'stopped'])).all()
found = False
for p in projects:
    nodes = (p.topology or {}).get('nodes', [])
    bastion = next((n for n in nodes if n.get('type') == 'vmNode' and 'bastion' in n.get('data', {}).get('name', '').lower()), None)
    if bastion:
        found = True
        ocp = p.ocp_status or '—'
        print(f'  {p.id[:8]}  {p.name:<40s}  ocp: {ocp}')
if not found:
    print('No active projects with bastion VMs found.')
db.close()
"
    exit 0
fi

# Resolve project + bastion VM ID
read -r PROJECT_ID BASTION_VM_ID PROJECT_NAME < <(cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import sys
from sqlalchemy import cast, String
from app.core.database import SessionLocal
from app.models.project import Project
db = SessionLocal()
prefix = '${PROJECT_PREFIX}'
projects = db.query(Project).filter(
    Project.state.in_(['active', 'stopped']),
    cast(Project.id, String).like(prefix + '%')
).all()
if not projects:
    print(f'ERROR: No active project matching {prefix}', file=sys.stderr)
    sys.exit(1)
if len(projects) > 1:
    print(f'ERROR: Multiple projects match {prefix}:', file=sys.stderr)
    for p in projects:
        print(f'  {p.id[:8]}  {p.name}', file=sys.stderr)
    sys.exit(1)
p = projects[0]
nodes = (p.topology or {}).get('nodes', [])
bastion = next((n for n in nodes if n.get('type') == 'vmNode' and 'bastion' in n.get('data', {}).get('name', '').lower()), None)
if not bastion:
    print(f'ERROR: No bastion VM found in project {p.name}', file=sys.stderr)
    sys.exit(1)
print(p.id, bastion['id'], p.name)
db.close()
")

if [[ -z "$PROJECT_ID" ]]; then
    exit 1
fi

shift  # remove project prefix

# Remove leading -- if present
if [[ "${1:-}" == "--" ]]; then
    shift
fi

exec_cmd() {
    local cmd="$1"
    local timeout="${2:-30}"
    local result
    result=$(curl -sf "$API/projects/$PROJECT_ID/vms/$BASTION_VM_ID/exec" \
        -X POST -H 'Content-Type: application/json' \
        -d "{\"command\": $(printf '%s' "$cmd" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"timeout\": $timeout}" 2>&1)

    local output error
    output=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('output',''))" 2>/dev/null)
    error=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',''))" 2>/dev/null)

    if [[ -n "$output" ]]; then
        echo "$output"
    fi
    if [[ -n "$error" ]]; then
        echo "$error" >&2
    fi
}

# Single command mode
if [[ $# -gt 0 ]]; then
    exec_cmd "$*" 30
    exit $?
fi

# Interactive mode
echo "Connected to bastion in: $PROJECT_NAME"
echo "Type commands, Ctrl-D to exit."
echo ""
while IFS= read -r -p "bastion> " line; do
    [[ -z "$line" ]] && continue
    exec_cmd "$line" 30
done
