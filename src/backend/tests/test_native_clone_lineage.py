"""Protected clone lineage contracts; no live cluster acceptance is implied."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.kubevirt_storage_helpers import report_fixture
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture
from tests.test_quota_concurrency import postgres as postgres_fixture

native_case = native_case_fixture
postgres = postgres_fixture


@pytest.fixture(params=["sqlite", "postgres"])
def clone_case(request, native_case):
    if request.param == "sqlite":
        return native_case
    import os

    from app.models.quota import QuotaReadiness
    from tests.test_kubevirt_storage_reports import make_native_case

    if not os.getenv("QUOTA_TEST_POSTGRES_URL"):
        pytest.skip("disposable PostgreSQL URL not configured")
    sessions, _, _ = request.getfixturevalue("postgres")
    with sessions.begin() as db:
        db.query(QuotaReadiness).delete()
    return make_native_case(sessions)


def chain():
    from app.schemas.kubevirt_storage import SnapshotKey
    from app.schemas.native_clone_lineage import CloneLineage

    report = report_fixture("zero-private-parent.json")
    source = report.cache_bindings[0]
    images = []
    proofs = []
    for index, binding in enumerate(report.volume_bindings):
        head = report.images[index]
        middle = report.images[index + 2]
        middle_snapshot = head.parent
        source_snapshot = middle.parent.model_copy(
            update={"snapshot_id": str(index + 1)}
        )
        images += [
            head,
            middle.model_copy(
                update={"snapshots": [middle_snapshot], "parent": source_snapshot}
            ),
        ]
        proofs.append(
            CloneLineage(
                lineage_id=uuid4(),
                operation_id=str(uuid4()),
                claim_name=f"disk-{index}",
                domain_id=uuid4(),
                target_id=uuid4(),
                cache_reference=dict(operation_id=uuid4(), digest="a" * 64),
                binding=binding,
                source=source,
                intermediate=middle.key,
                head_parent=middle_snapshot,
                intermediate_parent=source_snapshot,
                original_dv_uid=f"dv-{index}",
                verified_at=datetime.now(UTC),
            )
        )
    images.append(
        report.images[-1].model_copy(
            update={
                "snapshots": [
                    SnapshotKey(image_key=source.image_key, snapshot_id=str(index + 1))
                    for index in range(2)
                ]
            }
        )
    )
    images = [i.model_copy(update={"collected_at": datetime.now(UTC)}) for i in images]
    return report, images, proofs


def test_verified_three_image_chains_charge_private_intermediates_once():
    from app.services.kubevirt_storage_graph import attribute_graph
    from app.services.kubevirt_storage_reports import report_inventory

    report, images, proofs = chain()
    graph = attribute_graph(report_inventory(report), images, clone_lineages=proofs)
    assert [p.allocated_bytes for p in graph.projects] == [30, 50]
    assert [p.required_images for p in graph.projects] == [2, 2]
    assert graph.platform_pools[0].allocated_bytes == 100


def test_identical_unreceipted_snapshot_graph_stays_unready():
    from app.services.kubevirt_storage_graph import attribute_graph
    from app.services.kubevirt_storage_reports import report_inventory

    report, images, _ = chain()
    graph = attribute_graph(report_inventory(report), images)
    assert all("snapshot_ancestry_unverified" in p.blockers for p in graph.projects)
    assert graph.platform_pools[0].allocated_bytes is None


@pytest.mark.parametrize(
    "change",
    [
        "parent",
        "snapshots",
        "consumer",
        "missing",
        "copy",
        "partial",
        "source",
        "duplicate",
    ],
)
def test_lineage_current_mutations_stay_unready(change):
    from app.schemas.kubevirt_storage import SnapshotKey
    from app.services.kubevirt_storage_graph import attribute_graph
    from app.services.kubevirt_storage_reports import report_inventory

    report, images, proofs = chain()
    if change == "parent":
        images[1] = images[1].model_copy(update={"parent": proofs[0].head_parent})
    elif change == "snapshots":
        images[1] = images[1].model_copy(
            update={
                "snapshots": [
                    *images[1].snapshots,
                    SnapshotKey(image_key=images[1].key, snapshot_id="unknown"),
                ]
            }
        )
    elif change == "consumer":
        images[-1] = images[-1].model_copy(
            update={
                "children": [
                    *images[-1].children,
                    images[-1].key.model_copy(update={"image_id": "foreign"}),
                ]
            }
        )
    elif change == "missing":
        images.pop(1)
    elif change == "copy":
        images[0] = images[0].model_copy(update={"parent": None})
    elif change == "partial":
        images[1] = images[1].model_copy(
            update={"measurement_status": "timeout", "allocated_bytes": None}
        )
    elif change == "duplicate":
        images.insert(0, images[-1].model_copy(update={"snapshots": []}))
    else:
        report = report.model_copy(
            update={
                "cache_bindings": [
                    report.cache_bindings[0].model_copy(
                        update={"volume_handle": "replaced"}
                    )
                ]
            }
        )
    graph = attribute_graph(report_inventory(report), images, clone_lineages=proofs)
    assert graph.projects[0].allocated_bytes is None


def test_legacy_report_digest_omits_absent_clone_references():
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    report = report_fixture("zero-private-parent.json")
    assert "clone_references" not in report.model_dump(mode="json")
    assert inventory_digest(report_inventory(report)) == report.inventory_digest


def test_collector_references_cannot_publish_authority():
    from app.services.native_clone_lineage import validated_lineage

    class Missing:
        def get(self, model, key):
            return None

    with pytest.raises(ValueError, match="verified"):
        validated_lineage(Missing(), uuid4(), "a" * 64)


def managed_case(case):
    """Use the real cache birth and additive enrollment before clone preparation."""
    from app.models.kubevirt_storage import NativeEnrollment
    from app.models.quota import QuotaReservation
    from app.schemas.kubevirt_storage import InitializationTarget
    from app.schemas.kubevirt_storage_enrollment import DomainExtension
    from app.services.kubevirt_storage_enrollment import (
        cache_authorization_digest,
        digest,
        enroll_native_roots,
    )
    from app.services.native_clone_lineage import prepare_clone, record_clone_created
    from tests.test_native_cache_birth import enrollment, published
    from tests.test_native_initialization_evidence import synthetic_profile

    previous = enrollment(case)
    target = InitializationTarget.model_validate(
        {
            **synthetic_profile()["target"],
            "cluster_key": "cluster",
            "ceph_fsid": case.report.pool_keys[0].ceph_fsid,
            "pool_id": 1,
            "provisioner": case.report.volume_bindings[0].csi_driver,
        }
    )
    reference = published(case, target)
    pins = previous.pins.model_copy(update={"cache_birth_receipts": [reference]})
    extension = DomainExtension(
        version="native-domain-extension-v1",
        previous_enrollment_id=case.enrollment_id,
        previous_enrollment_digest=previous.digest,
        cache_authorization_digest=cache_authorization_digest(pins),
    )
    manifest = previous.model_copy(
        update={"pins": pins.model_copy(update={"domain_extension": extension})}
    )
    manifest = manifest.model_copy(
        update={"digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))}
    )
    with case.sessions.begin() as db:
        case.enrollment_id = enroll_native_roots(
            db, manifest, expected_target_id=case.target_id
        )
        case.digest = db.get(NativeEnrollment, case.enrollment_id).digest
    operations = []
    for index, binding in enumerate(case.report.volume_bindings):
        operation_id = str(uuid4())
        with case.sessions.begin() as db:
            db.add(
                QuotaReservation(
                    operation_id=operation_id,
                    project_id=str(binding.root.project_id),
                    owner_id=case.owner_id,
                    organization_id=str(uuid4()),
                    requested={},
                    observed={},
                    state="dispatched",
                    dispatch_metadata={
                        "claim": "original-worker",
                        "generation_id": operation_id,
                        "native_deploy": {
                            "phase": "bound",
                            "profile_digest": digest(target),
                            "payload": {"spec": {"managedPlanDigest": "b" * 64}},
                            "root": binding.root.model_dump(mode="json"),
                            "domain_id": str(pins.domain_id),
                            "provider_id": str(pins.provider_clusters[0].provider_id),
                            "claims": [f"disk-{index}"],
                        },
                    },
                )
            )
        with case.sessions.begin() as db:
            identity = prepare_clone(
                db,
                operation_id=operation_id,
                claim_name=f"disk-{index}",
                cache_reference=reference,
            )
        with case.sessions.begin() as db:
            record_clone_created(
                db,
                identity,
                dict(
                    dv_uid=f"dv-{index}",
                    dv_name=f"disk-{index}",
                    namespace_uid=binding.root.namespace_uid,
                    operation_id=operation_id,
                ),
            )
        operations.append(identity)
    return operations


class CloneAdapter:
    def __init__(self, case, index):
        self.case, self.index = case, index

    def observe_clone(self, intent, creation):
        from app.schemas.kubevirt_storage import SnapshotKey
        from app.schemas.native_clone_lineage import CloneLineage, CloneObservation

        _, _, proofs = chain()
        proof = proofs[self.index].model_copy(
            update={
                "lineage_id": intent.lineage_id,
                "operation_id": intent.operation_id,
                "domain_id": intent.domain_id,
                "target_id": intent.target_id,
                "cache_reference": intent.cache_reference,
                "source": intent.source,
                "intermediate_parent": SnapshotKey(
                    image_key=intent.source.image_key, snapshot_id=str(self.index + 1)
                ),
                "verified_at": datetime.now(UTC),
            }
        )
        return CloneObservation(
            target=intent.target,
            lineage=CloneLineage.model_validate(proof.model_dump()),
        )


def verified_case(case):
    from app.services.native_clone_lineage import validated_lineage, verify_clone

    identities = managed_case(case)
    refs = [
        verify_clone(case.sessions, identity, CloneAdapter(case, index))
        for index, identity in enumerate(identities)
    ]
    with case.sessions() as db:
        proofs = [validated_lineage(db, r.lineage_id, r.digest) for r in refs]
    return clone_report(case, refs, proofs), proofs


def clone_report(case, refs, proofs):
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    report, images, _ = chain()
    old, new = report.cache_bindings[0].image_key, proofs[0].source.image_key
    images = [
        image.model_copy(
            update={
                "key": new if image.key == old else image.key,
                "parent": image.parent.model_copy(update={"image_key": new})
                if image.parent and image.parent.image_key == old
                else image.parent,
                "snapshots": [
                    s.model_copy(update={"image_key": new}) if s.image_key == old else s
                    for s in image.snapshots
                ],
                "collected_at": datetime.now(UTC),
            }
        )
        for image in images
    ]
    now = datetime.now(UTC)
    report = report.model_copy(
        update={
            "report_id": uuid4(),
            "clone_references": refs,
            "images": images,
            "cache_bindings": [proofs[0].source],
            "inventory_blockers": [],
            "started_at": now,
            "completed_at": now,
        }
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    case.report = report
    return report


def test_protected_import_recomputes_private_usage_and_deduplicates_platform(
    clone_case,
):
    from app.services.native_clone_lineage import verify_clone
    from tests.test_kubevirt_storage_reports import ingest, usage

    native_case = clone_case
    report, proofs = verified_case(native_case)
    # Deliberately retain the stale report aggregates: only image observations count.
    report = report.model_copy(update={"projects": []})
    ingest(native_case, report)
    result = usage(native_case, admin=True)
    assert result.allocated_bytes == 80
    assert result.platform_bytes == 100
    assert result.status == "complete"
    again = verify_clone(native_case.sessions, str(proofs[0].lineage_id), None)
    assert again == report.clone_references[0]


@pytest.mark.parametrize(
    "change",
    [
        "forged",
        "foreign",
        "parent",
        "source",
        "snapshots",
        "missing",
        "copy",
        "pool",
        "partial",
        "consumer",
    ],
)
def test_import_and_usage_fail_closed_for_newest_bad_lineage(native_case, change):
    from datetime import timedelta

    from app.schemas.kubevirt_storage import CloneReference, SnapshotKey
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory
    from tests.test_kubevirt_storage_reports import ingest, usage

    report, _ = verified_case(native_case)
    ingest(native_case, report)
    later = report.completed_at + timedelta(microseconds=1)
    changes = dict(report_id=uuid4(), started_at=later, completed_at=later)
    images = list(report.images)
    if change == "forged":
        changes["clone_references"] = [
            CloneReference(lineage_id=uuid4(), digest="f" * 64)
        ]
    elif change == "foreign":
        changes["clone_references"] = [
            report.clone_references[0].model_copy(update={"digest": "f" * 64})
        ]
    elif change == "parent":
        images[1] = images[1].model_copy(update={"parent": images[0].parent})
    elif change == "source":
        changes["cache_bindings"] = [
            report.cache_bindings[0].model_copy(update={"volume_handle": "foreign"})
        ]
    elif change == "snapshots":
        images[-1] = images[-1].model_copy(
            update={
                "snapshots": [
                    *images[-1].snapshots,
                    SnapshotKey(image_key=images[-1].key, snapshot_id="extra"),
                ]
            }
        )
    elif change == "missing":
        images.pop(1)
    elif change == "copy":
        images[0] = images[0].model_copy(update={"parent": None})
    elif change == "pool":
        images[1] = images[1].model_copy(
            update={"key": images[1].key.model_copy(update={"pool_id": 2})}
        )
    elif change == "partial":
        images[1] = images[1].model_copy(
            update={"measurement_status": "timeout", "allocated_bytes": None}
        )
    else:
        images[-1] = images[-1].model_copy(
            update={"children": [*images[-1].children, images[0].key]}
        )
    changes["images"] = images
    report = report.model_copy(update=changes)
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    ingest(native_case, report)
    result = usage(native_case, now=later)
    assert result.allocated_bytes is None
    assert result.status != "complete"


@pytest.mark.parametrize("change", ["dv", "source", "changed_read", "intent", "demand"])
def test_journal_rejects_original_identity_and_observation_changes(native_case, change):
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.models.quota import QuotaReservation
    from app.services.native_clone_lineage import verify_clone

    identity = managed_case(native_case)[0]
    adapter = CloneAdapter(native_case, 0)
    original = adapter.observe_clone
    reads = []

    def observe(intent, creation):
        value = original(intent, creation)
        reads.append(value)
        if change == "dv":
            return value.model_copy(
                update={
                    "lineage": value.lineage.model_copy(
                        update={"original_dv_uid": "replacement"}
                    )
                }
            )
        if change == "source":
            return value.model_copy(
                update={
                    "lineage": value.lineage.model_copy(
                        update={
                            "source": value.lineage.source.model_copy(
                                update={"volume_handle": "replacement"}
                            )
                        }
                    )
                }
            )
        if change == "changed_read" and len(reads) == 2:
            return value.model_copy(
                update={
                    "lineage": value.lineage.model_copy(
                        update={
                            "binding": value.lineage.binding.model_copy(
                                update={"pv_uid": "replacement"}
                            )
                        }
                    )
                }
            )
        return value

    adapter.observe_clone = observe
    if change in {"intent", "demand"}:
        with native_case.sessions.begin() as db:
            row = db.get(NativeCloneLineage, identity)
            if change == "intent":
                row.intent = {**row.intent, "claim_name": "replacement"}
            else:
                operation = (
                    db.query(QuotaReservation)
                    .filter_by(operation_id=row.operation_id)
                    .one()
                )
                operation.requested = {**operation.requested, "storage_bytes": 1}
    with pytest.raises(ValueError):
        verify_clone(native_case.sessions, identity, adapter)
    with native_case.sessions() as db:
        assert db.get(NativeCloneLineage, identity).proof is None


def test_original_creation_is_immutable_and_preparation_reuses_same_operation(
    native_case,
):
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.services.native_clone_lineage import prepare_clone, record_clone_created

    identity = managed_case(native_case)[0]
    with native_case.sessions.begin() as db:
        row = db.get(NativeCloneLineage, identity)
        repeated = prepare_clone(
            db,
            operation_id=row.operation_id,
            claim_name=row.claim_name,
            cache_reference=row.intent["cache_reference"],
        )
        assert repeated == identity
    with native_case.sessions.begin() as db:
        row = db.get(NativeCloneLineage, identity)
        with pytest.raises(ValueError, match="replaced"):
            record_clone_created(
                db, identity, {**row.creation, "dv_uid": "replacement"}
            )
        assert row.proof is None


def test_concurrent_lineage_publication_is_one_retained_proof(clone_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models.native_clone_lineage import NativeCloneLineage
    from app.services.native_clone_lineage import verify_clone

    identity = managed_case(clone_case)[0]
    barrier = Barrier(2)

    def verify():
        adapter = CloneAdapter(clone_case, 0)
        original = adapter.observe_clone

        def observe(intent, creation):
            value = original(intent, creation)
            barrier.wait(timeout=10)
            return value

        adapter.observe_clone = observe
        return verify_clone(clone_case.sessions, identity, adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(lambda _: verify(), range(2)))
    assert refs[0] == refs[1]
    with clone_case.sessions() as db:
        row = db.get(NativeCloneLineage, identity)
        assert row.proof_digest == refs[0].digest
        assert row.creation["dv_uid"] == "dv-0"


def test_private_cache_namespace_intermediate_is_charged_and_not_retained_as_cache(
    native_case,
):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory
    from tests.test_kubevirt_storage_reports import ingest, usage

    report, proofs = verified_case(native_case)
    alias = proofs[0].source.model_copy(
        update={
            "image_key": proofs[0].intermediate,
            "pvc_uid": "temporary-pvc",
            "pv_uid": "temporary-pv",
            "volume_handle": "temporary-handle",
        }
    )
    report = report.model_copy(
        update={"cache_bindings": [*report.cache_bindings, alias]}
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    ingest(native_case, report)
    assert usage(native_case).allocated_bytes == 80
    with native_case.sessions() as db:
        assert not any(
            r.payload.get("pvc_uid") == "temporary-pvc"
            for r in db.query(NativeRetainedBinding)
        )


def test_preproof_import_does_not_leave_retained_temporary_cache_after_publication(
    native_case,
):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.schemas.native_clone_lineage import CloneCreation, CloneIntent
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_clone_lineage import validated_lineage, verify_clone
    from tests.test_kubevirt_storage_reports import ingest, usage

    identities = managed_case(native_case)
    with native_case.sessions() as db:
        pending = [db.get(NativeCloneLineage, identity) for identity in identities]
        assert all(row.proof is None for row in pending)
        # These report observations have no terminal authority yet.
        observed = [
            CloneAdapter(native_case, index)
            .observe_clone(
                CloneIntent.model_validate(row.intent),
                CloneCreation.model_validate(row.creation),
            )
            .lineage
            for index, row in enumerate(pending)
        ]
    alias = observed[0].source.model_copy(
        update={
            "image_key": observed[0].intermediate,
            "pvc_uid": "temporary-pvc",
            "pv_uid": "temporary-pv",
            "volume_handle": "temporary-handle",
        }
    )

    def with_temporary(report):
        report = report.model_copy(
            update={"cache_bindings": [*report.cache_bindings, alias]}
        )
        return report.model_copy(
            update={"inventory_digest": inventory_digest(report_inventory(report))}
        )

    preproof = with_temporary(clone_report(native_case, [], observed))
    ingest(native_case, preproof)
    pending_usage = usage(native_case, admin=True)
    assert pending_usage.allocated_bytes is None
    assert pending_usage.platform_bytes is None
    assert "snapshot_ancestry_unverified" in pending_usage.reason_codes
    with native_case.sessions() as db:
        caches = [
            r.payload
            for r in db.query(NativeRetainedBinding).filter_by(root_key="cache")
        ]
        assert caches == [observed[0].source.model_dump(mode="json")]
        assert all(
            db.get(NativeCloneLineage, identity).proof is None
            for identity in identities
        )

    refs = [
        verify_clone(native_case.sessions, identity, CloneAdapter(native_case, index))
        for index, identity in enumerate(identities)
    ]
    with native_case.sessions() as db:
        proofs = [validated_lineage(db, r.lineage_id, r.digest) for r in refs]
    ingest(native_case, with_temporary(clone_report(native_case, refs, proofs)))
    assert usage(native_case, admin=True).allocated_bytes == 80
    # CDI removes its temporary PVC; the intermediate backend image stays charged.
    disappeared = clone_report(native_case, refs, proofs)
    ingest(native_case, disappeared)
    result = usage(native_case, admin=True)
    assert result.status == "complete"
    assert result.allocated_bytes == 80
    assert result.platform_bytes == 100
    assert "retained_binding_missing" not in result.reason_codes

    # Filtering persistence must not filter unknown current graph evidence.
    unknown = alias.model_copy(
        update={
            "image_key": alias.image_key.model_copy(
                update={"image_id": "unknown-cache-image"}
            ),
            "pvc_uid": "unknown-cache-pvc",
        }
    )
    report = clone_report(native_case, refs, proofs)
    report = report.model_copy(
        update={"cache_bindings": [*report.cache_bindings, unknown]}
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    ingest(native_case, report)
    assert usage(native_case, admin=True).platform_bytes is None
    with native_case.sessions() as db:
        caches = [
            r.payload
            for r in db.query(NativeRetainedBinding).filter_by(root_key="cache")
        ]
        assert caches == [proofs[0].source.model_dump(mode="json")]


def test_proven_settlement_retires_only_its_clone_snapshot_requirement(native_case):
    from app.models.project import Project
    from app.models.quota import QuotaReservation
    from app.schemas.kubevirt_storage import NativeCleanupIntent
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory
    from app.services.native_clone_lineage import resolve_lineages
    from tests.test_kubevirt_storage_reports import ingest, usage
    from tests.test_native_cache_birth import enrollment

    report, proofs = verified_case(native_case)
    ingest(native_case, report)
    retired, remaining = proofs
    with native_case.sessions.begin() as db:
        operation = (
            db.query(QuotaReservation)
            .filter_by(operation_id=retired.operation_id)
            .one()
        )
        project = db.get(Project, str(retired.binding.root.project_id))
        now = datetime.now(UTC)
        # This is the existing protected cleanup producer's terminal contract.
        intent = NativeCleanupIntent(
            intent_id=uuid4(),
            operation_id=operation.operation_id,
            operation_ids=[operation.operation_id],
            generation=operation.operation_id,
            mode="delete",
            namespace_mode="managed",
            phase="settled",
            owner_id=operation.owner_id,
            organization_id=operation.organization_id,
            host_id=project.host_id,
            provider_id=native_case.pins.provider_clusters[0].provider_id,
            domain_id=retired.domain_id,
            enrollment_id=native_case.enrollment_id,
            initial_report_id=report.report_id,
            root=retired.binding.root,
            owned_images=[retired.binding.image_key, retired.intermediate],
            bindings=[retired.binding],
            stop_action_id="settled-stop",
            prepared_at=now,
            settlement_report_id=report.report_id,
            settled_at=now,
        )
        operation.dispatch_metadata = {
            **operation.dispatch_metadata,
            "native_cleanup": intent.model_dump(mode="json"),
        }
    with native_case.sessions() as db:
        assert resolve_lineages(
            db, report.clone_references, enrollment(native_case)
        ) == [remaining]
    gone = {retired.binding.image_key, retired.intermediate}
    images = [
        i.model_copy(
            update={
                "children": [k for k in i.children if k not in gone],
                "snapshots": [
                    s for s in i.snapshots if s != retired.intermediate_parent
                ],
                "collected_at": datetime.now(UTC),
            }
        )
        for i in report.images
        if i.key not in gone
    ]
    now = datetime.now(UTC)
    report = report.model_copy(
        update={
            "report_id": uuid4(),
            "started_at": now,
            "completed_at": now,
            "project_roots": [remaining.binding.root],
            "volume_bindings": [remaining.binding],
            "images": images,
        }
    )
    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    ingest(native_case, report)
    result = usage(
        native_case,
        scope="project",
        identity=str(remaining.binding.root.project_id),
        now=now,
    )
    assert result.status == "complete"
    assert result.allocated_bytes == 50


@pytest.mark.parametrize("database", ["sqlite", "postgres"])
def test_clone_journal_migration_roundtrip(request, database):
    import importlib.util
    import os
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = (
        Path(__file__).parents[1]
        / "alembic/versions/c83a6712fd90_native_clone_lineages.py"
    )
    spec = importlib.util.spec_from_file_location("clone_lineage_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    if database == "postgres":
        if not os.getenv("QUOTA_TEST_POSTGRES_URL"):
            pytest.skip("disposable PostgreSQL URL not configured")
        _, _, engine = request.getfixturevalue("postgres")
    else:
        engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        if database == "postgres":
            migration.downgrade()
        migration.upgrade()
        assert "native_clone_lineages" in inspect(connection).get_table_names()
        assert inspect(connection).get_foreign_keys("native_clone_lineages") == []
        migration.downgrade()
        assert "native_clone_lineages" not in inspect(connection).get_table_names()
    if database == "sqlite":
        engine.dispose()
