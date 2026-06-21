#!/usr/bin/env bash
#
# SSH into a project VM through the Troshka host.
# Two-hop: host SSH → network namespace → VM SSH.
#
# Usage:
#   ./vm-ssh.sh <project> <vm-name>           # interactive shell
#   ./vm-ssh.sh <project> <vm-name> <command>  # run command
#   ./vm-ssh.sh 768d8 bastion
#   ./vm-ssh.sh 768d8 bastion "oc get nodes"
#   ./vm-ssh.sh --user root 768d8 bastion
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

VM_USER="cloud-user"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user|-u) VM_USER="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: vm-ssh.sh [options] <project> <vm-name> [command]"
            echo ""
            echo "Options:"
            echo "  --user, -u     SSH username on VM (default: cloud-user)"
            echo ""
            echo "Examples:"
            echo "  ./vm-ssh.sh 768d8 bastion               # interactive shell"
            echo "  ./vm-ssh.sh 768d8 bastion 'oc get nodes' # run command"
            echo "  ./vm-ssh.sh --user root 768d8 hub-cp-0"
            exit 0
            ;;
        *) break ;;
    esac
done

PROJECT_PREFIX="${1:-}"
VM_NAME="${2:-}"
shift 2 2>/dev/null || true

if [[ -z "$PROJECT_PREFIX" || -z "$VM_NAME" ]]; then
    echo "Usage: vm-ssh.sh [options] <project> <vm-name> [command]" >&2
    exit 1
fi

read -r HOST_IP SSH_PORT SSH_USER KEY_FILE VM_IP VM_PASS NETNS < <(cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import sys, tempfile, os
from sqlalchemy import cast, String
from app.core.database import SessionLocal
from app.models.project import Project
from app.models.host import Host
from app.models.provider import Provider
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

vm_ip = ''
for nic in vm.get('data', {}).get('nics', []):
    if nic.get('ip'):
        vm_ip = nic['ip']
        break
if not vm_ip:
    print(f'ERROR: VM \"{name}\" has no IP address', file=sys.stderr)
    sys.exit(1)

vm_pass = vm.get('data', {}).get('ciCloudUserPassword', '')

h = db.query(Host).filter_by(id=p.host_id).first()
if not h:
    print(f'ERROR: Host not found for project', file=sys.stderr)
    sys.exit(1)

prov = db.query(Provider).filter_by(id=h.provider_id).first()
ptype = prov.type if prov else 'ec2'
ssh_port = 22000 if ptype == 'ocpvirt' else 22
ssh_user = 'cloud-user' if ptype == 'ocpvirt' else 'troshka' if ptype in ('gcp', 'azure') else 'ec2-user'

kf = tempfile.NamedTemporaryFile(delete=False, suffix='.pem', prefix='troshka-ssh-')
kf.write(h.private_key.encode())
kf.close()
os.chmod(kf.name, 0o600)

netns = f'troshka-{p.id[:8]}'

print(h.ip_address, ssh_port, ssh_user, kf.name, vm_ip, vm_pass, netns)
db.close()
")

if [[ -z "$HOST_IP" ]]; then
    exit 1
fi

trap "rm -f '$KEY_FILE'" EXIT

HOST_SSH_OPTS=(-p "$SSH_PORT" -i "$KEY_FILE" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o LogLevel=ERROR)

if [[ -n "$VM_PASS" ]]; then
    VM_SSH="sshpass -p '$VM_PASS' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR $VM_USER@$VM_IP"
else
    VM_SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR $VM_USER@$VM_IP"
fi

if [[ $# -gt 0 ]]; then
    exec ssh "${HOST_SSH_OPTS[@]}" -t "$SSH_USER@$HOST_IP" \
        "sudo ip netns exec $NETNS $VM_SSH '$*'"
else
    echo "Connecting to $VM_USER@$VM_IP via $SSH_USER@$HOST_IP (ns=$NETNS)..."
    exec ssh "${HOST_SSH_OPTS[@]}" -t "$SSH_USER@$HOST_IP" \
        "sudo ip netns exec $NETNS $VM_SSH"
fi
