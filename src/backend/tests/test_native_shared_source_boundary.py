"""Unrelated cache consumers do not confer ownership or block private cleanup."""

from datetime import UTC, datetime

import pytest

from app.schemas.kubevirt_storage import CollectionLimits, SourceSnapshotChildren
from app.services import kubevirt_storage_collector as collector
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.native_clone_graph import cleanup_owned_images
from tests.test_kubevirt_storage_collector import Rbd
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture
from tests.test_native_project_clone_graph import scoped_chain

native_case = native_case_fixture


def test_unreceipted_other_source_snapshot_is_outside_cleanup_boundary():
    inventory, images, proofs = scoped_chain()
    source = images[-1]
    other = SourceSnapshotChildren(
        snapshot=proofs[0].intermediate_parent.model_copy(
            update={"snapshot_id": "999"}
        ),
        present=True,
        children=[source.key.model_copy(update={"image_id": "unknown"})],
    )
    images[-1] = source.model_copy(
        update={
            "snapshots": [*source.snapshots, other.snapshot],
            "children": [*source.children, *other.children],
            "source_snapshot_children": [*source.source_snapshot_children, other],
        }
    )
    # No sibling custody is necessary to prove the selected private chain.
    graph = attribute_graph(inventory, images, clone_lineages=proofs[:1])
    assert graph.projects[0].allocated_bytes == 30
    assert cleanup_owned_images(
        inventory, images, proofs[:1], inventory.project_scope
    ) == {
        proofs[0].binding.image_key,
        proofs[0].intermediate,
    }


@pytest.mark.parametrize("known", [False, True])
def test_extra_child_on_selected_snapshot_blocks_even_known_project(known):
    inventory, images, proofs = scoped_chain()
    source = images[-1]
    foreign = (
        proofs[1].intermediate
        if known
        else source.key.model_copy(update={"image_id": "unknown"})
    )
    entries = list(source.source_snapshot_children)
    entries[0] = entries[0].model_copy(
        update={"children": [*entries[0].children, foreign]}
    )
    images[-1] = source.model_copy(
        update={
            "source_snapshot_children": entries,
            "children": list(dict.fromkeys([*source.children, foreign])),
        }
    )
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    assert result.projects[0].allocated_bytes is None


def test_historical_flattened_children_cannot_authorize_selected_snapshot():
    inventory, images, proofs = scoped_chain()
    images[-1] = images[-1].model_copy(update={"source_snapshot_children": None})
    assert "source_snapshot_children" not in images[-1].model_dump()
    result = attribute_graph(inventory, images, clone_lineages=proofs)
    assert result.projects[0].allocated_bytes is None
    assert "shared_source_snapshot_evidence_missing" in result.projects[0].blockers


def test_swapped_source_children_fail_despite_unchanged_aggregate():
    inventory, images, proofs = scoped_chain()
    source = images[-1]
    first, second = source.source_snapshot_children
    images[-1] = source.model_copy(
        update={
            "source_snapshot_children": [
                first.model_copy(update={"children": second.children}),
                second.model_copy(update={"children": first.children}),
            ]
        }
    )
    assert (
        attribute_graph(inventory, images, clone_lineages=proofs)
        .projects[0]
        .allocated_bytes
        is None
    )


@pytest.mark.parametrize(
    "fault", [None, "identity", "parent", "selected_child", "selected_snapshot"]
)
def test_source_observation_brackets_selected_edges_and_allows_unrelated_churn(fault):
    _, _, proofs = scoped_chain()
    own = proofs[0]
    reads = []

    class Source(Rbd):
        round = 0

        def image_info(self, key):
            result = super().image_info(key)
            if fault == "identity" and self.round:
                result.value["id"] = "replacement"
            if fault == "parent" and self.round:
                result.value["parent"] = {"pool": "pool", "id": "foreign", "snap_id": 9}
            return result

        def image_snapshots(self, key):
            self.round += 1
            result = super().image_snapshots(key)
            ids = [int(own.intermediate_parent.snapshot_id), 100 + self.round]
            if fault == "selected_snapshot" and self.round > 1:
                ids.pop(0)
            result.value.extend(
                {"id": i, "name": str(i), "namespace": {"type": "user"}} for i in ids
            )
            return result

        def image_children(self, key, snapshot):
            reads.append(snapshot)
            assert snapshot == own.intermediate_parent.snapshot_id
            result = super().image_children(key, snapshot)
            result.value.append({"pool": "pool", "id": own.intermediate.image_id})
            if fault == "selected_child" and self.round > 1:
                result.value.append({"pool": "pool", "id": "unrelated"})
            return result

    reader = Source()
    image = collector._observe_source(
        reader,
        own.source.image_key,
        {own.intermediate_parent},
        [{"poolnum": 1, "poolname": "pool"}],
        True,
        "test",
        lambda: datetime.now(UTC),
        CollectionLimits(),
    )
    assert (image.measurement_status == "complete") is (fault is None)
    if fault is None:
        collector._verify_image_graph(
            reader,
            image,
            [{"poolnum": 1, "poolname": "pool"}],
            CollectionLimits(),
            float("inf"),
            lambda: datetime.now(UTC),
        )
        assert reads == [own.intermediate_parent.snapshot_id] * 3


