"""Scoped attempts isolate failures and preserve append-only ordering."""

from datetime import timedelta
from uuid import uuid4

import pytest

from tests.kubevirt_storage_helpers import report_fixture
from tests.test_kubevirt_storage_reports import ingest, make_native_case
from tests.test_quota_native_storage import native_ledger as native_ledger_fixture

native_ledger = native_ledger_fixture


def scoped(report, project_id, offset=0, **changes):
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    result = report.model_copy(
        update={
            "report_id": uuid4(),
            "project_scope": project_id,
            "started_at": report.started_at + timedelta(seconds=offset),
            "completed_at": report.completed_at + timedelta(seconds=offset),
            "images": [
                i.model_copy(
                    update={"collected_at": i.collected_at + timedelta(seconds=offset)}
                )
                for i in report.images
            ],
            "project_roots": [
                r for r in report.project_roots if r.project_id == project_id
            ],
            "volume_bindings": [
                b for b in report.volume_bindings if b.root.project_id == project_id
            ],
            "retained_bindings": [
                b for b in report.retained_bindings if b.root.project_id == project_id
            ],
            **changes,
        }
    )
    return result.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(result))}
    )


@pytest.fixture
def two_projects(isolated_quota_database):
    report = report_fixture("zero-private-parent.json")
    from app.services.kubevirt_storage_inventory import inventory_digest
    from app.services.kubevirt_storage_reports import report_inventory

    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    return make_native_case(isolated_quota_database, report=report)


def test_scoped_attempts_isolate_failure_and_ordering(two_projects):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.schemas.kubevirt_storage import Blocker
    from app.services.kubevirt_storage_reports import report_cursor
    from app.services.kubevirt_storage_usage import domain_evidence

    case = two_projects
    legacy = ingest(case)
    a, b = [r.project_id for r in case.report.project_roots]
    good = ingest(case, scoped(case.report, a, 1))
    failed = ingest(
        case,
        scoped(
            case.report, a, 3, collection_errors=[Blocker(code="timeout", project_id=a)]
        ),
    )
    ingest(case, scoped(case.report, a, 2))
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        assert domain.latest_report_id == legacy
        assert report_cursor(db, domain, a).latest_report_id == failed
        assert report_cursor(db, domain, b).latest_report_id == legacy
        graph, source, _ = domain_evidence(db, domain, [], project_id=a)
        assert source.report_id != good
        assert any("timeout" in p.blockers for p in graph.projects)


def test_foreign_binding_rejected_only_for_scoped_project(two_projects):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    case = two_projects
    a, b = [r.project_id for r in case.report.project_roots]
    result = ingest(
        case, scoped(case.report, b, volume_bindings=case.report.volume_bindings)
    )
    with case.sessions() as db:
        saved = db.get(NativeStorageSnapshot, result)
        assert not saved.accepted
        assert "unenrolled_binding_root" in saved.reason_codes


def test_conflicting_same_time_and_newer_domain_recovery(two_projects):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.schemas.kubevirt_storage import Blocker
    from app.services.kubevirt_storage_reports import report_cursor

    case = two_projects
    a, b = [r.project_id for r in case.report.project_roots]
    ingest(case)
    original = scoped(case.report, a, 1)
    first = ingest(case, original)
    assert ingest(case, original) == first
    ingest(
        case,
        scoped(
            case.report, a, 1, collection_errors=[Blocker(code="timeout", project_id=a)]
        ),
    )
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        assert report_cursor(db, domain, a).conflicted
        assert not report_cursor(db, domain, b).conflicted
    later = case.report.model_copy(
        update={
            "report_id": uuid4(),
            "started_at": case.report.started_at + timedelta(seconds=2),
            "completed_at": case.report.completed_at + timedelta(seconds=2),
        }
    )
    restored = ingest(case, later)
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        assert report_cursor(db, domain, a).latest_report_id == restored
        assert not report_cursor(db, domain, a).conflicted


def test_stale_scoped_attempt_never_falls_back(two_projects):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectRoot
    from app.services.kubevirt_storage_reports import report_cursor
    from app.services.kubevirt_storage_usage import _read_authorized

    case = two_projects
    a = case.report.project_roots[0].project_id
    ingest(case)
    scoped_id = ingest(case, scoped(case.report, a, 1))
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        assert report_cursor(db, domain, a).latest_report_id == scoped_id
        usage = _read_authorized(
            db,
            case.owner_id,
            "project",
            str(a),
            case.report.started_at + timedelta(hours=1),
        )
        assert usage.status == "stale"
        root = db.query(NativeProjectRoot).filter_by(project_id=str(a)).one()
        root.key = "changed-root"
        db.flush()
        from app.services.kubevirt_storage_usage import domain_evidence

        graph, _, failures = domain_evidence(db, domain, [root], project_id=a)
        assert graph is None
        assert failures == {"report_unavailable"}


def test_scoped_unknown_project_and_replacement_root_fail_closed(two_projects):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    case = two_projects
    with pytest.raises(ValueError, match="one enrolled root"):
        ingest(case, scoped(case.report, uuid4()))
    a = case.report.project_roots[0]
    replacement = a.model_copy(update={"namespace_uid": "replacement"})
    result = ingest(
        case, scoped(case.report, a.project_id, project_roots=[replacement])
    )
    with case.sessions() as db:
        assert not db.get(NativeStorageSnapshot, result).accepted


