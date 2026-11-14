"""Trusted backend worker entrypoints for new protected S4 cache sources.

These functions own short transactions. Provider I/O always happens after a
committed intent/latch and outside locks. A missing create UID is irrecoverably
ambiguous for this operation; the same name is never adopted or recreated.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update

from app.models.kubevirt_cache_birth import NativeCacheBirth
from app.models.kubevirt_storage import NativeReportingTarget
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_cache_birth import (
    USE_POPULATOR,
    CacheBirthIntent,
    CacheBirthReceipt,
    CacheBirthReference,
    validate_import_annotations,
)
from app.services.kubevirt_storage_enrollment import digest
from app.services.native_collection_jobs import assert_collection_lease


def _new_intent(
    *,
    domain_id,
    target_id,
    target,
    namespace,
    namespace_uid,
    source,
    storage_request,
    secret_ref,
    operation_id=None,
    managed_authority=None,
):
    from kubernetes.utils.quantity import parse_quantity

    if parse_quantity(storage_request) <= 0:
        raise ValueError("positive final storage request required")
    operation_id = operation_id or uuid4()
    spec = {
        "source": {"s3": {"url": source.import_url(), "secretRef": secret_ref}},
        "pvc": {
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
            "resources": {"requests": {"storage": storage_request}},
            "storageClassName": target.storage_class_name,
        },
    }
    return CacheBirthIntent(
        operation_id=operation_id,
        domain_id=domain_id,
        target_id=target_id,
        target=target,
        namespace=namespace,
        namespace_uid=namespace_uid,
        source=source,
        name="managed-cache-" + operation_id.hex,
        spec=spec,
        spec_digest=digest(spec),
        created_at=datetime.now(UTC),
        managed_authority=managed_authority,
    )


def prepare_cache_birth(sessions, *, actor_id, **fields):
    """Administrative preparation, never exposed to collector or tenant input."""
    intent = _new_intent(**fields)
    with sessions.begin() as db:
        user = db.get(User, actor_id)
        provider = db.get(Provider, str(intent.target.provider_id))
        reporting = db.get(NativeReportingTarget, 1)
        if user is None or user.role != "admin" or not user.access_enabled:
            raise ValueError("active administrator required for cache creation")
        if provider is None or provider.type != "kubevirt":
            raise ValueError("native provider required")
        if reporting is None or reporting.target_id != str(intent.target_id):
            raise ValueError("reporting target changed")
        _insert(db, intent)
    return str(intent.operation_id)


def _insert(db, intent):
    db.add(
        NativeCacheBirth(
            id=str(intent.operation_id),
            domain_id=str(intent.domain_id),
            intent_digest=digest(intent),
            intent=intent.model_dump(mode="json"),
            state="prepared",
        )
    )


def prepare_managed_cache(sessions, operation_id, disk_id):
    """Internal admitted-operation authority; no actor override or impersonation."""
    from uuid import UUID

    from app.schemas.kubevirt_cache_birth import CacheSource
    from app.services.native_managed_images import managed_authority

    with sessions.begin() as db:
        target, profile, source, manifest, authority = managed_authority(
            db, operation_id, disk_id
        )
        existing = db.get(NativeCacheBirth, source["operation_id"])
        if existing is not None:
            intent = validated_intent(existing)
            _managed_current(db, intent)
            return existing.id
        intent = _new_intent(
            operation_id=UUID(source["operation_id"]),
            domain_id=target["domain_id"],
            target_id=manifest.request.target_id,
            target=profile.target,
            namespace=source["namespace"],
            namespace_uid=source["namespace_uid"],
            source=CacheSource.model_validate(source["source"]),
            storage_request=source["source_storage_request"],
            secret_ref=profile.source_policy.cache_secret_ref,
            managed_authority=authority,
        )
        _insert(db, intent)
        return str(intent.operation_id)


def _managed_current(db, intent, *, fresh=True):
    if intent.managed_authority is None:
        return
    from app.services.native_managed_images import managed_authority

    saved = intent.managed_authority
    target, profile, source, _, current = managed_authority(
        db, saved["operation_id"], saved["disk_id"], fresh=fresh
    )
    keys = {"enrollment_id", "initialization_observation"}
    if (
        {k: v for k, v in saved.items() if k not in keys}
        != {k: v for k, v in current.items() if k not in keys}
        or source["operation_id"] != str(intent.operation_id)
        or source["source"] != intent.source.model_dump(mode="json")
        or profile.target != intent.target
        or target["domain_id"] != str(intent.domain_id)
    ):
        raise ValueError("managed source operation authority changed")
    return current


def validated_intent(row):
    intent = CacheBirthIntent.model_validate(row.intent)
    if (
        str(intent.operation_id) != row.id
        or str(intent.domain_id) != row.domain_id
        or digest(intent) != row.intent_digest
    ):
        raise ValueError("cache intent content identity mismatch")
    return intent


def _response(intent, value):
    meta = value.get("metadata", {})
    validate_import_annotations(meta)
    if (
        not meta.get("uid")
        or meta.get("name") != intent.name
        or meta.get("namespace") != intent.namespace
        or value.get("spec") != intent.spec
        or meta.get("deletionTimestamp")
    ):
        raise ValueError("cache create response identity mismatch")
    # Never persist arbitrary response annotations or provider credentials.
    return {
        "metadata": {
            **{k: meta[k] for k in ("name", "namespace", "uid", "creationTimestamp")},
            "annotations": {USE_POPULATOR: "false"},
        },
        "spec": value["spec"],
    }


def submit_cache_birth(sessions, operation_id, adapter):
    """At most one create attempt, enforced by a DB conditional transition."""
    with sessions.begin() as db:
        row = db.get(NativeCacheBirth, operation_id)
        if row is None:
            raise ValueError("unknown cache operation")
        intent = validated_intent(row)
        dispatch_evidence = _managed_current(db, intent)
        if row.state in ("created", "verified"):
            return
        changed = db.execute(
            update(NativeCacheBirth)
            .where(
                NativeCacheBirth.id == operation_id,
                NativeCacheBirth.state == "prepared",
            )
            .values(state="submitted")
        ).rowcount
        if changed != 1:
            raise ValueError("ambiguous cache create remains pending; never recreate")
        if dispatch_evidence is not None:
            from app.models.quota import QuotaReservation

            operation = (
                db.query(QuotaReservation)
                .filter_by(operation_id=dispatch_evidence["operation_id"])
                .one()
            )
            operation.dispatch_metadata = {
                **operation.dispatch_metadata,
                "native_cache_dispatch": {
                    **operation.dispatch_metadata.get("native_cache_dispatch", {}),
                    str(intent.operation_id): {
                        "intent_digest": digest(intent),
                        "authority": dispatch_evidence,
                    },
                },
            }
    adapter.preflight(intent)
    response = _response(intent, adapter.create(intent))
    with sessions.begin() as db:
        changed = db.execute(
            update(NativeCacheBirth)
            .where(
                NativeCacheBirth.id == operation_id,
                NativeCacheBirth.state == "submitted",
                NativeCacheBirth.intent_digest == digest(intent),
            )
            .values(state="created", create_response=response)
        ).rowcount
        if changed != 1:
            raise ValueError("cache creation journal changed")


def validated_receipt(db, operation_id, expected_digest):
    """Protected accessor; a collector cannot turn a submitted proof into authority."""
    row = db.get(NativeCacheBirth, str(operation_id))
    if row is None or row.state != "verified" or row.receipt is None:
        raise ValueError("cache birth is not independently verified")
    intent = validated_intent(row)
    receipt = CacheBirthReceipt.model_validate(row.receipt)
    if (
        receipt.intent_digest != digest(intent)
        or str(receipt.operation_id) != row.id
        or receipt.dv_uid != row.create_response["metadata"]["uid"]
        or digest(receipt) != row.receipt_digest
        or row.receipt_digest != expected_digest
    ):
        raise ValueError("cache receipt content identity mismatch")
    return intent, receipt


def verify_cache_birth(sessions, operation_id, adapter):
    """Two independent fresh observations, then one conditional publication."""
    from app.services.native_cache_verification import (
        stable_receipt,
        verify_observation,
    )

    with sessions() as db:
        row = db.get(NativeCacheBirth, operation_id)
        if row is None or row.state not in ("created", "verified"):
            raise ValueError("cache create lacks an unambiguous original response")
        if row.state == "verified":
            validated_receipt(db, operation_id, row.receipt_digest)
            return CacheBirthReference(
                operation_id=operation_id, digest=row.receipt_digest
            )
        intent, response = validated_intent(row), row.create_response
        _managed_current(db, intent, fresh=False)
    first = verify_observation(intent, response, adapter.observe(intent, response))
    last = verify_observation(intent, response, adapter.observe(intent, response))
    if stable_receipt(first) != stable_receipt(last):
        raise ValueError("cache identity changed during independent verification")
    reference = CacheBirthReference(operation_id=operation_id, digest=digest(last))
    with sessions.begin() as db:
        _managed_current(db, intent, fresh=False)
        changed = db.execute(
            update(NativeCacheBirth)
            .where(
                NativeCacheBirth.id == operation_id,
                NativeCacheBirth.state == "created",
                NativeCacheBirth.intent_digest == digest(intent),
            )
            .values(
                state="verified",
                receipt=last.model_dump(mode="json"),
                receipt_digest=reference.digest,
            )
        ).rowcount
        if changed != 1:
            row = db.get(NativeCacheBirth, operation_id)
            _, stored = validated_receipt(db, operation_id, row.receipt_digest)
            if stable_receipt(stored) != stable_receipt(last):
                raise ValueError("concurrent cache verification identity changed")
            reference = CacheBirthReference(
                operation_id=operation_id, digest=row.receipt_digest
            )
        assert_collection_lease()
    return reference
