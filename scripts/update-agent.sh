#!/usr/bin/env bash
#
# Update the troshkad agent on connected hosts.
#
# Usage:
#   ./scripts/update-agent.sh              # Update all connected hosts
#   ./scripts/update-agent.sh <host-id>    # Update a specific host by ID prefix
#   ./scripts/update-agent.sh --force      # Force update even if version matches
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

HOST_PREFIX=""
FORCE=""
for arg in "$@"; do
    if [[ "$arg" == "--force" ]]; then
        FORCE="True"
    else
        HOST_PREFIX="$arg"
    fi
done

cd "$BACKEND_DIR" && exec "$VENV_PYTHON" -c "
import hashlib, os, sys, time

from app.core.database import SessionLocal
from app.models.host import Host
from app.services.troshkad_client import push_update, check_health, TroshkadError

prefix = '${HOST_PREFIX}'
force = ${FORCE:-False}

troshkad_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath('.'))),
    'src', 'troshkad', 'troshkad.py')
with open(troshkad_path, 'rb') as f:
    script_bytes = f.read()
version = hashlib.sha256(script_bytes).hexdigest()[:12]
script_text = script_bytes.decode().replace('VERSION = \"dev\"', f'VERSION = \"{version}\"')
script_bytes = script_text.encode()

db = SessionLocal()
try:
    if prefix:
        hosts = db.query(Host).filter(Host.agent_status == 'connected', Host.id.like(prefix + '%')).all()
    else:
        hosts = db.query(Host).filter(Host.agent_status == 'connected').all()

    if not hosts:
        print('No connected hosts found' + (f' matching {prefix}' if prefix else ''))
        sys.exit(1)

    for h in hosts:
        if not force and h.agent_version == version:
            print(f'{h.id[:8]} ({h.ip_address}): already up to date ({version})')
            continue

        old_ver = h.agent_version or 'unknown'
        print(f'{h.id[:8]} ({h.ip_address}): updating {old_ver} -> {version}...', end=' ', flush=True)
        try:
            push_update(h, script_bytes, version, force=force)
            # Wait for restart
            for _ in range(30):
                time.sleep(3)
                health = check_health(h)
                if health and health.get('version') == version:
                    h.agent_version = version
                    db.commit()
                    print('done')
                    break
            else:
                print('timed out waiting for agent')
        except TroshkadError as e:
            print(f'failed: {e}')
finally:
    db.close()
"
