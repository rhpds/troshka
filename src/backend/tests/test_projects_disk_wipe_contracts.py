"""Disk wipe operations must preserve failed stop/wipe/restart outcomes."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api import projects as api


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(api, "_get_storage_pool_for_host", lambda *_: None)
    monkeypatch.setattr(api, "troshkad_get_vm_state", lambda *_: {"state": "running"})
    start = Mock(side_effect=lambda host, path, params: path)
    wait = Mock(return_value={"status": "completed"})
    monkeypatch.setattr(api, "start_job", start)
    monkeypatch.setattr(api, "wait_for_job", wait)
    return start, wait


@pytest.mark.parametrize(
    "failed_job,status", [("/vms/stop", 409), ("/disks/wipe", 500), ("/vms/start", 500)]
)
def test_failed_agent_job_aborts_wipe(agent, failed_job, status):
    start, wait = agent
    wait.side_effect = lambda host, job, **kwargs: (
        {"status": "failed", "result": {"error": "disk busy"}}
        if job == failed_job
        else {"status": "completed"}
    )
    args = (
        SimpleNamespace(id="project"),
        Mock(),
        "vm",
        {"node_id": "disk", "format": "qcow2", "size_gb": 10},
        False,
        Mock(),
    )
    with pytest.raises(HTTPException) as exc:
        api._wipe_disk_troshkad(*args)
    assert exc.value.status_code == status
    paths = [call.args[1] for call in start.call_args_list]
    if failed_job != "/vms/start":
        assert "/vms/start" not in paths
    if failed_job == "/vms/stop":
        assert "/disks/wipe" not in paths


@pytest.mark.parametrize(
    "running,restart", [(True, False), (False, True), (False, False)]
)
def test_successful_wipe_preserves_disk_size_and_restart_intent(
    agent, monkeypatch, running, restart
):
    start, _ = agent
    monkeypatch.setattr(
        api,
        "troshkad_get_vm_state",
        lambda *_: {"state": "running" if running else "shut off"},
    )
    result = api._wipe_disk_troshkad(
        SimpleNamespace(id="project"),
        Mock(),
        "vm",
        {"node_id": "disk", "format": "qcow2", "size_gb": 10},
        restart,
        Mock(),
    )
    assert result == {"status": "wiped"}
    paths = [call.args[1] for call in start.call_args_list]
    assert ("/vms/stop" in paths) is running
    assert ("/vms/start" in paths) is (running or restart)
    params = next(
        call.args[2] for call in start.call_args_list if call.args[1] == "/disks/wipe"
    )
    assert params["size_gb"] == 10
    assert params["format"] == "qcow2"


@pytest.mark.parametrize(
    "failed_job,status", [("/vms/stop", 409), ("/disks/wipe", 500)]
)
def test_agent_transport_failure_is_reported(agent, failed_job, status):
    start, _ = agent

    def fail(host, path, params):
        if path == failed_job:
            raise api.TroshkadError("offline")
        return path

    start.side_effect = fail
    args = (
        SimpleNamespace(id="project"),
        Mock(),
        "vm",
        {"node_id": "disk", "format": "raw"},
        False,
        Mock(),
    )
    with pytest.raises(HTTPException) as exc:
        api._wipe_disk_troshkad(*args)
    assert exc.value.status_code == status


@pytest.fixture
def kubevirt_wipe(monkeypatch):
    custom, core = Mock(), Mock()
    monkeypatch.setattr("kubernetes.client.ApiClient", Mock())
    monkeypatch.setattr("kubernetes.client.CustomObjectsApi", lambda _: custom)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda _: core)
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[Mock()])
    core.read_namespaced_pod.side_effect = RuntimeError("pod gone")
    monkeypatch.setattr(
        "app.services.providers.kubevirt.wait_virt_launcher_compute_ready",
        lambda *args, **kwargs: "pod",
    )
    wipe = Mock()
    monkeypatch.setattr(
        "app.services.providers.kubevirt.wipe_boot_disk_in_virt_launcher", wipe
    )
    statuses = {}
    notify = Mock()
    monkeypatch.setattr(api, "_wipe_status", statuses)
    monkeypatch.setattr(api, "notify_project", notify)
    return custom, core, wipe, statuses, notify


def test_kubevirt_wipe_failed_restart_is_not_reported_running(kubevirt_wipe):
    custom, _, _, statuses, notify = kubevirt_wipe

    def patch(**kwargs):
        if any(op.get("value") == "Always" for op in kwargs["body"]):
            raise RuntimeError("restart forbidden")

    custom.patch_namespaced_custom_object.side_effect = patch
    api._do_kubevirt_wipe(
        {"api_url": "https://cluster.test", "token": "fake"},
        "ns",
        "vm",
        "disk",
        "/disk",
        "project",
        "vm",
        "key",
        True,
    )
    assert statuses["key"]["status"] == "error"
    assert not any(
        call.args[1].get("states", {}).get("vm") == "running"
        for call in notify.call_args_list
    )


@pytest.mark.parametrize("restart", [True, False])
def test_kubevirt_wipe_stops_and_optionally_restarts(kubevirt_wipe, restart):
    custom, _, wipe, statuses, notify = kubevirt_wipe
    api._do_kubevirt_wipe(
        {"api_url": "https://cluster.test", "token": "fake"},
        "ns",
        "vm",
        "disk",
        "/disk",
        "project",
        "vm",
        "key",
        restart,
    )
    assert statuses["key"] == {"status": "done"}
    wipe.assert_called_once()
    assert custom.patch_namespaced_custom_object.call_count == (2 if restart else 1)
    assert notify.call_args.args[1]["states"] == {
        "vm": "running" if restart else "stopped"
    }


def test_kubevirt_wipe_failure_does_not_report_completion(kubevirt_wipe):
    _, _, wipe, statuses, notify = kubevirt_wipe
    wipe.side_effect = RuntimeError("dd failed")
    api._do_kubevirt_wipe(
        {"api_url": "https://cluster.test", "token": "fake"},
        "ns",
        "vm",
        "disk",
        "/disk",
        "project",
        "vm",
        "key",
        False,
    )
    assert statuses["key"] == {"status": "error", "detail": "dd failed"}
    notify.assert_not_called()
