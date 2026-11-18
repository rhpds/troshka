"""Resolve actual origin/admin custody without manufacturing accounting history."""

from typing import TYPE_CHECKING, cast

from app.schemas.native_project_cleanup import CleanupAuthority, StorageLiabilities
from app.services import native_deployment_origin as origin
from app.services.kubevirt_storage_enrollment import digest


def resolve(db, project):
    intent = origin.active(db, project.id)
    if intent is not None:
        return _origin(db, project, intent)
    return _administrator(db, project)


def _origin(db, project, intent):
    from app.services import native_origin_custody as custody

    try:
        custody._active_identity(db, intent)
    except ValueError as error:
        raise origin.AccountingUnavailable(
            "Origin owner/provider identity changed"
        ) from error
    namespace, root_receipt = origin.receipts(db, str(intent.id))
    origin.require(
        namespace is not None
        and namespace.namespace_delete_authority
        and intent.namespace_mode == "managed",
        "Original managed namespace receipt required",
    )
    if TYPE_CHECKING:
        assert namespace is not None
    held = custody.retained(db, project, require_complete=False)
    source = dict(
        intent=intent.model_dump(mode="json"),
        namespace=namespace.model_dump(mode="json"),
        root=root_receipt.model_dump(mode="json") if root_receipt else None,
        custody=[held[0].id, held[0].digest] if held else None,
    )
    authority = CleanupAuthority(
        source_kind="origin",
        source_id=intent.id,
        source_digest=digest(source),
        project_id=intent.project_id,
        owner_id=intent.owner_id,
        host_id=intent.host_id,
        provider_id=intent.provider_id,
        cluster_uid=intent.cluster_uid,
        namespace=intent.namespace,
        namespace_uid=namespace.namespace_uid,
        root_uid=root_receipt.root_uid if root_receipt else None,
        domain_id=held[0].domain_id if held else None,
        enrollment_id=held[1].enrollment_id if held else None,
        root=held[2] if held else None,
    )
    if held is None:
        return authority, StorageLiabilities(
            unresolved=["storage_coverage_unavailable"]
        )
    receipt = held[1]
    debt = _remote_debt(db, str(intent.id))
    if not receipt.complete:
        debt.append("storage_coverage_incomplete")
    debt += ["pending_pvc:" + p.pvc_uid for p in custody.unresolved_claims(receipt)]
    known_snapshots = {p.producer_id for p in receipt.snapshots}
    debt += [
        "pending_snapshot:" + str(p.id)
        for p in custody.unresolved_snapshots(receipt)
        if p.id not in known_snapshots
    ]
    from app.services.native_origin_ops_state import cleanup_obligations

    _, boundaries = cleanup_obligations(db, str(intent.id))
    return authority, _liabilities(receipt, held[2], debt).model_copy(
        update={"support_boundaries": boundaries}
    )


def _remote_debt(db, intent_id):
    from app.models.native_origin_descendants import NativeOriginRemoteHold
    from app.services import native_origin_ops_state as ops
    from app.services import native_origin_writers as writers

    debt = []
    for writer in writers.registrations(db, intent_id):
        hold = db.get(NativeOriginRemoteHold, writer.id)
        if hold is not None:
            writers._checked(hold)
            debt.append("remote_outcome_unresolved:" + writer.id)
    unknown, _ = ops.cleanup_obligations(db, intent_id)
    debt.extend(unknown)
    return debt


def _administrator(db, project):
    from app.services import native_online_enrollment as enrollment

    held = enrollment.retained(db, project, require_topology=False)
    if held is None:
        return None
    row, receipt, root = held
    enrollment._require_cleanup_authority(receipt, project.id)
    identity = next(
        p for p in receipt.prepared.request.projects if str(p.project_id) == project.id
    )
    authority = CleanupAuthority(
        source_kind="admin_custody",
        source_id=row.id,
        source_digest=row.digest,
        project_id=project.id,
        owner_id=identity.owner_id,
        host_id=identity.host_id,
        provider_id=identity.provider_id,
        cluster_uid=root.cluster_key,
        namespace=root.namespace,
        namespace_uid=root.namespace_uid,
        root_uid=root.project_cr_uid,
        root=root,
        domain_id=receipt.enrollment.pins.domain_id,
        enrollment_id=row.payload["enrollment_id"],
    )
    return authority, _liabilities(receipt, root, [])


