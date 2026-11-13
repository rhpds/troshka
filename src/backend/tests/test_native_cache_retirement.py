"""Transient cache PVC retirement requires object and backend absence together."""

import pytest

from app.schemas.kubevirt_storage import (
    BackendImageInventory,
    CacheBinding,
    CollectionLimits,
    ReadResult,
)
from app.services.kubevirt_storage_collector import collect_report
from app.services.kubevirt_storage_inventory import discover_inventory, inventory_digest
from app.services.kubevirt_storage_reports import report_inventory
from tests.test_kubevirt_storage_collector import Rbd
from tests.test_kubevirt_storage_graph import NOW, key
from tests.test_kubevirt_storage_inventory import Kube


def setup_cache():
    binding = CacheBinding(
        cluster_key="cluster",
        namespace="cache",
        namespace_uid="ns-1",
        pvc_uid="scratch",
        pv_uid="scratch-pv",
        csi_driver="driver",
        volume_handle="scratch-handle",
        image_key=key("scratch"),
    )
    kube = Kube()
    kube.cache_roots = [
        {k: getattr(binding, k) for k in ("cluster_key", "namespace", "namespace_uid")}
    ]
    kube.retained_cache_bindings = [binding]
    kube.pvcs = lambda ns: []
    kube.pvs = lambda: []
    kube.scan_started_at = NOW
    return kube, binding


def test_retained_source_still_measured_when_scratch_is_absent():
    kube, scratch = setup_cache()
    source = scratch.model_copy(
        update={
            "pvc_uid": "source",
            "pv_uid": "source-pv",
            "volume_handle": "source-handle",
            "image_key": key("source"),
        }
    )
    kube.retained_cache_bindings.append(source)
    inv = discover_inventory(kube, [], [])
    reader = Rbd()
    reader.recheck_inventory = lambda: inv
    reader.image_inventory = lambda image, **kw: ReadResult(
        status="complete",
        collected_at=NOW,
        value=BackendImageInventory(
            ceph_fsid=image.ceph_fsid,
            pool_id=image.pool_id,
            rbd_namespace=image.rbd_namespace,
            active_images={"golden": "source"},
            trash_ids=[],
            collected_at=NOW,
        ),
    )
    reader.image_usage = lambda image: ReadResult(
        status="complete",
        collected_at=NOW,
        value={
            "images": [
                {"id": image.image_id, "used_size": 123, "provisioned_size": 1024}
            ]
        },
    )
    report = collect_report(inv, reader, limits=CollectionLimits(), clock=lambda: NOW)
    assert report.platform_pools[0].allocated_bytes == 123
    assert next(i for i in report.images if i.key == source.image_key).absence is None
    assert (
        next(i for i in report.images if i.key == scratch.image_key).absence is not None
    )


def test_deleted_scratch_remains_bound_but_can_prove_zero():
    kube, binding = setup_cache()
    inventory = discover_inventory(kube, [], [])
    reader = Rbd()
    reader.recheck_inventory = lambda: discover_inventory(kube, [], [])
    reader.image_info = lambda image: ReadResult(status="unavailable", collected_at=NOW)
    reader.image_inventory = lambda image, **kw: ReadResult(
        status="complete",
        collected_at=NOW,
        value=BackendImageInventory(
            ceph_fsid=image.ceph_fsid,
            pool_id=image.pool_id,
            rbd_namespace=image.rbd_namespace,
            active_images={},
            trash_ids=[],
            collected_at=NOW,
        ),
    )
    report = collect_report(
        inventory, reader, limits=CollectionLimits(), clock=lambda: NOW
    )
    assert report.cache_bindings == [binding]
    assert report.platform_pools[0].allocated_bytes == 0
    assert report.images[0].absence is not None
    assert inventory_digest(
        report_inventory(type(report).model_validate_json(report.model_dump_json()))
    ) == inventory_digest(inventory)
    from app.services.kubevirt_storage_graph import attribute_graph

    unproved = inventory.model_copy(update={"absent_cache_bindings": []})
    assert (
        attribute_graph(unproved, report.images).platform_pools[0].allocated_bytes
        is None
    )


