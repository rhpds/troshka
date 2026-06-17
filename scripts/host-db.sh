#!/usr/bin/env bash
#
# Run Python DB commands against the Troshka backend database.
#
# Usage:
#   ./host-db.sh                                    # interactive Python shell with DB
#   ./host-db.sh "print(db.query(Pattern).count())" # run inline code
#   echo "print(...)" | ./host-db.sh -              # pipe code from stdin
#
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../src/backend" && pwd)"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: backend venv not found at $VENV_PYTHON" >&2
    exit 1
fi

PREAMBLE='
from sqlalchemy import cast, String
from app.core.database import SessionLocal
from app.models.host import Host
from app.models.project import Project
from app.models.pattern import Pattern
from app.models.library import LibraryItem
db = SessionLocal()

def find(model, prefix):
    """Find a record by UUID prefix, e.g. find(Host, "c73f")"""
    return db.query(model).filter(cast(model.id, String).like(prefix + "%")).first()
'

if [[ $# -eq 0 ]]; then
    cd "$BACKEND_DIR" && exec "$VENV_PYTHON" -i -c "${PREAMBLE}
print('DB session ready. Models: Host, Project, Pattern, LibraryItem')
print('Example: db.query(Pattern).all()')
"
elif [[ "$1" == "-" ]]; then
    cd "$BACKEND_DIR" && exec "$VENV_PYTHON" -c "${PREAMBLE}
import sys
exec(sys.stdin.read())
db.close()
"
else
    cd "$BACKEND_DIR" && exec "$VENV_PYTHON" -c "${PREAMBLE}
$1
db.close()
"
fi
