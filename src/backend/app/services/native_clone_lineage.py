"""Protected lineage seam for the existing managed-deploy operation.

No create is dispatched here. Task4's operator create journal supplies original
DV identity; a configured backend observer independently resolves DV/PVC/PV/CSI
and immutable RBD lineage. Collector payloads can only reference terminal rows.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update

from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
from app.models.native_clone_lineage import NativeCloneLineage
from app.models.quota import QuotaReservation
from app.schemas.kubevirt_cache_birth import CacheBirthReference
from app.schemas.kubevirt_storage import CacheBinding, CloneReference, ProjectRoot
from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
from app.schemas.native_clone_lineage import (
    CloneCreation,
    CloneIntent,
    CloneLineage,
    CloneObservation,
)
from app.services.kubevirt_storage_enrollment import digest, root_identity
from app.services.native_cache_birth import validated_receipt
from app.services.native_collection_jobs import assert_collection_lease


def _source(db, reference):
    intent, receipt = validated_receipt(db, reference.operation_id, reference.digest)
    binding = CacheBinding(
        cluster_key=intent.target.cluster_key,
        namespace=intent.namespace,
        namespace_uid=intent.namespace_uid,
        pvc_uid=receipt.pvc_uid,
        pv_uid=receipt.pv_uid,
        csi_driver=receipt.csi_driver,
        volume_handle=receipt.volume_handle,
        image_key=receipt.image_key,
    )
    return intent, binding


def prepare_clone(db, *, operation_id, claim_name, cache_reference):
    """Caller commits before publishing the existing initialize request.

    Requires its existing admitted managed operation, not an administrator alias.
    Repeated preparation returns only the same frozen claim/source identity.
    """
    reference = CacheBirthReference.model_validate(cache_reference)
    operation = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .with_for_update()
        .one()
    )
    target = (operation.dispatch_metadata or {}).get("native_deploy", {})
    if (
        operation.state not in ("pending", "dispatched", "reconciling")
        or operation.dispatch_metadata.get("cancelled")
        or target.get("phase") != "bound"
        or not operation.dispatch_metadata.get("claim")
        or operation.dispatch_metadata.get("generation_id") != operation_id
    ):
        raise ValueError("bound admitted managed deployment required")
    root = ProjectRoot.model_validate(target["root"])
    birth, source = _source(db, reference)
    domain = db.get(NativeCollectionDomain, target["domain_id"])
    manifest = VerifiedEnrollment.model_validate(
        db.get(NativeEnrollment, domain.enrollment_id).payload
    )
    if (
        str(root.project_id) != operation.project_id
        or claim_name not in target["claims"]
        or reference not in manifest.pins.cache_birth_receipts
        or str(birth.domain_id) != target["domain_id"]
        or str(birth.target.provider_id) != target["provider_id"]
        or birth.target.cluster_key != root.cluster_key
        or birth.target_id != manifest.request.target_id
    ):
        raise ValueError("clone source or managed operation scope changed")
    previous = (
        db.query(NativeCloneLineage)
        .filter_by(operation_id=operation_id, claim_name=claim_name)
        .one_or_none()
    )
    if previous is not None:
        intent = _intent(previous)
        _current_operation(db, intent)
        if intent.cache_reference != reference or root_identity(
            intent.root
        ) != root_identity(root):
            raise ValueError("clone intent changed")
        return previous.id
    identity = uuid4()
    intent = CloneIntent(
        lineage_id=identity,
        operation_id=operation_id,
        claim_name=claim_name,
        root=root,
        domain_id=birth.domain_id,
        target_id=birth.target_id,
        target=birth.target,
        cache_reference=reference,
        source=source,
        profile_digest=target["profile_digest"],
        plan_digest=target["payload"]["spec"]["managedPlanDigest"],
        demand_digest=digest(operation.requested),
        created_at=datetime.now(UTC),
    )
    db.add(
        NativeCloneLineage(
            id=str(identity),
            operation_id=operation_id,
            claim_name=claim_name,
            domain_id=str(birth.domain_id),
            intent=intent.model_dump(mode="json"),
            intent_digest=digest(intent),
        )
    )
    db.flush()
    return str(identity)


def _intent(row):
    intent = CloneIntent.model_validate(row.intent)
    if (
        str(intent.lineage_id),
        intent.operation_id,
        intent.claim_name,
        str(intent.domain_id),
        digest(intent),
    ) != (row.id, row.operation_id, row.claim_name, row.domain_id, row.intent_digest):
        raise ValueError("clone intent content identity mismatch")
    return intent


def record_clone_created(db, lineage_id, creation):
    """Trusted transfer of the original operator DV POST journal, never GET adoption.

    The managed worker must verify the operator's exact request/plan/root and its
    original returned UID before calling. There is no report/upload endpoint.
    """
    creation = CloneCreation.model_validate(creation)
    row = (
        db.query(NativeCloneLineage)
        .filter_by(id=str(lineage_id))
        .with_for_update()
        .one()
    )
    intent = _intent(row)
    _current_operation(db, intent)
    if (creation.operation_id, creation.dv_name, creation.namespace_uid) != (
        intent.operation_id,
        intent.claim_name,
        intent.root.namespace_uid,
    ):
        raise ValueError("original clone creation identity mismatch")
    payload = creation.model_dump(mode="json")
    if row.creation is not None and row.creation != payload:
        raise ValueError("original clone creation cannot be replaced")
    row.creation = payload
    db.flush()


def _current_operation(db, intent):
    operation = (
        db.query(QuotaReservation).filter_by(operation_id=intent.operation_id).one()
    )
    metadata = operation.dispatch_metadata or {}
    target = metadata.get("native_deploy", {})
    if (
        metadata.get("cancelled")
        or metadata.get("generation_id") != intent.operation_id
        or str(intent.root.project_id) != operation.project_id
        or target.get("root") != intent.root.model_dump(mode="json")
        or target.get("profile_digest") != intent.profile_digest
        or target.get("payload", {}).get("spec", {}).get("managedPlanDigest")
        != intent.plan_digest
        or digest(operation.requested) != intent.demand_digest
    ):
        raise ValueError("managed clone operation changed")


def _validate_proof(db, row, proof):
    intent = _intent(row)
    creation = CloneCreation.model_validate(row.creation)
    birth, source = _source(db, intent.cache_reference)
    expected = dict(
        lineage_id=intent.lineage_id,
        operation_id=intent.operation_id,
        claim_name=intent.claim_name,
        domain_id=intent.domain_id,
        target_id=intent.target_id,
        cache_reference=intent.cache_reference,
        source=source,
        original_dv_uid=creation.dv_uid,
    )
    if any(getattr(proof, k) != v for k, v in expected.items()) or (
        root_identity(proof.binding.root) != root_identity(intent.root)
        or proof.binding.csi_driver != birth.target.provisioner
        or proof.verified_at < intent.created_at
        or source != intent.source
        or birth.target != intent.target
    ):
        raise ValueError("clone lineage identity mismatch")
    return intent


def validated_lineage(db, lineage_id, expected_digest):
    row = db.get(NativeCloneLineage, str(lineage_id))
    if row is None or row.proof is None or row.creation is None:
        raise ValueError("clone lineage is not independently verified")
    proof = CloneLineage.model_validate(row.proof)
    _validate_proof(db, row, proof)
    if digest(proof) != row.proof_digest or row.proof_digest != expected_digest:
        raise ValueError("clone proof content identity mismatch")
    return proof


def verify_clone(sessions, lineage_id, adapter):
    """Observe twice outside DB locks; publish once, never rewrite snapshot authority.

    adapter.observe_clone independently verifies the original DV create journal,
    Succeeded/direct ownership, writer absence, installed target, CSI request
    mapping and supported temporary clone chain. It returns CloneObservation.
    Actual provider implementation and invocation belong to task4.
    """
    with sessions() as db:
        row = db.get(NativeCloneLineage, str(lineage_id))
        if row is None or row.creation is None:
            raise ValueError("original managed clone create journal required")
        if row.proof is not None:
            validated_lineage(db, lineage_id, row.proof_digest)
            return CloneReference(lineage_id=lineage_id, digest=row.proof_digest)
        intent, creation = _intent(row), CloneCreation.model_validate(row.creation)
        _current_operation(db, intent)
        expected_intent = row.intent_digest
    observations = [
        CloneObservation.model_validate(adapter.observe_clone(intent, creation))
        for _ in range(2)
    ]
    first, last = observations
    if (
        first.target != intent.target
        or last.target != intent.target
        or (
            first.lineage.model_dump(exclude={"verified_at"})
            != last.lineage.model_dump(exclude={"verified_at"})
        )
    ):
        raise ValueError("clone identity changed during verification")
    proof = last.lineage
    with sessions.begin() as db:
        row = db.get(NativeCloneLineage, str(lineage_id))
        _current_operation(db, intent)
        _validate_proof(db, row, proof)
        changed = db.execute(
            update(NativeCloneLineage)
            .where(
                NativeCloneLineage.id == str(lineage_id),
                NativeCloneLineage.proof.is_(None),
                NativeCloneLineage.intent_digest == expected_intent,
            )
            .values(proof=proof.model_dump(mode="json"), proof_digest=digest(proof))
        ).rowcount
        if changed != 1:
            db.expire(row)
            winner = validated_lineage(db, lineage_id, row.proof_digest)
            if winner.model_dump(exclude={"verified_at"}) != proof.model_dump(
                exclude={"verified_at"}
            ):
                raise ValueError("concurrent clone lineage changed")
            proof = winner
        assert_collection_lease()
    return CloneReference(lineage_id=lineage_id, digest=digest(proof))


def resolve_lineages(db, references, manifest):
    from app.services.kubevirt_storage_enrollment import root_key
    from app.services.quota_cleanup import settled_root_keys

    retired = settled_root_keys(db)
    proofs, seen = [], set()
    for reference in references:
        proof = validated_lineage(db, reference.lineage_id, reference.digest)
        if (
            reference.lineage_id in seen
            or proof.domain_id != manifest.pins.domain_id
            or proof.target_id != manifest.request.target_id
            or proof.cache_reference not in manifest.pins.cache_birth_receipts
        ):
            raise ValueError("clone authority is foreign or duplicated")
        seen.add(reference.lineage_id)
        if root_key(proof.binding.root) not in retired:
            proofs.append(proof)
    return proofs
