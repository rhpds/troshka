#!/usr/bin/env bash
#
# SSH into a Troshka host, looking up credentials from the database.
#
# Usage:
#   ./host-ssh.sh <host-id-prefix>       # SSH into host by ID prefix
#   ./host-ssh.sh <host-id-prefix> <cmd> # Run a command on the host
#   ./host-ssh.sh pb                     # SSH into pattern buffer host
#
# The host ID prefix is required. Use the first 4-8 chars of the host UUID.
# Run --list to see all hosts.
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

HOST_PREFIX="${1:-}"

if [[ -z "$HOST_PREFIX" ]]; then
    echo "Usage: host-ssh.sh <host-id-prefix> [command]"
    echo "       host-ssh.sh --list"
    echo "       host-ssh.sh pb"
    exit 1
fi

# --list = show all hosts
if [[ "$HOST_PREFIX" == "--list" || "$HOST_PREFIX" == "-l" ]]; then
    cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
from app.core.database import SessionLocal
from app.models.host import Host
from app.models.provider import Provider
db = SessionLocal()
hosts = db.query(Host).filter(Host.state.in_(['active', 'stopped'])).all()
if not hosts:
    print('No hosts found')
else:
    print(f'{'ID':10} {'IP':18} {'Type':8} {'Provider':10} {'Agent':12} {'Instance'}')
    print('-' * 90)
    for h in hosts:
        prov = db.query(Provider).filter_by(id=h.provider_id).first()
        ptype = prov.type if prov else '?'
        print(f'{h.id[:8]:10} {(h.ip_address or \"-\"):18} {(h.host_type or \"shared\"):8} {ptype:10} {(h.agent_status or \"-\"):12} {h.instance_id}')
db.close()
"
    exit 0
fi

shift

read -r HOST_IP SSH_PORT SSH_USER KEY_FILE < <(cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import sys, tempfile, os
from app.core.database import SessionLocal
from app.models.host import Host
from app.models.provider import Provider
db = SessionLocal()
prefix = '${HOST_PREFIX}'
if prefix in ('pb', 'pattern-buffer', 'pattern_buffer'):
    hosts = db.query(Host).filter(Host.host_type == 'pattern_buffer', Host.state.in_(['active', 'stopped'])).all()
else:
    from sqlalchemy import cast, String
    hosts = db.query(Host).filter(Host.state.in_(['active', 'stopped']), cast(Host.id, String).like(prefix + '%')).all()
if not hosts:
    print('ERROR: No host found matching \"' + prefix + '\"', file=sys.stderr)
    sys.exit(1)
if len(hosts) > 1:
    print(f'ERROR: Multiple hosts match \"{prefix}\":', file=sys.stderr)
    for h in hosts:
        print(f'  {h.id[:8]}  {h.ip_address}  {h.instance_id}', file=sys.stderr)
    sys.exit(1)
h = hosts[0]
prov = db.query(Provider).filter_by(id=h.provider_id).first()
ptype = prov.type if prov else 'ec2'
ssh_port = 22000 if ptype == 'ocpvirt' else 22
ssh_user = 'cloud-user' if ptype == 'ocpvirt' else 'troshka' if ptype in ('gcp', 'azure') else 'ec2-user'
kf = tempfile.NamedTemporaryFile(delete=False, suffix='.pem', prefix='troshka-ssh-')
kf.write(h.private_key.encode())
kf.close()
os.chmod(kf.name, 0o600)
print(h.ip_address, ssh_port, ssh_user, kf.name)
db.close()
")

if [[ -z "$HOST_IP" ]]; then
    exit 1
fi

trap "rm -f '$KEY_FILE'" EXIT

SSH_OPTS=(-p "$SSH_PORT" -i "$KEY_FILE" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o LogLevel=ERROR)

if [[ $# -gt 0 ]]; then
    exec ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST_IP" "$@"
else
    echo "Connecting to $SSH_USER@$HOST_IP:$SSH_PORT..."
    exec ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST_IP"
fi
