"""Scoped observations retain exact shared edges without scanning sibling disks."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.schemas.kubevirt_storage import CollectionLimits, RetiringRoot
from app.services.kubevirt_storage_collector import collect_report
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_reports import report_inventory
from app.services.native_clone_graph import cleanup_owned_images
from tests.test_native_clone_cleanup import absent
from tests.test_native_clone_lineage import chain


def scoped_chain():
    report, images, proofs = chain()
    own = proofs[0]
    inventory = report_inventory(report).model_copy(
        update={
            "project_scope": own.binding.root.project_id,
            "project_roots": [own.binding.root],
            "volume_bindings": [own.binding],
        }
    )
    keys = {own.binding.image_key, own.intermediate, own.source.image_key}
    return inventory, [i for i in images if i.key in keys], proofs


def test_scoped_shared_source_needs_no_sibling_private_observation():
    inventory, images, proofs = scoped_chain()
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    assert len(result.projects) == 1
    assert result.projects[0].allocated_bytes == 30
    assert cleanup_owned_images(inventory, images, proofs, inventory.project_scope) == {
        proofs[0].binding.image_key,
        proofs[0].intermediate,
    }


def test_sibling_private_read_failure_does_not_invalidate_scope():
    inventory, images, proofs = scoped_chain()
    failed = images[0].model_copy(
        update={
            "key": proofs[1].binding.image_key,
            "measurement_status": "unavailable",
            "allocated_bytes": None,
            "measurement_semantics_version": None,
        }
    )
    result = attribute_graph(inventory, images + [failed], clone_lineages=proofs)
    assert result.projects[0].allocated_bytes == 30


def test_private_identity_shared_by_protected_projects_is_rejected():
    inventory, images, proofs = scoped_chain()
    sibling = proofs[1].model_copy(update={"intermediate": proofs[0].intermediate})
    result = attribute_graph(inventory, images, clone_lineages=[proofs[0], sibling])
    assert result.projects[0].allocated_bytes is None


@pytest.mark.parametrize(
    "fault",
    ["child", "snapshot", "removed_sibling", "private_child", "private_missing"],
)
def test_scoped_graph_rejects_unknown_or_unproven_edges(fault):
    inventory, images, proofs = scoped_chain()
    source = images[-1]
    if fault == "child":
        images[-1] = source.model_copy(
            update={
                "children": source.children
                + [source.key.model_copy(update={"image_id": "foreign"})]
            }
        )
    elif fault == "snapshot":
        images[-1] = source.model_copy(
            update={
                "snapshots": source.snapshots
                + [source.snapshots[0].model_copy(update={"snapshot_id": "999"})]
            }
        )
    elif fault == "removed_sibling":
        # Missing edges cannot be inferred to mean a sibling retired safely.
        images[-1] = source.model_copy(
            update={
                "children": [proofs[0].intermediate],
                "snapshots": [proofs[0].intermediate_parent],
            }
        )
    elif fault == "private_child":
        images[0] = images[0].model_copy(
            update={"children": [proofs[1].binding.image_key]}
        )
    else:
        images = images[1:]
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    assert result.projects[0].allocated_bytes is None
    assert "managed_clone_lineage_unverified" in result.projects[0].blockers


def test_scoped_cleanup_requires_own_absence_and_preserves_sibling_edges():
    inventory, images, proofs = scoped_chain()
    own, sibling = proofs
    now = datetime.now(UTC)
    private = {own.binding.image_key, own.intermediate}
    retiring = RetiringRoot(
        intent_id=uuid4(),
        operation_id=own.operation_id,
        root=own.binding.root,
        inventory_digest="a" * 64,
        owned_images=list(private),
        objects_absent=True,
        observed_at=now,
    )
    inventory = inventory.model_copy(update={"retiring_roots": [retiring]})
    images = [
        absent(i, now)
        if i.key in private
        else i.model_copy(
            update={
                "collected_at": now,
                "children": [sibling.intermediate],
                "snapshots": [sibling.intermediate_parent],
            }
        )
        for i in images
    ]
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    assert result.projects[0].allocated_bytes == 0
    assert (
        cleanup_owned_images(inventory, images, proofs, inventory.project_scope)
        == private
    )


def test_scoped_collection_does_not_measure_shared_source_children(monkeypatch):
    from app.services import kubevirt_storage_collector as collector
    from tests.test_kubevirt_storage_collector import Rbd

    inventory, images, proofs = scoped_chain()
    by_key = {i.key: i for i in images}
    visited = []

    def observe(reader, key, *args):
        visited.append(key)
        return by_key[key]  # A sibling traversal fails the test.

    monkeypatch.setattr(collector, "_collect_image", observe)
    monkeypatch.setattr(collector, "_backend_profile", lambda *a: ([], "test", True))
    monkeypatch.setattr(collector, "_final_checks", lambda *a: None)
    monkeypatch.setattr(
        collector, "_final_graph_check", lambda reader, images, *a: images
    )
    result = collect_report(
        inventory,
        Rbd(),
        limits=CollectionLimits(),
        clock=lambda: datetime.now(UTC),
        clone_lineages=proofs,
    )
    assert set(visited) == set(by_key)
    assert result.project_scope == inventory.project_scope
    assert result.projects[0].allocated_bytes == 30
