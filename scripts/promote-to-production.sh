#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ "$#" -ne 1 ]; then
  echo "Usage: scripts/promote-to-production.sh <release-tag>" >&2
  exit 2
fi
exec python3 "$ROOT/scripts/deploy_release.py" promote --tag "$1"
