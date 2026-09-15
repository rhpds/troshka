"""Unit tests for ops-pod per-cluster install progress (Plan 4, Task 7)."""

from app.services.ocp.ops_pod_install import (
    PHASE_COMPLETE,
    PHASE_FAILED,
    PHASE_WAITING,
    inject_dead_pod_failures,
    ops_pod_install_progress,
)

WAITING_LOG = "Waiting for cluster installation to complete"


def test_progress_waits_for_all_clusters():
    progress = ops_pod_install_progress(
        {
            "destination": "complete",
            "source": WAITING_LOG,
        }
    )
    assert progress["clusters"]["destination"] == PHASE_COMPLETE
    assert progress["clusters"]["source"] == PHASE_WAITING
    assert progress["done"] is False


def test_progress_all_complete_is_done():
    progress = ops_pod_install_progress(
        {"destination": "complete", "source": "[source] install complete"}
    )
    assert progress["done"] is True
    assert progress["overall"] == PHASE_COMPLETE


def test_progress_mixed_complete_and_failed_is_not_overall_success():
    progress = ops_pod_install_progress(
        {"destination": "complete", "source": PHASE_FAILED}
    )
    assert progress["done"] is True
    assert progress["overall"] == PHASE_FAILED


def test_inject_dead_pod_preserves_waiting_sibling_in_multi_cluster():
    logs = {
        "destination": "[destination] install complete",
        "source": WAITING_LOG,
    }
    result = inject_dead_pod_failures(logs, pod_running=False)
    assert result["destination"] == logs["destination"]
    assert result["source"] == logs["source"]


def test_inject_dead_pod_still_fails_single_cluster_waiting():
    logs = {"source": WAITING_LOG}
    result = inject_dead_pod_failures(logs, pod_running=False)
    assert result["source"] == PHASE_FAILED
