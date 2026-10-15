"""Exercise the final deploy gate with fake registry and Kubernetes commands."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["current", "stale-worker", "missing-digest"])
def test_full_deploy_checks_worker_and_fails_closed(tmp_path, scenario):
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts/deploy-full.sh").read_text()
    gate = script[script.index('echo "=== Step 5:') :]
    commands = {
        "skopeo": 'if [ "$SCENARIO" = missing-digest ]; then exit 1; fi\necho sha256:expected',
        "oc": (
            'echo "$*" >> "$CALL_LOG"\n'
            'case "$*" in\n'
            ' *"get pods"*) if [ "$SCENARIO" = stale-worker ] && '
            '[[ "$*" = *troshka-worker* ]]; then echo "troshka-worker sha256:old"; else echo "troshka-backend sha256:expected"; fi;;\n'
            "esac"
        ),
        "seq": "echo 60",
        "sleep": "exit 0",
    }
    for name, body in commands.items():
        path = tmp_path / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    log = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail; KC=unused; " + gate],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SCENARIO": scenario,
            "CALL_LOG": str(log),
        },
        capture_output=True,
        text=True,
    )
    if scenario == "current":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "rollout status deploy/troshka-worker" in log.read_text()
    else:
        assert result.returncode != 0, result.stdout
