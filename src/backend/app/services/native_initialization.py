"""Pinned initialization authority and fresh collector evidence, never calibration.

No real implementation is enabled. An exact physically accepted downstream
implementation must replace the closed selector before any real first deploy.
Synthetic integration tests inject an explicitly synthetic reader implementation.
"""

from datetime import UTC, datetime

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeProjectRoot,
)
from app.schemas.kubevirt_storage import InitializationObservation
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest, utc, validated_enrollment


def implementation(profile):
    raise AccountingUnavailable(
        "Native initialization requires independently reviewed physical acceptance"
    )


def validate_observation(profile, value, now):
    selected = implementation(profile)
    try:
        observation = InitializationObservation.model_validate(value)
    except ValueError as error:
        raise AccountingUnavailable(
            "Native initialization observation is unavailable"
        ) from error
    if (
        observation.profile_digest != digest(profile)
        or observation.target != profile.target
        or observation.headroom_semantics != selected.headroom_semantics
        or observation.headroom_bytes is None
        or not 0 <= (utc(now) - observation.observed_at).total_seconds() <= 60
    ):
        raise AccountingUnavailable(
            "Native initialization target/headroom is unavailable or changed"
        )
    return observation


def observe(profile, kube, backend, *, clock=None):
    """Only the accepted implementation may interpret exact installed semantics."""
    selected = implementation(profile)
    value = selected.observe(profile, kube, backend)
    now = clock() if clock is not None else datetime.now(UTC)
    return validate_observation(profile, value, now)


def enrollment(db, provider_id, *, domain_id=None):
    domains = db.query(NativeCollectionDomain)
    if domain_id is not None:
        domains = domains.filter_by(id=domain_id)
    choices = []
    for domain in domains.populate_existing():
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        profile = manifest.pins.initialization_evidence
        if profile is not None and str(profile.target.provider_id) == provider_id:
            choices.append((domain, manifest, profile))
    if len(choices) != 1:
        raise AccountingUnavailable(
            "One current protected native initialization profile is required"
        )
    domain, manifest, profile = choices[0]
    implementation(profile)
    if (
        len(manifest.pins.provider_clusters) != 1
        or len(manifest.pins.pool_keys) != 1
        or profile.target.pool_id != manifest.pins.pool_keys[0].pool_id
        or profile.target.ceph_fsid != manifest.pins.pool_keys[0].ceph_fsid
    ):
        raise AccountingUnavailable(
            "First deployment supports one exact provider and pool domain"
        )
    return domain, manifest, profile


def current(db, provider_id, now, *, domain_id=None):
    from app.services.quota_native import _measurement_report

    domain, manifest, profile = enrollment(db, provider_id, domain_id=domain_id)
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=domain.id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    # Refresh the manifest after taking the domain lock, not a cached predecessor.
    manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
    if manifest.pins.initialization_evidence != profile:
        raise AccountingUnavailable("Native initialization authorization changed")
    roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    _, source, _, _ = _measurement_report(db, domain, roots, manifest, now)
    observation = validate_observation(profile, source.initialization_observation, now)
    if not source.started_at <= observation.observed_at <= source.completed_at:
        raise AccountingUnavailable(
            "Initialization observation is outside the current collection"
        )
    return domain, manifest, profile, observation
