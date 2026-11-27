"""Verify already released map-based projects without deleting provider resources."""

from app.models.project import Project
from app.schemas.kubevirt_storage import ImageObservation, NativeInventory
from app.services import native_manual_retirement as manual
from app.services import native_project_namespace_cleanup as cleanup
from app.services.kubevirt_storage_enrollment import digest, root_key
from app.services.native_binding_retirement import require

PHASE = "map-storage-verified"


def _source(db, work):
    require(
        work.mode == "delete"
        and work.phase in {"orphan_reclaim", "finalized"}
        and work.namespace_delete_at is not None
        and not work.liabilities.unresolved,
        "Map retirement requires released cleanup with resolved identities",
    )
    require(
        db.get(Project, str(work.authority.project_id)) is None,
        "Map retirement project still exists",
    )
    evidence = {}
    for phase in ("namespace_absent", "project-released"):
        row = (
            db.query(cleanup.Evidence)
            .filter_by(cleanup_id=str(work.id), phase=phase)
            .one_or_none()
        )
        if row is None or row.digest != digest(row.payload):
            raise ValueError("Map retirement release evidence unavailable")
        evidence[phase] = row.digest
    # Active siblings may resolve enrollment through settled_keys again. Verify
    # this retained receipt's scope without traversing unrelated live authority.
    scopes = cleanup.storage_scopes(db, cleanup_id=work.id)
    require(len(scopes) == 1, "Map retirement storage scope unavailable")
    return scopes[0], evidence


def record(db, previous, inventory, images, *, administrator, reason):
    row = db.query(cleanup.State).filter_by(id=str(previous.id)).with_for_update().one()
    work = cleanup.load(row)
    require(work == previous, "Cleanup changed during map verification")
    require(
        bool(administrator.strip()) and bool(reason.strip()),
        "Map retirement attribution required",
    )
    scope, source = _source(db, work)
    proof = manual._verify(scope, inventory, images)
    work = cleanup._save(db, work, phase="finalized", last_error=None)
    db.flush()
    payload = dict(
        work_digest=row.digest,
        source=source,
        administrator=administrator,
        reason=reason,
        inventory=inventory.model_dump(mode="json"),
        images=[i.model_dump(mode="json") for i in images],
        proof=proof.model_dump(mode="json"),
    )
    cleanup._evidence(db, work, PHASE, payload)
    return root_key(scope.root)


def settled_keys(db):
    result = set()
    for row in db.query(cleanup.Evidence).filter_by(phase=PHASE):
        require(row.digest == digest(row.payload), "Map retirement receipt changed")
        state = db.get(cleanup.State, row.cleanup_id)
        work = cleanup.load(state)
        scope, source = _source(db, work)
        payload = row.payload
        require(
            state.digest == payload["work_digest"] and source == payload["source"],
            "Map retirement predecessor changed",
        )
        proof = manual._verify(
            scope,
            NativeInventory.model_validate(payload["inventory"]),
            [ImageObservation.model_validate(v) for v in payload["images"]],
        )
        require(
            proof.model_dump(mode="json") == payload["proof"],
            "Map retirement proof changed",
        )
        result.add(root_key(scope.root))
    return result


def reconcile_configured(cleanup_id, *, administrator, reason, apply=False):
    from app.core.database import SessionLocal
    from app.services.native_managed_collector import anchored

    with SessionLocal() as db:
        work = cleanup.load(db.get(cleanup.State, str(cleanup_id)))
        scope, _ = _source(db, work)
        if root_key(scope.root) in settled_keys(db):
            return {"status": "already_verified", "root_key": root_key(scope.root)}
    inventory, images, pins, enrollment_id = manual.observe_configured(scope)
    with SessionLocal() as db:
        domain, _ = anchored(db, pins)
        require(
            domain.enrollment_id == enrollment_id,
            "Enrollment changed during map verification",
        )
        if not apply:
            return {"status": "verified_dry_run", "root_key": root_key(scope.root)}
        key = record(
            db, work, inventory, images, administrator=administrator, reason=reason
        )
        db.commit()
        return {"status": "recorded", "root_key": key}
