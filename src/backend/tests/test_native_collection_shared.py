"""Independent coordinated identity freshness never re-stamps disk usage."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import NativeCollectionDomain, NativeSharedObservation
from app.schemas.kubevirt_storage import InitializationObservation
from app.schemas.kubevirt_storage_initialization import InitializationEvidence
from app.schemas.quota import AccountingUnavailable
from app.services import native_collection_shared as shared
from app.services import native_initialization as initialization
from app.services.kubevirt_storage_enrollment import digest
from tests.test_native_initialization_evidence import synthetic_profile


@pytest.fixture
def case(isolated_quota_database, monkeypatch):
    sessions = isolated_quota_database
    profile = InitializationEvidence.model_validate(synthetic_profile())
    now = datetime.now(UTC)
    observation = InitializationObservation(
        version="native-initialization-observation-v1",
        target=profile.target,
        metric="rbd_allocated_extent_bytes",
        measurement_version="native-rbd-measurement-v1",
        profile_digest=digest(profile),
        observed_at=now - timedelta(seconds=2),
    )
    domain_id, enrollment_id = str(uuid4()), str(uuid4())
    manifest = SimpleNamespace(pins=SimpleNamespace(initialization_evidence=profile))
    with sessions.begin() as db:
        db.add(NativeCollectionDomain(id=domain_id, enrollment_id=enrollment_id))
    monkeypatch.setattr(shared, "validated_enrollment", lambda row: manifest)
    monkeypatch.setattr(initialization, "validated_enrollment", lambda row: manifest)
    monkeypatch.setattr(initialization, "implementation", lambda profile: None)
    monkeypatch.setattr(
        initialization,
        "enrollment",
        lambda db, *a, **kw: (
            db.get(NativeCollectionDomain, domain_id),
            manifest,
            profile,
        ),
    )
    return SimpleNamespace(
        sessions=sessions,
        profile=profile,
        observation=observation,
        domain_id=domain_id,
        enrollment_id=enrollment_id,
        now=now,
        start=now - timedelta(seconds=3),
    )


def publish(case, observation="default", **kwargs):
    return shared.publish_shared_observation(
        case.sessions,
        case.domain_id,
        case.enrollment_id,
        case.observation if observation == "default" else observation,
        kwargs.pop("started_at", case.start),
        assert_current=kwargs.pop("assert_current", lambda: None),
        clock=lambda: case.now,
        **kwargs,
    )


def current(case, now=None):
    with case.sessions() as db:
        return initialization.current(
            db, str(case.profile.target.provider_id), now or case.now
        )


def test_fresh_shared_observation_bypasses_full_domain_disk_report(case, monkeypatch):
    from app.services import quota_native

    monkeypatch.setattr(
        quota_native,
        "_measurement_report",
        lambda *a: pytest.fail("private rescan dependency"),
    )
    assert publish(case)
    assert current(case)[-1] == case.observation
    with case.sessions() as db:
        row = db.get(NativeSharedObservation, case.domain_id)
        assert (
            row.observation["observed_at"]
            == case.observation.model_dump(mode="json")["observed_at"]
        )


def test_stale_shared_observation_cannot_fall_back(case):
    assert publish(case)
    with pytest.raises(AccountingUnavailable):
        current(case, case.now + timedelta(seconds=61))


def test_failed_new_attempt_invalidates_previous_success(case):
    assert publish(case)
    assert not publish(case, None, started_at=case.start + timedelta(seconds=1))
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_obsolete_enrollment_cannot_publish_or_restore_readiness(case):
    assert publish(case)
    with case.sessions.begin() as db:
        db.get(NativeCollectionDomain, case.domain_id).enrollment_id = str(uuid4())
    with pytest.raises(AccountingUnavailable):
        publish(case)
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_no_shared_attempt_uses_legacy_domain_observation(case, monkeypatch):
    from app.services import quota_native

    source = SimpleNamespace(
        initialization_observation=case.observation,
        started_at=case.start,
        completed_at=case.now,
    )
    monkeypatch.setattr(
        quota_native, "_measurement_report", lambda *a: (None, source, None, None)
    )
    assert current(case)[-1] == case.observation


def test_conflicting_same_time_attempt_stays_invalid(case):
    assert publish(case)
    assert not publish(case, None)
    assert not publish(case)
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_late_success_cannot_replace_newer_failure(case):
    assert not publish(case, None, started_at=case.start + timedelta(seconds=1))
    assert not publish(case)
    with pytest.raises(AccountingUnavailable):
        current(case)


@pytest.mark.parametrize("fault", ["wrong_profile", "before_start", "stale"])
def test_invalid_source_observation_records_failed_attempt(case, fault):
    changes = {
        "wrong_profile": {"profile_digest": "0" * 64},
        "before_start": {"observed_at": case.start - timedelta(seconds=1)},
        "stale": {"observed_at": case.now - timedelta(seconds=61)},
    }
    assert not publish(case, case.observation.model_copy(update=changes[fault]))
    with pytest.raises(AccountingUnavailable):
        current(case)


def test_lease_loss_before_commit_rolls_back_attempt(case):
    calls = []

    def lease():
        calls.append(True)
        if len(calls) == 2:
            raise AccountingUnavailable("lease lost")

    with pytest.raises(AccountingUnavailable):
        publish(case, assert_current=lease)
    with case.sessions() as db:
        assert db.get(NativeSharedObservation, case.domain_id) is None
