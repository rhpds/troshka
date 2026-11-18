"""Shared platform freshness is independent of project measurement attempts."""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeSharedStorageObservation,
)
from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
from app.schemas.quota import AccountingUnavailable
from app.services import native_collection_shared_storage as shared
from tests.test_kubevirt_storage_collector import Rbd
from tests.test_kubevirt_storage_reports import ingest
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


def test_first_shared_scan_requires_exact_discovered_custody_source():
    from app.services.kubevirt_storage_reports import report_inventory
    from tests.test_native_clone_lineage import chain

    report, _, proofs = chain()
    inventory = report_inventory(report)
    bindings = shared._source_bindings([], proofs, inventory)
    assert bindings == [proofs[0].source]


@pytest.mark.parametrize("fault", ["missing", "replaced", "absent", "no_inventory"])
def test_first_shared_scan_rejects_missing_or_changed_source(fault):
    from app.services.kubevirt_storage_reports import report_inventory
    from tests.test_native_clone_lineage import chain

    report, _, proofs = chain()
    inventory = report_inventory(report)
    if fault == "missing":
        inventory = inventory.model_copy(update={"cache_bindings": []})
    elif fault == "replaced":
        inventory = inventory.model_copy(
            update={
                "cache_bindings": [
                    proofs[0].source.model_copy(update={"pvc_uid": "replacement"})
                ]
            }
        )
    elif fault == "absent":
        inventory = inventory.model_copy(
            update={"absent_cache_bindings": [proofs[0].source]}
        )
    else:
        inventory = None
    with pytest.raises(AccountingUnavailable):
        shared._source_bindings([], proofs, inventory)


@pytest.mark.parametrize("detached", [False, True])
def test_bootstrap_discovery_survives_publication_and_reuse(
    native_case, monkeypatch, detached
):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services import native_clone_lineage
    from app.services.kubevirt_storage_reports import report_inventory
    from tests.test_native_clone_lineage import chain

    case = native_case
    report, images, proofs = chain()
    if detached:
        images[-1] = images[-1].model_copy(
            update={"snapshots": [], "children": [], "source_snapshot_children": None}
        )
    inventory = report_inventory(report)
    monkeypatch.setattr(native_clone_lineage, "resolve_lineages", lambda *args: proofs)
    monkeypatch.setattr(shared, "_scan", lambda *args: [images[-1]])
    with case.sessions() as db:
        assert db.query(NativeRetainedBinding).count() == 0
        manifest = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, case.enrollment_id).payload
        )
    measured = shared.collect_shared_storage(
        case.sessions,
        manifest,
        Rbd(),
        images[-1].collected_at,
        clock=lambda: images[-1].collected_at,
        cache_inventory=lambda: inventory,
    )
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        assert (
            shared.reusable_shared_images(db, domain, images[-1].collected_at)[0]
            == measured
        )
        assert db.query(NativeRetainedBinding).count() == 0


@pytest.mark.parametrize("state", ["complete", "stale", "failed"])
def test_cache_only_baseline_uses_independent_shared_evidence(native_case, state):
    from app.services.quota_capacity_baseline import _cache_only_domains

    case = native_case
    ingest(case)
    publish(case)
    identity = str(case.pins.domain_id)
    source = {
        "native_roots": [],
        "retired_native_domains": [identity],
        "native_retained_bindings": [
            {"domain_id": identity, "coverage": "platform_cache"}
        ],
    }
    with case.sessions() as db:
        # RQ refreshes shared evidence, never this legacy whole-domain pointer.
        domain = db.get(NativeCollectionDomain, identity)
        domain.latest_report_id = None
        if state == "failed":
            db.get(NativeSharedStorageObservation, identity).status = "failed"
        db.commit()
        now = case.report.completed_at + timedelta(
            seconds=901 if state == "stale" else 1
        )
        if state == "complete":
            assert _cache_only_domains(db, source, now) == {identity}
        else:
            with pytest.raises(AccountingUnavailable):
                _cache_only_domains(db, source, now)


def publish(case, backend=None, offset=0, **kwargs):
    with case.sessions() as db:
        manifest = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, case.enrollment_id).payload
        )
    return shared.collect_shared_storage(
        case.sessions,
        manifest,
        backend or Rbd(),
        case.report.started_at + timedelta(seconds=offset),
        clock=lambda: case.report.completed_at + timedelta(seconds=offset + 1),
        **kwargs,
    )


def current(case, offset=1):
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        return shared.reusable_shared_images(
            db, domain, case.report.completed_at + timedelta(seconds=offset)
        )


