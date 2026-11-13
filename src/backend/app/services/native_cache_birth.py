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


def prepare_cache_birth(
    sessions,
    *,
    actor_id,
    domain_id,
    target_id,
    target,
    namespace,
    namespace_uid,
    source,
    storage_request,
    secret_ref,
):
    """Administrative preparation, never exposed to collector or tenant input."""
    from kubernetes.utils.quantity import parse_quantity

    if parse_quantity(storage_request) <= 0:
        raise ValueError("positive final storage request required")
    operation_id = uuid4()
    spec = {
        "source": {"s3": {"url": source.import_url(), "secretRef": secret_ref}},
        "pvc": {
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
            "resources": {"requests": {"storage": storage_request}},
            "storageClassName": target.storage_class_name,
        },
    }
    intent = CacheBirthIntent(
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
    )
    with sessions.begin() as db:
        user = db.get(User, actor_id)
        provider = db.get(Provider, str(target.provider_id))
        reporting = db.get(NativeReportingTarget, 1)
        if user is None or user.role != "admin" or not user.access_enabled:
            raise ValueError("active administrator required for cache creation")
        if provider is None or provider.type != "kubevirt":
            raise ValueError("native provider required")
        if reporting is None or reporting.target_id != str(target_id):
            raise ValueError("reporting target changed")
        db.add(
            NativeCacheBirth(
                id=str(operation_id),
                domain_id=str(domain_id),
                intent_digest=digest(intent),
                intent=intent.model_dump(mode="json"),
                state="prepared",
            )
        )
    return str(operation_id)


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
    first = verify_observation(intent, response, adapter.observe(intent, response))
    last = verify_observation(intent, response, adapter.observe(intent, response))
    if stable_receipt(first) != stable_receipt(last):
        raise ValueError("cache identity changed during independent verification")
    reference = CacheBirthReference(operation_id=operation_id, digest=digest(last))
    with sessions.begin() as db:
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
    return reference
