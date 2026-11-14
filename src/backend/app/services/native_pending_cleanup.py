"""Cancel an original, unreleased native initialization through managed cleanup.

Unknown interrupted creates use Kubernetes/CSI settlement after namespace removal.
Known initialized storage keeps its measured bindings and backend absence boundary.
No create, clone-success receipt, or journal repair occurs.
"""

import json
from copy import deepcopy
from uuid import uuid4

from app.schemas.kubevirt_storage import CleanupObject, NativeCleanupIntent
from app.services import native_managed_power as power
from app.services import quota_cleanup as cleanup
from app.services import quota_operations as operations
from app.services.quota_accounting import AccountingUnavailable


def validate(db, project, rows):
    from app.models.user import User
    from app.services.kubevirt_storage_enrollment import digest

    if len(rows) != 1:
        raise AccountingUnavailable("Pending cleanup requires one original deploy")
    row = rows[0]
    target = row.dispatch_metadata.get("native_deploy", {})
    identity = row.dispatch_metadata.get("native_storage_identity", {})
    owner = db.get(User, project.owner_id)
    if (
        row.state != "reconciling"
        or operations._generation(row) != row.operation_id
        or not row.dispatch_metadata.get("worker_finished")
        or target.get("phase") != "initialization-intent"
        or target.get("release_handoff")
        or target.get("running_proof")
        or row.observed.get("cpu")
        or row.observed.get("ram_bytes")
        or not identity
        or identity.get("root") != target.get("root")
        or owner is None
        or (row.owner_id, row.organization_id) != (owner.id, owner.organization_id)
        or (target.get("owner_id"), target.get("organization_id"))
        != (owner.id, owner.organization_id)
    ):
        raise AccountingUnavailable("Pending cleanup original initialization changed")
    request = target["initialization_request"]
    if (
        project.topology != target["payload"]["spec"]["topology"]
        or digest(target["payload"]["spec"]) != target["payload_digest"]
        or request["payloadDigest"] != target["payload_digest"]
        or request["profileDigest"] != target["profile_digest"]
        or request["planDigest"] != target["payload"]["spec"]["managedPlanDigest"]
        or request["requestId"] != row.operation_id
        or request["allocationGeneration"] != row.operation_id
        or identity["provider_id"] != target["provider_id"]
        or identity["host_id"] != target["host_id"]
        or project.host_id != identity["host_id"]
        or project.provider_id not in (None, identity["provider_id"])
    ):
        raise AccountingUnavailable("Pending cleanup immutable plan changed")
    return row, target, identity


def require_no_known_storage(db, domain_id, root):
    from app.models.kubevirt_storage import NativeRetainedBinding, NativeStorageSnapshot
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.services.kubevirt_storage_enrollment import root_key

    if db.query(NativeRetainedBinding).filter_by(root_key=root_key(root)).first():
        raise AccountingUnavailable("Pending cleanup retains known storage")
    for lineage in db.query(NativeCloneLineage).filter_by(domain_id=domain_id):
        if lineage.intent.get("root") == root.model_dump(mode="json") and (
            lineage.creation or lineage.proof
        ):
            raise AccountingUnavailable("Pending cleanup retains known clone storage")
    # Do not downgrade previously observed storage when the latest report is empty
    # or unavailable. Historical evidence is append-only and survives GC.
    for report in db.query(NativeStorageSnapshot).filter_by(domain_id=domain_id):
        payload = report.payload
        evidence = (
            payload.get("volume_bindings", [])
            + payload.get("retained_bindings", [])
            + payload.get("snapshot_evidence", [])
        )
        if any(v.get("root") == root.model_dump(mode="json") for v in evidence):
            raise AccountingUnavailable("Pending cleanup retains known storage")