def test_shared_storage_reads_only_cache_once_and_preserves_source_time(native_case):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_reports import _binding_identity, binding_key

    case = native_case
    ingest(case)
    duplicate = case.report.cache_bindings[0].model_copy(
        update={"pvc_uid": "alias-pvc"}
    )
    with case.sessions.begin() as db:
        db.add(
            NativeRetainedBinding(
                key=binding_key(duplicate),
                root_key="cache",
                domain_id=str(case.pins.domain_id),
                payload=_binding_identity(duplicate),
            )
        )
    backend = Rbd()
    measured = []
    original = backend.image_usage
    backend.image_usage = lambda key: (measured.append(key) or original(key))
    images = publish(case, backend)
    assert measured == [case.report.cache_bindings[0].image_key]
    assert len(images) == 1
    assert images[0].collected_at == case.report.started_at
    assert current(case)[0] == images


def test_failed_shared_attempt_invalidates_old_success(native_case):
    case = native_case
    ingest(case)
    publish(case)
    backend = Rbd()
    backend.cluster_identity = lambda: (_ for _ in ()).throw(ValueError("offline"))
    with pytest.raises(AccountingUnavailable):
        publish(case, backend, offset=2)
    with pytest.raises(AccountingUnavailable):
        current(case, offset=3)


def test_shared_stale_and_changed_enrollment_never_fall_back(native_case):
    case = native_case
    ingest(case)
    publish(case)
    with pytest.raises(AccountingUnavailable, match="stale"):
        current(case, offset=901)
    with case.sessions.begin() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        domain.enrollment_id = str(uuid4())
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_shared_platform_replaces_legacy_totals_without_project_duplication(
    native_case,
):
    from app.services.kubevirt_storage_usage import _read_authorized

    case = native_case
    ingest(case)
    backend = Rbd()
    original = backend.image_usage

    def measured(key):
        result = original(key)
        result.value["images"][0]["used_size"] = 17
        return result

    backend.image_usage = measured
    publish(case, backend)
    with case.sessions() as db:
        usage = _read_authorized(
            db,
            case.admin_id,
            "user",
            case.owner_id,
            case.report.completed_at + timedelta(seconds=1),
        )
        assert usage.platform_bytes == 17
        assert usage.allocated_bytes == 80
        assert usage.expected_projects == 2


def test_shared_same_time_conflict_is_sticky(native_case):
    case = native_case
    ingest(case)
    publish(case)
    with case.sessions.begin() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        shared._advance(
            db, domain, None, case.report.started_at, case.report.completed_at
        )
    with pytest.raises(AccountingUnavailable):
        current(case)
    with case.sessions() as db:
        assert (
            db.get(NativeSharedStorageObservation, str(case.pins.domain_id)).status
            == "conflict"
        )


def test_shared_sources_validate_all_sibling_edges_without_private_reads():
    from tests.test_native_clone_lineage import chain

    report, images, proofs = chain()
    source = images[-1]
    shared._validate_images(
        [source],
        report.cache_bindings,
        proofs,
        source.collected_at,
        source.collected_at,
    )
    changed = source.model_copy(update={"children": source.children[:-1]})
    with pytest.raises(AccountingUnavailable, match="graph edges"):
        shared._validate_images(
            [changed],
            report.cache_bindings,
            proofs,
            source.collected_at,
            source.collected_at,
        )


@pytest.mark.parametrize("remaining", [0, 1])
def test_full_shared_scan_accepts_removed_clone_edges(remaining):
    from tests.test_native_clone_lineage import chain

    report, images, proofs = chain()
    source = images[-1].model_copy(
        update={
            "snapshots": [p.intermediate_parent for p in proofs[:remaining]],
            "children": [p.intermediate for p in proofs[:remaining]],
            "source_snapshot_children": None,
        }
    )
    shared._validate_images(
        [source],
        report.cache_bindings,
        proofs,
        source.collected_at,
        source.collected_at,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_snapshot",
        "unknown_child",
        "detached_child",
        "missing_child",
        "duplicate_snapshot",
        "incomplete",
        "stale",
        "partial",
    ],
)
def test_removed_shared_edges_do_not_authorize_uncertain_graphs(fault):
    from tests.test_native_clone_lineage import chain

    report, images, proofs = chain()
    original = images[-1]
    source = original.model_copy(
        update={
            "snapshots": [proofs[0].intermediate_parent],
            "children": [proofs[0].intermediate],
            "source_snapshot_children": None,
        }
    )
    updates = {
        "unknown_snapshot": {
            "snapshots": [
                proofs[0].intermediate_parent.model_copy(
                    update={"snapshot_id": "99999"}
                )
            ]
        },
        "unknown_child": {
            "children": [
                proofs[0].intermediate.model_copy(update={"image_id": "unknown"})
            ]
        },
        "detached_child": {"children": [p.intermediate for p in proofs]},
        "missing_child": {"children": []},
        "duplicate_snapshot": {"snapshots": source.snapshots * 2},
        "incomplete": {"measurement_status": "unavailable"},
        "stale": {"collected_at": original.collected_at - timedelta(days=1)},
        "partial": {"source_snapshot_children": original.source_snapshot_children[:1]},
    }
    with pytest.raises(AccountingUnavailable):
        shared._validate_images(
            [source.model_copy(update=updates[fault])],
            report.cache_bindings,
            proofs,
            original.collected_at,
            original.collected_at,
        )


