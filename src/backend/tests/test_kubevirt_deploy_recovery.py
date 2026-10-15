"""Recovery must attach to an existing CR without restarting deployment."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def recovery(monkeypatch):
    from app.services import kubevirt_deploy_recovery as recovery

    project = SimpleNamespace(
        id="project-id",
        state="error",
        deploy_error="Deploy timed out waiting for operator (2 hours)",
        host_id="host-id",
        topology={"nodes": []},
        vni_map={"network": 100},
    )
    host = SimpleNamespace(
        id="host-id", host_type="kubevirt-cluster", provider_id="provider-id"
    )
    provider = SimpleNamespace(id="provider-id")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [
        project,
        host,
        provider,
    ]
    driver = MagicMock(spec=["get_project_status"])
    driver.get_project_status.return_value = {"phase": "Deploying"}
    monkeypatch.setattr(recovery, "SessionLocal", lambda: db)
    monkeypatch.setattr(recovery, "get_provider_driver", lambda _: driver)
    for name in (
        "set_progress",
        "delete_progress",
        "notify_project",
        "_poll_kubevirt_deploy",
        "_deploy_after_special_host",
    ):
        monkeypatch.setattr(recovery, name, MagicMock())
    return SimpleNamespace(
        module=recovery,
        project=project,
        host=host,
        provider=provider,
        db=db,
        driver=driver,
    )


def test_attaches_to_existing_cr_and_runs_completion_hooks(recovery):
    r = recovery

    def complete(*args):
        assert r.project.state == "deploying"
        assert r.project.deploy_error is None
        r.project.state = "active"

    r.module._poll_kubevirt_deploy.side_effect = complete
    r.module.resume_kubevirt_deploy_monitor(r.project.id)

    r.driver.get_project_status.assert_called_once_with(r.provider, r.project.id)
    r.module.set_progress.assert_called_once()
    assert r.module.set_progress.call_args.kwargs["ttl"] == 10800
    r.module._deploy_after_special_host.assert_called_once_with(
        r.db, r.host, r.project.id, r.project, r.project.topology, r.project.vni_map
    )
    r.db.close.assert_called_once()


def test_recovery_preserves_runtime_showroom_token_without_mutating_canvas(recovery):
    r = recovery
    r.project.deployed_topology = {"_showroom_access_token": "existing-capability"}

    def complete(_pid, project, _provider, _driver, topology, _db):
        assert topology["_showroom_access_token"] == "existing-capability"
        project.state = "active"

    r.module._poll_kubevirt_deploy.side_effect = complete
    r.module.resume_kubevirt_deploy_monitor(r.project.id)
    assert "_showroom_access_token" not in r.project.topology


@pytest.mark.parametrize("phase", ["Pending", "Error", "NotFound", None])
def test_rejects_missing_or_unsafe_cr_without_changing_state(recovery, phase):
    r = recovery
    r.driver.get_project_status.return_value = {"phase": phase}
    with pytest.raises(ValueError, match="existing"):
        r.module.resume_kubevirt_deploy_monitor(r.project.id)
    assert r.project.state == "error"
    r.db.commit.assert_not_called()
    r.module._poll_kubevirt_deploy.assert_not_called()


@pytest.mark.parametrize(
    "state,error",
    [("active", None), ("error", "disk image corrupt"), ("stopped", None)],
)
def test_rejects_unrelated_project_states(recovery, state, error):
    r = recovery
    r.project.state, r.project.deploy_error = state, error
    with pytest.raises(ValueError):
        r.module.resume_kubevirt_deploy_monitor(r.project.id)
    r.driver.get_project_status.assert_not_called()
    r.db.commit.assert_not_called()


@pytest.mark.parametrize("state", ["error", "deploying"])
def test_does_not_launch_completion_hooks_after_error_or_cancellation(recovery, state):
    r = recovery
    r.module._poll_kubevirt_deploy.side_effect = lambda *args: setattr(
        r.project, "state", state
    )
    r.module.resume_kubevirt_deploy_monitor(r.project.id)
    r.module._deploy_after_special_host.assert_not_called()


def test_poll_exception_marks_error_and_clears_progress(recovery):
    r = recovery
    r.module._poll_kubevirt_deploy.side_effect = RuntimeError(
        "monitor connection failed"
    )
    with pytest.raises(RuntimeError, match="monitor connection failed"):
        r.module.resume_kubevirt_deploy_monitor(r.project.id)
    assert r.project.state == "error"
    assert r.project.deploy_error == "monitor connection failed"
    r.module.delete_progress.assert_called_once_with("deploy:project-id")
    r.db.close.assert_called_once()


@pytest.mark.parametrize(
    "state,error",
    [
        ("deploying", None),
        ("error", "Background job lost while deploying — please retry"),
    ],
)
def test_accepts_running_cr_for_interrupted_deployment(recovery, state, error):
    r = recovery
    r.project.state, r.project.deploy_error = state, error
    r.driver.get_project_status.return_value = {"phase": "Running"}
    r.module.resume_kubevirt_deploy_monitor(r.project.id)
    r.module._poll_kubevirt_deploy.assert_called_once()


def test_status_lookup_error_preserves_project(recovery):
    r = recovery
    r.driver.get_project_status.side_effect = RuntimeError("CR not found")
    with pytest.raises(RuntimeError, match="CR not found"):
        r.module.resume_kubevirt_deploy_monitor(r.project.id)
    assert r.project.state == "error"
    r.db.commit.assert_not_called()
    r.module.set_progress.assert_not_called()


def test_rejects_other_host_type(recovery):
    r = recovery
    r.host.host_type = "shared"
    with pytest.raises(ValueError, match="native KubeVirt"):
        r.module.resume_kubevirt_deploy_monitor(r.project.id)
    r.driver.get_project_status.assert_not_called()
    r.db.commit.assert_not_called()