def _liabilities(receipt, root, debt):
    bindings = [b for b in receipt.bindings if b.root == root]
    ancestry = [a for a in receipt.ancestry if a.binding.root == root]
    snapshots = [p for p in getattr(receipt, "snapshots", []) if p.source.root == root]
    restores = [
        p for p in getattr(receipt, "snapshot_restores", []) if p.binding.root == root
    ]
    private = {b.image_key for b in bindings} | {a.intermediate for a in ancestry}
    private |= {p.image for p in snapshots} | {p.binding.image_key for p in restores}
    shared = {a.source.image_key for a in ancestry}
    origin.require(not private & shared, "Private storage overlaps shared source")
    return StorageLiabilities(
        bindings=bindings,
        ancestry=ancestry,
        private_images=sorted(private, key=lambda k: k.model_dump_json()),
        shared_images=sorted(shared, key=lambda k: k.model_dump_json()),
        unresolved=sorted(debt),
        producers=getattr(receipt, "producers", []),
        snapshots=snapshots,
        snapshot_restores=restores,
        snapshot_retirements=getattr(receipt, "snapshot_retirements", []),
    )


def lock(db, project, expected=None):
    """Target/domain precede canonical operation and project locks; no remote I/O."""
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeReportingTarget,
    )
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_enrollment import retained_enrollment

    selected = resolve(db, project)
    if selected is None:
        return None
    authority, _ = selected
    if authority.domain_id is not None:
        db.query(NativeReportingTarget).filter_by(
            id=1
        ).populate_existing().with_for_update().one()
        domain = (
            db.query(NativeCollectionDomain)
            .filter_by(id=str(authority.domain_id))
            .populate_existing()
            .with_for_update()
            .one()
        )
        origin.require(
            authority.root is not None, "Cleanup retained enrollment root unavailable"
        )
        if TYPE_CHECKING:
            assert authority.root is not None
        retained_enrollment(
            db,
            domain,
            dict(
                enrollment_id=str(authority.enrollment_id),
                root=authority.root.model_dump(mode="json"),
            ),
        )
    quota_cleanup.fence(db, project.id)
    db.refresh(project)
    current = resolve(db, project)
    origin.require(
        current == selected
        and (
            expected is None
            or cast(tuple[CleanupAuthority, StorageLiabilities], current)[0] == expected
        ),
        "Cleanup custody predecessor or owner/provider changed",
    )
    return current


def enrollment_boundary(db, selected):
    """Current live predecessor, distinct from the original custody authority."""
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    if selected.domain_id is None:
        return None
    domain = db.get(NativeCollectionDomain, str(selected.domain_id))
    row = (
        db.query(NativeEnrollment)
        .filter_by(id=domain.enrollment_id)
        .populate_existing()
        .one()
    )
    manifest = validated_enrollment(row)
    return dict(enrollment_id=row.id, digest=manifest.digest)


def local_writers_closed(db, authority):
    """Exact local invocation exits; remote debt is deliberately not called closed."""
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_descendants as descendants
    from app.services import native_origin_writers as writers

    if authority.source_kind != "origin":
        from app.services import quota_operations

        rows = quota_operations._rows(db, str(authority.project_id))
        origin.require(
            all(
                not r.dispatch_metadata.get("claim")
                or r.dispatch_metadata.get("worker_finished")
                for r in rows
            ),
            "Original provider invocation is still running",
        )
        return
    intent_id = str(authority.source_id)
    descendants.close_origin(db, intent_id)
    for writer in writers.registrations(db, intent_id):
        terminal = db.get(NativeOriginWriterTerminal, writer.id)
        origin.require(
            terminal is not None, "Original local writer is still running or unobserved"
        )
        value = writers._checked(terminal)
        origin.require(
            value.get("version") == 2
            and value.get("writer_digest") == writer.digest
            and value.get("execution") == writer.payload,
            "Original local writer exit changed",
        )
    for handoff in descendants._handoffs(db, intent_id, all_parents=True):
        claim = db.get(descendants.Claim, handoff.id)
        origin.require(
            claim is not None
            and writers._checked(claim).get("handoff_digest") == handoff.digest,
            "Original descendant dispatch is unresolved",
        )
