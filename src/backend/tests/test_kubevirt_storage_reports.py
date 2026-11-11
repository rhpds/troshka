"""Operator trust and append-only native reporting; never quota admission."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaReadiness, QuotaReservation
from app.models.user import User
from tests.kubevirt_storage_helpers import report_fixture


@pytest.fixture
def native_case(isolated_quota_database):
    from app.models.kubevirt_storage import NativeReportingTarget
    from app.schemas.kubevirt_storage import ReadResult
    from app.schemas.kubevirt_storage_enrollment import (
        ProviderCluster,
        VerificationPins,
    )
    from app.services.kubevirt_storage_enrollment import (
        enroll_native_roots,
        prepare_native_enrollment,
        verify_native_enrollment,
    )

    sessions = isolated_quota_database
    report = report_fixture("zero-private-parent.json")
    target_id = str(uuid4())
    with sessions() as db:
        owner = User(email="native@test.invalid", role="user")
        admin = User(email="native-admin@test.invalid", role="admin")
        provider = Provider(name="native", type="kubevirt")
        db.add_all(
            [owner, admin, provider, NativeReportingTarget(id=1, target_id=target_id)]
        )
        db.flush()
        host = Host(provider_id=provider.id)
        db.add(host)
        db.flush()
        for root in report.project_roots:
            db.add(
                Project(
                    id=str(root.project_id),
                    name=root.namespace,
                    owner_id=owner.id,
                    host_id=host.id,
                )
            )
        db.add(QuotaReadiness(id="baseline", ready=False))
        db.commit()
        owner_id, admin_id, provider_id = owner.id, admin.id, provider.id
        request = prepare_native_enrollment(
            db, [str(r.project_id) for r in report.project_roots]
        )
        pins = VerificationPins(
            domain_id=uuid4(),
            collector_ids=[report.collector_id],
            provider_clusters=[
                ProviderCluster(provider_id=provider_id, cluster_key="cluster")
            ],
            project_roots=report.project_roots,
            cache_roots=[
                dict(
                    cluster_key="cluster",
                    namespace="trusted-cache",
                    namespace_uid="cache-ns",
                )
            ],
            pool_keys=report.pool_keys,
        )

        class Kube:
            def cluster_identity(self):
                return "cluster"

            def namespace(self, name):
                uid = (
                    "cache-ns"
                    if name == "trusted-cache"
                    else next(
                        r.namespace_uid
                        for r in report.project_roots
                        if r.namespace == name
                    )
                )
                return {"metadata": {"uid": uid}}

            def projects(self, namespace):
                root = next(r for r in report.project_roots if r.namespace == namespace)
                return [
                    {
                        "metadata": {"uid": root.project_cr_uid},
                        "spec": {"projectId": str(root.project_id)},
                    }
                ]

        class Backend:
            def cluster_identity(self):
                return ReadResult(
                    status="complete",
                    collected_at=report.completed_at,
                    value=report.pool_keys[0].ceph_fsid,
                )

            def pool_identity(self):
                return ReadResult(
                    status="complete",
                    collected_at=report.completed_at,
                    value=[{"poolnum": 1}],
                )

        manifest = verify_native_enrollment(
            request, {provider_id: Kube()}, Backend(), pins=pins
        )
        enrollment_id = enroll_native_roots(db, manifest, expected_target_id=target_id)
        db.commit()
    return SimpleNamespace(
        sessions=sessions,
        report=report,
        target_id=target_id,
        enrollment_id=enrollment_id,
        digest=manifest.digest,
        owner_id=owner_id,
        admin_id=admin_id,
        pins=pins,
        request=request,
    )


def ingest(case, report=None):
    from app.services.kubevirt_storage_reports import import_report

    with case.sessions() as db:
        result = import_report(
            db,
            report or case.report,
            enrollment_id=case.enrollment_id,
            expected_enrollment_digest=case.digest,
            now=datetime.now(UTC),
        )
        db.commit()
        return result


def test_append_only_replay_retains_source_times_and_never_enables_accounting(
    native_case,
):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    q = native_case
    identity = ingest(q)
    assert ingest(q) == identity
    changed = q.report.model_copy(update={"collector_id": "impostor"})
    with pytest.raises(ValueError):
        ingest(q, changed)
    with q.sessions() as db:
        row = db.get(NativeStorageSnapshot, identity)
        assert (
            row.payload["started_at"] == q.report.model_dump(mode="json")["started_at"]
        )
        assert db.query(NativeStorageSnapshot).count() == 1
        assert db.query(QuotaReservation).count() == 0
        assert db.get(QuotaReadiness, "baseline").ready is False


def test_unknown_namespace_and_collector_cannot_bootstrap(native_case):
    q = native_case
    from app.services.kubevirt_storage_reports import import_report

    with q.sessions() as db, pytest.raises(ValueError):
        import_report(
            db,
            q.report,
            enrollment_id=str(uuid4()),
            expected_enrollment_digest=q.digest,
            now=datetime.now(UTC),
        )
    forged = q.report.model_copy(
        update={
            "project_roots": [
                q.report.project_roots[0].model_copy(update={"namespace_uid": "forged"})
            ]
        }
    )
    with pytest.raises(ValueError):
        ingest(q, forged)


def test_new_partial_invalidates_old_complete_and_late_old_does_not_resurrect(
    native_case,
):
    from app.services.kubevirt_storage_usage import read_native_usage

    q = native_case
    from app.schemas.kubevirt_storage import Blocker
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    later = q.report.model_copy(
        update={
            "report_id": uuid4(),
            "started_at": q.report.started_at + timedelta(seconds=1),
            "completed_at": q.report.completed_at + timedelta(seconds=1),
            "collection_errors": [Blocker(code="unavailable")],
        }
    )
    later = later.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(later))}
    )
    ingest(q)
    ingest(q, later)
    ingest(q, q.report.model_copy(update={"report_id": uuid4()}))
    with q.sessions() as db:
        usage = read_native_usage(
            db,
            db.get(User, q.owner_id),
            "user",
            q.owner_id,
            q.report.completed_at + timedelta(seconds=2),
        )
    assert usage.allocated_bytes is None
    assert "unavailable" in usage.reason_codes
    assert usage.enforcement_eligible is False


def revised(report, **updates):
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    value = report.model_copy(update={"report_id": uuid4(), **updates})
    return value.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(value))}
    )


def usage(case, scope="user", identity=None, now=None, admin=False):
    from app.services.kubevirt_storage_usage import read_native_usage

    with case.sessions() as db:
        actor = db.get(User, case.admin_id if admin else case.owner_id)
        return read_native_usage(
            db, actor, scope, identity or case.owner_id, now or case.report.completed_at
        )


def test_newest_omission_never_interpreted_as_zero(native_case):
    q = native_case
    ingest(q)
    root = q.report.project_roots[0]
    report = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        project_roots=[root],
        volume_bindings=[b for b in q.report.volume_bindings if b.root == root],
    )
    ingest(q, report)
    result = usage(q, now=report.completed_at)
    assert result.allocated_bytes is None
    assert result.known_allocated_bytes == 30
    assert result.expected_projects == 2
    assert result.covered_projects == 1
    assert "expected_root_missing" in result.reason_codes


def test_equal_start_conflicts_until_explicitly_later_report(native_case):
    q = native_case
    ingest(q)
    images = [
        q.report.images[0].model_copy(update={"allocated_bytes": 31}),
        *q.report.images[1:],
    ]
    ingest(q, revised(q.report, images=images))
    assert "collection_domain_conflict" in usage(q).reason_codes
    later = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
    )
    ingest(q, later)
    assert usage(q, now=later.completed_at).allocated_bytes == 80


def test_same_start_consistent_observations_do_not_conflict_by_random_id(native_case):
    q = native_case
    ingest(q)
    ingest(
        q, revised(q.report, completed_at=q.report.completed_at + timedelta(seconds=1))
    )
    assert usage(q).allocated_bytes == 80


def test_alias_roots_are_deduplicated_and_report_aggregate_fields_are_untrusted(
    native_case,
):
    q = native_case
    report = revised(
        q.report,
        project_roots=q.report.project_roots * 2,
        volume_bindings=q.report.volume_bindings * 2,
        projects=[
            p.model_copy(update={"allocated_bytes": 999}) for p in q.report.projects
        ],
    )
    ingest(q, report)
    assert usage(q).allocated_bytes == 80
    assert usage(q, admin=True).platform_bytes == 100


def test_two_roots_claiming_one_image_leave_bytes_unresolved_once(native_case):
    q = native_case
    bindings = [
        q.report.volume_bindings[0],
        q.report.volume_bindings[1].model_copy(
            update={"image_key": q.report.volume_bindings[0].image_key}
        ),
    ]
    ingest(q, revised(q.report, volume_bindings=bindings))
    result = usage(q)
    assert result.allocated_bytes is None
    assert result.known_allocated_bytes == 0
    assert "unresolved_ownership" in result.reason_codes


def test_staleness_is_source_age_and_topology_writes_do_not_change_enrollment(
    native_case,
):
    q = native_case
    with q.sessions() as db:
        project = db.get(Project, str(q.report.project_roots[0].project_id))
        project.name = "Renamed during scan"
        project.updated_at = datetime.now(UTC)
        db.commit()
    ingest(q)
    result = usage(q, now=q.report.completed_at + timedelta(days=1))
    assert result.status == "stale"
    assert result.allocated_bytes is None
    assert result.known_allocated_bytes == 80
    assert result.observed_at == q.report.started_at


def test_changed_namespace_and_pvc_identity_retains_old_evidence(native_case):
    from app.models.kubevirt_storage import NativeRetainedBinding, NativeStorageSnapshot

    q = native_case
    ingest(q)
    binding = q.report.volume_bindings[0].model_copy(update={"pv_uid": "replacement"})
    bad = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        volume_bindings=[binding, q.report.volume_bindings[1]],
    )
    identity = ingest(q, bad)
    with q.sessions() as db:
        assert db.query(NativeStorageSnapshot).count() == 2
        assert not db.get(NativeStorageSnapshot, identity).accepted
        assert not any(
            b.payload.get("pv_uid") == "replacement"
            for b in db.query(NativeRetainedBinding)
        )
    assert usage(q).allocated_bytes is None


def test_independent_enrollment_rejects_unprepared_and_identity_drift(native_case):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import digest, enroll_native_roots

    q = native_case
    with q.sessions() as db:
        original = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, q.enrollment_id).payload
        )
        forged = original.model_copy(
            update={
                "request": original.request.model_copy(update={"request_id": uuid4()})
            }
        )
        forged = forged.model_copy(
            update={
                "digest": digest(forged.model_dump(mode="json", exclude={"digest"}))
            }
        )
        with pytest.raises(ValueError, match="independently prepared"):
            enroll_native_roots(db, forged, expected_target_id=q.target_id)
        db.get(Project, str(q.report.project_roots[0].project_id)).host_id = None
        db.flush()
        with pytest.raises(ValueError, match="assigned native"):
            enroll_native_roots(db, original, expected_target_id=q.target_id)


def test_first_partial_can_preserve_complete_discovery_without_certifying_usage(
    native_case,
):
    from app.schemas.kubevirt_storage import Blocker

    q = native_case
    partial = revised(
        q.report,
        inventory_blockers=[],
        collection_errors=[Blocker(code="backend_graph_recheck_unavailable")],
    )
    # Aggregate claims still say complete; the importer must recompute conservatively.
    ingest(q, partial)
    assert usage(q).allocated_bytes is None
    assert usage(q).known_allocated_bytes == 80
    assert "backend_graph_recheck_unavailable" in usage(q).reason_codes


def test_legacy_partial_with_nonreconstructible_digest_is_rejected(native_case):
    from app.schemas.kubevirt_storage import Blocker

    q = native_case
    report = q.report.model_copy(
        update={
            "collection_errors": [Blocker(code="backend_graph_recheck_unavailable")]
        }
    )
    with pytest.raises(ValueError, match="unsupported legacy inventory evidence"):
        ingest(q, report)


def test_snapshot_identity_participates_in_digest_and_always_blocks_complete(
    native_case,
):
    from app.schemas.kubevirt_storage import CSISnapshotEvidence

    q = native_case
    snapshot = CSISnapshotEvidence(
        root=q.report.project_roots[0],
        snapshot_uid="snap-uid",
        snapshot_name="snap",
        content_name=None,
        content_uid=None,
        source_pvc_name="pvc",
        csi_driver=None,
        ready_to_use=False,
        snapshot_handle_sha256=None,
        source_snapshot_handle_sha256=None,
        source_volume_handle_sha256=None,
    )
    report = revised(q.report, inventory_blockers=[], snapshot_evidence=[snapshot])
    ingest(q, report)
    result = usage(q)
    assert result.allocated_bytes is None
    assert "csi_snapshot_backend_unresolved" in result.reason_codes
    bad = report.model_copy(update={"report_id": uuid4(), "snapshot_evidence": []})
    with pytest.raises(ValueError, match="inventory digest mismatch"):
        ingest(q, bad)


def test_import_and_read_leave_all_existing_quota_rows_unchanged(native_case):
    from app.models.quota import (
        QuotaAllocation,
        QuotaAudit,
        QuotaCharge,
        QuotaPolicy,
        QuotaReservation,
    )
    from app.schemas.quota import Resources

    q = native_case
    models = [
        QuotaAllocation,
        QuotaReservation,
        QuotaReadiness,
        QuotaAudit,
        QuotaCharge,
        QuotaPolicy,
    ]

    def snapshot(db):
        return {
            m.__name__: [
                dict((c.name, getattr(r, c.name)) for c in m.__table__.columns)
                for r in db.query(m).all()
            ]
            for m in models
        }

    with q.sessions() as db:
        db.add(QuotaPolicy(scope="user", scope_id=q.owner_id, cpu=2))
        db.add(
            QuotaAllocation(
                project_id=str(q.report.project_roots[0].project_id),
                owner_id=q.owner_id,
                organization_id=str(uuid4()),
                operation_id="native-test",
                requested=Resources(cpu=1).model_dump(mode="json"),
            )
        )
        db.add(
            QuotaReservation(
                project_id=str(q.report.project_roots[0].project_id),
                owner_id=q.owner_id,
                organization_id=str(uuid4()),
                operation_id="native-reservation",
                requested=Resources(cpu=2).model_dump(mode="json"),
            )
        )
        db.add(
            QuotaCharge(
                project_id=str(q.report.project_roots[0].project_id),
                project_name="history",
                owner_id=q.owner_id,
                organization_id=str(uuid4()),
                source="native-test",
                source_id="history",
                kind="vcpu",
                quantity=1,
                rate=1,
                amount=1,
                start_at=q.report.started_at,
                end_at=q.report.started_at + timedelta(seconds=1),
            )
        )
        db.commit()
        before = snapshot(db)
    ingest(q)
    assert usage(q).allocated_bytes == 80
    with q.sessions() as db:
        assert snapshot(db) == before


def test_real_collector_serialization_preserves_original_digest_with_late_failures(
    native_case,
):
    from app.schemas.kubevirt_storage import (
        CollectionLimits,
        NativeStorageReport,
        ReadResult,
    )
    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_inventory import inventory_digest
    from tests.test_kubevirt_storage_collector import Rbd
    from tests.test_kubevirt_storage_graph import inventory

    q = native_case
    source = inventory().model_copy(update={"collector_id": q.report.collector_id})
    reader = Rbd()
    reader.recheck_inventory = lambda: (_ for _ in ()).throw(
        ValueError("read unavailable")
    )
    original_usage = reader.image_usage
    reader.image_usage = (
        lambda key: ReadResult(status="timeout", collected_at=source.collected_at)
        if key.image_id == "child-1"
        else original_usage(key)
    )
    report = collect_report(
        source, reader, limits=CollectionLimits(), clock=lambda: source.collected_at
    )
    report = NativeStorageReport.model_validate_json(report.model_dump_json())
    assert report.inventory_blockers == []
    assert report.inventory_digest == inventory_digest(source)
    assert any(
        b.code == "inventory_recheck_unavailable" for b in report.collection_errors
    )
    ingest(q, report)
    assert "inventory_recheck_unavailable" in usage(q).reason_codes
    assert usage(q).allocated_bytes is None


def test_independent_read_snapshot_does_not_flush_or_rollback_caller_writes(
    native_case,
):
    from app.services.kubevirt_storage_usage import read_native_usage

    q = native_case
    ingest(q)
    pid = str(q.report.project_roots[0].project_id)
    with q.sessions() as db:
        project = db.get(Project, pid)
        project.name = "Uncommitted caller edit"
        read_native_usage(
            db, db.get(User, q.owner_id), "project", pid, q.report.completed_at
        )
        assert project.name == "Uncommitted caller edit"
        assert db.in_transaction()
        db.rollback()
    with q.sessions() as db:
        assert db.get(Project, pid).name == "project-1"


def test_transfer_during_read_cannot_mix_scope_versions(native_case, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.services import kubevirt_storage_usage

    q = native_case
    ingest(q)
    with q.sessions.kw["bind"].connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
    original = kubevirt_storage_usage.authorize_policy
    changed = False
    pid = str(q.report.project_roots[0].project_id)

    def transfer(db, actor, scope, scope_id, **kwargs):
        nonlocal changed
        target = original(db, actor, scope, scope_id, **kwargs)
        if not changed:
            changed = True
            with q.sessions() as other:
                other.get(Project, pid).owner_id = q.admin_id
                other.commit()
        return target

    monkeypatch.setattr(kubevirt_storage_usage, "authorize_policy", transfer)
    # The initial snapshot can consistently authorize its pre-transfer owner.
    assert usage(q, "project", pid).allocated_bytes == 30
    # A fresh snapshot honors the committed revocation immediately.
    with pytest.raises(HTTPException) as error:
        usage(q, "project", pid)
    assert error.value.status_code == 403


def test_physical_pool_cannot_be_enrolled_by_a_second_domain(native_case):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import digest, enroll_native_roots

    q = native_case
    with q.sessions() as db:
        manifest = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, q.enrollment_id).payload
        )
        manifest = manifest.model_copy(
            update={"pins": manifest.pins.model_copy(update={"domain_id": uuid4()})}
        )
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        with pytest.raises(ValueError, match="physical pool"):
            enroll_native_roots(db, manifest, expected_target_id=q.target_id)


@pytest.mark.parametrize("invalid", [-1, 1.5, True, 2**63])
def test_import_revalidates_exact_nonnegative_signed_int64(native_case, invalid):
    from pydantic import ValidationError

    q = native_case
    image = q.report.images[0].model_copy(update={"allocated_bytes": invalid})
    report = q.report.model_copy(update={"images": [image, *q.report.images[1:]]})
    with pytest.raises(ValidationError):
        ingest(q, report)


def test_verified_enrollment_requires_full_project_id_not_namespace_prefix(native_case):
    from app.services.kubevirt_storage_enrollment import verify_native_enrollment

    q = native_case

    class Kube:
        def cluster_identity(self):
            return "cluster"

        def namespace(self, name):
            return {
                "metadata": {
                    "uid": next(
                        r.namespace_uid
                        for r in q.report.project_roots
                        if r.namespace == name
                    )
                }
            }

        def projects(self, namespace):
            root = next(r for r in q.report.project_roots if r.namespace == namespace)
            return [
                {
                    "metadata": {"uid": root.project_cr_uid},
                    "spec": {"projectId": str(root.project_id)[:8]},
                }
            ]

    readers = {str(p.provider_id): Kube() for p in q.request.projects}
    with pytest.raises(ValueError, match="full TroshkaProject identity"):
        verify_native_enrollment(q.request, readers, None, pins=q.pins)


def test_complete_native_zero_is_distinct_from_missing_or_unavailable(native_case):
    q = native_case
    images = [
        i if i.key.image_id == "cache" else i.model_copy(update={"allocated_bytes": 0})
        for i in q.report.images
    ]
    ingest(q, revised(q.report, images=images))
    result = usage(q)
    assert result.status == "complete"
    assert result.allocated_bytes == 0
    assert result.known_allocated_bytes == 0
    assert result.reason_codes == []


def test_scope_overflow_does_not_round_or_expose_out_of_range_integer(native_case):
    q = native_case
    images = [
        i.model_copy(update={"allocated_bytes": 2**63 - 1})
        if i.key.image_id == "child-1"
        else i
        for i in q.report.images
    ]
    ingest(q, revised(q.report, images=images))
    result = usage(q)
    assert result.allocated_bytes is None
    assert result.known_allocated_bytes == 2**63 - 1
    assert "aggregation_overflow" in result.reason_codes


def test_explicit_new_cr_enrollment_retains_old_root_identity(native_case):
    from app.models.kubevirt_storage import NativeEnrollment, NativeProjectRoot
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import digest, enroll_native_roots

    q = native_case
    with q.sessions() as db:
        manifest = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, q.enrollment_id).payload
        )
        replacement = q.report.project_roots[0].model_copy(
            update={"project_cr_uid": "explicitly-verified-new-cr"}
        )
        pins = manifest.pins.model_copy(
            update={"project_roots": [replacement, q.report.project_roots[1]]}
        )
        manifest = manifest.model_copy(update={"pins": pins})
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        enroll_native_roots(db, manifest, expected_target_id=q.target_id)
        db.commit()
        assert db.query(NativeProjectRoot).count() == 3
        assert {r.payload["project_cr_uid"] for r in db.query(NativeProjectRoot)} == {
            "cr-1",
            "cr-2",
            "explicitly-verified-new-cr",
        }


@pytest.mark.parametrize("source", ["live", "retained", "live-retained", "cache"])
@pytest.mark.parametrize(
    "field", ["pv_uid", "volume_handle", "csi_driver", "image_key"]
)
def test_first_report_conflicting_pvc_mappings_never_promote_identity(
    native_case, source, field
):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeRetainedBinding,
        NativeStorageSnapshot,
    )

    q = native_case
    binding = (
        q.report.cache_bindings[0] if source == "cache" else q.report.volume_bindings[0]
    )
    replacement = (
        binding.image_key.model_copy(update={"image_id": "contradictory-image"})
        if field == "image_key"
        else "contradictory-identity"
    )
    conflicting = binding.model_copy(update={field: replacement})
    if source == "cache":
        report = revised(q.report, cache_bindings=[binding, conflicting])
    elif source == "live":
        report = revised(
            q.report, volume_bindings=[*q.report.volume_bindings, conflicting]
        )
    elif source == "retained":
        report = revised(
            q.report,
            volume_bindings=q.report.volume_bindings[1:],
            retained_bindings=[binding, conflicting],
        )
    else:
        report = revised(q.report, retained_bindings=[conflicting])
    identity = ingest(q, report)
    with q.sessions() as db:
        snapshot = db.get(NativeStorageSnapshot, identity)
        assert snapshot.accepted is False
        assert "retained_binding_identity_changed" in snapshot.reason_codes
        assert db.query(NativeRetainedBinding).count() == 0
        assert (
            db.get(NativeCollectionDomain, snapshot.domain_id).latest_report_id
            == identity
        )
    assert usage(q).allocated_bytes is None


def test_identical_live_retained_and_cache_aliases_still_deduplicate(native_case):
    from app.models.kubevirt_storage import NativeRetainedBinding

    q = native_case
    report = revised(
        q.report,
        volume_bindings=q.report.volume_bindings * 2,
        retained_bindings=q.report.volume_bindings,
        cache_bindings=q.report.cache_bindings * 2,
    )
    ingest(q, report)
    assert usage(q).allocated_bytes == 80
    assert usage(q, admin=True).platform_bytes == 100
    with q.sessions() as db:
        assert db.query(NativeRetainedBinding).count() == 3


def test_new_contradictory_attempt_preserves_prior_bindings_but_invalidates_current_usage(
    native_case,
):
    from app.models.kubevirt_storage import NativeRetainedBinding

    q = native_case
    ingest(q)
    with q.sessions() as db:
        previous = {b.key: b.payload for b in db.query(NativeRetainedBinding)}
    binding = q.report.volume_bindings[0].model_copy(
        update={"pvc_uid": "new-pvc", "pv_uid": "new-pv"}
    )
    report = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        volume_bindings=[*q.report.volume_bindings, binding],
        retained_bindings=[binding.model_copy(update={"pv_uid": "conflicting-new-pv"})],
    )
    ingest(q, report)
    assert usage(q, now=report.completed_at).allocated_bytes is None
    with q.sessions() as db:
        assert {b.key: b.payload for b in db.query(NativeRetainedBinding)} == previous
