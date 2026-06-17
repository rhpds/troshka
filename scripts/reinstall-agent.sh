#!/usr/bin/env bash
#
# Full SSH reinstall of the troshkad agent (for broken agents or first-time setup).
# Use update-agent.sh for routine updates — it's faster and uses the troshkad API.
#
# Usage:
#   ./scripts/reinstall-agent.sh              # Reinstall on all connected hosts
#   ./scripts/reinstall-agent.sh <host-id>    # Reinstall on a specific host
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

HOST_PREFIX="${1:-}"

cd "$BACKEND_DIR" && exec "$VENV_PYTHON" -c "
import sys, time
from app.core.database import SessionLocal
from app.models.host import Host
from app.services.agent_deployer import deploy_agent
from app.services.troshkad_client import check_health

prefix = '${HOST_PREFIX}'

db = SessionLocal()
try:
    if prefix:
        hosts = db.query(Host).filter(Host.id == prefix).all()
        if not hosts:
            from sqlalchemy import cast, String
            hosts = db.query(Host).filter(cast(Host.id, String).like(prefix + '%')).all()
    else:
        hosts = db.query(Host).filter(Host.agent_status == 'connected').all()

    if not hosts:
        print('No hosts found' + (f' matching {prefix}' if prefix else ''))
        sys.exit(1)

    for h in hosts:
        print(f'{h.id[:8]} ({h.ip_address}): reinstalling...', end=' ', flush=True)
        try:
            result = deploy_agent(h.ip_address, h.private_key, h.id)
            if not result.get('success'):
                print('FAILED')
                continue
            # Wait for agent and update DB version
            for _ in range(20):
                time.sleep(3)
                health = check_health(h)
                if health:
                    h.agent_version = health.get('version', '')
                    h.agent_status = 'connected'
                    db.commit()
                    print(f'done (version {h.agent_version})')
                    break
            else:
                print('installed but agent not responding')
        except Exception as e:
            print(f'failed: {e}')
finally:
    db.close()
"
