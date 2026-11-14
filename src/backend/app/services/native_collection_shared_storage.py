"""Coordinated platform bytes, without traversing private clone descendants."""

import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeRetainedBinding,
    NativeSharedStorageObservation,
)
from app.schemas.kubevirt_storage import (
    BackendScope,
    CacheBinding,
    CollectionLimits,
    ImageKey,
    ImageObservation,
    NativeInventory,
)
from app.schemas.quota import AccountingUnavailable
from app.services.kubevirt_storage_enrollment import digest, utc


def _image_identity(key: ImageKey) -> tuple[str, int, str, str]:
    return key.ceph_fsid, key.pool_id, key.rbd_namespace, key.image_id


def _inputs(db, manifest):
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.schemas.kubevirt_storage import CloneReference
    from app.services.native_clone_lineage import resolve_lineages

    domain_id = str(manifest.pins.domain_id)
    references = [
        CloneReference(lineage_id=r.id, digest=r.proof_digest)
        for r in db.query(NativeCloneLineage).filter_by(domain_id=domain_id)
        if r.proof_digest
    ]
    proofs = resolve_lineages(db, references, manifest)
    private = {
        _image_identity(key)
        for p in proofs
        for key in (p.binding.image_key, p.intermediate)
    }
    bindings = [
        CacheBinding.model_validate(r.payload)
        for r in db.query(NativeRetainedBinding).filter_by(
            domain_id=domain_id, root_key="cache"
        )
    ]
    if private & {_image_identity(b.image_key) for b in bindings}:
        raise AccountingUnavailable("Shared cache overlaps private clone storage")
    cache_keys = {digest(b) for b in bindings}
    if any(digest(p.source) not in cache_keys for p in proofs):
        # Binding identity excludes mutable provisioned size, just as import does.
        from app.services.native_clone_graph import binding_identity

        identities = [binding_identity(b) for b in bindings]
        if any(binding_identity(p.source) not in identities for p in proofs):
            raise AccountingUnavailable("Shared source binding unavailable")
    return bindings, proofs


def _validate_images(images, bindings, proofs, started, completed, absent_keys=()):
    from app.services.native_clone_graph import _current_subject_matches

    if started > completed:
        raise AccountingUnavailable("Shared observation starts after completion")
    snapshots, children, verified = defaultdict(set), defaultdict(set), {}
    for proof in proofs:
        key = proof.source.image_key
        snapshots[key].add(proof.intermediate_parent)
        children[key].add(proof.intermediate)
        verified[key] = max(verified.get(key, proof.verified_at), proof.verified_at)
    expected = {b.image_key for b in bindings}
    if len(images) != len(expected) or {i.key for i in images} != expected:
        raise AccountingUnavailable("Shared image inventory changed")
    for image in images:
        if image.absence is not None:
            if image.key not in absent_keys or image.key in verified:
                raise AccountingUnavailable("Shared source absence is not authorized")
            if not started <= image.collected_at <= completed:
                raise AccountingUnavailable("Shared source absence is stale")
            continue
        if not _current_subject_matches(
            image,
            None,
            snapshots[image.key],
            children[image.key],
            verified.get(image.key, started),
        ):
            raise AccountingUnavailable("Shared source has unverified graph edges")
        if (
            image.allocated_bytes is None
            or not started <= image.collected_at <= completed
        ):
            raise AccountingUnavailable("Shared source measurement is unavailable")


