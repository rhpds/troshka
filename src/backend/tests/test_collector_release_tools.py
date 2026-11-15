"""Deployment checks use durable latest attempts and preserve enrollment identity."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


def load_script(name):
    path = Path(__file__).resolve().parents[3] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_check_follows_failed_latest_project_attempt(native_case):
    check = load_script("collector_release_check")
    q = native_case
    project = q.report.project_roots[0].project_id
    report = q.report
    from app.models.kubevirt_storage import NativeProjectAttempt, NativeStorageSnapshot

    successful, failed = str(uuid4()), str(uuid4())
    with q.sessions.begin() as db:
        for identity, accepted, offset in [(successful, True, 0), (failed, False, 2)]:
            db.add(
                NativeStorageSnapshot(
                    id=identity,
                    domain_id=str(q.pins.domain_id),
                    enrollment_id=q.enrollment_id,
                    digest="a" * 64,
                    started_at=report.started_at + timedelta(seconds=offset),
                    completed_at=report.completed_at + timedelta(seconds=offset),
                    imported_at=report.completed_at + timedelta(seconds=offset),
                    accepted=accepted,
                    reason_codes=[] if accepted else ["incomplete_attempt"],
                    payload=report.model_dump(mode="json"),
                )
            )
        db.add(
            NativeProjectAttempt(
                domain_id=str(q.pins.domain_id),
                project_id=str(project),
                root_key="b" * 64,
                enrollment_id=q.enrollment_id,
                latest_report_id=successful,
                latest_started_at=report.started_at,
                conflicted=False,
            )
        )
    row = check.observe(str(q.pins.domain_id), str(project), "project")
    assert row["status"] == "complete"
    with q.sessions.begin() as db:
        cursor = db.get(NativeProjectAttempt, (str(q.pins.domain_id), str(project)))
        cursor.latest_report_id = failed
        cursor.latest_started_at = report.started_at + timedelta(seconds=2)
    row = check.observe(str(q.pins.domain_id), str(project), "project")
    assert row["status"] == "failed"
    assert check.evidence_status(row, report.started_at.isoformat()) == "failed"


def test_bootstrap_preserves_existing_anchor_and_rejects_changed_pins(
    native_case, monkeypatch
):
    bootstrap = load_script("collector_bootstrap")
    q = native_case
    from app.core import config as configuration
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services import native_collection_config

    monkeypatch.setattr(
        configuration,
        "config",
        {
            "native_collection.profiles": [
                {"domain_id": str(q.pins.domain_id), "target_id": q.target_id}
            ]
        },
    )
    monkeypatch.setattr(
        native_collection_config, "_managed_inputs", lambda _: (q.pins, None, None)
    )
    with q.sessions() as db:
        pending, preserved = bootstrap.prepared_profiles(db)
        assert pending == []
        assert preserved == [str(q.pins.domain_id)]
        assert (
            db.get(NativeCollectionDomain, str(q.pins.domain_id)).enrollment_id
            == q.enrollment_id
        )
        assert db.query(NativeEnrollment).count() == 1
        changed = q.pins.model_copy(update={"collector_ids": ["replacement"]})
        monkeypatch.setattr(
            native_collection_config, "_managed_inputs", lambda _: (changed, None, None)
        )
        with pytest.raises(ValueError, match="exact protected pins anchor"):
            bootstrap.prepared_profiles(db)
