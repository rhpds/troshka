from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.kubevirt_storage import (
    CacheBinding,
    ImageKey,
    ImageObservation,
    NativeInventory,
    ProjectRoot,
    VolumeBinding,
)
from app.services.kubevirt_storage_graph import attribute_graph
from tests.kubevirt_storage_helpers import report_fixture

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def key(name):
    return ImageKey(
        ceph_fsid="11111111-1111-4111-8111-111111111111",
        pool_id=1,
        rbd_namespace="",
        image_id=name,
    )


def root(number):
    return ProjectRoot(
        project_id=f"00000000-0000-4000-8000-{number:012d}",
        cluster_key="cluster",
        namespace=f"project-{number}",
        namespace_uid=f"ns-{number}",
        project_cr_uid=f"cr-{number}",
    )


def inventory():
    roots = [root(1), root(2)]
    cache = CacheBinding(
        cluster_key="cluster",
        namespace="trusted-cache",
        namespace_uid="cache-ns",
        pvc_uid="cache-pvc",
        pv_uid="cache-pv",
        csi_driver="openshift-storage.rbd.csi.ceph.com",
        volume_handle="cache-handle",
        image_key=key("cache"),
    )
    return NativeInventory(
        project_roots=roots,
        cache_bindings=[cache],
        volume_bindings=[
            VolumeBinding(
                root=r,
                pvc_uid=f"pvc-{i}",
                pv_uid=f"pv-{i}",
                csi_driver="openshift-storage.rbd.csi.ceph.com",
                volume_handle=f"handle-{i}",
                image_key=key(f"child-{i}"),
                provisioned_bytes=100,
            )
            for i, r in enumerate(roots, 1)
        ],
        collected_at=NOW,
    )


def observation(name, size, parent=None, children=()):
    return ImageObservation(
        key=key(name),
        parent={"image_key": key(parent), "snapshot_id": "1"} if parent else None,
        snapshots=[],
        children=[key(c) for c in children],
        collected_at=NOW,
        allocated_bytes=size,
        provisioned_bytes=100,
        measurement_status="complete",
        command_version="test",
        measurement_semantics_version="kubevirt-rbd-allocated-v1",
    )


def observations():
    return [
        observation("child-1", 30, "parent-1"),
        observation("child-2", 40, "parent-2"),
        observation("parent-1", 0, "cache", ["child-1"]),
        observation("parent-2", 10, "cache", ["child-2"]),
        observation("cache", 100, children=["parent-1", "parent-2"]),
    ]


def test_private_parents_and_shared_cache_are_counted_once():
    result = attribute_graph(inventory(), observations())
    assert [p.allocated_bytes for p in result.projects] == [30, 50]
    assert result.platform_pools[0].allocated_bytes == 100
    assert not result.unattributed_images


def test_saved_zero_and_timeout_recordings_keep_distinct_coverage():
    complete = report_fixture("zero-private-parent.json")
    partial = report_fixture("timeout-private-parent.json")
    assert [p.allocated_bytes for p in complete.projects] == [30, 50]
    assert complete.platform_pools[0].allocated_bytes == 100
    assert partial.projects[0].allocated_bytes is None
    assert partial.projects[0].known_allocated_bytes == 30
    assert partial.projects[0].coverage == "partial"
    assert partial.enforcement_eligible is False


def test_timeout_parent_keeps_known_subset_and_null_total():
    images = observations()
    images[2] = images[2].model_copy(
        update={"allocated_bytes": None, "measurement_status": "timeout"}
    )
    result = attribute_graph(inventory(), images)
    assert result.projects[0].allocated_bytes is None
    assert result.projects[0].known_allocated_bytes == 30
    assert result.projects[0].coverage == "partial"


def test_unknown_child_invalidates_private_ancestor():
    images = observations()
    images[2] = images[2].model_copy(
        update={"children": [key("child-1"), key("foreign")]}
    )
    result = attribute_graph(inventory(), images)
    assert result.projects[0].allocated_bytes is None
    assert key("parent-1") in result.unattributed_images


def test_shared_private_parent_has_nullable_totals_and_single_unattributed_key():
    images = observations()
    images[0] = images[0].model_copy(update={"parent": images[1].parent})
    images[3] = images[3].model_copy(
        update={"children": [key("child-1"), key("child-2")]}
    )
    images.pop(2)
    result = attribute_graph(inventory(), images)
    assert [p.allocated_bytes for p in result.projects] == [None, None]
    assert [p.known_allocated_bytes for p in result.projects] == [30, 40]
    assert result.unattributed_images == [key("parent-2")]


def test_cache_parent_requires_explicit_platform_proof():
    images = observations()
    images[-1] = images[-1].model_copy(update={"parent": images[0].parent})
    result = attribute_graph(inventory(), images)
    assert all(p.allocated_bytes is None for p in result.projects)


def test_cycle_is_partial():
    images = observations()
    images[2] = images[2].model_copy(update={"parent": images[0].parent})
    assert attribute_graph(inventory(), images).projects[0].allocated_bytes is None


@pytest.mark.parametrize("amount", [-1, 2**63, True])
def test_strict_bytes(amount):
    with pytest.raises(ValidationError):
        observation("bad", amount)