def _scan(bindings, proofs, manifest, backend, started, limits, clock, absent_keys=()):
    from app.services.kubevirt_storage_collector import (
        _backend_identity,
        _backend_profile,
        _call,
        _final_graph_check,
        _observe,
        _retiring_image,
        _trash_ids,
    )

    inventory = NativeInventory(
        collected_at=started,
        cache_bindings=bindings,
        backend_scope=BackendScope(
            cluster_keys=sorted(
                {p.cluster_key for p in manifest.pins.provider_clusters}
            ),
            pool_keys=manifest.pins.pool_keys,
        ),
    )
    errors = []
    pools, version, supported = _backend_profile(inventory, backend, errors)
    deadline = time.monotonic() + limits.scan_timeout_seconds
    images, trash = [], {}
    for key in sorted(
        {b.image_key for b in bindings}, key=lambda k: k.model_dump_json()
    ):
        if time.monotonic() >= deadline or len(images) >= limits.max_objects:
            raise AccountingUnavailable("Shared storage scan limit")
        pool = (key.ceph_fsid, key.pool_id, key.rbd_namespace)
        if pool not in trash:
            trash[pool] = _trash_ids(backend, key, errors)
        if key.image_id in trash[pool]:
            raise AccountingUnavailable("Shared source is in trash")
        image = None
        if (
            supported
            and key in absent_keys
            and not any(p.source.image_key == key for p in proofs)
        ):
            image = _retiring_image(backend, key, version, limits, clock)
        images.append(
            image
            if image is not None
            else _observe(backend, key, pools, supported, version, clock, limits)
        )
    images = _final_graph_check(backend, images, pools, limits, deadline, errors, clock)
    _backend_identity(
        inventory,
        _call(backend.cluster_identity).value,
        _call(backend.pool_identity).value,
    )
    if errors:
        raise AccountingUnavailable("Shared backend evidence is unavailable")
    _validate_images(images, bindings, proofs, started, clock(), absent_keys)
    return images


def _absence_keys(inventory, bindings):
    """Require existing full cache namespace/PVC/PV/snapshot discovery authority."""
    from app.services.native_clone_graph import binding_identity

    if inventory is None:
        return set()
    if inventory.blockers:
        raise AccountingUnavailable("Shared cache discovery is incomplete")
    retained = [binding_identity(b) for b in bindings]
    if any(
        b not in inventory.cache_bindings or binding_identity(b) not in retained
        for b in inventory.absent_cache_bindings
    ):
        raise AccountingUnavailable("Shared cache absence identity changed")
    return {b.image_key for b in inventory.absent_cache_bindings}


def _advance(db, domain, payload, started, completed):
    row = db.get(NativeSharedStorageObservation, domain.id)
    fingerprint = digest(payload) if payload is not None else None
    if row is not None and utc(row.started_at) > started:
        return False
    if row is not None and utc(row.started_at) == started:
        if utc(row.started_at) == started and (
            row.digest != fingerprint or row.enrollment_id != domain.enrollment_id
        ):
            row.status = "conflict"
        return row.status == "complete"
    if row is None:
        row = NativeSharedStorageObservation(domain_id=domain.id)
        db.add(row)
    row.enrollment_id = domain.enrollment_id
    row.started_at, row.completed_at = started, completed
    row.payload, row.digest = payload, fingerprint
    row.status = "complete" if payload is not None else "failed"
    return row.status == "complete"


