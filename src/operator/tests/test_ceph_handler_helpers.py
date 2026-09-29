"""Unit tests for ceph handler helpers (Sonar new-code coverage)."""

from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from handlers.ceph import (
    _apply_rbac,
    _bind_scc,
    _ensure_external_secret,
    _ensure_service_account,
    _log_restore_mode,
    _set_ceph_status_endpoints,
    _unbind_scc,
    _update_ceph_ready_phase,
)


def test_ensure_service_account_creates():
    api = MagicMock()
    _ensure_service_account(api, "ns")
    api.create_namespaced_service_account.assert_called_once()


def test_ensure_service_account_ignores_409():
    api = MagicMock()
    api.create_namespaced_service_account.side_effect = ApiException(status=409)
    _ensure_service_account(api, "ns")


def test_ensure_service_account_reraises_other():
    api = MagicMock()
    api.create_namespaced_service_account.side_effect = ApiException(status=500)
    try:
        _ensure_service_account(api, "ns")
        assert False, "expected ApiException"
    except ApiException as e:
        assert e.status == 500


def test_apply_rbac_role_and_binding():
    rbac = MagicMock()
    role = {"kind": "Role", "metadata": {"name": "r"}}
    binding = {"kind": "RoleBinding", "metadata": {"name": "b"}}
    _apply_rbac(rbac, "ns", role, binding)
    rbac.create_namespaced_role.assert_called_once()
    rbac.create_namespaced_role_binding.assert_called_once()


def test_apply_rbac_ignores_409():
    rbac = MagicMock()
    rbac.create_namespaced_role.side_effect = ApiException(status=409)
    rbac.create_namespaced_role_binding.side_effect = ApiException(status=409)
    _apply_rbac(rbac, "ns", {"kind": "Role"}, {"kind": "RoleBinding"})


def test_log_restore_mode_sets_status():
    status_patch = MagicMock()
    status_patch.status = {}
    _log_restore_mode(
        {"monPvc": "mon", "osdPvcs": ["o1", "o2"]},
        {"cephId": "c1"},
        status_patch,
        "ns",
    )
    assert status_patch.status["restoreMode"] is True


@patch("handlers.ceph.nested_mon_host_from_secret", return_value="10.0.0.5")
def test_set_ceph_status_endpoints(_mon):
    status_patch = MagicMock()
    status_patch.status = {}
    _set_ceph_status_endpoints(
        status_patch, {"cephId": "x"}, MagicMock(), "ns", "10.0.0.1", 3, 2
    )
    assert status_patch.status["monEndpoint"] == "10.0.0.5"
    assert status_patch.status["labMonEndpoint"] == "10.0.0.1:3300"
    assert status_patch.status["osdCount"] == 3


@patch("handlers.ceph._modify_scc_users")
def test_bind_and_unbind_scc(mock_mod):
    custom = MagicMock()
    _bind_scc(custom, "ns")
    assert mock_mod.call_count >= 1
    mock_mod.reset_mock()
    _unbind_scc(custom, "ns")
    assert mock_mod.call_count >= 1


@patch("handlers.ceph._modify_scc_users", side_effect=RuntimeError("nope"))
def test_bind_scc_swallows_errors(_mod):
    _bind_scc(MagicMock(), "ns")


@patch("handlers.ceph.ceph_external_details_exported", return_value=False)
@patch("handlers.ceph.build_external_secret", return_value={"metadata": {"name": "s"}})
def test_ensure_external_secret_creates(mock_build, _exported):
    core = MagicMock()
    _ensure_external_secret(core, "ns", {}, "fsid", "10.0.0.1")
    core.create_namespaced_secret.assert_called_once()


@patch("handlers.ceph.ceph_external_details_exported", return_value=True)
@patch("handlers.ceph.build_external_secret", return_value={"metadata": {"name": "s"}})
def test_ensure_external_secret_409_already_exported(_build, _exported):
    core = MagicMock()
    core.create_namespaced_secret.side_effect = ApiException(status=409)
    _ensure_external_secret(core, "ns", {}, "fsid", "10.0.0.1")
    core.read_namespaced_secret.assert_not_called()


@patch("handlers.ceph.appliance_is_ready", return_value=False)
def test_update_ceph_ready_phase_progressing(_ready):
    status_patch = MagicMock()
    status_patch.status = {}
    _update_ceph_ready_phase(status_patch, MagicMock(), MagicMock(), {}, "ns", 3)
    assert status_patch.status["phase"] == "Progressing"


@patch("handlers.ceph.nested_mon_host_from_secret", return_value="10.0.0.9")
@patch("handlers.ceph.ceph_external_details_exported", return_value=True)
@patch("handlers.ceph.ensure_ceph_export_job")
@patch("handlers.ceph.appliance_is_ready", return_value=True)
@patch("handlers.ceph.client.BatchV1Api")
def test_update_ceph_ready_phase_ready(_batch, _ready, _export, _exported, _mon):
    status_patch = MagicMock()
    status_patch.status = {}
    _update_ceph_ready_phase(status_patch, MagicMock(), MagicMock(), {}, "ns", 3)
    assert status_patch.status["phase"] == "Ready"
    assert status_patch.status["monEndpoint"] == "10.0.0.9"
