"""Ops-pod create-image isolates agent cache per cluster (no shared flock)."""

from __future__ import annotations

from app.services.ocp.ops_pod_install import build_ops_pod_install_script


def _two_cluster_script() -> str:
    clusters = [
        {"id": "source", "name": "source", "type": "sno"},
        {"id": "destination", "name": "destination", "type": "sno"},
    ]
    bmc = {
        "source": (["192.168.100.10"], "pw"),
        "destination": (["192.168.100.14"], "pw"),
    }
    return build_ops_pod_install_script(
        clusters, bmc, ocp_version="4.20.0", workdir="/workdir"
    )


def test_create_image_uses_per_cluster_cache_not_flock():
    script = _two_cluster_script()
    assert "flock" not in script
    assert "/workdir/.agent-create-image.lock" not in script
    assert script.count("export XDG_CACHE_HOME=") == 2
    assert "/workdir/source/.cache" in script
    assert "/workdir/destination/.cache" in script
    assert "create-image: using isolated cache" in script


def test_create_image_still_runs_per_cluster():
    script = _two_cluster_script()
    assert script.count("agent create image --dir .") == 2
