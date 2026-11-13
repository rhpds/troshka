"""Protected blank-PVC startup allowances and fresh installed identity evidence.

Creation may exceed the explicit allowance. Every measured byte remains charged;
runtime requires complete writer-free observation within that allowance.
"""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeProjectRoot,
)
from app.schemas.kubevirt_storage import InitializationObservation
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest, utc, validated_enrollment


def implementation(profile):
    from app.services.native_initialization_observer import OBSERVER_VERSION

    target = profile.target
    if (
        profile.profile_name != "odf-rbd-blank-startup-allowance-v1"
        or profile.reservation_semantics != "measured-startup-allowance-v1"
        or target.csi_version != OBSERVER_VERSION
        or target.csi_installation is None
        or target.storage_class_options is None
        or target.provisioner != "openshift-storage.rbd.csi.ceph.com"
        or not re.match(r"^ceph version 19\.2\.1(?:[- (])", target.ceph_version)
        or target.rbd_command_version != target.ceph_version
        or not supported_blank_storage(target)
    ):
        raise AccountingUnavailable(
            "Native initialization requires explicit supported startup allowance; "
            "legacy physical acceptance profiles remain unsupported"
        )
    return SimpleNamespace(vnc_image=profile.vnc_image)


def supported_blank_storage(target):
    params = target.storage_class_parameters
    return (
        params.get("imageFormat") == "2"
        and params.get("thickProvision", "false") == "false"
        and params.get("radosNamespace", "") == target.rbd_namespace
        and params.get("journalPool", params.get("pool")) == params.get("pool")
        and bool(params.get("pool"))
        and params.get("clusterID") == "openshift-storage"
    )


def validate_observation(profile, value, now):
    try:
        observation = InitializationObservation.model_validate(value)
    except ValueError as error:
        raise AccountingUnavailable(
            "Native initialization observation is unavailable"
        ) from error
    if (
        observation.profile_digest != digest(profile)
        or observation.target != profile.target
        or not 0 <= (utc(now) - observation.observed_at).total_seconds() <= 60
    ):
        raise AccountingUnavailable(
            "Native initialization target is unavailable or changed"
        )
    return observation


def observe(profile, kube, backend, *, clock=None):
    """Observe installation identity; this never opens the physical capability."""
    if profile.target.csi_installation is not None:
        from app.services.native_initialization_observer import ProductionObserver

        selected = ProductionObserver()
    else:
        # Legacy synthetic test implementations remain injected, never shipped.
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
