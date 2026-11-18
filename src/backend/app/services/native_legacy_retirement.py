"""Operator-only receipts for previously manually deleted legacy roots.

Custody establishes identity, never deletion authority. The original operator
audit and fresh provider absence establish retrospective accounting retirement.
No namespace cleanup State, deletion request, or automated settlement is created.
"""

from datetime import datetime
from uuid import uuid4

from app.models.native_project_cleanup import NativeProjectNamespaceEvidence as Evidence
from app.models.project import Project
from app.models.quota import QuotaAudit
from app.schemas.kubevirt_storage import (
    ImageKey,
    ImageObservation,
    NativeInventory,
    VolumeBinding,
)
from app.schemas.native_project_cleanup import CleanupStorageScope
from app.services import native_manual_retirement as manual
from app.services.kubevirt_storage_enrollment import digest, root_key, utc
from app.services.native_binding_retirement import require

PHASE = "manual-accounting-root-verified"


def _source(db, audit_id):
    from app.services.native_custody_reconciliation import effective

    audit = db.get(QuotaAudit, str(audit_id))
    require(
        audit is not None
        and audit.scope == "project"
        and audit.action == "manual-legacy-project-retirement",
        "Original operator deletion audit required",
    )
    assert audit is not None
    require(db.get(Project, audit.scope_id) is None, "Legacy project still exists")
    held = effective(db, audit.scope_id)
    require(held is not None, "Original accounting custody unavailable")
    assert held is not None
    row, receipt = held
    roots = [
        r
        for r in receipt.enrollment.pins.project_roots
        if str(r.project_id) == audit.scope_id
    ]
    require(len(roots) == 1, "Legacy root identity ambiguous")
    root = roots[0]
    value = audit.after
    require(
        value.get("method") == "user-authorized-manual-legacy-cleanup"
        and value.get("namespace") == root.namespace
        and value.get("namespace_uid") == root.namespace_uid
        and value.get("namespace_absent") is True
        and value.get("pv_absent") is True
        and value.get("automatic_settlement_claimed") is False,
        "Original manual deletion identity changed",
    )
    source = {
        "audit_id": str(audit.id),
        "audit_digest": digest(value),
        "custody_id": str(row.id),
        "custody_digest": row.digest,
    }
    return audit, receipt, root, source


def _captured(capture):
    """Require every preserved pre-deletion PV to have an exact image binding."""
    require(bool(capture.get("provenance")), "Capture provenance required")
    volumes = capture["pvs"]
    bindings = [VolumeBinding.model_validate(v) for v in capture["bindings"]]
    require(
        bool(volumes)
        and len({v["claim_uid"] for v in volumes}) == len(volumes)
        and len({b.pvc_uid for b in bindings}) == len(bindings)
        and {v["claim_uid"] for v in volumes} == {b.pvc_uid for b in bindings},
        "Captured PV inventory is incomplete",
    )
    by_uid = {b.pvc_uid: b for b in bindings}
    for volume in volumes:
        binding = by_uid[volume["claim_uid"]]
        require(
            binding.pv_uid == volume["uid"]
            and binding.csi_driver == volume["driver"]
            and binding.volume_handle == volume["handle"],
            "Captured PV identity changed",
        )
    return bindings


