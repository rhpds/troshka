"""Ops-pod create-image must serialize across parallel clusters (files_cache race)."""

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


def test_create_image_uses_shared_flock():
    script = _two_cluster_script()
    assert script.count("flock -w 15 200") == 2
    assert script.count("/workdir/.agent-create-image.lock") == 2
    assert 'echo "create-image: acquired lock"' in script
    assert "another cluster holds the shared agent cache lock" in script
    assert "still waiting on shared agent cache" in script


def test_create_image_still_runs_per_cluster():
    script = _two_cluster_script()
    assert script.count("agent create image --dir .") == 2
