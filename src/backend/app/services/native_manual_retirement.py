"""Operator-only reconciliation of already deleted, manually retired projects.

No tenant API accepts these proofs. The configured entry point performs read-only
provider checks and appends evidence; it never deletes resources or rewrites the
original cleanup as an automated settlement.
"""

from datetime import UTC, datetime

from app.models.native_project_cleanup import NativeProjectNamespaceCleanup as State
from app.models.native_project_cleanup import NativeProjectNamespaceEvidence as Evidence
from app.models.project import Project
from app.schemas.kubevirt_storage import (
    BackendImageState,
    ImageObservation,
    NativeInventory,
)
from app.schemas.native_project_cleanup import CleanupStorageScope
from app.services import native_project_namespace_cleanup as cleanup
from app.services.kubevirt_storage_enrollment import digest, root_key, utc
from app.services.native_binding_retirement import require
from app.services.native_project_retirement import verify_project_retirement

PHASE = "manual-storage-verified"


def _manual_evidence(db, work):
    row = (
        db.query(Evidence)
        .filter_by(cleanup_id=str(work.id), phase="manual-legacy-retirement")
        .one_or_none()
    )
    require(
        row is not None and row.digest == digest(row.payload),
        "Original manual cleanup evidence unavailable",
    )
    assert row is not None
    value = row.payload
    require(
        value.get("method") == "user-authorized-manual-legacy-cleanup"
        and value.get("namespace") == work.authority.namespace
        and value.get("namespace_uid") == work.authority.namespace_uid
        and value.get("namespace_absent") is True
        and value.get("pv_absent") is True
        and value.get("automatic_settlement_claimed") is False,
        "Original manual cleanup identity changed",
    )
    return row


def _scope(work, manual):
    authority, liabilities = work.authority, work.liabilities
    require(
        digest(liabilities.model_dump(mode="json")) == work.liability_digest,
        "Storage liability digest changed",
    )
    require(
        work.phase == "finalized"
        and work.mode == "delete"
        and work.settlement_report_id is None
        and work.settled_at is None,
        "Only manually finalized deletion requires reconciliation",
    )
    require(
        authority.root is not None and authority.domain_id is not None,
        "Original storage authority unavailable",
    )
    assert authority.root is not None and authority.domain_id is not None
    require(
        set(liabilities.unresolved) <= {"storage_coverage_incomplete"},
        "Unresolved storage identities require investigation",
    )
    require(bool(liabilities.bindings), "Original storage bindings unavailable")
    private = {b.image_key for b in liabilities.bindings}
    private.update(p.intermediate for p in liabilities.ancestry)
    private.update(p.image for p in liabilities.snapshots)
    shared = {p.source.image_key for p in liabilities.ancestry}
    require(
        private == set(liabilities.private_images)
        and shared == set(liabilities.shared_images)
        and not private & shared,
        "Storage liability identities are incomplete",
    )
    require(
        all(b.root == authority.root for b in liabilities.bindings)
        and all(p.binding in liabilities.bindings for p in liabilities.ancestry)
        and {p.id for p in liabilities.producers}
        == {s.producer_id for s in liabilities.snapshots},
        "Storage producer or binding coverage is incomplete",
    )
    # Only byte-coverage debt can be replaced by fresh full absence evidence.
    resolved = liabilities.model_copy(update={"unresolved": []})
    return CleanupStorageScope(
        intent_id=work.id,
        operation_id="manual-retirement:" + str(work.id),
        domain_id=authority.domain_id,
        root=authority.root,
        phase="finalized",
        inventory_digest=digest(resolved.model_dump(mode="json")),
        owned_images=resolved.private_images,
        bindings=resolved.bindings,
        teardown_at=utc(datetime.fromisoformat(manual["observed_at"])),
        liabilities=resolved,
    )


def _verify(scope, inventory, images):
    require(
        not inventory.blockers and inventory.project_roots == [scope.root],
        "Manual retirement inventory incomplete",
    )
    require(
        {i.key for i in images}
        == set(scope.owned_images) | set(scope.liabilities.shared_images),
        "Manual retirement image coverage changed",
    )
    return verify_project_retirement(scope, inventory, images)


