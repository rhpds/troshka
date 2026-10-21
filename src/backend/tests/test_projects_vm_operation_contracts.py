"""VM API routing, power intent and file-transfer credential contracts."""

import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile

from app.api import projects as api
from app.models.host import Host
from app.models.project import Project


@pytest.fixture
def context(monkeypatch):
    project = Project(
        id="project",
        owner_id="owner",
        host_id="host",
        state="active",
        topology={
            "nodes": [
                {
                    "id": "vm",
                    "type": "vmNode",
                    "data": {
                        "nics": [{"ip": "10.0.0.10"}],
                        "ciCloudUserPassword": "topology-password",
                    },
                }
            ]
        },
    )
    host = SimpleNamespace(
        id="host",
        host_type="libvirt",
        ip_address="192.0.2.1",
        private_key="fake",
        provider_id="provider",
    )
    provider = SimpleNamespace(id="provider")
    db = Mock()
    db.query.side_effect = lambda model: Mock(
        **{
            "filter_by.return_value.first.return_value": project
            if model is Project
            else host
            if model is Host
            else provider
        }
    )
    notify, meter = Mock(), Mock()
    monkeypatch.setattr(api, "notify_project", notify)
    monkeypatch.setattr("app.services.metering_service.touch_metering", meter)
    monkeypatch.setattr("app.services.ws_pubsub.get_cached_vm_states", lambda _: None)
    return project, host, db, SimpleNamespace(id="owner", role="user"), notify, meter


@pytest.mark.parametrize("state", ["active", "stopped"])
def test_start_queues_infrastructure_only_for_stopped_project(
    context, monkeypatch, state
):
    project, _, db, user, notify, _ = context
    project.state = state
    enqueue = Mock()
    monkeypatch.setattr("app.core.redis.enqueue_job", enqueue)
    result = api.start_vm("project", "vm", user, db)
    assert result["success"] is True
    assert result.get("starting_project", False) is (state == "stopped")
    assert enqueue.call_args.args[0].__name__ == (
        "job_start_infra_then_vm" if state == "stopped" else "job_cache_and_start_vm"
    )
    assert enqueue.call_args.args[1:] == ("project", "host", "vm")
    assert notify.call_args.args[1]["states"] == {"vm": "starting"}


@pytest.mark.parametrize("fail", [False, True])
def test_start_paused_vm_resumes_without_cold_start(context, monkeypatch, fail):
    _, host, db, user, notify, meter = context
    monkeypatch.setattr(
        "app.services.ws_pubsub.get_cached_vm_states",
        lambda _: {"states": {"vm": "paused"}},
    )
    resume = Mock(side_effect=RuntimeError("offline") if fail else None)
    enqueue = Mock()
    monkeypatch.setattr(api, "unpause_vm_on_host", resume)
    monkeypatch.setattr("app.core.redis.enqueue_job", enqueue)
    assert api.start_vm("project", "vm", user, db) == {
        "action": "start",
        "success": not fail,
    }
    resume.assert_called_once_with(host, "project", "vm")
    enqueue.assert_not_called()
    assert notify.called is not fail
    assert meter.called is not fail


@pytest.mark.parametrize(
    "operation,path,state",
    [
        ("stop", "/vms/stop", "stopped"),
        ("forcestop", "/vms/force-off", "stopped"),
        ("restart", "/vms/reboot", "running"),
    ],
)
@pytest.mark.parametrize("fail", [False, True])
def test_agent_power_operations_report_transport_failure(
    context, monkeypatch, operation, path, state, fail
):
    _, _, db, user, notify, _ = context
    start = Mock(
        return_value="job", side_effect=api.TroshkadError("offline") if fail else None
    )
    monkeypatch.setattr(api, "start_job", start)
    monkeypatch.setattr(api, "wait_for_job", Mock(return_value={"status": "completed"}))
    result = getattr(api, f"{operation}_vm")("project", "vm", user, db)
    assert result == {"action": operation, "success": not fail}
    assert start.call_args.args[1] == path
    if not fail:
        assert notify.call_args.args[1]["states"] == {"vm": state}
    else:
        notify.assert_not_called()


@pytest.mark.parametrize(
    "operation,state", [("pause", "paused"), ("unpause", "running")]
)
@pytest.mark.parametrize("fail", [False, True])
def test_pause_routes_only_notify_after_success(
    context, monkeypatch, operation, state, fail
):
    _, host, db, user, notify, meter = context
    action = Mock(side_effect=RuntimeError("offline") if fail else None)
    monkeypatch.setattr(api, f"{operation}_vm_on_host", action)
    assert getattr(api, f"{operation}_vm")("project", "vm", user, db) == {
        "action": operation,
        "success": not fail,
    }
    action.assert_called_once_with(host, "project", "vm")
    assert meter.called is not fail
    if not fail:
        assert notify.call_args.args[1]["states"] == {"vm": state}


