"""Unit tests for container mount collection helpers."""

from handlers.container import _add_container_mount, _collect_mount_specs


def test_add_container_mount_skips_missing_disk():
    volumes, mounts, seen_vols, seen_mounts = [], [], set(), set()
    _add_container_mount(
        {"diskNodeId": "deadbeef", "mountPath": "/data"},
        {},
        volumes,
        mounts,
        seen_vols,
        seen_mounts,
    )
    assert volumes == []
    assert mounts == []


def test_add_container_mount_dedupes_volume_and_mount():
    disk_pvcs = {"abcd1234xxxx": "ns-disk-abcd1234"}
    volumes, mounts, seen_vols, seen_mounts = [], [], set(), set()
    mount = {"diskNodeId": "abcd1234xxxx", "mountPath": "/data"}
    _add_container_mount(mount, disk_pvcs, volumes, mounts, seen_vols, seen_mounts)
    _add_container_mount(mount, disk_pvcs, volumes, mounts, seen_vols, seen_mounts)
    assert len(volumes) == 1
    assert len(mounts) == 1
    assert volumes[0]["persistentVolumeClaim"]["claimName"] == "ns-disk-abcd1234"
    assert mounts[0] == {"name": "disk-abcd1234", "mountPath": "/data"}


def test_collect_mount_specs_from_init_and_pod_containers():
    disk_id = "abcd1234xxxx"
    ctr = {
        "mounts": [{"diskNodeId": disk_id, "mountPath": "/main"}],
        "initContainers": [
            {"mounts": [{"diskNodeId": disk_id, "mountPath": "/init"}]}
        ],
        "podContainers": [
            {"mounts": [{"diskNodeId": disk_id, "mountPath": "/sidecar"}]}
        ],
    }
    volumes, mounts = _collect_mount_specs(ctr, {disk_id: "pvc-1"})
    assert len(volumes) == 1
    paths = {m["mountPath"] for m in mounts}
    assert paths == {"/main", "/init", "/sidecar"}