def record(db, previous, inventory, images, *, administrator, reason):
    """Persist trusted provider evidence with an exact predecessor check."""
    row = db.query(State).filter_by(id=str(previous.id)).with_for_update().one()
    work = cleanup.load(row)
    require(work == previous, "Cleanup changed during manual verification")
    require(
        db.get(Project, str(work.authority.project_id)) is None,
        "Manual retirement project still exists",
    )
    require(
        bool(administrator.strip()) and bool(reason.strip()),
        "Manual retirement attribution required",
    )
    manual = _manual_evidence(db, work)
    scope = _scope(work, manual.payload)
    proof = _verify(scope, inventory, images)
    payload = dict(
        work_digest=row.digest,
        manual_digest=manual.digest,
        administrator=administrator,
        reason=reason,
        inventory=inventory.model_dump(mode="json"),
        images=[i.model_dump(mode="json") for i in images],
        proof=proof.model_dump(mode="json"),
    )
    cleanup._evidence(db, work, PHASE, payload)
    return root_key(scope.root)


def settled_keys(db):
    """Recognize only immutable, independently verified manual receipts."""
    result = set()
    for receipt in db.query(Evidence).filter_by(phase=PHASE):
        require(receipt.digest == digest(receipt.payload), "Manual receipt changed")
        row = db.get(State, receipt.cleanup_id)
        work = cleanup.load(row)
        payload = receipt.payload
        manual = _manual_evidence(db, work)
        require(
            row.digest == payload["work_digest"]
            and manual.digest == payload["manual_digest"],
            "Manual retirement predecessor changed",
        )
        require(
            db.get(Project, str(work.authority.project_id)) is None,
            "Manually retired project identity reused",
        )
        scope = _scope(work, manual.payload)
        proof = _verify(
            scope,
            NativeInventory.model_validate(payload["inventory"]),
            [ImageObservation.model_validate(v) for v in payload["images"]],
        )
        require(
            proof.model_dump(mode="json") == payload["proof"],
            "Manual retirement proof changed",
        )
        result.add(root_key(scope.root))
    return result


def _private_absence(backend, keys, version, limits):
    """Batch complete directory/trash reads instead of rescanning for each ID."""
    result = backend.images_inventory(keys, max_objects=limits.max_objects)
    require(
        result.status == "complete"
        and isinstance(result.value, list)
        and len(result.value) == len(keys),
        "Private storage inventory incomplete",
    )
    images = []
    for key, raw in zip(keys, result.value):
        proof = BackendImageState.model_validate(raw)
        require(proof.excludes(key), "Private storage remains or identity changed")
        images.append(
            ImageObservation(
                key=key,
                collected_at=proof.collected_at,
                allocated_bytes=0,
                provisioned_bytes=0,
                measurement_status="complete",
                command_version=version,
                measurement_semantics_version="kubevirt-rbd-absence-v1",
                absence=proof,
            )
        )
    return images