def collect_shared_storage(
    sessions,
    manifest,
    backend,
    started,
    *,
    limits=None,
    clock=None,
    cache_inventory=None,
):
    """Publish success or failure under the domain lock; never collect private images."""
    from app.services.native_collection_jobs import assert_collection_lease

    clock = clock or (lambda: datetime.now(UTC))
    started = utc(started)
    limits = limits or CollectionLimits()
    images, failure, inventory = None, None, None
    try:
        with sessions() as db:
            bindings, proofs = _inputs(db, manifest)
        inventory = cache_inventory() if cache_inventory is not None else None
        absent_keys = _absence_keys(inventory, bindings)
        images = _scan(
            bindings, proofs, manifest, backend, started, limits, clock, absent_keys
        )
        if cache_inventory is not None:
            from app.services.kubevirt_storage_inventory import inventory_digest

            rechecked = cache_inventory()
            _absence_keys(rechecked, bindings)
            if inventory_digest(inventory) != inventory_digest(rechecked):
                raise AccountingUnavailable(
                    "Shared cache discovery changed during scan"
                )
    except Exception as error:
        images, failure = None, error
    with sessions.begin() as db:
        domain = (
            db.query(NativeCollectionDomain)
            .filter_by(id=str(manifest.pins.domain_id))
            .with_for_update()
            .populate_existing()
            .one()
        )
        assert_collection_lease()
        from app.models.kubevirt_storage import NativeEnrollment

        enrollment = db.get(NativeEnrollment, domain.enrollment_id)
        if enrollment.digest != manifest.digest:
            raise AccountingUnavailable("Shared storage enrollment changed")
        completed = utc(clock())
        payload = None
        if images is not None:
            try:
                bindings, proofs = _inputs(db, manifest)
                _validate_images(
                    images,
                    bindings,
                    proofs,
                    started,
                    completed,
                    _absence_keys(inventory, bindings),
                )
                payload = {"images": [i.model_dump(mode="json") for i in images]}
                if inventory is not None and any(i.absence is not None for i in images):
                    payload["cache_absence_inventory"] = inventory.model_dump(
                        mode="json"
                    )
            except Exception as error:
                failure = error
        accepted = _advance(db, domain, payload, started, completed)
        db.flush()
        assert_collection_lease()
    if failure is not None or not accepted:
        raise AccountingUnavailable(
            "Latest shared storage attempt is unavailable"
        ) from failure
    return images


def publish_shared_storage_failure(
    sessions, domain_id, enrollment_id, started, *, clock=None
):
    """Invalidate only this ordered shared attempt after pre-scan setup fails."""
    from app.services.native_collection_jobs import assert_collection_lease

    clock = clock or (lambda: datetime.now(UTC))
    started = utc(started)
    with sessions.begin() as db:
        domain = (
            db.query(NativeCollectionDomain)
            .filter_by(id=str(domain_id))
            .with_for_update()
            .populate_existing()
            .one()
        )
        assert_collection_lease()
        if domain.enrollment_id != str(enrollment_id):
            raise AccountingUnavailable("Shared storage enrollment changed")
        completed = utc(clock())
        if started > completed:
            raise AccountingUnavailable("Shared storage attempt starts in the future")
        _advance(db, domain, None, started, completed)
        db.flush()
        assert_collection_lease()


def reusable_shared_images(db, domain, now):
    """None allows legacy fallback; any existing failed/stale attempt raises.

    Consumers must retain these timestamps and separately recheck mutable ancestry.
    This does not authorize substituting shared bytes for private measurements.
    """
    row = db.get(NativeSharedStorageObservation, domain.id)
    if row is None:
        return None
    if (
        row.status != "complete"
        or row.enrollment_id != domain.enrollment_id
        or row.payload is None
        or digest(row.payload) != row.digest
        or utc(row.completed_at) > utc(now)
    ):
        raise AccountingUnavailable("Shared storage attempt unavailable")
    images = [ImageObservation.model_validate(i) for i in row.payload["images"]]
    oldest = min([utc(row.started_at), *[i.collected_at for i in images]])
    if utc(now) - oldest > timedelta(minutes=15):
        raise AccountingUnavailable("Shared storage evidence stale")
    if any(
        not utc(row.started_at) <= i.collected_at <= utc(row.completed_at)
        for i in images
    ):
        raise AccountingUnavailable("Shared storage evidence timestamp changed")
    from app.models.kubevirt_storage import NativeEnrollment
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    try:
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        bindings, proofs = _inputs(db, manifest)
        raw = row.payload.get("cache_absence_inventory")
        inventory = NativeInventory.model_validate(raw) if raw is not None else None
        _validate_images(
            images,
            bindings,
            proofs,
            utc(row.started_at),
            utc(row.completed_at),
            _absence_keys(inventory, bindings),
        )
    except (ValueError, TypeError, AttributeError) as error:
        raise AccountingUnavailable("Shared storage authority changed") from error
    return images, utc(row.started_at)
