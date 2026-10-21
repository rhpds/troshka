"""API deployment admission and install observability contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api import projects as api
from app.models.project import Project


@pytest.fixture
def context():
    project = Project(
        id="project",
        owner_id="owner",
        host_id="host",
        state="active",
        topology={
            "ocpInstallVia": "pod",
            "clusters": [{"id": "c", "type": "ocp", "ocpInstallStatus": "monitoring"}],
        },
    )
    host = SimpleNamespace(id="host", ip_address="192.0.2.1")
    db = Mock()
    db.query.side_effect = lambda model: Mock(
        **{
            "filter_by.return_value.first.return_value": project
            if model is Project
            else host
        }
    )
    return project, host, db, SimpleNamespace(id="owner", role="user")


@pytest.mark.parametrize(
    "endpoint",
    [api.get_ocp_install_log, api.restart_ocp_install, api.cancel_ocp_install],
)
@pytest.mark.parametrize("missing,status", [(True, 404), (False, 403)])
def test_install_routes_enforce_project_ownership(endpoint, missing, status):
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = (
        None if missing else SimpleNamespace(owner_id="other")
    )
    user = SimpleNamespace(id="user", role="user")
    with pytest.raises(HTTPException) as exc:
        endpoint("project", user, db, cluster="c")
    assert exc.value.status_code == status


@pytest.mark.parametrize("endpoint", [api.restart_ocp_install, api.cancel_ocp_install])
def test_install_mutations_reject_inactive_project(context, endpoint):
    project, _, db, user = context
    project.state = "stopped"
    with pytest.raises(HTTPException) as exc:
        endpoint("project", user, db, cluster="c")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("mode", ["bastion", "no-host", "missing-host"])
def test_install_log_handles_non_pod_and_missing_host(context, mode):
    project, _, db, user = context
    if mode == "bastion":
        project.topology = {"ocpInstallVia": "bastion"}
    elif mode == "no-host":
        project.host_id = None
    else:
        db.query.side_effect = lambda model: Mock(
            **{
                "filter_by.return_value.first.return_value": project
                if model is Project
                else None
            }
        )
    result = api.get_ocp_install_log("project", user, db)
    assert result == {
        "install_via": "bastion" if mode == "bastion" else "pod",
        "output": "",
        "clusters": [],
    }


@pytest.mark.parametrize("cluster", [None, "c", "unknown"])
def test_install_logs_report_per_cluster_state_and_timing(
    context, monkeypatch, cluster
):
    project, host, db, user = context
    project.deploy_started_at = datetime.fromtimestamp(100, UTC)
    project.ocp_install_elapsed = 99
    project.ocp_control_plane_usable_elapsed = 60
    project.topology["clusters"][0].update(
        ocpInstallStatus="error", ocpInstallElapsed=70, ocpInstallStartedAt=120
    )
    reader = Mock(return_value={"c": "install failed", "other": "other log"})
    monkeypatch.setattr("app.services.deploy_service.read_ops_pod_install_log", reader)
    result = api.get_ocp_install_log("project", user, db, cluster=cluster)
    reader.assert_called_once_with(host, "project", project.topology)
    assert result["clusters"] == ["c", "other"]
    if cluster is None:
        assert (
            result["output"] == "=== c ===\ninstall failed\n\n=== other ===\nother log"
        )
    elif cluster == "c":
        assert result["cluster_status"] == "error"
        assert result["ocp_install_elapsed"] == 70
        assert result["install_started_at"] == 120
        assert result["ocp_control_plane_usable_elapsed"] is None
    else:
        assert result["output"] == ""
        assert result["cluster_status"] == "monitoring"


@pytest.mark.parametrize(
    "endpoint,validation",
    [
        (api.restart_ocp_install, "validate_restart_ocp_cluster_install"),
        (api.cancel_ocp_install, "validate_cancel_ocp_cluster_install"),
    ],
)
def test_install_routes_relay_service_validation(
    context, monkeypatch, endpoint, validation
):
    _, _, db, user = context
    monkeypatch.setattr(
        f"app.services.deploy_service.{validation}",
        lambda *_: "Cluster install already completed",
    )
    with pytest.raises(HTTPException) as exc:
        endpoint("project", user, db, cluster="c")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Cluster install already completed"


def test_cancel_marks_pending_before_enqueue(context, monkeypatch):
    _, _, db, user = context
    monkeypatch.setattr(
        "app.services.deploy_service.validate_cancel_ocp_cluster_install",
        lambda *_: None,
    )
    calls = []
    monkeypatch.setattr(
        "app.services.deploy_service._mark_ocp_install_cancel_pending",
        lambda *args: calls.append(("mark", args)),
    )
    enqueue = Mock(side_effect=lambda *args, **kwargs: calls.append(("enqueue", args)))
    monkeypatch.setattr("app.core.redis.enqueue_job", enqueue)
    result = api.cancel_ocp_install("project", user, db, cluster="c")
    assert result["status"] == "cancelling"
    assert [c[0] for c in calls] == ["mark", "enqueue"]
    assert enqueue.call_args.kwargs == {"project_id": "project", "job_timeout": 300}


@pytest.mark.parametrize("registry_error", [None, "Registry unavailable"])
def test_restart_checks_registry_before_reset_and_enqueue(
    context, monkeypatch, registry_error
):
    project, _, db, user = context
    monkeypatch.setattr(
        "app.services.deploy_service.validate_restart_ocp_cluster_install",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "app.services.ocp.pull_through_registry.check_pull_through_registry_for_project",
        lambda *args, **kwargs: registry_error,
    )
    monkeypatch.setattr(
        "app.services.deploy_service.read_ops_pod_install_log", lambda *_: {}
    )
    monkeypatch.setattr("app.core.redis.get_progress", lambda _: None)
    marker, enqueue = Mock(), Mock()
    monkeypatch.setattr("app.core.redis.set_progress", marker)
    monkeypatch.setattr("app.services.deploy_service.set_progress", marker)
    monkeypatch.setattr(
        "app.services.deploy_service.clear_ops_pod_log_cache_keys", Mock()
    )
    monkeypatch.setattr("app.core.redis.enqueue_job", enqueue)
    if registry_error:
        with pytest.raises(HTTPException) as exc:
            api.restart_ocp_install("project", user, db, cluster="c")
        assert exc.value.detail == registry_error
        marker.assert_not_called()
        enqueue.assert_not_called()
    else:
        result = api.restart_ocp_install("project", user, db, cluster="c")
        assert result["status"] == "restarting"
        assert (
            result["install_started_at"]
            == project.topology["clusters"][0]["ocpInstallStartedAt"]
        )
        assert project.ocp_status == "monitoring"
        assert enqueue.call_args.kwargs == {
            "project_id": "project",
            "job_timeout": 3600,
        }


@pytest.mark.parametrize(
    "role,pool,host,status",
    [
        ("user", "pool", None, 403),
        ("admin", "pool", None, 404),
        ("admin", None, "host", 404),
    ],
)
def test_explicit_placement_requires_admin_and_existing_resources(
    role, pool, host, status
):
    db = Mock()
    db.get.return_value = None
    db.query.return_value.filter_by.return_value.first.return_value = None
    user = SimpleNamespace(role=role)
    with pytest.raises(HTTPException) as exc:
        api._validate_deploy_pool_and_host(db, user, pool, host)
    assert exc.value.status_code == status


@pytest.mark.parametrize(
    "mode,status,allowed",
    [
        ("shared-nfs", "available", True),
        ("shared-nfs", "error", False),
        ("local", "error", True),
    ],
)
def test_pool_admission_checks_shared_pool_health(mode, status, allowed):
    db = Mock()
    db.get.return_value = SimpleNamespace(mode=mode, status=status)
    user = SimpleNamespace(role="admin")
    if allowed:
        api._validate_deploy_pool_and_host(
            db, SimpleNamespace(role="admin"), "pool", None
        )
    else:
        with pytest.raises(HTTPException) as exc:
            api._validate_deploy_pool_and_host(db, user, "pool", None)
        assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "state,agent,allowed",
    [
        ("active", "connected", True),
        ("active", "disconnected", False),
        ("error", "connected", False),
    ],
)
def test_host_admission_requires_active_connected_agent(state, agent, allowed):
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        state=state, agent_status=agent
    )
    user = SimpleNamespace(role="admin")
    if allowed:
        api._validate_deploy_pool_and_host(
            db, SimpleNamespace(role="admin"), None, "host"
        )
    else:
        with pytest.raises(HTTPException) as exc:
            api._validate_deploy_pool_and_host(db, user, None, "host")
        assert exc.value.status_code == 400


def test_multi_host_placement_persists_vm_to_host_mapping(context):
    project, _, db, _ = context
    result = {
        "network_host_id": "h1",
        "vni_map": {"net": 123},
        "host_assignments": {"h1": ["vm1", "vm2"], "h2": ["vm3"]},
    }
    api._setup_multi_host_placement(project, result, db)
    assert project.host_assignments == {"vm1": "h1", "vm2": "h1", "vm3": "h2"}
    assert project.host_id == project.mesh_network_host_id == "h1"
    assert project.state == "deploying"
    assert project.vni_map == {"net": 123}
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    "measurement",
    [
        None,
        RuntimeError("offline"),
        {"used_pct": 89, "free_bytes": 1024**3},
        {"used_pct": 90, "free_bytes": 1024**3},
    ],
)
def test_disk_admission_rejects_confirmed_full_disk_only(
    context, monkeypatch, measurement
):
    project, _, db, _ = context
    check = Mock()
    if isinstance(measurement, Exception):
        check.side_effect = measurement
    else:
        check.return_value = measurement
    monkeypatch.setattr("app.services.troshkad_client.check_disk_usage", check)
    if isinstance(measurement, dict) and measurement["used_pct"] >= 90:
        with pytest.raises(HTTPException) as exc:
            api._check_single_host_disk(project, {"host_id": "host"}, db)
        assert exc.value.status_code == 507
        assert project.state == "draft"
        db.commit.assert_called_once()
    else:
        api._check_single_host_disk(project, {"host_id": "host"}, db)
        assert project.state == "active"
        db.commit.assert_not_called()