def observe(scope, kube, backend, limits):
    """Read original namespace/PV/content absence and exact backend identities."""
    from app.services.kubevirt_storage_collector import _call, _observe
    from app.services.kubevirt_storage_inventory import _retiring_discovery

    require(
        kube.cluster_identity() == scope.root.cluster_key, "Cluster identity changed"
    )
    fsid = _call(backend.cluster_identity).value
    pools = _call(backend.pool_identity).value
    require(isinstance(pools, list), "Backend pool inventory incomplete")
    assert isinstance(pools, list)
    keys = scope.owned_images + scope.liabilities.shared_images
    require(
        all(
            k.ceph_fsid == str(fsid).strip()
            and any(p["poolnum"] == k.pool_id for p in pools)
            for k in keys
        ),
        "Backend identity changed",
    )
    kube.scan_started_at = datetime.now(UTC)
    pvs = {p["metadata"]["name"]: p for p in kube.pvs()}
    contents = {p["metadata"]["name"]: p for p in kube.snapshot_contents()}
    _, _, errors, proof = _retiring_discovery(kube, scope.root, scope, pvs, contents)
    require(proof.objects_absent and not errors, "Project objects remain")
    inventory = NativeInventory(
        project_roots=[scope.root],
        retiring_roots=[proof],
        collected_at=kube.scan_started_at,
    )
    version = str(_call(backend.command_version).value)
    capability = backend.capability
    require(
        capability.command_version == version
        and capability.merge_snapshots_validated
        and capability.validation_reference
        and capability.semantics_version == "kubevirt-rbd-allocated-v1",
        "Backend measurement capability unavailable",
    )
    images = _private_absence(backend, scope.owned_images, version, limits)
    images.extend(
        _observe(backend, k, pools, True, version, lambda: datetime.now(UTC), limits)
        for k in scope.liabilities.shared_images
    )
    require(
        kube.namespace_if_exists(scope.root.namespace) is None
        and _call(backend.cluster_identity).value == fsid
        and _call(backend.pool_identity).value == pools,
        "Provider identity changed during verification",
    )
    _, _, final_errors, final_proof = _retiring_discovery(
        kube,
        scope.root,
        scope,
        {p["metadata"]["name"]: p for p in kube.pvs()},
        {p["metadata"]["name"]: p for p in kube.snapshot_contents()},
    )
    require(
        final_proof.objects_absent and not final_errors, "Project inventory changed"
    )
    _verify(scope, inventory, images)
    return inventory, images


def observe_configured(scope):
    """Read protected provider evidence without changing accounting records."""
    import time
    from contextlib import ExitStack

    from app.core.database import SessionLocal
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services.kubevirt_storage_inventory import (
        KubernetesReader,
        read_static_kubeconfig,
    )
    from app.services.native_collection_config import (
        ToolboxBackendConfig,
        _backend_reader,
        _managed_inputs,
    )
    from app.services.native_collection_runtime import profile_for
    from app.services.native_managed_collector import anchored

    with SessionLocal() as db:
        args = profile_for(scope.domain_id)
        pins, config, backend_config = _managed_inputs(args)
        domain, manifest = anchored(db, pins)
        require(
            scope.root in manifest.pins.project_roots, "Original root is not enrolled"
        )
        enrollment_id = domain.enrollment_id
    limits = CollectionLimits(
        command_timeout_seconds=args.command_timeout_seconds,
        scan_timeout_seconds=args.verification_timeout_seconds,
    )
    deadline = time.monotonic() + limits.scan_timeout_seconds
    with ExitStack() as stack:
        data = (
            read_static_kubeconfig(backend_config.kubeconfig)
            if isinstance(backend_config, ToolboxBackendConfig)
            else None
        )
        backend = _backend_reader(backend_config, limits, deadline, stack, data)
        backend.capability = config.capability
        kube = KubernetesReader(
            args.kubeconfig,
            backend,
            collector_id=config.collector_id,
            cache_roots=[],
            scan_deadline=deadline,
            max_objects=limits.max_objects,
        )
        stack.callback(kube.api.close)
        inventory, images = observe(scope, kube, backend, limits)
    return inventory, images, pins, enrollment_id


def reconcile_configured(cleanup_id, *, administrator, reason, apply=False):
    """Explicit operator entry point; credentials come only from protected config."""
    from app.core.database import SessionLocal
    from app.services.native_managed_collector import anchored

    with SessionLocal() as db:
        work = cleanup.load(db.get(State, str(cleanup_id)))
        manual = _manual_evidence(db, work)
        scope = _scope(work, manual.payload)
        if root_key(scope.root) in settled_keys(db):
            return {"root_key": root_key(scope.root), "status": "already_verified"}
    inventory, images, pins, enrollment_id = observe_configured(scope)
    with SessionLocal() as db:
        domain, _ = anchored(db, pins)
        require(
            domain.enrollment_id == enrollment_id,
            "Enrollment changed during verification",
        )
        if not apply:
            return {"root_key": root_key(scope.root), "status": "verified_dry_run"}
        key = record(
            db, work, inventory, images, administrator=administrator, reason=reason
        )
        db.commit()
        return {"root_key": key, "status": "recorded"}