def prepare(db, project, rows, mode):
    from app.services import quota_enforcement as enforcement
    from app.services import quota_native

    if mode != "undeploy":
        raise AccountingUnavailable("Pending initialization supports Undeploy only")
    row, target, identity = validate(db, project, rows)
    root, domain = quota_native._measurement_domain(db, project.id, identity)
    _, _, pinned, _ = quota_native._measurement_binding(
        db, project, root, domain, identity
    )
    bindings, owned = [], []
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services.kubevirt_storage_enrollment import root_key

    if (
        row.observed.get("storage_bytes")
        or db.query(NativeRetainedBinding).filter_by(root_key=root_key(pinned)).first()
    ):
        bindings, owned = known_storage(db, project, row, identity, pinned, domain)
    else:
        require_no_known_storage(db, domain.id, pinned)
    if not domain.latest_report_id:
        raise AccountingUnavailable("Pending cleanup enrollment report unavailable")
    action = enforcement._action(
        db,
        "project",
        project.id,
        0,
        row.operation_id,
        "stop_run",
        100,
        operations.now(),
        project.id,
    )
    pending = {
        "request": deepcopy(target["initialization_request"]),
        "target_digest": power.helpers().digest(target),
    }
    value = NativeCleanupIntent(
        intent_id=uuid4(),
        operation_id=row.operation_id,
        operation_ids=[row.operation_id],
        generation=row.operation_id,
        mode=mode,
        namespace_mode="managed",
        phase="stopping",
        owner_id=row.owner_id,
        organization_id=row.organization_id,
        host_id=identity["host_id"],
        provider_id=identity["provider_id"],
        domain_id=domain.id,
        enrollment_id=domain.enrollment_id,
        initial_report_id=domain.latest_report_id,
        root=pinned,
        owned_images=owned,
        bindings=bindings,
        stop_action_id=action.id,
        prepared_at=operations.now(),
        pending_initialization=pending,
    )
    row.dispatch_metadata = {**row.dispatch_metadata, "cancelled": True}
    cleanup._save_native(db, value)
    project.state = "cleanup_pending"
    project.deploy_error = "Cleanup pending: cancelling original initialization and waiting for Kubernetes/CSI settlement"
    return value


def known_storage(db, project, row, identity, pinned, domain):
    """Use existing measurement authority, never clone success, for cleanup."""
    from app.models.kubevirt_storage import (
        NativeProjectRoot,
        NativeRetainedBinding,
        NativeStorageSnapshot,
    )
    from app.schemas.kubevirt_storage import NativeStorageReport
    from app.services import quota_native
    from app.services.kubevirt_storage_graph import _ownership
    from app.services.kubevirt_storage_reports import normalized_inventory

    measurement = quota_native.storage_measurement(
        db, project, row.operation_id, identity, operations.now()
    )
    source = NativeStorageReport.model_validate(
        db.get(NativeStorageSnapshot, measurement.report_id).payload
    )
    roots = cleanup.active_roots(
        db, db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    )
    inventory = normalized_inventory(
        source,
        roots,
        db.query(NativeRetainedBinding).filter_by(domain_id=domain.id).all(),
    )
    cache = set(b.image_key for b in inventory.cache_bindings)
    _, nodes, _ = _ownership(inventory, {i.key: i for i in source.images}, cache, 32)
    owned = nodes[pinned.project_id] - cache
    bindings = {
        b.pvc_uid: b
        for b in source.volume_bindings + source.retained_bindings
        if b.root == pinned
    }
    if (
        not owned
        or not bindings
        or any(s.root == pinned for s in source.snapshot_evidence)
        or any(i.snapshots for i in source.images if i.key in owned)
    ):
        raise AccountingUnavailable(
            "Pending cleanup requires complete snapshot-free owned storage"
        )
    return sorted(bindings.values(), key=lambda b: b.pvc_uid), sorted(
        owned, key=lambda k: k.model_dump_json()
    )


