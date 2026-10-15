"""Download metrics enrich only the selected disk's pre-conversion status."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def wiring(monkeypatch):
    from app.services import deploy_service as deploy
    from app.services import disk_transfer_progress as transfer
    from app.services.providers import kubevirt

    core = MagicMock()
    provider = SimpleNamespace(id="provider-id")
    golden = {
        "metadata": {
            "name": "golden-disk",
            "namespace": "troshka-cache",
            "uid": "golden-uid",
        },
        "spec": {"source": {"s3": {"url": "http://s4/bucket/disk.qcow2"}}},
        "status": {"phase": "ImportInProgress", "progress": "N/A"},
    }
    clone = {
        "metadata": {
            "name": "vm-vmid-disk-storage",
            "namespace": "project-ns",
            "uid": "clone-uid",
        },
        "spec": {
            "source": {"pvc": {"namespace": "troshka-cache", "name": "golden-disk"}}
        },
        "status": {"phase": "CloneScheduled"},
    }
    dvs = [golden, clone]
    topology = {
        "nodes": [{"id": "storage", "type": "storageNode", "data": {"label": "disk"}}],
        "edges": [{"source": "storage", "target": "vmid"}],
    }
    probe = MagicMock(return_value="downloading 25.0% — 25.0 / 100.0 MB · 2.0 MB/s")
    monkeypatch.setattr(transfer, "get_s3_download_progress", probe)
    monkeypatch.setattr(
        kubevirt, "_get_k8s_clients", lambda _: (MagicMock(), core, MagicMock())
    )
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *_: "project-ns")
    monkeypatch.setattr(deploy, "_list_project_datavolumes", lambda *_: dvs)
    monkeypatch.setattr(deploy, "_collect_ceph_restore_progress", lambda *_: [])
    return SimpleNamespace(
        deploy=deploy,
        core=core,
        provider=provider,
        golden=golden,
        clone=clone,
        dvs=dvs,
        topology=topology,
        probe=probe,
    )


def test_collect_probes_importing_golden_when_clone_is_waiting(wiring):
    w = wiring
    lines = w.deploy._collect_dv_progress("project-id", w.provider, w.topology)
    assert lines == ["disk: downloading 25.0% — 25.0 / 100.0 MB · 2.0 MB/s"]
    w.probe.assert_called_once_with(w.core, w.golden, "provider-id")


def test_collect_probes_direct_s3_import_when_no_golden_exists(wiring):
    w = wiring
    w.dvs.remove(w.golden)
    w.clone["spec"]["source"] = w.golden["spec"]["source"]
    w.clone["status"] = {
        "phase": "ImportInProgress",
        "progress": "N/A",
        "conditions": [{"type": "Running", "reason": "TransferRunning"}],
    }
    assert (
        "downloading 25.0%"
        in w.deploy._collect_dv_progress("project-id", w.provider, w.topology)[0]
    )
    w.probe.assert_called_once_with(w.core, w.clone, "provider-id")


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("Succeeded", "done"),
        ("CloneInProgress", "cloning"),
        ("Failed", "error — failed"),
    ],
)
def test_collect_does_not_probe_completed_cloning_or_failed_disks(
    wiring, phase, expected
):
    w = wiring
    w.clone["status"]["phase"] = phase
    assert w.deploy._collect_dv_progress("project-id", w.provider, w.topology) == [
        f"disk: {expected}"
    ]
    w.probe.assert_not_called()


def test_collect_keeps_conversion_progress(wiring):
    w = wiring
    w.golden["status"]["progress"] = "58.0%"
    assert w.deploy._collect_dv_progress("project-id", w.provider, w.topology) == [
        "disk: converting image 58.0%"
    ]
    w.probe.assert_not_called()


@pytest.mark.parametrize("unavailable", [None, "", RuntimeError("API unavailable")])
def test_collect_preserves_import_status_if_measurements_unavailable(
    wiring, unavailable
):
    w = wiring
    if isinstance(unavailable, Exception):
        w.probe.side_effect = unavailable
    else:
        w.probe.return_value = unavailable
    assert w.deploy._collect_dv_progress("project-id", w.provider, w.topology) == [
        "disk: importing image"
    ]


def test_missing_metadata_is_a_noop(wiring):
    w = wiring
    w.golden.pop("metadata")
    statuses = {"disk": "importing image"}
    w.deploy._add_disk_download_progress(
        w.core, w.provider, statuses, {"disk": w.golden}, {}
    )
    assert statuses == {"disk": "importing image"}
    w.probe.assert_not_called()
