"""Native import -> explicit ledger reconciliation -> durable overage evaluation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import QuotaAction, QuotaAllocation, QuotaPolicy, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_enforcement as enforcement
from app.services import quota_native as native
from app.services import quota_operations as operations
from app.services import quota_storage as storage
from app.services.kubevirt_storage_enrollment import (
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
)
from app.services.kubevirt_storage_inventory import inventory_digest
from app.services.kubevirt_storage_reports import import_report, report_inventory
from tests.test_kubevirt_storage_reports import make_native_case
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture

NOW = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)


def revised(report, **updates):
    value = report.model_copy(update={"report_id": uuid4(), **updates})
    return value.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(value))}
    )


@pytest.fixture
def native_ledger(request, monkeypatch):
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: 0)
    monkeypatch.setattr(operations, "now", lambda: NOW)
    monkeypatch.setattr(enforcement, "_now", lambda: NOW)
    from tests.kubevirt_storage_helpers import report_fixture

    report = report_fixture("zero-private-parent.json")
    roots = [
        r.model_copy(update={"deployment_generation": 7}) for r in report.project_roots
    ]
    report = revised(
        report,
        project_roots=roots,
        volume_bindings=[
            b.model_copy(update={"root": r})
            for b, r in zip(report.volume_bindings, roots)
        ],
    )
    q = make_native_case(
        request.getfixturevalue("isolated_quota_database"),
        report=report,
        organization=True,
    )
    with q.sessions() as db:
        q.project_ids = [str(r.project_id) for r in roots]
        for index, root in enumerate(roots):
            project = db.get(Project, str(root.project_id))
            project.state = "active"
            vector = Resources(
                cpu=2, ram_bytes=4096, projects=1, storage_bytes=10
            ).model_dump()
            operation_id = f"native-deploy-{index}"
            db.add_all(
                [
                    QuotaReservation(
                        operation_id=operation_id,
                        project_id=project.id,
                        owner_id=q.owner_id,
                        organization_id=q.org_id,
                        state="confirmed",
                        requested=vector,
                        observed=vector,
                        observation_id="native-compute",
                        dispatch_metadata={
                            "adapter": native.ADAPTER,
                            "kind": "deploy",
                            "generation_id": operation_id,
                            "worker_finished": True,
                            "worker_finished_at": (
                                q.report.started_at - timedelta(seconds=1)
                            ).isoformat(),
                            "native_storage_identity": {
                                "domain_id": str(q.pins.domain_id),
                                "enrollment_id": q.enrollment_id,
                                "host_id": project.host_id,
                                "provider_id": str(
                                    q.request.projects[index].provider_id
                                ),
                                "root": root.model_dump(mode="json"),
                            },
                        },
                    ),
                    QuotaAllocation(
                        operation_id=operation_id,
                        project_id=project.id,
                        owner_id=q.owner_id,
                        organization_id=q.org_id,
                        state="active",
                        requested=vector,
                        observed=vector,
                        observation_id="native-compute",
                        observed_at=q.report.started_at - timedelta(seconds=1),
                    ),
                ]
            )
        db.commit()
    return q


def ingest(q, report=None):
    with q.sessions() as db:
        identity = import_report(
            db,
            report or q.report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=NOW,
        )
        db.commit()
        return identity


def reconcile(q):
    for project_id in q.project_ids:
        storage.reconcile_project(project_id)


def root_row(db, q, index=0):
    return db.query(QuotaReservation).filter_by(project_id=q.project_ids[index]).one()


@pytest.mark.parametrize(
    "failure", [None, "tenant", "ancestor", "lineage", "collection"]
)
def test_unreferenced_unknown_platform_storage_does_not_gate_tenant_measurement(
    native_ledger, failure
):
    from app.models.kubevirt_storage import NativeProjectRoot, NativeRetainedBinding
    from app.schemas.kubevirt_storage import Blocker, CloneReference
    from app.services.kubevirt_storage_usage import domain_evidence

    q = native_ledger
    source = q.report.cache_bindings[0]
    scratch = source.model_copy(
        update={
            "pvc_uid": "transient-scratch",
            "pv_uid": "scratch-pv",
            "volume_handle": "scratch-handle",
            "image_key": source.image_key.model_copy(update={"image_id": "scratch"}),
        }
    )
    unavailable = q.report.images[0].model_copy(
        update={
            "key": scratch.image_key,
            "parent": None,
            "snapshots": [],
            "children": [],
            "measurement_status": "unavailable",
            "allocated_bytes": None,
            "provisioned_bytes": None,
            "measurement_semantics_version": None,
        }
    )
    images = [*q.report.images, unavailable]
    if failure in {"tenant", "ancestor"}:
        target = (
            source.image_key
            if failure == "ancestor"
            else q.report.volume_bindings[1].image_key
        )
        images = [
            i.model_copy(
                update={
                    "measurement_status": "unavailable",
                    "allocated_bytes": None,
                    "measurement_semantics_version": None,
                }
            )
            if i.key == target
            else i
            for i in images
        ]
    report = revised(
        q.report,
        images=images,
        cache_bindings=[*q.report.cache_bindings, scratch],
        clone_references=[CloneReference(lineage_id=uuid4(), digest="a" * 64)]
        if failure == "lineage"
        else [],
        collection_errors=[Blocker(code="inventory_recheck_unavailable")]
        if failure == "collection"
        else [],
    )
    ingest(q, report)
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
        graph, _, errors = domain_evidence(db, domain, roots)
        if failure is None:
            assert not errors
            assert all(p.coverage == "complete" for p in graph.projects)
            assert graph.platform_pools[0].allocated_bytes is None
        if failure is None:
            assert any(
                b.payload.get("pvc_uid") == scratch.pvc_uid
                for b in db.query(NativeRetainedBinding)
            )
        row = root_row(db, q)
        args = (
            db,
            db.get(Project, q.project_ids[0]),
            row.operation_id,
            row.dispatch_metadata["native_storage_identity"],
            NOW,
        )
        if failure is None:
            assert native.storage_measurement(*args).allocated_bytes == 30
        else:
            with pytest.raises(AccountingUnavailable):
                native.storage_measurement(*args)


def extension_manifest(db, q, *, previous=None):
    """Trusted synthetic extension, never a physical initialization profile."""
    from app.schemas.kubevirt_storage import ProjectRoot
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    previous = previous or db.get(NativeEnrollment, q.enrollment_id)
    manifest = VerifiedEnrollment.model_validate(previous.payload)
    project = Project(
        name="new-draft",
        owner_id=q.owner_id,
        host_id=db.get(Project, q.project_ids[0]).host_id,
    )
    db.add(project)
    db.flush()
    root = ProjectRoot(
        project_id=project.id,
        cluster_key="cluster",
        namespace="new-draft",
        namespace_uid="new-namespace-" + project.id,
        project_cr_uid="new-cr-" + project.id,
        deployment_generation=1,
    )
    request = prepare_native_enrollment(
        db, [str(r.project_id) for r in manifest.pins.project_roots] + [project.id]
    )
    payload = manifest.model_dump(mode="json", exclude={"digest"})
    payload["request"] = request.model_dump(mode="json")
    payload["pins"]["project_roots"].append(root.model_dump(mode="json"))
    payload["pins"]["domain_extension"] = {
        "version": "native-domain-extension-v1",
        "previous_enrollment_id": previous.id,
        "previous_enrollment_digest": previous.digest,
    }
    return (
        VerifiedEnrollment.model_validate({**payload, "digest": digest(payload)}),
        root,
    )


def test_expanded_domain_retains_old_allocations_until_complete_current_report(
    native_ledger,
):
    from copy import deepcopy

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        original = {
            r.operation_id: deepcopy(r.dispatch_metadata)
            for r in db.query(QuotaReservation)
        }
        manifest, new_root = extension_manifest(db, q)
        enrollment_id = enroll_native_roots(
            db, manifest, expected_target_id=q.target_id
        )
        db.commit()
        assert admission.usage(db, "user", q.owner_id)[0].storage_bytes == 80
        assert {
            r.operation_id: r.dispatch_metadata for r in db.query(QuotaReservation)
        } == original
        with pytest.raises(AccountingUnavailable):
            storage.scope_evidence(db, "user", q.owner_id, NOW)
    with pytest.raises(AccountingUnavailable):
        reconcile(q)
    report = revised(
        q.report,
        project_roots=[*q.report.project_roots, new_root],
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        images=[
            i.model_copy(update={"collected_at": i.collected_at + timedelta(seconds=1)})
            for i in q.report.images
        ],
    )
    q.enrollment_id, q.digest = enrollment_id, manifest.digest
    ingest(q, report)
    reconcile(q)
    with q.sessions() as db:
        storage.scope_evidence(db, "user", q.owner_id, NOW)
        assert admission.usage(db, "user", q.owner_id)[0].storage_bytes == 80
        for row in db.query(QuotaReservation):
            assert (
                row.dispatch_metadata["native_storage_identity"]
                == original[row.operation_id]["native_storage_identity"]
            )
            assert (
                row.dispatch_metadata["native_storage_evidence"][
                    "current_enrollment_id"
                ]
                == enrollment_id
            )
    from app.schemas.kubevirt_storage import Blocker

    ingest(
        q,
        revised(
            report,
            started_at=report.started_at + timedelta(seconds=1),
            completed_at=report.completed_at + timedelta(seconds=1),
            collection_errors=[Blocker(code="backend_graph_recheck_unavailable")],
        ),
    )
    ingest(q, revised(report))
    with pytest.raises(AccountingUnavailable):
        reconcile(q)
    with q.sessions() as db:
        assert admission.usage(db, "user", q.owner_id)[0].storage_bytes == 80


@pytest.mark.parametrize(
    "failure",
    ["root", "pool", "collector", "cache", "provider", "predecessor", "owner"],
)
def test_domain_extension_rejects_identity_loss_or_preparation_drift(
    native_ledger, failure
):
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    q = native_ledger
    with q.sessions() as db:
        manifest, _ = extension_manifest(db, q)
        payload = manifest.model_dump(mode="json", exclude={"digest"})
        if failure in {"root", "pool", "collector", "cache", "provider"}:
            key = {
                "root": "project_roots",
                "pool": "pool_keys",
                "collector": "collector_ids",
                "cache": "cache_roots",
                "provider": "provider_clusters",
            }[failure]
            payload["pins"][key] = payload["pins"][key][1:]
            # Keep nonempty schema lists valid while replacing an identity.
            if not payload["pins"][key] and failure != "cache":
                payload["pins"][key] = manifest.model_dump(mode="json")["pins"][key]
                if failure == "pool":
                    payload["pins"][key][0]["pool_id"] = 99
                elif failure == "collector":
                    payload["pins"][key][0] = "foreign-collector"
                elif failure == "provider":
                    payload["pins"][key][0]["cluster_key"] = "foreign-cluster"
        elif failure == "predecessor":
            payload["pins"]["domain_extension"]["previous_enrollment_id"] = str(uuid4())
        else:
            db.get(Project, q.project_ids[0]).owner_id = q.admin_id
            db.flush()
        changed = VerifiedEnrollment.model_validate(
            {**payload, "digest": digest(payload)}
        )
        with pytest.raises(ValueError):
            enroll_native_roots(db, changed, expected_target_id=q.target_id)


def test_domain_extension_replay_and_competing_predecessor_preserve_history(
    native_ledger,
):
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    q = native_ledger
    ingest(q)
    with q.sessions() as db:
        old = db.get(NativeEnrollment, q.enrollment_id)
        # Legacy evidence roundtrips byte-for-byte at the JSON/digest boundary.
        assert (
            VerifiedEnrollment.model_validate(old.payload).model_dump(mode="json")
            == old.payload
        )
        assert "domain_extension" not in old.payload["pins"]
        original_payload = old.payload
        first, _ = extension_manifest(db, q)
        second, _ = extension_manifest(db, q)
        new_id = enroll_native_roots(db, first, expected_target_id=q.target_id)
        assert enroll_native_roots(db, first, expected_target_id=q.target_id) == new_id
        with pytest.raises(ValueError, match="no longer current"):
            enroll_native_roots(db, second, expected_target_id=q.target_id)
        assert db.get(NativeEnrollment, q.enrollment_id).payload == original_payload
        assert db.query(NativeStorageSnapshot).count() == 1
        assert (
            db.query(NativeEnrollment).count() == 2
        )  # initial enrollment + explicit extension


@pytest.mark.parametrize("advance_first", [False, True])
def test_omitted_extension_cannot_advance_or_replace_current_domain(
    native_ledger, advance_first
):
    from copy import deepcopy

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectRoot
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        first, _ = extension_manifest(db, q)
        omitted, _ = extension_manifest(db, q)
        body = omitted.model_dump(mode="json", exclude={"digest"})
        body["pins"].pop("domain_extension")
        omitted = VerifiedEnrollment.model_validate({**body, "digest": digest(body)})
        if advance_first:
            enroll_native_roots(db, first, expected_target_id=q.target_id)
        domain = db.get(NativeCollectionDomain, str(first.pins.domain_id))
        pointer = domain.enrollment_id
        roots = {r.key for r in db.query(NativeProjectRoot)}
        original = {
            r.operation_id: deepcopy(r.dispatch_metadata)
            for r in db.query(QuotaReservation)
        }
        count = db.query(NativeEnrollment).count()
        with pytest.raises(ValueError, match="explicit domain extension"):
            enroll_native_roots(db, omitted, expected_target_id=q.target_id)
        assert domain.enrollment_id == pointer
        assert {r.key for r in db.query(NativeProjectRoot)} == roots
        assert db.query(NativeEnrollment).count() == count
        assert {
            r.operation_id: r.dispatch_metadata for r in db.query(QuotaReservation)
        } == original
        assert admission.usage(db, "user", q.owner_id)[0].storage_bytes == 80


def test_domain_extension_does_not_revert_an_authorized_org_transfer(native_ledger):
    q = native_ledger
    with q.sessions() as db:
        destination = ManagedGroup(
            name="transferred", normalized_name="transferred", is_organization=True
        )
        db.add(destination)
        db.flush()
        db.get(User, q.owner_id).organization_id = destination.id
        for model in (QuotaReservation, QuotaAllocation):
            for row in db.query(model).filter_by(owner_id=q.owner_id):
                row.organization_id = destination.id
        manifest, _ = extension_manifest(db, q)
        enroll_native_roots(db, manifest, expected_target_id=q.target_id)
        assert all(
            row.organization_id == destination.id for row in db.query(QuotaReservation)
        )


@pytest.mark.parametrize(
    "failure", ["missing", "digest", "cycle", "root", "pool", "semantics"]
)
def test_malformed_extension_chain_never_equates_old_enrollment(native_ledger, failure):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services.kubevirt_storage_enrollment import retained_enrollment
    from tests.test_native_initialization_evidence import synthetic_profile

    q = native_ledger
    with q.sessions() as db:
        identity = root_row(db, q).dispatch_metadata["native_storage_identity"]
        manifest, _ = extension_manifest(db, q)
        new_id = enroll_native_roots(db, manifest, expected_target_id=q.target_id)
        row = db.get(NativeEnrollment, new_id)
        payload = row.payload.copy()
        if failure == "missing":
            payload["pins"] = {
                k: v for k, v in payload["pins"].items() if k != "domain_extension"
            }
        elif failure == "digest":
            payload["digest"] = "b" * 64
        elif failure == "cycle":
            payload["pins"] = {
                **payload["pins"],
                "domain_extension": {
                    **payload["pins"]["domain_extension"],
                    "previous_enrollment_id": new_id,
                },
            }
        elif failure == "root":
            payload["pins"] = {
                **payload["pins"],
                "project_roots": [
                    {**root, "namespace_uid": "changed"}
                    if root["project_id"] == q.project_ids[0]
                    else root
                    for root in payload["pins"]["project_roots"]
                ],
            }
        elif failure == "pool":
            payload["pins"] = {
                **payload["pins"],
                "pool_keys": [{"ceph_fsid": "foreign", "pool_id": 1}],
            }
        else:
            payload["pins"] = {
                **payload["pins"],
                "initialization_evidence": synthetic_profile(),
            }
        if failure != "digest":
            payload["digest"] = digest(
                {k: v for k, v in payload.items() if k != "digest"}
            )
        row.payload, row.digest = payload, payload["digest"]
        db.flush()
        with pytest.raises(ValueError):
            retained_enrollment(
                db, db.get(NativeCollectionDomain, identity["domain_id"]), identity
            )


def test_extension_depth_limit_rejects_before_advancing_domain(native_ledger):
    from app.models.kubevirt_storage import NativeCollectionDomain

    q = native_ledger
    with q.sessions() as db:
        original = root_row(db, q).dispatch_metadata["native_storage_identity"]
        previous = db.get(NativeEnrollment, q.enrollment_id)
        for _ in range(63):
            manifest, _ = extension_manifest(db, q, previous=previous)
            current_id = enroll_native_roots(
                db, manifest, expected_target_id=q.target_id
            )
            previous = db.get(NativeEnrollment, current_id)
        manifest, _ = extension_manifest(db, q, previous=previous)
        with pytest.raises(ValueError, match="depth"):
            enroll_native_roots(db, manifest, expected_target_id=q.target_id)
        assert (
            db.get(NativeCollectionDomain, original["domain_id"]).enrollment_id
            == previous.id
        )
        assert root_row(db, q).observed["storage_bytes"] == 10


def test_explicit_profile_authorization_preserves_old_measurement_identity(
    native_ledger,
):
    from copy import deepcopy

    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from tests.test_native_initialization_evidence import synthetic_profile

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        old = db.get(NativeEnrollment, q.enrollment_id)
        original = deepcopy(old.payload)
        identity = deepcopy(
            root_row(db, q).dispatch_metadata["native_storage_identity"]
        )
        payload = deepcopy(original)
        payload["request"] = prepare_native_enrollment(db, q.project_ids).model_dump(
            mode="json"
        )
        profile = synthetic_profile()
        profile["target"].update(
            provider_id=str(q.request.projects[0].provider_id),
            cluster_key="cluster",
            ceph_fsid=q.pins.pool_keys[0].ceph_fsid,
        )
        payload["pins"]["initialization_evidence"] = profile
        payload["pins"]["domain_extension"] = {
            "version": "native-domain-extension-v1",
            "previous_enrollment_id": old.id,
            "previous_enrollment_digest": old.digest,
            "initialization_authorization_digest": digest(profile),
        }
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        manifest = VerifiedEnrollment.model_validate(payload)
        new_id = enroll_native_roots(db, manifest, expected_target_id=q.target_id)
        db.commit()
        assert db.get(NativeEnrollment, q.enrollment_id).payload == original
        assert root_row(db, q).dispatch_metadata["native_storage_identity"] == identity
    q.enrollment_id, q.digest = new_id, manifest.digest
    later = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        images=[
            i.model_copy(update={"collected_at": i.collected_at + timedelta(seconds=1)})
            for i in q.report.images
        ],
    )
    ingest(q, later)
    reconcile(q)
    with q.sessions() as db:
        assert root_row(db, q).observed["storage_bytes"] == 30
        assert root_row(db, q).dispatch_metadata["native_storage_identity"] == identity


@pytest.mark.parametrize("change", ["unreviewed", "wrong_digest", "unchanged"])
def test_root_expansion_cannot_promote_or_reuse_initialization_authority(
    native_ledger, change
):
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from tests.test_native_initialization_evidence import synthetic_profile

    q = native_ledger
    with q.sessions() as db:
        manifest, _ = extension_manifest(db, q)
        payload = manifest.model_dump(mode="json")
        profile = synthetic_profile()
        profile["target"].update(
            provider_id=str(q.request.projects[0].provider_id),
            cluster_key="cluster",
            ceph_fsid=q.pins.pool_keys[0].ceph_fsid,
        )
        if change != "unchanged":
            payload["pins"]["initialization_evidence"] = profile
        if change != "unreviewed":
            payload["pins"]["domain_extension"][
                "initialization_authorization_digest"
            ] = ("b" * 64)
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        with pytest.raises(ValueError, match="initialization"):
            enroll_native_roots(
                db,
                VerifiedEnrollment.model_validate(payload),
                expected_target_id=q.target_id,
            )


def test_model_copy_cannot_bypass_strict_initialization_bound_validation(native_ledger):
    from app.schemas.kubevirt_storage_initialization import InitializationEvidence
    from tests.test_native_initialization_evidence import synthetic_profile

    q = native_ledger
    with q.sessions() as db:
        manifest, _ = extension_manifest(db, q)
        supplied = synthetic_profile()
        supplied["target"].update(
            provider_id=str(q.request.projects[0].provider_id),
            cluster_key="cluster",
            ceph_fsid=q.pins.pool_keys[0].ceph_fsid,
        )
        invalid = InitializationEvidence.model_validate(supplied).model_copy(
            update={"minimum_capacity_bytes": 0}
        )
        extension = manifest.pins.domain_extension.model_copy(
            update={"initialization_authorization_digest": digest(invalid)}
        )
        pins = manifest.pins.model_copy(
            update={"initialization_evidence": invalid, "domain_extension": extension}
        )
        manifest = manifest.model_copy(update={"pins": pins})
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        with pytest.raises(ValueError):
            enroll_native_roots(db, manifest, expected_target_id=q.target_id)


def test_import_reconcile_and_evaluate_exact_boundary_idempotently(native_ledger):
    q = native_ledger
    report_id = ingest(q)
    with q.sessions() as db:
        assert admission.usage(db, "user", q.owner_id)[0].storage_bytes == 20
        with pytest.raises(AccountingUnavailable, match="not been reconciled"):
            storage.scope_evidence(db, "user", q.owner_id, NOW)
    reconcile(q)
    with q.sessions() as db:
        assert admission.usage(db, "user", q.owner_id)[0] == Resources(
            cpu=4, ram_bytes=8192, projects=2, storage_bytes=80
        )
        root = root_row(db, q)
        assert root.dispatch_metadata["storage_version"] == native.MEASUREMENT_VERSION
        evidence = root.dispatch_metadata["native_storage_evidence"]
        assert evidence["metric"] == native.METRIC
        assert evidence["allocated_bytes"] == 30
        assert evidence["report_id"] == report_id
        assert (
            "files" not in evidence and "storage_evidence" not in root.dispatch_metadata
        )
        db.add(
            QuotaPolicy(scope="project", scope_id=q.project_ids[0], storage_bytes=30)
        )
        db.commit()
        enforcement.evaluate(db, NOW)
        enforcement.evaluate(db, NOW)
        db.commit()
        cause = db.query(QuotaAction).filter_by(kind="stop").one()
        assert cause.details == {
            "reason": "physical_storage_limit",
            "used_bytes": 30,
            "limit_bytes": 30,
        }
        assert db.query(QuotaAction).filter_by(kind="stop_run").count() == 1
    assert ingest(q) == report_id
    reconcile(q)
    with q.sessions() as db:
        assert db.query(QuotaAllocation).count() == 2
        root = root_row(db, q)
        assert not storage.apply_measurement(
            db, db.get(Project, root.project_id), [root], None, NOW
        )
        enforcement.evaluate(db, NOW)
        assert db.query(QuotaAction).count() == 2


def test_latest_partial_blocks_new_decisions_but_retains_proven_cause(native_ledger):
    from app.schemas.kubevirt_storage import Blocker

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        db.add(
            QuotaPolicy(scope="project", scope_id=q.project_ids[0], storage_bytes=30)
        )
        db.commit()
        enforcement.evaluate(db, NOW)
        db.commit()
        cause_id = db.query(QuotaAction).filter_by(kind="stop").one().id
    partial = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        collection_errors=[Blocker(code="backend_graph_recheck_unavailable")],
    )
    ingest(q, partial)
    ingest(q, revised(q.report))  # Late complete evidence cannot resurrect old usage.
    with pytest.raises(AccountingUnavailable):
        reconcile(q)
    with q.sessions() as db:
        with pytest.raises(AccountingUnavailable):
            storage.scope_evidence(db, "organization", q.org_id, NOW)
        db.add(QuotaPolicy(scope="project", scope_id=q.project_ids[1], storage_bytes=1))
        db.commit()
        enforcement.evaluate(db, NOW)
        db.commit()
        assert db.query(QuotaAction).filter_by(kind="stop").count() == 1
        assert db.get(QuotaAction, cause_id).state == "pending"
        assert root_row(db, q).observed["storage_bytes"] == 30


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "future",
        "host",
        "provider",
        "owner",
        "organization",
        "generation",
        "namespace",
        "enrollment",
        "conflict",
    ],
)
def test_changed_or_unavailable_identities_cannot_replace_storage(
    native_ledger, failure
):
    from app.models.host import Host
    from app.models.provider import Provider

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        root, project = root_row(db, q), db.get(Project, q.project_ids[0])
        identity = dict(root.dispatch_metadata["native_storage_identity"])
        clock = NOW
        if failure == "stale":
            clock += timedelta(minutes=16)
        elif failure == "future":
            clock = q.report.started_at - timedelta(seconds=1)
        elif failure == "host":
            project.host_id = None
        elif failure == "provider":
            db.get(Provider, db.get(Host, project.host_id).provider_id).type = "other"
        elif failure == "owner":
            project.owner_id = q.admin_id
        elif failure == "organization":
            root.organization_id = str(uuid4())
        elif failure in {"generation", "namespace"}:
            field = (
                "deployment_generation" if failure == "generation" else "namespace_uid"
            )
            identity["root"] = {
                **identity["root"],
                field: 8 if failure == "generation" else "changed",
            }
        elif failure == "enrollment":
            identity["enrollment_id"] = str(uuid4())
        elif failure == "conflict":
            db.get(NativeCollectionDomain, identity["domain_id"]).conflicted = True
        root.dispatch_metadata = {
            **root.dispatch_metadata,
            "native_storage_identity": identity,
        }
        db.flush()
        with pytest.raises(AccountingUnavailable):
            storage.apply_measurement(db, project, [root], None, clock)
        assert root.observed["storage_bytes"] == 30


@pytest.mark.parametrize("source", ["root", "binding"])
def test_report_generation_must_match_retained_enrollment(native_ledger, source):
    q = native_ledger
    changes = {"deployment_generation": 8}
    root = q.report.project_roots[0].model_copy(update=changes)
    report = revised(
        q.report,
        **(
            {"project_roots": [root, q.report.project_roots[1]]}
            if source == "root"
            else {
                "volume_bindings": [
                    q.report.volume_bindings[0].model_copy(update={"root": root}),
                    q.report.volume_bindings[1],
                ]
            }
        ),
    )
    ingest(q, report)
    with pytest.raises(AccountingUnavailable, match="generation"):
        storage.reconcile_project(q.project_ids[0])


def test_snapshot_ancestry_guard_cannot_be_bypassed_by_complete_aggregates(
    native_ledger,
):
    from tests.kubevirt_storage_helpers import with_retained_snapshot

    q = native_ledger
    ingest(q, with_retained_snapshot(q.report))
    with pytest.raises(AccountingUnavailable, match="coverage is incomplete"):
        reconcile(q)
    with q.sessions() as db:
        assert root_row(db, q).observed["storage_bytes"] == 10


def test_unknown_pool_or_wrong_binding_never_updates_allocation(native_ledger):
    q = native_ledger
    binding = q.report.volume_bindings[0]
    bad = binding.model_copy(
        update={"image_key": binding.image_key.model_copy(update={"pool_id": 99})}
    )
    ingest(q, revised(q.report, volume_bindings=[bad, q.report.volume_bindings[1]]))
    with pytest.raises(AccountingUnavailable):
        reconcile(q)


@pytest.mark.parametrize("missing", ["other_root", "pool"])
def test_incomplete_domain_inventory_cannot_certify_target_subtotal(
    native_ledger, missing
):
    q = native_ledger
    changes = (
        {"pool_keys": []}
        if missing == "pool"
        else {
            "project_roots": q.report.project_roots[:1],
            "volume_bindings": q.report.volume_bindings[:1],
        }
    )
    ingest(q, revised(q.report, **changes))
    with pytest.raises(AccountingUnavailable, match="inventory is incomplete"):
        storage.reconcile_project(q.project_ids[0])


def test_older_report_cannot_roll_back_reconciled_accounting(native_ledger):
    q = native_ledger
    original_id = ingest(q)
    later = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        images=[
            i.model_copy(
                update={
                    "collected_at": i.collected_at + timedelta(seconds=1),
                    "allocated_bytes": 40
                    if i.key.image_id == "child-1"
                    else i.allocated_bytes,
                }
            )
            for i in q.report.images
        ],
    )
    ingest(q, later)
    reconcile(q)
    with q.sessions() as db:
        root = root_row(db, q)
        domain = db.get(
            NativeCollectionDomain,
            root.dispatch_metadata["native_storage_identity"]["domain_id"],
        )
        # Simulate a stale snapshot/attempt pointer; service must retain the ledger.
        domain.latest_report_id, domain.latest_started_at = (
            original_id,
            q.report.started_at,
        )
        db.commit()
    with pytest.raises(AccountingUnavailable, match="roll back"):
        storage.reconcile_project(q.project_ids[0])
    with q.sessions() as db:
        assert root_row(db, q).observed["storage_bytes"] == 40


def test_native_and_nested_scope_evidence_each_remains_required(native_ledger):
    from app.models.host import Host
    from tests.test_quota_storage import evidence

    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        host = Host()
        db.add(host)
        db.flush()
        project = Project(
            name="nested",
            owner_id=q.owner_id,
            host_id=host.id,
        )
        db.add(project)
        db.flush()
        source = evidence(project)
        source["observed_at"] = NOW.isoformat()
        vector = Resources(projects=1, storage_bytes=4096).model_dump()
        row = QuotaReservation(
            operation_id="nested",
            project_id=project.id,
            owner_id=q.owner_id,
            organization_id=q.org_id,
            state="confirmed",
            observed=vector,
            requested=vector,
            dispatch_metadata={
                "kind": "deploy",
                "generation_id": "deploy",
                "storage_host_id": project.host_id,
                "storage_evidence": source,
                "storage_measured_at": NOW.isoformat(),
            },
        )
        db.add(row)
        db.flush()
        storage.scope_evidence(db, "user", q.owner_id, NOW)
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "storage_measured_at": "wrong",
        }
        with pytest.raises(AccountingUnavailable, match="not been measured"):
            storage.scope_evidence(db, "user", q.owner_id, NOW)


@pytest.mark.parametrize("drift", ["missing", "owner", "vector"])
def test_retained_ledger_drift_is_not_authoritative_native_usage(native_ledger, drift):
    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        row = root_row(db, q)
        allocation = (
            db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
        )
        if drift == "missing":
            db.delete(allocation)
        elif drift == "owner":
            allocation.owner_id = q.admin_id
        else:
            allocation.observed = {**allocation.observed, "storage_bytes": 0}
        db.flush()
        with pytest.raises(AccountingUnavailable, match="ledger is inconsistent"):
            storage.scope_evidence(db, "project", row.project_id, NOW)


def test_stopped_storage_and_operation_identities_are_retained(native_ledger):
    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        root = root_row(db, q)
        zero = Resources().model_dump()
        start = QuotaReservation(
            operation_id="native-start",
            project_id=root.project_id,
            owner_id=root.owner_id,
            organization_id=root.organization_id,
            state="confirmed",
            observed=zero,
            requested=zero,
            dispatch_metadata={**root.dispatch_metadata, "kind": "start"},
        )
        db.add(start)
        allocation = (
            db.query(QuotaAllocation).filter_by(operation_id=root.operation_id).one()
        )
        root.observed = allocation.observed = {
            **root.observed,
            "cpu": 0,
            "ram_bytes": 0,
        }
        allocation.state = "stopped"
        db.commit()
    later = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        images=[
            i.model_copy(update={"collected_at": i.collected_at + timedelta(seconds=1)})
            for i in q.report.images
        ],
    )
    ingest(q, later)
    reconcile(q)
    with q.sessions() as db:
        root = (
            db.query(QuotaReservation).filter_by(operation_id="native-deploy-0").one()
        )
        assert root.observed == Resources(projects=1, storage_bytes=30).model_dump()
        assert (
            db.query(QuotaReservation)
            .filter_by(operation_id="native-start")
            .one()
            .observed["storage_bytes"]
            == 0
        )
        assert root.owner_id == q.owner_id and root.organization_id == q.org_id


def test_complete_measurement_does_not_supply_deploy_provider_or_stop_authority(
    native_ledger, monkeypatch
):
    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        assert operations.select_adapter(db, project) == "kubevirt"
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            operations.prepare(db, project, "deploy")
        assert (
            "requires the project's selected provider" in error.value.detail["message"]
        )
        db.add(QuotaPolicy(scope="project", scope_id=project.id, storage_bytes=30))
        db.commit()
        enforcement.evaluate(db, NOW)
        db.commit()
        run = db.query(QuotaAction).filter_by(kind="stop_run").one()
        monkeypatch.setattr(
            enforcement,
            "_confirm_off",
            lambda *args: pytest.fail("Nested off proof used for native allocation"),
        )
        assert not enforcement._prepare_stop(db, run, NOW)
        assert "Native managed shutdown" in run.last_error
        assert run.state == "pending" and run.attempts == 0
        assert root_row(db, q).observed["cpu"] == 2


@pytest.mark.parametrize("nodes", [[], [{"id": "vm", "type": "vmNode"}]])
def test_native_cached_vm_off_cannot_release_compute(native_ledger, monkeypatch, nodes):
    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        db.get(Project, q.project_ids[0]).topology = {"nodes": nodes}
        db.commit()
    monkeypatch.setattr(
        "app.services.ws_pubsub.get_cached_vm_states",
        lambda _: {
            "states": {"vm": "shut_off"},
            "observed_at": NOW,
            "observation_id": "native-vm-off-is-incomplete",
        },
    )
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        root = root_row(db, q)
        assert (
            root.observed
            == Resources(
                cpu=2, ram_bytes=4096, projects=1, storage_bytes=30
            ).model_dump()
        )