def prepare(db, audit_id, capture):
    """Build a read-only verification scope from retained and captured identities."""
    from app.services.native_cleanup_authority import _liabilities

    audit, receipt, root, source = _source(db, audit_id)
    source["capture_digest"] = digest(capture)
    liabilities = _liabilities(receipt, root, [])
    bindings = {b.pvc_uid: b for b in liabilities.bindings}
    for binding in _captured(capture):
        require(binding.root == root, "Captured binding belongs to another root")
        require(
            binding.pvc_uid not in bindings or bindings[binding.pvc_uid] == binding,
            "Captured PVC identity conflicts with retained custody",
        )
        bindings[binding.pvc_uid] = binding
    private = set(liabilities.private_images).union(
        b.image_key for b in bindings.values()
    )
    known = audit.after.get("storage_verification", {})
    require(
        set(ImageKey.model_validate(k) for k in known.get("private_images", []))
        <= private,
        "Original private storage omitted",
    )
    require(
        set(ImageKey.model_validate(k) for k in known.get("shared_images", []))
        <= set(liabilities.shared_images),
        "Original shared storage omitted",
    )
    require(
        bool(bindings)
        and not private & set(liabilities.shared_images)
        and not liabilities.unresolved
        and {p.id for p in liabilities.producers}
        == {s.producer_id for s in liabilities.snapshots},
        "Legacy storage identities incomplete",
    )
    liabilities = liabilities.model_copy(
        update={
            "bindings": sorted(bindings.values(), key=lambda b: b.pvc_uid),
            "private_images": sorted(private, key=lambda k: k.model_dump_json()),
        }
    )
    scope = CleanupStorageScope(
        intent_id=audit.id,
        operation_id="manual-legacy-accounting:" + str(audit.id),
        domain_id=receipt.enrollment.pins.domain_id,
        root=root,
        phase="finalized",
        inventory_digest=digest(liabilities.model_dump(mode="json")),
        owned_images=liabilities.private_images,
        bindings=liabilities.bindings,
        teardown_at=utc(datetime.fromisoformat(audit.after["observed_at"])),
        liabilities=liabilities,
    )
    return scope, source


def record(db, scope, source, capture, inventory, images, *, administrator, reason):
    require(
        bool(administrator.strip()) and bool(reason.strip()),
        "Operator attribution required",
    )
    current, current_source = prepare(db, scope.intent_id, capture)
    require(
        current == scope and current_source == source,
        "Legacy verification predecessor changed",
    )
    proof = manual._verify(scope, inventory, images)
    payload = dict(
        source=source,
        capture=capture,
        administrator=administrator,
        reason=reason,
        inventory=inventory.model_dump(mode="json"),
        images=[i.model_dump(mode="json") for i in images],
        proof=proof.model_dump(mode="json"),
    )
    existing = (
        db.query(Evidence)
        .filter_by(cleanup_id=str(scope.intent_id), phase=PHASE)
        .one_or_none()
    )
    require(existing is None, "Legacy accounting receipt already exists")
    db.add(
        Evidence(
            id=str(uuid4()),
            cleanup_id=str(scope.intent_id),
            phase=PHASE,
            digest=digest(payload),
            payload=payload,
        )
    )
    db.flush()
    return root_key(scope.root)


def settled_keys(db):
    result = set()
    for row in db.query(Evidence).filter_by(phase=PHASE):
        require(row.digest == digest(row.payload), "Legacy accounting receipt changed")
        payload = row.payload
        scope, source = prepare(db, row.cleanup_id, payload["capture"])
        require(source == payload["source"], "Legacy accounting provenance changed")
        proof = manual._verify(
            scope,
            NativeInventory.model_validate(payload["inventory"]),
            [ImageObservation.model_validate(i) for i in payload["images"]],
        )
        require(
            proof.model_dump(mode="json") == payload["proof"],
            "Legacy accounting proof changed",
        )
        result.add(root_key(scope.root))
    return result


def reconcile_configured(audit_id, capture, *, administrator, reason, apply=False):
    from app.core.database import SessionLocal
    from app.services.native_managed_collector import anchored

    with SessionLocal() as db:
        scope, source = prepare(db, audit_id, capture)
        if root_key(scope.root) in settled_keys(db):
            return {"root_key": root_key(scope.root), "status": "already_verified"}
    inventory, images, pins, enrollment_id = manual.observe_configured(scope)
    with SessionLocal() as db:
        domain, _ = anchored(db, pins)
        require(
            domain.enrollment_id == enrollment_id,
            "Enrollment changed during verification",
        )
        if not apply:
            return {"root_key": root_key(scope.root), "status": "verified_dry_run"}
        key = record(
            db,
            scope,
            source,
            capture,
            inventory,
            images,
            administrator=administrator,
            reason=reason,
        )
        db.commit()
        return {"root_key": key, "status": "recorded"}
