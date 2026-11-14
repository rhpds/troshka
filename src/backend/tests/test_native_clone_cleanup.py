"""Verified COW retirement preserves shared sources and rejects partial absence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import BackendImageState, RetiringRoot, SnapshotKey
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_reports import report_inventory
from tests.test_native_clone_lineage import chain


def absent(image, now):
    return image.model_copy(
        update={
            "parent": None,
            "snapshots": [],
            "children": [],
            "allocated_bytes": 0,
            "provisioned_bytes": 0,
            "collected_at": now,
            "measurement_semantics_version": "kubevirt-rbd-absence-v1",
            "absence": BackendImageState(
                **image.key.model_dump(), active=False, trash=False, collected_at=now
            ),
        }
    )


@pytest.mark.parametrize("keep_snapshot", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "head_live",
        "middle_live",
        "objects_live",
        "wrong_root",
        "missing_owned",
        "source_owned",
        "stale",
        "foreign_child",
        "foreign_snapshot",
        "source_absent",
    ],
)
def test_retiring_clone_requires_both_private_absences(keep_snapshot, fault):
    report, original, proofs = chain()
    retired, survivor = proofs
    now = datetime.now(UTC)
    private = {retired.binding.image_key, retired.intermediate}
    retiring = RetiringRoot(
        intent_id=uuid4(),
        operation_id=retired.operation_id,
        root=retired.binding.root,
        inventory_digest="a" * 64,
        owned_images=list(private),
        objects_absent=True,
        observed_at=now,
    )
    images = []
    for image in original:
        value = (
            absent(image, now)
            if image.key in private
            else image.model_copy(update={"collected_at": now})
        )
        if image.key == retired.source.image_key:
            value = value.model_copy(
                update={
                    "children": [survivor.intermediate],
                    "snapshots": [survivor.intermediate_parent]
                    + ([retired.intermediate_parent] if keep_snapshot else []),
                }
            )
        if (fault == "head_live" and image.key == retired.binding.image_key) or (
            fault == "middle_live" and image.key == retired.intermediate
        ):
            value = image
        if fault == "stale" and image.key in private:
            value = absent(image, now - timedelta(seconds=1))
        if image.key == retired.source.image_key:
            if fault == "source_absent":
                value = absent(image, now)
            elif fault == "foreign_child":
                value = value.model_copy(
                    update={"children": value.children + [retired.intermediate]}
                )
            elif fault == "foreign_snapshot":
                value = value.model_copy(
                    update={
                        "snapshots": value.snapshots
                        + [SnapshotKey(image_key=image.key, snapshot_id="unrecognized")]
                    }
                )
        images.append(value)
    if fault == "objects_live":
        retiring = retiring.model_copy(update={"objects_absent": False})
    elif fault == "wrong_root":
        retiring = retiring.model_copy(update={"root": survivor.binding.root})
    elif fault == "missing_owned":
        retiring = retiring.model_copy(
            update={"owned_images": [retired.binding.image_key]}
        )
    elif fault == "source_owned":
        retiring = retiring.model_copy(
            update={"owned_images": list(private) + [retired.source.image_key]}
        )
    inventory = report_inventory(report).model_copy(
        update={"retiring_roots": [retiring]}
    )
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    if fault is None and not keep_snapshot:
        assert [p.allocated_bytes for p in result.projects] == [0, 50]
        assert result.platform_pools[0].allocated_bytes == 100
    else:
        assert all(p.coverage != "complete" for p in result.projects)


def test_cleanup_owned_images_includes_verified_private_middle():
    from app.services.native_clone_graph import cleanup_owned_images

    report, images, proofs = chain()
    proof = proofs[0]
    inventory = report_inventory(report)
    # CDI can initially expose its private intermediate in the cache namespace.
    inventory = inventory.model_copy(
        update={
            "cache_bindings": inventory.cache_bindings
            + [
                proof.source.model_copy(
                    update={"image_key": proof.intermediate, "pvc_uid": "temporary"}
                )
            ]
        }
    )
    assert cleanup_owned_images(
        inventory, images, proofs, proof.binding.root.project_id
    ) == {proof.binding.image_key, proof.intermediate}