@pytest.mark.parametrize(
    "condition",
    [
        "pv_uid",
        "claim_uid",
        "handle",
        "snapshot",
        "namespace",
        "pvc_unavailable",
        "live",
    ],
)
def test_object_absence_requires_complete_exact_discovery(condition):
    kube, binding = setup_cache()
    pv = {"metadata": {"name": "other", "uid": "other"}, "spec": {}}
    if condition == "pv_uid":
        pv["metadata"]["uid"] = binding.pv_uid
    elif condition == "claim_uid":
        pv["spec"]["claimRef"] = {"uid": binding.pvc_uid}
    elif condition == "handle":
        pv["spec"]["csi"] = {"volumeHandle": binding.volume_handle}
    elif condition == "snapshot":
        kube.snapshot_contents = lambda: [
            {
                "metadata": {"name": "content"},
                "spec": {"volumeSnapshotRef": {"namespace": binding.namespace}},
            }
        ]
    elif condition == "namespace":
        kube.namespace = lambda ns: {"metadata": {"uid": "replacement"}}
    elif condition == "pvc_unavailable":

        def unavailable(ns):
            raise TimeoutError()

        kube.pvcs = unavailable
    else:
        pv = {
            "metadata": {"name": "pv", "uid": binding.pv_uid},
            "spec": {
                "claimRef": {"uid": binding.pvc_uid, "namespace": binding.namespace},
                "csi": {
                    "driver": binding.csi_driver,
                    "volumeHandle": binding.volume_handle,
                },
            },
        }
        kube.pvcs = lambda ns: [
            {
                "metadata": {"namespace": ns, "uid": binding.pvc_uid},
                "spec": {"volumeName": "pv"},
                "status": {"phase": "Bound"},
            }
        ]
        kube.resolve_csi = lambda *args, **kw: ReadResult(
            status="complete", value=binding.image_key, collected_at=NOW
        )
    kube.pvs = lambda: [pv]
    assert not discover_inventory(kube, [], []).absent_cache_bindings


@pytest.mark.parametrize(
    "condition", ["unavailable", "trash", "active", "unknown", "changed"]
)
def test_backend_absence_never_inferred_from_failed_image_read(condition):
    kube, binding = setup_cache()
    inv = discover_inventory(kube, [], [])
    if condition == "unknown":
        inv = inv.model_copy(update={"absent_cache_bindings": []})
    reader = Rbd()
    reader.recheck_inventory = lambda: inv
    reader.image_info = lambda image: ReadResult(status="unavailable", collected_at=NOW)
    calls = []

    def backend(image, **kw):
        calls.append(image)
        return ReadResult(
            status="unavailable" if condition == "unavailable" else "complete",
            collected_at=NOW,
            value=BackendImageInventory(
                ceph_fsid=image.ceph_fsid,
                pool_id=image.pool_id,
                rbd_namespace=image.rbd_namespace,
                active_images={"scratch": "scratch"}
                if condition == "active" or (condition == "changed" and len(calls) > 1)
                else {},
                trash_ids=["scratch"] if condition == "trash" else [],
                collected_at=NOW,
            ),
        )

    reader.image_inventory = backend
    report = collect_report(inv, reader, limits=CollectionLimits(), clock=lambda: NOW)
    assert report.platform_pools[0].allocated_bytes is None


@pytest.mark.parametrize("condition", ["retained", "new", "changed", "omitted"])
def test_import_revalidates_exact_durable_cache_identity(
    isolated_quota_database, condition
):
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from tests.test_kubevirt_storage_reports import ingest, make_native_case, revised

    case = make_native_case(isolated_quota_database)
    _, binding = setup_cache()
    binding = binding.model_copy(
        update={"namespace": "trusted-cache", "namespace_uid": "cache-ns"}
    )
    if condition != "new":
        ingest(case, revised(case.report, cache_bindings=[binding]))
    candidate = (
        binding.model_copy(update={"pv_uid": "changed"})
        if condition == "changed"
        else binding
    )
    report = revised(
        case.report,
        cache_bindings=[] if condition == "omitted" else [candidate],
        absent_cache_bindings=[candidate],
    )
    identity = ingest(case, report)
    with case.sessions() as db:
        snapshot = db.get(NativeStorageSnapshot, identity)
        assert snapshot.accepted == (condition == "retained")
        if condition != "retained":
            assert (
                "cache_absence_requires_retained_identity_and_complete_discovery"
                in snapshot.reason_codes
            )
