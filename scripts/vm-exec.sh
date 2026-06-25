#!/usr/bin/env bash
#
# Execute a command on a project VM via the Troshka exec API.
# Handles JSON quoting automatically — no need to construct curl bodies.
#
# Usage:
#   ./vm-exec.sh <project> <vm-name> <command...>
#   ./vm-exec.sh 768d8 bastion "oc get pods -A"
#   ./vm-exec.sh 768d8 bastion 'python3 -c "print(1+2)"'
#   ./vm-exec.sh --user root 768d8 bastion "systemctl status sshd"
#   ./vm-exec.sh --timeout 300 768d8 bastion "long-running-command"
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"
API_URL="${TROSHKA_API_URL:-http://localhost:8200}"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

USERNAME="cloud-user"
PASSWORD=""
METHOD=""
TIMEOUT=600
BACKGROUND=false
LOG_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user|-u) USERNAME="$2"; shift 2 ;;
        --password|-p) PASSWORD="$2"; shift 2 ;;
        --timeout|-t) TIMEOUT="$2"; shift 2 ;;
        --serial|-s) METHOD="serial"; shift ;;
        --console|-c) METHOD="console"; shift ;;
        --console-text) METHOD="console-text"; shift ;;
        --bg|--background) BACKGROUND=true; shift ;;
        --log|-l) LOG_FILE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: vm-exec.sh [options] <project> <vm-name> <command...>"
            echo ""
            echo "Options:"
            echo "  --user, -u       SSH username (default: cloud-user)"
            echo "  --password, -p   Password for serial console login"
            echo "  --serial, -s     Force serial console exec"
            echo "  --console, -c    Force VNC console exec (send-key + screenshot + OCR)"
            echo "  --console-text   Console exec, switch to TTY2 first (for graphical VMs)"
            echo "  --timeout, -t    Command timeout in seconds (default: 600)"
            echo "  --bg             Run command in background on the VM (nohup)"
            echo "  --log, -l        Log file path on VM (default: /tmp/vm-exec-bg.log)"
            echo ""
            echo "Examples:"
            echo "  ./vm-exec.sh 768d8 bastion 'oc get nodes'"
            echo "  ./vm-exec.sh 768d8 bastion 'python3 -c \"print(1+2)\"'"
            echo "  ./vm-exec.sh --user root 768d8 bastion 'systemctl status sshd'"
            echo "  ./vm-exec.sh --bg --log ~/build.log 768d8 bastion './build.sh'"
            exit 0
            ;;
        *) break ;;
    esac
done

PROJECT_PREFIX="${1:-}"
VM_NAME="${2:-}"
shift 2 2>/dev/null || true
COMMAND="$*"

if [[ -z "$PROJECT_PREFIX" || -z "$VM_NAME" || -z "$COMMAND" ]]; then
    echo "Usage: vm-exec.sh [options] <project> <vm-name> <command...>" >&2
    exit 1
fi

read -r PROJECT_ID VM_ID < <(cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import sys
from sqlalchemy import cast, String
from app.core.database import SessionLocal
from app.models.project import Project
db = SessionLocal()
projects = db.query(Project).filter(
    Project.state.in_(['active', 'stopped']),
    (cast(Project.id, String).like('${PROJECT_PREFIX}%')) | (Project.name == '${PROJECT_PREFIX}')
).all()
if not projects:
    print('ERROR: No project found matching \"${PROJECT_PREFIX}\"', file=sys.stderr)
    sys.exit(1)
if len(projects) > 1:
    print(f'ERROR: Multiple projects match:', file=sys.stderr)
    for p in projects:
        print(f'  {p.id[:8]}  {p.name}  {p.state}', file=sys.stderr)
    sys.exit(1)
p = projects[0]
nodes = (p.topology or {}).get('nodes', [])
vm = None
name = '${VM_NAME}'.lower()
for n in nodes:
    if n.get('type') != 'vmNode':
        continue
    label = n.get('data', {}).get('name', '').lower()
    if label == name or n['id'].startswith(name):
        vm = n
        break
if not vm:
    print(f'ERROR: No VM named \"${VM_NAME}\" in project {p.name}', file=sys.stderr)
    print('Available VMs:', file=sys.stderr)
    for n in nodes:
        if n.get('type') == 'vmNode':
            print(f'  {n[\"data\"].get(\"name\", n[\"id\"][:8])}', file=sys.stderr)
    sys.exit(1)
print(p.id, vm['id'])
db.close()
")

if [[ -z "$PROJECT_ID" ]]; then
    exit 1
fi

# Wrap command for background execution
if [[ "$BACKGROUND" == "true" ]]; then
    if [[ -z "$LOG_FILE" ]]; then
        LOG_FILE="/tmp/vm-exec-bg-$(date +%s).log"
    fi
    COMMAND="nohup bash -c '${COMMAND}' > ${LOG_FILE} 2>&1 & echo \"PID: \$!\"; echo \"Log: ${LOG_FILE}\""
    TIMEOUT=15
fi

cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import json, urllib.request, sys

command = sys.argv[1]
payload = {
    'command': command,
    'username': '${USERNAME}',
    'timeout': ${TIMEOUT},
}
if '${PASSWORD}':
    payload['password'] = '${PASSWORD}'
if '${METHOD}':
    payload['method'] = '${METHOD}'
body = json.dumps(payload).encode()

req = urllib.request.Request(
    '${API_URL}/api/v1/projects/${PROJECT_ID}/vms/${VM_ID}/exec',
    data=body,
    headers={'Content-Type': 'application/json'},
    method='POST',
)
try:
    with urllib.request.urlopen(req, timeout=${TIMEOUT} + 30) as resp:
        result = json.loads(resp.read())
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f'API error {e.code}: {err}', file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as e:
    print(f'Connection error: {e.reason}', file=sys.stderr)
    sys.exit(1)

output = result.get('output', '')
error = result.get('error', '')
exit_code = result.get('exit_code', 0)

if output:
    print(output)
if error:
    print(error, file=sys.stderr)
sys.exit(exit_code)
" "$COMMAND"