def cache_absence(case):
    from app.services.kubevirt_storage_reports import report_inventory

    return report_inventory(case.report).model_copy(
        update={
            "project_roots": [],
            "volume_bindings": [],
            "retained_bindings": [],
            "absent_cache_bindings": case.report.cache_bindings,
        }
    )


class AbsentCache(Rbd):
    def __init__(self, timestamp, fault=None):
        self.timestamp, self.fault, self.calls = timestamp, fault, 0

    def image_info(self, key):
        from app.schemas.kubevirt_storage import ReadResult

        return ReadResult(status="unavailable", collected_at=self.timestamp)

    def image_inventory(self, key, *, max_objects):
        from app.schemas.kubevirt_storage import BackendImageState, ReadResult

        self.calls += 1
        if self.fault == "unavailable":
            return ReadResult(status="unavailable", collected_at=self.timestamp)
        value = BackendImageState(
            **key.model_dump(),
            collected_at=self.timestamp,
            active=self.fault == "active"
            or (self.fault == "replacement" and self.calls > 1),
            trash=self.fault == "trash",
        )
        return ReadResult(status="complete", value=value, collected_at=self.timestamp)


def test_confirmed_absent_cache_requires_discovery_and_repeat_backend_absence(
    native_case,
):
    case = native_case
    ingest(case)
    backend = AbsentCache(case.report.started_at)
    calls = []

    def discover():
        calls.append(True)
        return cache_absence(case)

    images = publish(case, backend, cache_inventory=discover)
    assert len(calls) == 2
    assert backend.calls == 2
    assert images[0].absence is not None
    assert images[0].allocated_bytes == 0
    assert current(case)[0] == images
    with case.sessions() as db:
        row = db.get(NativeSharedStorageObservation, str(case.pins.domain_id))
        assert row.payload["cache_absence_inventory"]["absent_cache_bindings"]


@pytest.mark.parametrize(
    "fault",
    [
        "active",
        "trash",
        "unavailable",
        "replacement",
        "no_discovery",
        "changed_discovery",
    ],
)
def test_cache_absence_uncertainty_never_becomes_zero(native_case, fault):
    from app.schemas.kubevirt_storage import Blocker

    case = native_case
    ingest(case)
    backend = AbsentCache(case.report.started_at, fault)
    calls = []

    def discover():
        calls.append(True)
        inventory = cache_absence(case)
        if fault == "changed_discovery" and len(calls) > 1:
            inventory = inventory.model_copy(
                update={"blockers": [Blocker(code="cache_discovery_unavailable")]}
            )
        return inventory

    with pytest.raises(AccountingUnavailable):
        publish(
            case, backend, cache_inventory=None if fault == "no_discovery" else discover
        )
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_absent_cache_with_active_lineage_or_unknown_identity_is_rejected():
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_collector import _retiring_image
    from tests.test_native_clone_lineage import chain

    report, images, proofs = chain()
    source = images[-1]
    absent = _retiring_image(
        AbsentCache(source.collected_at),
        source.key,
        "test",
        CollectionLimits(),
        lambda: source.collected_at,
    )
    with pytest.raises(AccountingUnavailable, match="not authorized"):
        shared._validate_images(
            [absent],
            report.cache_bindings,
            proofs,
            source.collected_at,
            source.collected_at,
            {source.key},
        )
    with pytest.raises(AccountingUnavailable, match="not authorized"):
        shared._validate_images(
            [absent],
            report.cache_bindings,
            [],
            source.collected_at,
            source.collected_at,
        )
