"""Tests for operator_updater helper functions."""

from unittest.mock import MagicMock

from app.services.operator_updater import _read_deployment_info, _read_pod_digest


# ---------------------------------------------------------------------------
# Tests for _read_deployment_info
# ---------------------------------------------------------------------------
def test_read_deployment_info_ready():
    """Deployment exists, fully ready → returns (False, image_tag)."""
    apps_api = MagicMock()
    mock_dep = MagicMock()
    mock_dep.spec.replicas = 1
    mock_dep.status.updated_replicas = 1
    mock_dep.status.ready_replicas = 1
    mock_dep.spec.template.spec.containers = [
        MagicMock(image="quay.io/redhat-gpte/troshka-operator:v2.5.0")
    ]
    apps_api.read_namespaced_deployment.return_value = mock_dep

    rolling_out, tag = _read_deployment_info(apps_api, "troshka-operator")

    assert rolling_out is False
    assert tag == "v2.5.0"
    apps_api.read_namespaced_deployment.assert_called_once_with(
        name="troshka-operator", namespace="troshka-operator"
    )


def test_read_deployment_info_not_found():
    """Deployment not found (exception) → returns (False, default TAG)."""
    from app.services.operator_updater import TAG

    apps_api = MagicMock()
    apps_api.read_namespaced_deployment.side_effect = Exception("404 Not Found")

    rolling_out, tag = _read_deployment_info(apps_api, "troshka-operator")

    # Exception path: rolling_out stays False, tag stays module-level TAG default
    assert rolling_out is False
    assert tag == TAG


def test_read_deployment_info_rolling_out():
    """Deployment exists but not all replicas ready → returns (True, tag)."""
    apps_api = MagicMock()
    mock_dep = MagicMock()
    mock_dep.spec.replicas = 2
    mock_dep.status.updated_replicas = 1  # Not all updated
    mock_dep.status.ready_replicas = 1  # Not all ready
    mock_dep.spec.template.spec.containers = [
        MagicMock(image="quay.io/redhat-gpte/troshka-operator:latest")
    ]
    apps_api.read_namespaced_deployment.return_value = mock_dep

    rolling_out, tag = _read_deployment_info(apps_api, "troshka-operator")

    assert rolling_out is True
    assert tag == "latest"


def test_read_deployment_info_no_tag_in_image():
    """Image string without a colon → tag stays as module-level TAG default."""
    from app.services.operator_updater import TAG

    apps_api = MagicMock()
    mock_dep = MagicMock()
    mock_dep.spec.replicas = 1
    mock_dep.status.updated_replicas = 1
    mock_dep.status.ready_replicas = 1
    mock_dep.spec.template.spec.containers = [
        MagicMock(image="quay.io/redhat-gpte/troshka-operator")  # No tag
    ]
    apps_api.read_namespaced_deployment.return_value = mock_dep

    rolling_out, tag = _read_deployment_info(apps_api, "troshka-operator")

    assert rolling_out is False
    assert tag == TAG


# ---------------------------------------------------------------------------
# Tests for _read_pod_digest
# ---------------------------------------------------------------------------
def test_read_pod_digest_found():
    """Running pod with ready container → returns sha256 digest."""
    core_api = MagicMock()

    mock_cs = MagicMock()
    mock_cs.ready = True
    mock_cs.started = True
    mock_cs.image_id = (
        "quay.io/redhat-gpte/troshka-operator"
        "@sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    )

    mock_pod = MagicMock()
    mock_pod.status.phase = "Running"
    mock_pod.status.container_statuses = [mock_cs]

    mock_pod_list = MagicMock()
    mock_pod_list.items = [mock_pod]
    core_api.list_namespaced_pod.return_value = mock_pod_list

    digest = _read_pod_digest(core_api, "troshka-operator")

    assert digest == (
        "sha256:abcdef1234567890abcdef1234567890" "abcdef1234567890abcdef1234567890"
    )
    core_api.list_namespaced_pod.assert_called_once_with(
        namespace="troshka-operator",
        label_selector="app=troshka-operator",
    )


def test_read_pod_digest_no_pods():
    """No pods found → returns None."""
    core_api = MagicMock()
    mock_pod_list = MagicMock()
    mock_pod_list.items = []
    core_api.list_namespaced_pod.return_value = mock_pod_list

    digest = _read_pod_digest(core_api, "troshka-operator")

    assert digest is None


def test_read_pod_digest_api_error():
    """API exception → returns None (swallowed by except block)."""
    core_api = MagicMock()
    core_api.list_namespaced_pod.side_effect = Exception("connection refused")

    digest = _read_pod_digest(core_api, "troshka-operator")

    assert digest is None


def test_read_pod_digest_pod_not_running():
    """Pod exists but not Running → skipped, returns None."""
    core_api = MagicMock()

    mock_pod = MagicMock()
    mock_pod.status.phase = "Pending"

    mock_pod_list = MagicMock()
    mock_pod_list.items = [mock_pod]
    core_api.list_namespaced_pod.return_value = mock_pod_list

    digest = _read_pod_digest(core_api, "troshka-operator")

    assert digest is None


def test_read_pod_digest_container_not_ready():
    """Pod running but container not ready → skipped, returns None."""
    core_api = MagicMock()

    mock_cs = MagicMock()
    mock_cs.ready = False
    mock_cs.started = True
    mock_cs.image_id = "quay.io/test@sha256:abc123"

    mock_pod = MagicMock()
    mock_pod.status.phase = "Running"
    mock_pod.status.container_statuses = [mock_cs]

    mock_pod_list = MagicMock()
    mock_pod_list.items = [mock_pod]
    core_api.list_namespaced_pod.return_value = mock_pod_list

    digest = _read_pod_digest(core_api, "troshka-operator")

    assert digest is None
