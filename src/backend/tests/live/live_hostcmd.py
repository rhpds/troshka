"""Subprocess wrappers for host-side assertions (host-ssh / oc / host-db)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]  # tests/live -> backend -> src -> repo
_HOST_SSH = str(REPO_ROOT / "scripts" / "host-ssh.sh")
_HOST_DB = str(REPO_ROOT / "scripts" / "host-db.sh")


class HostCmdError(RuntimeError):
    pass


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HostCmdError(f"{argv!r} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def host_ssh(prefix: str, *cmd: str) -> str:
    return _run([_HOST_SSH, prefix, *cmd])


def host_podman(prefix: str, *args: str) -> str:
    """Run ``sudo podman <args>`` on a troshkad host.

    troshkad runs containers with rootful podman (as root), so an SSH session
    as the unprivileged login user only sees them via sudo.
    """
    return host_ssh(prefix, "sudo", "podman", *args)


def oc(*args: str, kubeconfig: str) -> str:
    return _run(["oc", f"--kubeconfig={kubeconfig}", *args])


def host_db(python_src: str) -> str:
    return _run([_HOST_DB, python_src])


def ops_pod_apikey_row(project_id: str) -> dict | None:
    """Return {'is_active', 'scopes'} for the ops-pod:<pid> key, or None."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    src = (
        "import json;"
        "from app.models.api_key import ApiKey;"
        f"k=db.query(ApiKey).filter_by(name='ops-pod:{project_id}')"
        ".order_by(ApiKey.created_at.desc()).first();"
        "print(json.dumps(None if k is None else "
        "{'is_active': bool(k.is_active), 'scopes': k.scopes}))"
    )
    out = host_db(src).strip().splitlines()[-1]
    return json.loads(out)
