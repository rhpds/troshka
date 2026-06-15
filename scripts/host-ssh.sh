#!/usr/bin/env bash
#
# SSH into a Troshka host, looking up credentials from the database.
#
# Usage:
#   ./host-ssh.sh                    # SSH into the first connected host
#   ./host-ssh.sh <host-id-prefix>   # SSH into a specific host by ID prefix
#   ./host-ssh.sh <host-id> <cmd>    # Run a command on the host
#   ./host-ssh.sh -- <cmd>           # Run a command on the first connected host
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

HOST_PREFIX="${1:-}"
shift_args=0

if [[ "$HOST_PREFIX" == "--" ]]; then
    HOST_PREFIX=""
    shift_args=1
elif [[ -n "$HOST_PREFIX" && "$HOST_PREFIX" != -* ]]; then
    shift_args=1
fi

read -r HOST_IP KEY_FILE < <(cd "$BACKEND_DIR" && "$VENV_PYTHON" -c "
import sys, tempfile, os
from app.core.database import SessionLocal
from app.models.host import Host
db = SessionLocal()
prefix = '${HOST_PREFIX}'
if prefix:
    from sqlalchemy import cast, String
    hosts = db.query(Host).filter(Host.agent_status == 'connected', cast(Host.id, String).like(prefix + '%')).all()
else:
    hosts = db.query(Host).filter(Host.agent_status == 'connected').all()
if not hosts:
    print('ERROR: No connected host found' + (f' matching {prefix}' if prefix else ''), file=sys.stderr)
    sys.exit(1)
if len(hosts) > 1 and prefix:
    print(f'ERROR: Multiple hosts match {prefix}:', file=sys.stderr)
    for h in hosts:
        print(f'  {h.id[:8]}  {h.ip_address}', file=sys.stderr)
    sys.exit(1)
h = hosts[0]
kf = tempfile.NamedTemporaryFile(delete=False, suffix='.pem', prefix='troshka-ssh-')
kf.write(h.private_key.encode())
kf.close()
os.chmod(kf.name, 0o600)
print(h.ip_address, kf.name)
db.close()
")

if [[ -z "$HOST_IP" ]]; then
    exit 1
fi

trap "rm -f '$KEY_FILE'" EXIT

SSH_OPTS=(-i "$KEY_FILE" -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o LogLevel=ERROR)

if [[ $shift_args -eq 1 ]]; then
    shift
fi

if [[ $# -gt 0 ]]; then
    exec ssh "${SSH_OPTS[@]}" "ec2-user@$HOST_IP" "$@"
else
    echo "Connecting to $HOST_IP..."
    exec ssh "${SSH_OPTS[@]}" "ec2-user@$HOST_IP"
fi