def test_source_cache_binding_tamper_is_not_relaxed():
    inventory, images, proofs = scoped_chain()
    inventory = inventory.model_copy(
        update={
            "cache_bindings": [
                inventory.cache_bindings[0].model_copy(update={"pv_uid": "replacement"})
            ]
        }
    )
    assert (
        attribute_graph(inventory, images, clone_lineages=proofs)
        .projects[0]
        .allocated_bytes
        is None
    )


@pytest.mark.parametrize("foreign_child", [False, True])
def test_current_custody_observe_ignores_only_other_snapshot_consumers(
    monkeypatch, foreign_child
):
    from app.schemas.kubevirt_storage import ReadResult
    from app.services import native_ancestry_custody as custody
    from tests.test_native_ancestry_custody import fixture

    case = fixture(monkeypatch)
    own, sibling = case.proofs
    calls = []

    def result(value):
        return ReadResult(status="complete", value=value, collected_at=case.now)

    def children(key, snapshot):
        calls.append(snapshot)
        assert snapshot == own.intermediate_parent.snapshot_id
        values = [{"pool": "pool", "id": own.intermediate.image_id}]
        if foreign_child:
            values.append({"pool": "pool", "id": sibling.intermediate.image_id})
        return result(values)

    case.backend.pool_identity = lambda: result([{"poolnum": 1, "poolname": "pool"}])
    case.backend.image_info = lambda key: result({"id": key.image_id})
    case.backend.image_snapshots = lambda key: result(
        [
            {
                "id": int(p.intermediate_parent.snapshot_id),
                "name": "snapshot",
                "namespace": {"type": "user"},
            }
            for p in case.proofs
        ]
    )
    case.backend.image_children = children
    monkeypatch.setattr(custody, "_source_metadata", collector._source_metadata)
    if foreign_child:
        with pytest.raises(ValueError, match="unverified edges"):
            custody.observe([own.binding], None, case.backend, None, case.now)
    else:
        assert (
            len(custody.observe([own.binding], None, case.backend, None, case.now)) == 1
        )
        assert calls == [own.intermediate_parent.snapshot_id] * 2


@pytest.mark.parametrize("fault", [None, "digest", "conflict", "malformed"])
def test_historical_report_refresh_preserves_identity_guards(native_case, fault):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectRoot,
        NativeStorageSnapshot,
    )
    from app.services.kubevirt_storage_enrollment import validated_enrollment
    from app.services.quota_accounting import AccountingUnavailable
    from app.services.quota_native import (
        NativeObservationRefreshRequired,
        _measurement_report,
    )
    from tests.test_kubevirt_storage_reports import ingest
    from tests.test_native_clone_lineage import verified_case

    report, _ = verified_case(native_case)
    now = datetime.now(UTC)
    images = [
        i.model_copy(update={"source_snapshot_children": None, "collected_at": now})
        for i in report.images
    ]
    report = report.model_copy(
        update={"images": images, "started_at": now, "completed_at": now}
    )
    report_id = ingest(native_case, report)
    with native_case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(native_case.pins.domain_id))
        row = db.get(NativeStorageSnapshot, report_id)
        assert row.accepted
        if fault == "digest":
            row.digest = "0" * 64
        elif fault == "conflict":
            domain.conflicted = True
        elif fault == "malformed":
            row.payload = {**row.payload, "images": "invalid"}
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
        with pytest.raises((AccountingUnavailable, ValueError)) as raised:
            _measurement_report(db, domain, roots, manifest, datetime.now(UTC))
        assert isinstance(raised.value, NativeObservationRefreshRequired) is (
            fault is None
        )
