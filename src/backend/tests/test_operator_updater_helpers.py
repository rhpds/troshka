"""Tests for operator_updater helper functions."""

from unittest.mock import MagicMock, patch

from app.services.operator_updater import (
    K8S_REQUEST_TIMEOUT,
    _fetch_registry_digest,
    _get_operator_info,
    _poll_operator_digests,
    _read_deployment_info,
    _read_pod_digest,
    get_registry_digest,
)


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
        name="troshka-operator",
        namespace="troshka-operator",
        _request_timeout=K8S_REQUEST_TIMEOUT,
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
        _request_timeout=K8S_REQUEST_TIMEOUT,
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


# ---------------------------------------------------------------------------
# Tests for get_registry_digest
# ---------------------------------------------------------------------------
def test_get_registry_digest_returns_cached():
    import app.services.operator_updater as mod

    old = mod._registry_digest
    mod._registry_digest = "sha256:cached"
    try:
        assert get_registry_digest() == "sha256:cached"
    finally:
        mod._registry_digest = old


# ---------------------------------------------------------------------------
# Tests for _fetch_registry_digest
# ---------------------------------------------------------------------------
def test_fetch_registry_digest_success():
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = "sha256:abc123"

    with patch(
        "app.services.operator_updater.urllib.request.urlopen",
        return_value=mock_resp,
    ):
        result = _fetch_registry_digest("latest")
    assert result == "sha256:abc123"


def test_fetch_registry_digest_fallback_to_body():
    import json

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = None
    mock_resp.read.return_value = json.dumps(
        {"config": {"digest": "sha256:body"}}
    ).encode()

    with patch(
        "app.services.operator_updater.urllib.request.urlopen",
        return_value=mock_resp,
    ):
        result = _fetch_registry_digest()
    assert result == "sha256:body"


def test_fetch_registry_digest_error():
    with patch(
        "app.services.operator_updater.urllib.request.urlopen",
        side_effect=Exception("network error"),
    ):
        result = _fetch_registry_digest()
    assert result is None


# ---------------------------------------------------------------------------
# Tests for _get_operator_info
# ---------------------------------------------------------------------------
@patch("app.services.operator_updater._read_pod_digest", return_value="sha256:run")
@patch(
    "app.services.operator_updater._read_deployment_info",
    return_value=(False, "latest"),
)
@patch("kubernetes.client")
def test_get_operator_info_success(mock_client, mock_dep, mock_pod):
    provider = MagicMock()
    provider.get_credentials.return_value = {
        "api_url": "https://api.test:6443",
        "token": "fake-token",
        "verify_ssl": False,
        "namespace": "troshka-op",
    }
    digest, rolling, tag = _get_operator_info(provider)
    assert digest == "sha256:run"
    assert rolling is False
    assert tag == "latest"


# ---------------------------------------------------------------------------
# Tests for _poll_operator_digests
# ---------------------------------------------------------------------------
@patch("app.services.operator_updater._get_operator_info")
@patch(
    "app.services.operator_updater._fetch_registry_digest", return_value="sha256:new"
)
@patch("app.core.database.SessionLocal")
def test_poll_operator_digests_updates_host(mock_sl, mock_fetch, mock_info):
    import app.services.operator_updater as mod

    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    host.provider = MagicMock()
    host.operator_digest = "sha256:old"

    mock_info.return_value = ("sha256:run", False, "latest")

    db = MagicMock()
    mock_sl.return_value = db
    db.query.return_value.filter.return_value.all.return_value = [host]

    old_digest = mod._registry_digest
    try:
        _poll_operator_digests()
        assert host.operator_digest == "sha256:run"
        assert mod._registry_digest == "sha256:new"
        db.commit.assert_called_once()
    finally:
        mod._registry_digest = old_digest


@patch("app.services.operator_updater._get_operator_info")
@patch("app.services.operator_updater._fetch_registry_digest", return_value=None)
@patch("app.core.database.SessionLocal")
def test_poll_skips_rolling_out(mock_sl, mock_fetch, mock_info):
    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    host.provider = MagicMock()
    host.operator_digest = "sha256:old"

    mock_info.return_value = ("sha256:new", True, "latest")

    db = MagicMock()
    mock_sl.return_value = db
    db.query.return_value.filter.return_value.all.return_value = [host]

    _poll_operator_digests()
    assert host.operator_digest == "sha256:old"


@patch("app.services.operator_updater._get_operator_info")
@patch("app.services.operator_updater._fetch_registry_digest", return_value=None)
@patch("app.core.database.SessionLocal")
def test_poll_skips_host_without_provider(mock_sl, mock_fetch, mock_info):
    host = MagicMock()
    host.host_type = "kubevirt-cluster"
    host.provider = None

    db = MagicMock()
    mock_sl.return_value = db
    db.query.return_value.filter.return_value.all.return_value = [host]

    _poll_operator_digests()
    mock_info.assert_not_called()
