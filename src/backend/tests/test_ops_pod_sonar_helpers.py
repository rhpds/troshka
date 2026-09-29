"""Coverage for ops_pod_install phase / dead-pod helpers (Sonar new-code)."""

from app.services.ocp.ops_pod_install import (
    inject_dead_pod_failures,
    ops_pod_install_progress,
)


def test_complete_requires_cluster_breadcrumb():
    bare = ops_pod_install_progress({"c1": "install complete"})
    assert bare["clusters"]["c1"] != "complete"

    good = ops_pod_install_progress({"c1": "[c1] install complete"})
    assert good["clusters"]["c1"] == "complete"
    assert good["overall"] == "complete"
    assert good["done"] is True


def test_inject_preserves_waiting_sibling_in_multicluster():
    logs = {
        "c1": "[c1] install complete",
        "c2": "Waiting for cluster installation to complete",
    }
    out = inject_dead_pod_failures(logs, pod_running=False)
    prog = ops_pod_install_progress(out)
    assert prog["clusters"]["c1"] == "complete"
    # Multi-cluster: waiting sibling is not false-failed when the pod is down.
    assert prog["clusters"]["c2"] == "waiting"


def test_inject_noop_when_pod_running():
    logs = {"c1": "Waiting for cluster installation to complete"}
    assert inject_dead_pod_failures(logs, pod_running=True) == logs


def test_inject_single_cluster_waiting_becomes_failed():
    out = inject_dead_pod_failures(
        {"c1": "Waiting for cluster installation to complete"}, pod_running=False
    )
    assert ops_pod_install_progress(out)["clusters"]["c1"] == "failed"