def test_failed_project_does_not_block_sibling_quota_measurement(native_ledger):
    from app.models.project import Project
    from app.schemas.kubevirt_storage import Blocker
    from app.schemas.quota import AccountingUnavailable
    from app.services.quota_native import storage_measurement
    from tests.test_quota_native_storage import NOW, root_row
    from tests.test_quota_native_storage import ingest as ledger_ingest

    case = native_ledger
    ledger_ingest(case)
    a, b = [r.project_id for r in case.report.project_roots]
    ledger_ingest(
        case,
        scoped(
            case.report, a, 1, collection_errors=[Blocker(code="timeout", project_id=a)]
        ),
    )
    good = ledger_ingest(case, scoped(case.report, b, 2))
    with case.sessions() as db:
        for index, identity in enumerate((a, b)):
            row = root_row(db, case, index)
            project = db.get(Project, str(identity))
            args = (
                db,
                project,
                row.operation_id,
                row.dispatch_metadata["native_storage_identity"],
                NOW,
            )
            if index == 0:
                with pytest.raises(AccountingUnavailable):
                    storage_measurement(*args)
            else:
                measurement = storage_measurement(*args)
                assert measurement.report_id == good
                assert measurement.allocated_bytes == 50


def test_scoped_private_measurement_cannot_predate_scan_without_provenance(
    native_ledger,
):
    from app.models.project import Project
    from app.schemas.quota import AccountingUnavailable
    from app.services.quota_native import storage_measurement
    from tests.test_quota_native_storage import NOW, root_row
    from tests.test_quota_native_storage import ingest as ledger_ingest

    case = native_ledger
    a = case.report.project_roots[0].project_id
    report = scoped(case.report, a, 2)
    private = report.volume_bindings[0].image_key
    report = report.model_copy(
        update={
            "images": [
                image.model_copy(update={"collected_at": case.report.started_at})
                if image.key == private
                else image
                for image in report.images
            ]
        }
    )
    ledger_ingest(case, report)
    with case.sessions() as db:
        project = db.get(Project, str(a))
        row = root_row(db, case)
        with pytest.raises(AccountingUnavailable, match="stale or future-dated"):
            storage_measurement(
                db,
                project,
                row.operation_id,
                row.dispatch_metadata["native_storage_identity"],
                NOW,
            )


def test_scoped_report_ignores_unrelated_retained_cache(two_projects):
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeRetainedBinding,
    )
    from app.services.kubevirt_storage_reports import _binding_identity, binding_key
    from app.services.kubevirt_storage_usage import domain_evidence

    case = two_projects
    ingest(case)
    other = case.report.cache_bindings[0].model_copy(
        update={"pvc_uid": "unrelated-cache"}
    )
    with case.sessions() as db:
        db.add(
            NativeRetainedBinding(
                key=binding_key(other),
                root_key="cache",
                domain_id=str(case.pins.domain_id),
                payload=_binding_identity(other),
            )
        )
        db.commit()
    a = case.report.project_roots[0].project_id
    ingest(case, scoped(case.report, a, 1))
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        graph, _, failures = domain_evidence(db, domain, [], project_id=a)
        assert not failures
        assert graph.projects[0].coverage == "complete"


def test_scoped_report_missing_required_cache_source_is_incomplete(two_projects):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services.kubevirt_storage_usage import domain_evidence

    case = two_projects
    ingest(case)
    a = case.report.project_roots[0].project_id
    ingest(case, scoped(case.report, a, 1, cache_bindings=[]))
    with case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        graph, _, failures = domain_evidence(db, domain, [], project_id=a)
        assert failures or graph.projects[0].coverage != "complete"


@pytest.mark.parametrize("settled", [False, True])
def test_scoped_redeploy_excludes_only_confirmed_settled_roots(
    two_projects, monkeypatch, settled
):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectRoot
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_usage import domain_evidence

    case = two_projects
    project_id = case.report.project_roots[0].project_id
    ingest(case, scoped(case.report, project_id, 1))
    with case.sessions() as db:
        current = (
            db.query(NativeProjectRoot).filter_by(project_id=str(project_id)).one()
        )
        historical = NativeProjectRoot(
            key="historical-root",
            domain_id=current.domain_id,
            project_id=current.project_id,
            provider_id=current.provider_id,
            enrollment_id=current.enrollment_id,
            payload={**current.payload, "namespace_uid": "previous-namespace"},
        )
        db.add(historical)
        db.commit()
        monkeypatch.setattr(
            quota_cleanup,
            "settled_root_keys",
            lambda db: {historical.key} if settled else set(),
        )
        domain = db.get(NativeCollectionDomain, str(case.pins.domain_id))
        graph, _, failures = domain_evidence(db, domain, [], project_id=project_id)
        if settled:
            assert not failures
            assert graph.projects[0].coverage == "complete"
        else:
            assert graph is None
            assert failures == {"report_unavailable"}
