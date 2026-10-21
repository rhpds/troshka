"""Failure-aware deployment cleanup and cluster completion contracts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from app.models.project import Project
from app.services import deploy_service as deploy


@pytest.fixture
def namespace_clients(monkeypatch):
    custom, core = Mock(), Mock()
    monkeypatch.setattr(
        "app.services.providers.kubevirt._get_k8s_clients",
        lambda _: (custom, core, Mock()),
    )
    monkeypatch.setattr(
        "app.services.providers.kubevirt._project_ns", lambda *_: "project-ns"
    )
    monkeypatch.setattr("time.sleep", Mock())
    cleanup = Mock()
    finalizers = Mock()
    monkeypatch.setattr(
        "app.services.providers.kubevirt._cleanup_project_persistent_volumes", cleanup
    )
    monkeypatch.setattr(
        "app.services.providers.kubevirt._force_clear_rook_finalizers", finalizers
    )
    return custom, core, cleanup, finalizers


def test_namespace_deletion_requires_explicit_not_found(namespace_clients):
    _, core, cleanup, _ = namespace_clients
    core.read_namespace.side_effect = [
        ApiException(status=403),
        SimpleNamespace(),
        ApiException(status=404),
    ]
    assert deploy._wait_for_namespace_deletion(Mock(), "project") is True
    assert core.read_namespace.call_count == 3
    cleanup.assert_called_once_with(core, "project-ns")


def test_namespace_transport_error_is_unknown_not_deleted(namespace_clients):
    _, core, cleanup, _ = namespace_clients
    core.read_namespace.side_effect = ConnectionError("offline")
    assert deploy._wait_for_namespace_deletion(Mock(), "project") is False
    cleanup.assert_not_called()


@pytest.mark.parametrize("finalizer_failure", [False, True])
def test_namespace_timeout_retains_failed_outcome_after_cleanup(
    namespace_clients, finalizer_failure
):
    _, core, cleanup, finalizers = namespace_clients
    if finalizer_failure:
        finalizers.side_effect = RuntimeError("failed")
    assert deploy._wait_for_namespace_deletion(Mock(), "project") is False
    assert core.read_namespace.call_count == (84 if finalizer_failure else 60)
    assert finalizers.call_count == (2 if finalizer_failure else 1)
    cleanup.assert_called_once()


def test_confirmed_namespace_deletion_survives_pv_cleanup_error(namespace_clients):
    _, core, cleanup, _ = namespace_clients
    core.read_namespace.side_effect = ApiException(status=404)
    cleanup.side_effect = RuntimeError("PV API unavailable")
    assert deploy._wait_for_namespace_deletion(Mock(), "project") is True


@pytest.mark.parametrize(
    "response,expected",
    [
        (ApiException(status=404), True),
        (ApiException(status=403), None),
        (ValueError("bad response"), False),
    ],
)
def test_namespace_short_poll_distinguishes_absent_unknown_and_pending(
    namespace_clients, response, expected
):
    _, core, _, _ = namespace_clients
    core.read_namespace.side_effect = response
    assert deploy._poll_namespace_until_gone(core, "project-ns", "p", 2) is expected


@pytest.mark.parametrize("deleted", [False, True])
def test_destroy_kubevirt_retains_record_until_namespace_confirmed(
    namespace_clients, monkeypatch, deleted
):
    _, core, _, _ = namespace_clients
    core.read_namespace.side_effect = (
        ApiException(status=404) if deleted else ConnectionError("offline")
    )
    provider, eip = Mock(), SimpleNamespace(public_ip="203.0.113.1")
    db = Mock()
    db.query.side_effect = lambda model: Mock(
        **{
            "filter_by.return_value.first.return_value": provider,
            "filter_by.return_value.all.return_value": [eip],
        }
    )
    driver, release, delete, error = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr("app.services.providers.get_provider_driver", lambda _: driver)
    monkeypatch.setattr("app.services.eip_service.release_eip", release)
    monkeypatch.setattr(deploy, "_delete_project_record", delete)
    monkeypatch.setattr(deploy, "_set_destroy_error", error)
    deploy._destroy_kubevirt_native(
        "project", SimpleNamespace(provider_id="provider"), db, True
    )
    driver.destroy_project.assert_called_once_with(provider, "project")
    release.assert_called_once_with(db, eip)
    assert delete.called is deleted
    assert error.called is not deleted


def test_destroy_kubevirt_provider_error_preserves_record(monkeypatch):
    db = Mock()
    db.query.return_value.filter_by.return_value.all.return_value = []
    driver = Mock()
    driver.destroy_project.side_effect = RuntimeError("operator offline")
    monkeypatch.setattr("app.services.providers.get_provider_driver", lambda _: driver)
    delete, error = Mock(), Mock()
    monkeypatch.setattr(deploy, "_delete_project_record", delete)
    monkeypatch.setattr(deploy, "_set_destroy_error", error)
    deploy._destroy_kubevirt_native(
        "project", SimpleNamespace(provider_id="provider"), db, True
    )
    delete.assert_not_called()
    error.assert_called_once_with("project", "operator offline")


@pytest.mark.parametrize("agent_error", [False, True])
def test_orphan_reaper_limits_deletion_to_project_prefix(monkeypatch, agent_error):
    monkeypatch.setattr(
        deploy,
        "get_all_vm_states",
        lambda _: {
            "troshka-project1-known": "off",
            "troshka-project1-orphan": "off",
            "troshka-project2-other": "running",
        },
    )
    start = Mock(return_value="job")
    if agent_error:
        start.side_effect = deploy.TroshkadError("offline")
    wait = Mock()
    monkeypatch.setattr(deploy, "start_job", start)
    monkeypatch.setattr(deploy, "wait_for_job", wait)
    deploy._reap_orphan_project_domains(
        Mock(), "project1-full-id", {"troshka-project1-known"}
    )
    assert start.call_count == 1
    assert start.call_args.args[2] == {"domain_name": "troshka-project1-orphan"}
    assert wait.called is not agent_error


def test_orphan_reaper_listing_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        deploy, "get_all_vm_states", Mock(side_effect=RuntimeError("offline"))
    )
    start = Mock()
    monkeypatch.setattr(deploy, "start_job", start)
    deploy._reap_orphan_project_domains(Mock(), "project", set())
    start.assert_not_called()


@pytest.mark.parametrize(
    "phase,status,elapsed", [("complete", "ready", 90), ("failed", "error", None)]
)
def test_cluster_finalization_updates_both_snapshots_and_notifies(
    monkeypatch, phase, status, elapsed
):
    from app.services.ocp.ops_pod_install import PHASE_COMPLETE, PHASE_FAILED

    project = Project(
        id="p",
        topology={"clusters": [{"id": "c", "ocpInstallStatus": "monitoring"}]},
        deployed_topology={"clusters": [{"id": "c", "ocpInstallStatus": "monitoring"}]},
    )
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = project
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    notify = Mock()
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notify)
    deploy._finalize_cluster_ocp_status(
        "p", "c", PHASE_COMPLETE if phase == "complete" else PHASE_FAILED, 90
    )
    assert project.topology["clusters"][0]["ocpInstallStatus"] == status
    assert project.deployed_topology["clusters"][0]["ocpInstallStatus"] == status
    assert project.topology["clusters"][0].get("ocpInstallElapsed") == elapsed
    assert project.ocp_status == status
    assert project.ocp_install_elapsed == 90
    assert notify.call_args.args[1]["type"] == "topology-update"
    assert db.close.call_count == 2


def test_cluster_finalization_unknown_cluster_does_not_notify(monkeypatch):
    from app.services.ocp.ops_pod_install import PHASE_FAILED

    project = Project(id="p", topology={"clusters": [{"id": "other"}]})
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = project
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    notify = Mock()
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notify)
    deploy._finalize_cluster_ocp_status("p", "missing", PHASE_FAILED, 90)
    db.commit.assert_not_called()
    notify.assert_not_called()
    db.close.assert_called_once()


@pytest.mark.parametrize("phase", ["missing", "error"])
def test_cluster_finalization_closes_db_on_missing_or_failed_lookup(monkeypatch, phase):
    from app.services.ocp.ops_pod_install import PHASE_FAILED

    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    if phase == "error":
        db.query.side_effect = RuntimeError("db unavailable")
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    deploy._finalize_cluster_ocp_status("p", "c", PHASE_FAILED, 90)
    db.close.assert_called_once()
    db.commit.assert_not_called()