@pytest.mark.parametrize("operation", ["stop", "restart", "forcestop", "start"])
@pytest.mark.parametrize("fail", [False, True])
def test_kubevirt_power_routes_report_api_failures(
    context, monkeypatch, operation, fail
):
    project, host, db, user, notify, _ = context
    host.host_type = "kubevirt-cluster"
    custom, core = Mock(), Mock()
    custom.get_namespaced_custom_object.return_value = {
        "status": {"phase": "Running"},
        "spec": {},
    }
    if fail:
        custom.patch_namespaced_custom_object.side_effect = RuntimeError("forbidden")
    monkeypatch.setattr(
        "app.services.providers.kubevirt._get_k8s_clients",
        lambda _: (custom, core, Mock()),
    )
    monkeypatch.setattr("app.services.providers.kubevirt._project_ns", lambda *_: "ns")
    force = Mock(side_effect=RuntimeError("forbidden") if fail else None)
    monkeypatch.setattr("app.services.providers.kubevirt.force_stop_kubevirt_vm", force)
    if operation == "start":
        project.state = "stopped"
    if operation == "start" and fail:
        with pytest.raises(HTTPException) as exc:
            api.start_vm("project", "vm", user, db)
        assert exc.value.status_code == 500
    else:
        assert getattr(api, f"{operation}_vm")("project", "vm", user, db) == {
            "action": operation,
            "success": not fail,
        }
        if operation == "start":
            assert project.state == "active"
        elif not fail:
            assert notify.called


@pytest.mark.parametrize("kubevirt", [False, True])
@pytest.mark.parametrize("explicit_password", ["", "override-password"])
def test_upload_routes_bytes_and_resolved_credentials(
    context, monkeypatch, kubevirt, explicit_password
):
    _, host, db, user, _, _ = context
    host.host_type = "kubevirt-cluster" if kubevirt else "libvirt"
    upload = Mock(return_value={"success": True})
    monkeypatch.setattr(api, "troshkad_upload_to_vm", upload)
    monkeypatch.setattr("app.services.providers.kubevirt.kubevirt_upload_to_vm", upload)
    file = UploadFile(filename="input.txt", file=BytesIO(b"test content"))
    result = asyncio.run(
        api.vm_upload_file(
            "project",
            "vm",
            file,
            "/tmp/input.txt",
            user,
            db,
            password=explicit_password,
        )
    )
    assert result == {"success": True}
    args = upload.call_args.args
    assert b"test content" in args
    assert "10.0.0.10" in args
    assert (explicit_password or "topology-password") in args
    assert "/tmp/input.txt" in args


@pytest.mark.parametrize("kubevirt", [False, True])
def test_download_returns_attachment_and_bytes(context, monkeypatch, kubevirt):
    _, host, db, user, _, _ = context
    host.host_type = "kubevirt-cluster" if kubevirt else "libvirt"
    download = Mock(return_value=b"downloaded")
    monkeypatch.setattr(api, "troshkad_download_from_vm", download)
    monkeypatch.setattr(
        "app.services.providers.kubevirt.kubevirt_download_from_vm", download
    )
    response = api.vm_download_file("project", "vm", "/home/user/result.txt", user, db)
    assert response.body == b"downloaded"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="result.txt"'
    )
    assert "topology-password" in download.call_args.args


def _transfer_file(upload, user, db):
    if upload:
        return asyncio.run(
            api.vm_upload_file(
                "project", "vm", UploadFile(file=BytesIO(b"x")), "/file", user, db
            )
        )
    return api.vm_download_file("project", "vm", "/file", user, db)


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("missing", ["ip", "password", "vm"])
def test_transfer_rejects_missing_connection_metadata(context, upload, missing):
    project, _, db, user, _, _ = context
    data = project.topology["nodes"][0]["data"]
    if missing == "ip":
        data["nics"] = []
    elif missing == "password":
        data["ciCloudUserPassword"] = ""
    else:
        project.topology["nodes"] = []
    with pytest.raises(HTTPException) as exc:
        _transfer_file(upload, user, db)
    assert exc.value.status_code == (404 if missing == "vm" else 400)


@pytest.mark.parametrize("upload", [False, True])
def test_transfer_agent_failure_maps_to_unavailable(context, monkeypatch, upload):
    _, _, db, user, _, _ = context
    monkeypatch.setattr(
        api,
        "troshkad_upload_to_vm" if upload else "troshkad_download_from_vm",
        Mock(side_effect=api.TroshkadError("offline")),
    )
    with pytest.raises(HTTPException) as exc:
        _transfer_file(upload, user, db)
    assert exc.value.status_code == 503
