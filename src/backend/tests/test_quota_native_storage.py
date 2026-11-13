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
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    q = request.getfixturevalue("native_case")
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: 0)
    monkeypatch.setattr(operations, "now", lambda: NOW)
    monkeypatch.setattr(enforcement, "_now", lambda: NOW)
    roots = [
        r.model_copy(update={"deployment_generation": 7})
        for r in q.report.project_roots
    ]
    q.report = revised(
        q.report,
        project_roots=roots,
        volume_bindings=[
            b.model_copy(update={"root": r})
            for b, r in zip(q.report.volume_bindings, roots)
        ],
    )
    with q.sessions() as db:
        org = ManagedGroup(
            name="native-org", normalized_name="native-org", is_organization=True
        )
        db.add(org)
        db.flush()
        db.get(User, q.owner_id).organization_id = org.id
        db.flush()
        request = prepare_native_enrollment(db, [str(r.project_id) for r in roots])
        original = VerifiedEnrollment.model_validate(
            db.get(NativeEnrollment, q.enrollment_id).payload
        )
        manifest = original.model_copy(
            update={
                "request": request,
                "pins": original.pins.model_copy(update={"project_roots": roots}),
            }
        )
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        q.enrollment_id = enroll_native_roots(
            db, manifest, expected_target_id=q.target_id
        )
        q.digest, q.org_id = manifest.digest, org.id
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
                        organization_id=org.id,
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
                                "domain_id": str(manifest.pins.domain_id),
                                "enrollment_id": q.enrollment_id,
                                "host_id": project.host_id,
                                "provider_id": str(request.projects[index].provider_id),
                                "root": root.model_dump(mode="json"),
                            },
                        },
                    ),
                    QuotaAllocation(
                        operation_id=operation_id,
                        project_id=project.id,
                        owner_id=q.owner_id,
                        organization_id=org.id,
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


def test_complete_measurement_does_not_enable_native_growth_or_stop(
    native_ledger, monkeypatch
):
    q = native_ledger
    ingest(q)
    reconcile(q)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        with pytest.raises(AccountingUnavailable, match="lifecycle readiness"):
            operations.select_adapter(db, project)
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
