"""Invalidated authority keeps its ordering watermark until a newer scan."""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import NativeCollectionDomain, NativeStorageSnapshot
from tests import test_kubevirt_storage_reports as reports
from tests import test_native_cleanup_completion as cleanup

isolated_quota_database = cleanup.isolated_quota_database
native_case = reports.native_case
cleanup_case = cleanup.cleanup_case
deploy_case = cleanup.deploy_case
resume_case = cleanup.resume_case
native_ledger = cleanup.native_ledger


@pytest.mark.parametrize("conflicted", [False, True])
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("scoped", [False, True])
def test_import_preserves_cutoff_until_strictly_newer_report(
    native_case, conflicted, offset, scoped
):
    from app.models.kubevirt_storage import NativeProjectRoot
    from app.services.kubevirt_storage_enrollment import utc
    from app.services.kubevirt_storage_reports import _project_cursor, report_cursor

    q = native_case
    cutoff = q.report.started_at + timedelta(seconds=5)
    project_id = str(q.report.project_roots[0].project_id) if scoped else None
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        cursor = domain
        if scoped:
            root = (
                db.query(NativeProjectRoot)
                .filter_by(domain_id=domain.id, project_id=project_id)
                .one()
            )
            cursor = _project_cursor(db, domain, root)
        cursor.latest_report_id = None
        cursor.latest_started_at = cutoff
        cursor.conflicted = conflicted
        db.commit()
    value = reports.revised(
        q.report,
        started_at=cutoff + timedelta(seconds=offset),
        completed_at=cutoff + timedelta(seconds=offset + 1),
        project_scope=project_id,
    )
    report_id = reports.ingest(q, value)
    with q.sessions() as db:
        assert db.get(NativeStorageSnapshot, report_id) is not None
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        domain = report_cursor(db, domain, project_id)
        if offset <= 0:
            assert domain.latest_report_id is None
            assert utc(domain.latest_started_at) == cutoff
            assert domain.conflicted is conflicted
        else:
            assert domain.latest_report_id == report_id
            assert domain.conflicted is False


@pytest.mark.parametrize(
    "fault", [None, "future", "conflict", "domain", "enrollment", "root"]
)
def test_cleanup_refreshes_only_valid_cutoff(cleanup_case, monkeypatch, fault):
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.models.kubevirt_storage import NativeProjectRoot
    from app.models.project import Project
    from app.services import native_collection_jobs, quota_cleanup
    from app.services.kubevirt_storage_reports import _project_cursor

    q = cleanup_case
    enqueue = MagicMock(return_value=True)
    monkeypatch.setattr(native_collection_jobs, "enqueue_collection", enqueue)
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        root = (
            db.query(NativeProjectRoot)
            .filter_by(domain_id=domain.id, project_id=q.project_ids[0])
            .one()
        )
        cursor = _project_cursor(db, domain, root)
        cursor.latest_report_id = None
        cursor.latest_started_at = cleanup.FixedDatetime.current + timedelta(
            seconds=1 if fault == "future" else -1
        )
        cursor.conflicted = fault == "conflict"
        if fault == "domain":
            domain.conflicted = True
        elif fault == "enrollment":
            cursor.enrollment_id = str(uuid4())
        elif fault == "root":
            cursor.root_key = "changed"
        db.commit()
        with pytest.raises((HTTPException, ValueError)):
            quota_cleanup.request_native(
                db, db.get(Project, q.project_ids[0]), "delete"
            )
        if fault is None:
            enqueue.assert_called_once_with(
                str(q.pins.domain_id), q.project_ids[0], observation_only=True
            )
        else:
            enqueue.assert_not_called()
        assert quota_cleanup.native_intent(db, q.project_ids[0]) is None
        assert not q.queued


@pytest.mark.parametrize("scoped", [False, True])
def test_collector_stamps_start_before_reading_authority(monkeypatch, scoped):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.services import native_managed_collector as collector
    from tests.kubevirt_storage_helpers import report_fixture

    report = report_fixture("zero-private-parent.json")
    started = report.started_at
    events = []

    class Clock:
        @staticmethod
        def now(*args):
            events.append("clock")
            return started if len(events) == 1 else started + timedelta(seconds=30)

    def snapshot(*args):
        events.append("snapshot")
        return {"previous": None}

    def collect(manifest, previous, scan_started):
        events.append("collect")
        assert scan_started == started
        return report.model_copy(update={"project_scope": project_id})

    class ImportedError(Exception):
        pass

    def imported(db, value, **kwargs):
        assert value.started_at == started
        raise ImportedError

    project_id = report.project_roots[0].project_id if scoped else None
    profile = SimpleNamespace(target=SimpleNamespace(provider_id="provider"))
    manifest = SimpleNamespace(
        pins=SimpleNamespace(initialization_evidence=profile), digest="digest"
    )
    monkeypatch.setattr(collector, "datetime", Clock)
    monkeypatch.setattr(collector, "project_snapshot", snapshot)
    monkeypatch.setattr(collector, "prepare_work", snapshot)
    monkeypatch.setattr(
        collector,
        "anchored",
        lambda *a: (SimpleNamespace(enrollment_id="enrolled"), manifest),
    )
    monkeypatch.setattr(collector.initialization, "observe", lambda *a, **kw: None)
    monkeypatch.setattr(collector, "import_report", imported)
    with pytest.raises(ImportedError):
        collector.cycle(
            MagicMock(),
            object(),
            "target",
            report.collector_id,
            {"provider": object()},
            object(),
            collect,
            project_id=project_id,
        )
    assert events[:3] == ["clock", "snapshot", "collect"]