def stop(db, provider, intent):
    from app.models.quota import QuotaAction
    from app.services.quota_enforcement import _execution_guard

    # Cancellation was committed before waiting for any older provider publisher.
    with _execution_guard(
        db, "native-deploy:" + str(intent.root.project_id)
    ) as acquired:
        if not acquired:
            return
        project, rows, current = cleanup._checked_native(
            db, str(intent.root.project_id), str(intent.intent_id)
        )
        _, target, _ = validate(db, project, rows)
        if (
            power.helpers().digest(target)
            != current.pending_initialization["target_digest"]
        ):
            raise AccountingUnavailable("Pending cleanup deployment changed")
        if current.bindings:
            from app.services import quota_native

            _, domain = quota_native._measurement_domain(
                db, project.id, rows[0].dispatch_metadata["native_storage_identity"]
            )
            bindings, owned = known_storage(
                db,
                project,
                rows[0],
                rows[0].dispatch_metadata["native_storage_identity"],
                current.root,
                domain,
            )
            if bindings != current.bindings or owned != current.owned_images:
                raise AccountingUnavailable("Pending cleanup storage identity changed")
        else:
            require_no_known_storage(db, str(current.domain_id), current.root)
        db.commit()
        proof = power.with_client(provider, cancel_and_observe, current)
        if proof is None:
            return
        _, _, current = cleanup._checked_native(
            db, str(intent.root.project_id), str(intent.intent_id)
        )
        action = db.get(QuotaAction, current.stop_action_id)
        action.state = "completed"
        action.details = {**action.details, "managed_off_proof": proof}
        pending = {**current.pending_initialization, "journal": proof["journal"]}
        cleanup._save_native(db, current, pending_initialization=pending)
        db.commit()


def wanted(intent):
    value = {
        "version": 1,
        "mode": "stopped",
        "requestId": intent.stop_action_id,
        "allocationGeneration": intent.generation,
        "pendingInitialization": intent.pending_initialization["request"],
    }
    if intent.bindings:
        value["pendingStorage"] = [b.model_dump(mode="json") for b in intent.bindings]
    return value


def cancel_and_observe(api, intent):
    module = power.helpers()
    root = power._live_root(
        api, cleanup._cleanup_context(intent), intent.stop_action_id
    )
    request = wanted(intent)
    current = module.intent(root)
    module.require(
        current in (request, intent.pending_initialization["request"]),
        "pending_cleanup_episode_changed",
    )
    if current != request:
        annotations = {
            **root["metadata"].get("annotations", {}),
            module.ANNOTATION: json.dumps(request, sort_keys=True),
        }
        api.patch(
            root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}]
        )
    return observe(api, intent)


def observe(api, intent):
    from app.services.native_managed_deploy import helpers

    module = power.helpers()
    root = power._live_root(
        api, cleanup._cleanup_context(intent), intent.stop_action_id
    )
    module.require(
        module.intent(root) == wanted(intent), "pending_cleanup_episode_changed"
    )
    state = root.get("status", {}).get("managedPower", {})
    if state.get("state") != "off" or state.get("producerDrained") is not True:
        return None
    _, journal = helpers().observe_pending_cleanup(api, root)
    module.require(
        state.get("pendingCleanup")
        == module.digest({"request": wanted(intent), "journal": journal}),
        "pending_cleanup_ack_changed",
    )
    saved = intent.pending_initialization.get("journal")
    module.require(saved is None or saved == journal, "pending_cleanup_journal_changed")
    final = power._live_root(
        api, cleanup._cleanup_context(intent), intent.stop_action_id
    )
    module.require(final == root, "pending_cleanup_observation_changed")
    return {
        "request_id": intent.stop_action_id,
        "generation": intent.generation,
        "observed_at": operations.now().isoformat(),
        "journal": journal,
    }


def seal(api, intent):
    from app.services.native_managed_deploy import helpers

    proof = observe(api, intent)
    if proof is None:
        raise AccountingUnavailable("Pending cleanup producer is not drained")
    root = power._live_root(
        api, cleanup._cleanup_context(intent), intent.stop_action_id
    )
    rows, _ = helpers().observe_pending_cleanup(api, root)
    namespace = api.get("Namespace", None, intent.root.namespace)
    objects = [
        CleanupObject(
            kind=v["kind"],
            name=v["metadata"]["name"],
            uid=v["metadata"]["uid"],
            namespace=v["metadata"].get("namespace"),
            resource_version=v["metadata"]["resourceVersion"],
            reclaim_policy=(
                v["spec"].get("persistentVolumeReclaimPolicy")
                if v["kind"] == "PersistentVolume"
                else None
            ),
            namespace_bootstrap=power.helpers().cleanup_bootstrap(
                v, {o["metadata"]["uid"]: o for o in rows}
            ),
        )
        for v in [root, namespace, *rows]
    ]
    payload = [
        v.model_dump(mode="json")
        for v in sorted(objects, key=lambda v: (v.kind, v.name))
    ]
    return objects, power.helpers().digest(payload), proof
