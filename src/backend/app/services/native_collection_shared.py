"""Lease-fenced, independently fresh installed-identity evidence.

This is not storage usage or clone-graph authority. Source observation timestamps
are preserved; only a new protected domain observation can advance readiness.
"""

from datetime import UTC, datetime

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeSharedObservation,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest, utc, validated_enrollment


def _validated_payload(profile, observation, started_at, completed_at):
    from app.services.native_initialization import implementation, validate_observation

    if observation is None:
        return None
    try:
        implementation(profile)
        value = validate_observation(profile, observation, completed_at)
        if not started_at <= value.observed_at <= completed_at:
            return None
        return value.model_dump(mode="json")
    except (AccountingUnavailable, ValueError, AttributeError):
        return None


def _advance(db, domain, enrollment_id, payload, started_at, completed_at):
    row = db.get(NativeSharedObservation, domain.id)
    fingerprint = digest(payload) if payload is not None else None
    if row is not None and utc(row.started_at) > started_at:
        return False
    if row is not None and utc(row.started_at) == started_at:
        if (
            row.enrollment_id != str(enrollment_id)
            or row.observation_digest != fingerprint
        ):
            row.status = "conflict"
        return row.status == "complete"
    if row is None:
        row = NativeSharedObservation(domain_id=domain.id)
        db.add(row)
    row.enrollment_id = str(enrollment_id)
    row.started_at = started_at
    row.completed_at = completed_at
    row.observation = payload
    row.observation_digest = fingerprint
    row.status = "complete" if payload is not None else "failed"
    return row.status == "complete"


def publish_shared_observation(
    sessions,
    domain_id,
    enrollment_id,
    observation,
    started_at,
    *,
    assert_current,
    clock=None,
):
    """Publish one attempt; None/invalid evidence durably invalidates readiness.

    The required callback raises when the domain/provider lease is no longer
    current. No caller-supplied envelope can bypass protected enrollment checks.
    Return False for rejected, conflicting, or superseded observation attempts.
    """
    started_at = utc(started_at)
    with sessions.begin() as db:
        assert_current()
        domain = (
            db.query(NativeCollectionDomain)
            .filter_by(id=str(domain_id))
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if domain is None or domain.enrollment_id != str(enrollment_id):
            raise AccountingUnavailable("Native shared observation enrollment changed")
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        completed_at = utc(clock() if clock is not None else datetime.now(UTC))
        if started_at > completed_at:
            raise AccountingUnavailable(
                "Native shared observation starts in the future"
            )
        payload = _validated_payload(
            manifest.pins.initialization_evidence, observation, started_at, completed_at
        )
        accepted = _advance(
            db, domain, enrollment_id, payload, started_at, completed_at
        )
        db.flush()
        assert_current()
    return accepted


def current_shared_observation(db, domain, profile, now):
    """None means no scoped shared attempt exists; every failed attempt raises."""
    from app.services.native_initialization import validate_observation

    row = db.get(NativeSharedObservation, domain.id, populate_existing=True)
    if row is None:
        return None
    if (
        row.status != "complete"
        or row.enrollment_id != domain.enrollment_id
        or row.observation is None
        or digest(row.observation) != row.observation_digest
        or utc(row.completed_at) > utc(now)
    ):
        raise AccountingUnavailable(
            "Native shared initialization attempt is unavailable"
        )
    observation = validate_observation(profile, row.observation, now)
    if not utc(row.started_at) <= observation.observed_at <= utc(row.completed_at):
        raise AccountingUnavailable(
            "Native shared initialization observation time changed"
        )
    return observation
