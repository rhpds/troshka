"""Capture/reconfigure must prove VM shutdown before touching live disks."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from handlers import project, vm
from helpers.native_origin_checkpoint import CheckpointHold
from kubernetes.client.exceptions import ApiException


@pytest.fixture(autouse=True)
def no_real_shutdown_wait(monkeypatch):
    monkeypatch.setattr(project.asyncio, "sleep", AsyncMock())


class KubeVirtApi:
    """In-memory API boundary with KubeVirt's mutually exclusive power fields."""

    def __init__(self, spec, failure=None):
        self.spec = deepcopy(spec)
        self.failure = failure
        self.vmi_present = True
        self.observations = 0

    def list_namespaced_custom_object(self, *, plural, **kwargs):
        if plural == "troshkavms":
            if self.failure == "vm-list":
                raise ApiException(status=403)
            return {"items": [{"metadata": {"name": "one"},
                               "status": {"kubevirtVmName": "vm-one"}}]}
        assert plural == "virtualmachineinstances"
        self.observations += 1
        if self.failure == "vmi-list":
            raise ApiException(status=403)
        if self.failure == "malformed-vmi-list":
            return {}
        return {"items": [{"metadata": {"name": "vm-one"}}]
                if self.vmi_present else []}

    def patch_namespaced_custom_object(self, *, plural, body, **kwargs):
        assert plural == "virtualmachines"
        if self.failure == "stop":
            raise ApiException(status=403)
        if self.failure == "vm-absent":
            raise ApiException(status=404)
        if self.failure == "hold":
            raise CheckpointHold("test_checkpoint_hold")
        updated = dict(self.spec)
        for key, value in body["spec"].items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        if "running" in updated and "runStrategy" in updated:
            raise ApiException(status=422, reason="Running and RunStrategy are mutually exclusive")
        self.spec = updated
        if self.failure != "stuck" and (
            updated.get("runStrategy") == "Halted" or updated.get("running") is False
        ):
            self.vmi_present = False

    def get_namespaced_custom_object(self, *, plural, **kwargs):
        assert plural == "virtualmachineinstances"
        if self.failure == "vmi-list":
            raise ApiException(status=403)
        self.observations += 1
        if not self.vmi_present:
            raise ApiException(status=404)
        return {"metadata": {"name": "vm-one"}}


@pytest.mark.parametrize("initial", [{"runStrategy": "Always"}, {"running": True}])
def test_capture_stop_accepts_modern_and_legacy_vms(initial):
    api = KubeVirtApi(initial)
    asyncio.run(project._stop_all_vms(api, "ns"))
    assert not api.vmi_present
    assert api.observations > 0
    assert api.spec == {"runStrategy": "Halted"}


@pytest.mark.parametrize("failure", ["vm-list", "stop", "vmi-list", "malformed-vmi-list", "stuck", "hold"])
def test_capture_cannot_export_without_verified_shutdown(monkeypatch, failure):
    api = KubeVirtApi({"running": True}, failure)
    exports = []
    monkeypatch.setattr(project.client, "CustomObjectsApi", lambda: api)
    monkeypatch.setattr(project, "guard_client", lambda value: value)
    monkeypatch.setattr(project.snapshot_workflow, "resume", lambda *args: False)
    monkeypatch.setattr(project, "_patch_cr_status", lambda *args: None)
    monkeypatch.setattr(project.asyncio, "sleep", AsyncMock())
    def export_boundary(*args):
        exports.append("export")
        raise AssertionError("capture reached export before verified shutdown")

    monkeypatch.setattr(project, "_setup_export_sa", export_boundary)
    error = CheckpointHold if failure == "hold" else (
        TimeoutError if failure == "stuck" else (RuntimeError if failure == "malformed-vmi-list" else ApiException)
    )
    with pytest.raises(error):
        asyncio.run(project._handle_capture({"restartAfter": True}, "ns", "project", {}))
    assert exports == []
    assert api.observations <= 40


@pytest.mark.parametrize("initial", [{"runStrategy": "Always"}, {"running": True}])
def test_reconfigure_stop_waits_for_absence(initial):
    api = KubeVirtApi(initial)
    asyncio.run(vm._stop_kubevirt_vm(api, "ns", "vm-one"))
    assert not api.vmi_present
    assert api.observations > 0
    assert api.spec == {"runStrategy": "Halted"}


@pytest.mark.parametrize("vmi_present", [False, True])
def test_reconfigure_retry_after_vm_delete_still_checks_vmi(vmi_present):
    api = KubeVirtApi({"runStrategy": "Halted"}, "vm-absent")
    api.vmi_present = vmi_present
    if vmi_present:
        with pytest.raises(TimeoutError):
            asyncio.run(vm._stop_kubevirt_vm(api, "ns", "vm-one"))
    else:
        asyncio.run(vm._stop_kubevirt_vm(api, "ns", "vm-one"))
    assert api.observations > 0


@pytest.mark.parametrize("failure,error", [("stop", ApiException), ("vmi-list", ApiException),
                                          ("stuck", TimeoutError), ("hold", CheckpointHold)])
def test_reconfigure_stop_does_not_claim_unverified_shutdown(monkeypatch, failure, error):
    api = KubeVirtApi({"running": True}, failure)
    monkeypatch.setattr(vm.asyncio, "sleep", AsyncMock())
    with pytest.raises(error):
        asyncio.run(vm._stop_kubevirt_vm(api, "ns", "vm-one"))
    assert api.observations <= 60


@pytest.mark.parametrize("initial", [{"runStrategy": "Halted"}, {"running": False}])
def test_capture_restart_accepts_modern_and_legacy_vms(initial):
    api = KubeVirtApi(initial)
    result = project._start_kubevirt_vms(api, [{"status": {"kubevirtVmName": "vm-one"}}], "ns")
    assert result == 1
    assert api.spec == {"runStrategy": "Always"}
