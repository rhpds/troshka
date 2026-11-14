"""Remove only original project trash; accounting still requires later absence."""

from kubernetes.client.exceptions import ApiException

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeRetainedBinding,
    NativeStorageSnapshot,
)
from app.schemas.kubevirt_storage import (
    BackendImageState,
    ImageKey,
    NativeTrashRemoval,
    ProjectRoot,
)
from app.services import quota_cleanup as cleanup
from app.services import quota_operations as operations
from app.services.kubevirt_storage_enrollment import digest, utc
from app.services.kubevirt_storage_usage import MAX_REPORT_AGE


def _authorized_report(db, intent, proof, report):
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=str(intent.domain_id))
        .with_for_update()
        .one()
    )
    saved = db.get(NativeStorageSnapshot, str(report.report_id))
    now = operations.now()
    if (
        saved is None
        or not saved.accepted
        or domain.latest_report_id != saved.id
        or saved.domain_id != str(intent.domain_id)
        or saved.digest != digest(report)
        or not 0
        <= (now - utc(report.started_at)).total_seconds()
        <= MAX_REPORT_AGE.total_seconds()
        or intent.phase != "pending"
        or not intent.stop_proof
        or not intent.teardown_at
        or not intent.namespace_delete_at
        or not proof.objects_absent
        or proof.root != intent.root
        or proof.operation_id != intent.operation_id
        or proof.inventory_digest != intent.inventory_digest
        or set(proof.owned_images) != set(intent.owned_images)
        or utc(proof.observed_at) < utc(intent.teardown_at)
    ):
        raise ValueError("original trash cleanup authorization unavailable")


def _exclusive_delete_images(db, intent, report):
    delete_pvs = {
        v.uid
        for v in intent.objects
        if v.kind == "PersistentVolume" and v.reclaim_policy == "Delete"
    }
    keys = {
        b.image_key
        for b in intent.bindings
        if b.pv_uid in delete_pvs and b.root == intent.root
    }
    keys &= set(intent.owned_images)
    excluded = {b.image_key for b in report.cache_bindings}
    for row in db.query(NativeRetainedBinding).filter_by(
        domain_id=str(intent.domain_id)
    ):
        payload = row.payload
        root = (
            ProjectRoot.model_validate(payload["root"]) if "root" in payload else None
        )
        if root is not None:
            # Retained bindings predate the final controller-generation receipt;
            # preserve every immutable root field while using the current receipt.
            root = root.model_copy(
                update={"deployment_generation": intent.root.deployment_generation}
            )
        if root != intent.root:
            excluded.add(ImageKey.model_validate(payload["image_key"]))
    return sorted(keys - excluded, key=lambda key: key.model_dump_json())


def _namespace_absent(reader, intent):
    if reader.cluster_identity() != intent.root.cluster_key:
        raise ValueError("cleanup provider cluster changed")
    try:
        reader.namespace(intent.root.namespace)
    except ApiException as error:
        if error.status == 404:
            return
        raise
    raise ValueError("cleanup namespace still exists or was replaced")


def _remove_one(sessions, report, proof, readers, backend, key):
    with sessions() as db:
        _, _, intent = cleanup._checked_native(
            db, str(proof.root.project_id), str(proof.intent_id)
        )
        _authorized_report(db, intent, proof, report)
        receipt_key = digest(key)
        if receipt_key in intent.trash_removals or key not in _exclusive_delete_images(
            db, intent, report
        ):
            return
        reader = readers[str(intent.provider_id)]
        _namespace_absent(reader, intent)
        state = backend.image_inventory(key, max_objects=10000)
        if state.status != "complete" or not isinstance(state.value, BackendImageState):
            return
        value = state.value
        if (
            ImageKey(**{k: getattr(value, k) for k in ImageKey.model_fields}) != key
            or value.active
            or not value.trash
        ):
            return
        receipt = NativeTrashRemoval(
            image_key=key, report_id=report.report_id, submitted_at=operations.now()
        )
        intent = cleanup._save_native(
            db, intent, trash_removals={**intent.trash_removals, receipt_key: receipt}
        )
        db.commit()  # Persist before submission; ambiguity must never replay a removal.
        _, _, intent = cleanup._checked_native(
            db, str(proof.root.project_id), str(proof.intent_id)
        )
        _authorized_report(db, intent, proof, report)
        _namespace_absent(reader, intent)
        status = "uncertain"
        try:
            result = backend.remove_trash(key)
            if result.status == "complete":
                status = "accepted"
        finally:
            cleanup._save_native(
                db,
                intent,
                trash_removals={
                    **intent.trash_removals,
                    receipt_key: receipt.model_copy(update={"status": status}),
                },
            )
            db.commit()


def reconcile(sessions, report, readers, backend):
    """Normal cleanup stage, never a pool purge or an accounting release."""
    for proof in report.retiring_roots:
        for key in proof.owned_images:
            try:
                _remove_one(sessions, report, proof, readers, backend, key)
            except Exception:
                # Keep the durable pending/submitted state and full storage charge.
                # A later complete collector report is the only settlement authority.
                continue
