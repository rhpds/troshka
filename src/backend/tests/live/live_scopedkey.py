"""Fetch + use the ops-pod scoped API key for least-privilege assertions."""

from __future__ import annotations

from live_api import LiveClient
from live_hostcmd import host_podman, oc

_ENV_VAR = "TROSHKA_API_KEY"


def _pid8(pid: str) -> str:
    return pid[:8]


def scoped_key_from_pod(cfg, pid: str, provider: str) -> str:
    """Read the ops pod's own TROSHKA_API_KEY (the scoped trk_ key)."""
    container = f"troshka-{_pid8(pid)}-ops"
    if provider == "kubevirt":
        ns = f"troshka-{_pid8(pid)}"
        out = oc(
            "exec",
            "-n",
            ns,
            container,
            "--",
            "printenv",
            _ENV_VAR,
            kubeconfig=cfg.kubeconfig,
        )
    else:
        out = host_podman(cfg.troshkad_host, "exec", container, "printenv", _ENV_VAR)
    return out.strip().splitlines()[-1].strip()


def client_for_key(base_url: str, raw_key: str) -> LiveClient:
    return LiveClient(base_url, token=raw_key)
