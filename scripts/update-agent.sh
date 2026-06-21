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
from app.services.troshkad_client import push_update, push_vncd_update, check_health, TroshkadError

prefix = '${HOST_PREFIX}'
force = ${FORCE:-False}

troshkad_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath('.'))),
    'src', 'troshkad', 'troshkad.py')
with open(troshkad_path, 'rb') as f:
    script_bytes = f.read()
version = hashlib.sha256(script_bytes).hexdigest()[:12]
script_text = script_bytes.decode().replace('VERSION = \"dev\"', f'VERSION = \"{version}\"')
script_bytes = script_text.encode()

vncd_path = os.path.join(os.path.dirname(troshkad_path), '..', 'troshka-vncd', 'troshka-vncd.py')
vncd_bytes = b''
if os.path.exists(vncd_path):
    with open(vncd_path, 'rb') as f:
        vncd_bytes = f.read()
    vncd_text = vncd_bytes.decode().replace('VERSION = \"dev\"', f'VERSION = \"{version}\"')
    vncd_bytes = vncd_text.encode()

db = SessionLocal()
try:
    if prefix:
        from sqlalchemy import cast, String
        hosts = db.query(Host).filter(Host.state == 'active', Host.agent_status == 'connected', cast(Host.id, String).like(prefix + '%')).all()
    else:
        hosts = db.query(Host).filter(Host.state == 'active', Host.agent_status == 'connected').all()

    if not hosts:
        print('No connected hosts found' + (f' matching {prefix}' if prefix else ''))
        sys.exit(1)

    for h in hosts:
        if not force and h.agent_version == version:
            print(f'{h.id[:8]} ({h.ip_address}): already up to date ({version})')
            continue

        old_ver = h.agent_version or 'unknown'
        # Skip hosts that aren't actually reachable
        try:
            health = check_health(h)
            if not health:
                print(f'{h.id[:8]} ({h.ip_address}): skipped (unreachable)')
                continue
        except Exception:
            print(f'{h.id[:8]} ({h.ip_address}): skipped (unreachable)')
            continue

        print(f'{h.id[:8]} ({h.ip_address}): updating {old_ver} -> {version}...', end=' ', flush=True)
        try:
            push_update(h, script_bytes, version, force=force)
            if vncd_bytes and h.host_type != 'pattern_buffer':
                push_vncd_update(h, vncd_bytes)
            # Wait for restart (agent drains running jobs before shutdown, up to 120s)
            for _ in range(60):
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
