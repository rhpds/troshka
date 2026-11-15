"""Confirmed absence is the only whole-environment resource release boundary."""

import json
from uuid import UUID, uuid4

from app.models.quota import QuotaReservation
from app.schemas.kubevirt_storage import NativeCleanupIntent
from app.schemas.quota import Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_operations as operations


def native_intents(db, project_id=None):
    query = db.query(QuotaReservation)
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    result = []
    for row in query.populate_existing():
        payload = row.dispatch_metadata.get("native_cleanup")
        if payload:
            result.append(NativeCleanupIntent.model_validate(payload))
    return result


def native_intent(db, project_id):
    intents = native_intents(db, project_id)
    return max(intents, key=lambda v: v.prepared_at) if intents else None


def settled_root_keys(db):
    from app.services.kubevirt_storage_enrollment import root_key

    return {
        root_key(v.root)
        for v in native_intents(db)
        if v.phase in {"settled", "finalized"}
        and v.settlement_report_id
        and v.settled_at
    }


def active_roots(db, roots):
    retired = settled_root_keys(db)
    return [root for root in roots if root.key not in retired]


def _save_native(db, intent, **updates):
    value = NativeCleanupIntent.model_validate({**intent.model_dump(), **updates})
    row = db.query(QuotaReservation).filter_by(operation_id=value.operation_id).one()
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "native_cleanup": value.model_dump(mode="json"),
    }
    db.flush()
    return value


def _native_lock(db, project_id):
    from app.models.managed_group import ManagedGroup
    from app.services.quota_enforcement import _snapshot
    from app.services.quota_locks import _retained_project_identity, lock_rows

    rows = operations._rows(db, project_id)
    ids = {r.operation_id for r in rows}
    admission.lock_operations(db, ids)
    closure = _snapshot(db, project_id)
    retained = _retained_project_identity(db, project_id)
    groups = set(retained[2]) | {
        r.organization_id for r in closure if r.organization_id
    }
    existing = [
        value
        for (value,) in db.query(ManagedGroup.id).filter(ManagedGroup.id.in_(groups))
    ]
    # The subsequent existing Stop helper needs the current spending closure.
    # Acquire its complete project tier here, after every retained organization,
    # rather than acquiring lower project IDs after the target project lock.
    lock_rows(
        db,
        user_ids=[retained[0]],
        organization_ids=existing,
        project_ids=[r.id for r in closure],
    )
    if closure != _snapshot(db, project_id) or retained != _retained_project_identity(
        db, project_id
    ):
        raise accounting.AccountingUnavailable("Cleanup identity closure changed")
    rows = operations._rows(db, project_id)
    if ids != {r.operation_id for r in rows}:
        raise accounting.AccountingUnavailable("Cleanup operation membership changed")
    from app.models.project import Project

    return db.query(Project).filter_by(
        id=project_id
    ).populate_existing().one(), operations.current_generation(rows)


def _namespace_authority(rows, db=None, project=None):
    originals = [row for row in rows if row.operation_id == operations._generation(row)]
    if len(originals) != 1:
        raise accounting.AccountingUnavailable(
            "Original native namespace ownership is unavailable; verified ownership enrollment is required before cleanup"
        )
    row = originals[0]
    if row.dispatch_metadata.get("native_custody_id"):
        from app.services.native_online_enrollment import namespace_authority

        return namespace_authority(db, project, row)
    target = row.dispatch_metadata.get("native_deploy", {})
    identity = row.dispatch_metadata.get("native_storage_identity", {})
    if target.get("base_grants_mode") != "managed" or target.get(
        "create_namespace_uid"
    ) != identity.get("root", {}).get("namespace_uid"):
        raise accounting.AccountingUnavailable(
            "Original native namespace ownership is external or unknown"
        )
    return "managed"


def prepare_native(db, project, mode):
    """Persist the original cleanup identity before queue, credentials or teardown."""
    if operations.select_adapter(db, project) != "kubevirt":
        return None
    if not project.host_id and project.state == "draft":
        return None
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.services import native_managed_power as power
    from app.services import quota_native, quota_storage
    from app.services.kubevirt_storage_enrollment import root_key
    from app.services.kubevirt_storage_reports import (
        normalized_inventory,
        scoped_retained,
    )
    from app.services.native_clone_graph import cleanup_owned_images
    from app.services.native_clone_lineage import resolve_lineages
    from app.services.quota_enforcement import request_native_stop

    project, rows = _native_lock(db, project.id)
    previous = native_intent(db, project.id)
    if previous and previous.phase != "finalized":
        if previous.mode != mode:
            operations.unavailable("Original cleanup mode is already pending")
        return previous
    from app.services.native_online_enrollment import (
        cleanup_anchor,
        validate_cleanup_bindings,
    )

    rows = cleanup_anchor(db, project, rows)
    namespace_mode = _namespace_authority(rows, db, project)
    if any(r.state != "confirmed" for r in rows):
        from app.services.native_pending_cleanup import prepare

        return prepare(db, project, rows, mode)
    context = power.retained_context(db, project, rows)
    row = quota_storage.measurement_row(rows)
    identity = context["identity"]
    root, domain = quota_native._measurement_domain(db, project.id, identity)
    _, _, pinned, manifest = quota_native._measurement_binding(
        db, project, root, domain, identity
    )
    from app.models.kubevirt_storage import NativeProjectRoot

    roots = active_roots(
        db, db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
    )
    _, source, report, _ = quota_native._measurement_report(
        db, domain, roots, manifest, operations.now(), project_id=project.id
    )
    if source.project_scope is not None:
        roots = [r for r in roots if r.project_id == project.id]
    retired = settled_root_keys(db)
    retained = [
        b
        for b in db.query(NativeRetainedBinding).filter_by(domain_id=domain.id)
        if b.root_key not in retired
    ]
    retained = scoped_retained(source, roots, retained)
    inventory = normalized_inventory(source, roots, retained)
    retired_images = {
        key.model_dump_json()
        for proof in source.retiring_roots
        if root_key(proof.root) in retired
        for key in proof.owned_images
    }
    owned = cleanup_owned_images(
        inventory,
        [
            image
            for image in source.images
            if image.key.model_dump_json() not in retired_images
        ],
        resolve_lineages(db, source.clone_references, manifest),
        pinned.project_id,
    )
    bindings = {
        b.pvc_uid: b
        for b in source.volume_bindings + source.retained_bindings
        if b.root == pinned
    }
    if not owned or not bindings or source.snapshot_evidence:
        raise accounting.AccountingUnavailable(
            "Native cleanup requires a complete supported original storage graph"
        )
    validate_cleanup_bindings(db, project, rows, bindings.values())
    action_id = request_native_stop(db, project, for_cleanup=True)
    if not action_id:
        raise accounting.AccountingUnavailable(
            "Native cleanup stop identity unavailable"
        )
    if _already_off(rows):
        from app.models.quota import QuotaAction

        request_id, _ = power.expected_stop(db, project, rows)
        action = db.get(QuotaAction, action_id)
        action.details = {**action.details, "managed_stop_id": request_id}
    value = NativeCleanupIntent(
        intent_id=uuid4(),
        operation_id=row.operation_id,
        operation_ids=sorted(r.operation_id for r in rows),
        generation=context["generation"],
        mode=mode,
        namespace_mode=namespace_mode,
        phase="stopping",
        owner_id=UUID(str(row.owner_id)),
        organization_id=UUID(str(row.organization_id)) if row.organization_id else None,
        host_id=identity["host_id"],
        provider_id=identity["provider_id"],
        domain_id=domain.id,
        enrollment_id=domain.enrollment_id,
        initial_report_id=report.id,
        root=pinned,
        owned_images=sorted(owned, key=lambda k: k.model_dump_json()),
        bindings=list(bindings.values()),
        stop_action_id=action_id,
        prepared_at=operations.now(),
    )
    _save_native(db, value)
    project.state = "cleanup_pending"
    project.deploy_error = (
        "Cleanup pending: waiting for complete managed shutdown and storage settlement"
    )
    return value


def _already_off(rows):
    # An online legacy anchor has unknown compute, not a stop observation.
    return all(
        not r.dispatch_metadata.get("cleanup_only")
        and not r.observed.get("cpu")
        and not r.observed.get("ram_bytes")
        for r in rows
    )


def request_native(db, project, mode):
    from app.core.redis import enqueue_job

    try:
        completed = _complete_unclaimed_native(db, project, mode)
        if completed is not None:
            return completed
        intent = prepare_native(db, project, mode)
    except accounting.AccountingUnavailable as error:
        raise operations._http_denial(error) from error
    if intent is None:
        return None
    db.commit()
    enqueue_job(reconcile_native, project.id, str(intent.intent_id))
    return {
        "status": "cleanup_pending",
        "id": project.id,
        "mode": intent.mode,
        "cleanup_id": str(intent.intent_id),
    }


def _unclaimed_candidate(rows):
    return (
        len(rows) == 1
        and rows[0].dispatch_metadata.get("native_deploy", {}).get("phase")
        == "reserved"
    )


def _require_unclaimed_worker(db, project, rows):
    row = rows[0]
    metadata = row.dispatch_metadata
    target = metadata["native_deploy"]
    forbidden = {
        "root",
        "create_started_at",
        "create_handoff",
        "create_namespace_uid",
        "initialized_report",
        "release_handoff",
        "clone_lineages",
    }
    if (
        not operations.known_empty(db, project)
        or metadata.get("adapter") != "kubevirt"
        or metadata.get("kind") != "deploy"
        or metadata.get("calls")
        or metadata.get("native_storage_identity")
        or metadata.get("native_cleanup")
        or row.dispatched_at
        or any(row.observed.values())
        or forbidden.intersection(target)
    ):
        raise accounting.AccountingUnavailable(
            "Native unclaimed cleanup requires never-dispatched work"
        )
    from app.services.native_managed_deploy import validate

    # Original attribution, placement and payload remain mandatory. A failed
    # current collector attempt cannot prevent cancellation before dispatch.
    validate(db, project, row, physical=False)


def _require_no_unclaimed_assets(db, project):
    from app.models.elastic_ip import ElasticIp
    from app.models.kubevirt_storage import NativeProjectRoot
    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaAllocation
    from app.services.quota_capacity_baseline import _binding_inventory

    allocations = db.query(QuotaAllocation).filter_by(project_id=project.id).all()
    roots = db.query(NativeProjectRoot).filter_by(project_id=project.id).all()
    root_keys = {root.key for root in roots}
    bindings = _binding_inventory(db, [])
    retained = (
        project.disks
        or active_roots(db, roots)
        or any(a.state != "released" or any(a.observed.values()) for a in allocations)
        or any(any(r.observed.values()) for r in operations._rows(db, project.id))
        or any(
            b["root_key"] in root_keys
            or b["payload"].get("root", {}).get("project_id") == project.id
            for b in bindings
        )
        or db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .first()
        or db.query(ElasticIp)
        .filter_by(project_id=project.id)
        .filter(ElasticIp.state.in_(("allocated", "associated")))
        .first()
    )
    if retained:
        raise accounting.AccountingUnavailable(
            "Native unclaimed cleanup retains unresolved resources"
        )


def _complete_unclaimed_native(db, project, mode):
    """Fence the original admission under the same lock used by worker claim.

    A queued or preclaim-failed worker need not have a completion timestamp:
    native claim preflight has no side effects, and claim refreshes this row
    under the operation lock before any provider handoff. Whichever wins the
    lock determines whether cancellation remains safe.
    """
    if (
        mode != "undeploy"
        or operations.select_adapter(db, project) != "kubevirt"
        or not _unclaimed_candidate(
            operations.current_generation(operations._rows(db, project.id))
        )
    ):
        return None
    project, rows = _native_lock(db, project.id)
    if not _unclaimed_candidate(rows):
        raise accounting.AccountingUnavailable("Native cleanup operation changed")
    _require_unclaimed_worker(db, project, rows)
    _require_no_unclaimed_assets(db, project)
    fence(db, project.id)
    finalize_absence(db, project, operations.now())
    project.state, project.host_id, project.vni_map, project.deploy_error = (
        "draft",
        None,
        None,
        None,
    )
    project.deployed_topology = None
    db.commit()
    return {"status": "draft"}


def guard_pending(db, project, *, lock=False):
    if (
        lock
        and operations.select_adapter(db, project) == "kubevirt"
        and project.host_id
    ):
        project, _ = _native_lock(db, project.id)
    intent = native_intent(db, project.id)
    if intent is not None and intent.phase != "finalized":
        operations.unavailable(
            "Original native cleanup is pending; resources and owner remain retained"
        )


def _cleanup_context(intent):
    return {
        "root": intent.root.model_dump(mode="json"),
        "generation": intent.generation,
    }


def _namespace_rows(api, namespace, *, excluded_kinds=frozenset()):
    from app.services.native_managed_power import helpers

    module = helpers()
    kinds = set(module.KINDS) - {"Namespace", "PersistentVolume", "TroshkaProject"}
    kinds |= set(module.CLEANUP_KINDS)
    return {kind: api.rows(kind, namespace) for kind in sorted(kinds - excluded_kinds)}


def seal_native_inventory(api, work):
    """Fresh full off proof plus exact storage identity before any destructive call."""
    from app.schemas.kubevirt_storage import CleanupObject
    from app.services import native_managed_power as power
    from app.services.kubevirt_storage_enrollment import digest

    intent = NativeCleanupIntent.model_validate(work["intent"])
    context = work["context"]
    if intent.pending_initialization is not None:
        from app.services.native_pending_cleanup import seal

        return seal(api, intent)
    module = power.helpers()
    if isinstance(api, module.PowerClient):
        api.begin_inventory()
    proof = intent.stop_proof
    if proof is None:
        raise accounting.AccountingUnavailable("Native cleanup off proof unavailable")
    fresh = power.observe_off(
        api, context, proof["request_id"], operations.now(), operations.now
    )
    if fresh is None:
        raise module.ManagedPowerError("cleanup_off_proof_unavailable")
    root = power._live_root(api, context, proof["request_id"])
    fresh = {**fresh, "root_spec_digest": module.digest(root["spec"])}
    rows, by_uid = module.inventory(api, root)
    module.profile(root, rows, by_uid, api)
    full = _namespace_rows(api, intent.root.namespace)
    objects = []
    all_rows = [root] + [value for values in full.values() for value in values]
    by_uid = {value["metadata"]["uid"]: value for value in all_rows}
    bindings = {b.pvc_uid: b for b in intent.bindings}
    claims = full["PersistentVolumeClaim"]
    module.require(
        {v["metadata"]["uid"] for v in claims} == set(bindings),
        "cleanup_storage_inventory_changed",
    )
    for value in all_rows:
        meta = value["metadata"]
        grant = module.cleanup_bootstrap(value, by_uid)
        if value is not root and grant is None:
            module.verify_chain(value, root, by_uid)
        module.require(
            value["kind"] != "VolumeSnapshot", "cleanup_snapshot_unsupported"
        )
        objects.append(
            CleanupObject(
                kind=value["kind"],
                name=meta["name"],
                uid=meta["uid"],
                namespace=meta.get("namespace"),
                resource_version=meta["resourceVersion"],
                namespace_bootstrap=grant,
            )
        )
    for pvc in claims:
        binding = bindings[pvc["metadata"]["uid"]]
        pv = api.get("PersistentVolume", None, pvc["spec"]["volumeName"])
        meta, spec = pv["metadata"], pv["spec"]
        csi = spec.get("csi", {})
        module.require(
            meta["uid"] == binding.pv_uid
            and spec["claimRef"]["uid"] == binding.pvc_uid
            and csi.get("driver") == binding.csi_driver
            and csi.get("volumeHandle") == binding.volume_handle,
            "cleanup_pv_identity_changed",
        )
        policy = spec.get("persistentVolumeReclaimPolicy")
        module.require(policy in {"Delete", "Retain"}, "cleanup_reclaim_policy_unknown")
        objects.append(
            CleanupObject(
                kind="PersistentVolume",
                name=meta["name"],
                uid=meta["uid"],
                resource_version=meta["resourceVersion"],
                reclaim_policy=policy,
            )
        )
    namespace = api.get("Namespace", None, intent.root.namespace)
    module.require(
        namespace["metadata"]["uid"] == intent.root.namespace_uid,
        "cleanup_namespace_changed",
    )
    objects.append(
        CleanupObject(
            kind="Namespace",
            name=intent.root.namespace,
            uid=intent.root.namespace_uid,
            resource_version=namespace["metadata"]["resourceVersion"],
        )
    )
    payload = [
        v.model_dump(mode="json")
        for v in sorted(objects, key=lambda v: (v.kind, v.name))
    ]
    return objects, digest(payload), fresh


def _checked_native(db, project_id, intent_id):
    project, rows = _native_lock(db, project_id)
    intent = native_intent(db, project_id)
    if intent is None or str(intent.intent_id) != intent_id:
        raise accounting.AccountingUnavailable("Original cleanup intent changed")
    from app.models.user import User

    owner = db.get(User, project.owner_id)
    if intent.phase not in {"settled", "finalized"} and (
        sorted(r.operation_id for r in rows) != intent.operation_ids
        or any(operations._generation(r) != intent.generation for r in rows)
        or project.owner_id != str(intent.owner_id)
        or owner.organization_id
        != (str(intent.organization_id) if intent.organization_id else None)
        or project.host_id != str(intent.host_id)
    ):
        raise accounting.AccountingUnavailable(
            "Original cleanup allocation or owner changed"
        )
    if intent.phase == "settled" and (
        rows
        or project.owner_id != str(intent.owner_id)
        or project.host_id != str(intent.host_id)
    ):
        raise accounting.AccountingUnavailable("Settled cleanup target was replaced")
    if intent.pending_initialization is not None and intent.phase not in {
        "settled",
        "finalized",
    }:
        from app.services.native_managed_power import helpers
        from app.services.native_pending_cleanup import validate

        _, target, _ = validate(db, project, rows)
        if helpers().digest(target) != intent.pending_initialization["target_digest"]:
            raise accounting.AccountingUnavailable("Pending cleanup deployment changed")
    return project, rows, intent


def reconcile_native(project_id, intent_id):
    """Existing queued coordinator; durable phases recover without replaying deletes."""
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.models.provider import Provider
    from app.models.quota import QuotaAction
    from app.services import native_managed_power as power
    from app.services import quota_enforcement as enforcement

    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            return
        with enforcement._execution_guard(db, "cleanup:" + project_id) as acquired:
            if not acquired:
                return
            try:
                project, rows, intent = _checked_native(db, project_id, intent_id)
                if intent.phase == "finalized":
                    db.commit()
                    return
                provider = db.get(Provider, str(intent.provider_id))
                db.expunge(provider)
                db.commit()
                if intent.phase == "stopping":
                    from app.services.native_online_enrollment import preflight_cleanup

                    preflight_cleanup(provider, rows, intent)
                    if intent.pending_initialization is not None:
                        from app.services.native_pending_cleanup import stop

                        stop(db, provider, intent)
                    else:
                        enforcement.reconcile_worker(intent.stop_action_id)
                    project, rows, intent = _checked_native(db, project_id, intent_id)
                    action = db.get(QuotaAction, intent.stop_action_id)
                    proof = (
                        action.details.get("managed_off_proof")
                        if action and action.state == "completed"
                        else None
                    )
                    project.state = "cleanup_pending"
                    if not proof:
                        db.commit()
                        return
                    intent = _save_native(db, intent, stop_proof=proof)
                    db.commit()
                    work = {
                        "context": _cleanup_context(intent),
                        "intent": intent.model_dump(mode="json"),
                    }
                    objects, inventory_digest, fresh = power.with_client(
                        provider, seal_native_inventory, work
                    )
                    project, rows, intent = _checked_native(db, project_id, intent_id)
                    intent = _save_native(
                        db,
                        intent,
                        phase="off",
                        objects=objects,
                        inventory_digest=inventory_digest,
                        stop_proof=fresh,
                    )
                    db.commit()
                _advance_native(db, provider, project_id, intent_id)
            except Exception:
                db.rollback()
                if db.get(Project, project_id) is not None:
                    project, rows, intent = _checked_native(db, project_id, intent_id)
                    project.state = "cleanup_pending"
                    project.deploy_error = "Cleanup pending: original resources or storage settlement unavailable"
                    _save_native(
                        db,
                        intent,
                        last_error="Original resource or storage observation unavailable",
                    )
                    db.commit()


def _advance_native(db, provider, project_id, intent_id):
    """Each provider submission is durably recorded and never blindly replayed."""
    from app.services import native_managed_power as power

    project, rows, intent = _checked_native(db, project_id, intent_id)
    if intent.phase == "settled":
        _finish_native(db, project, intent)
        return
    if intent.phase == "off":
        intent = _save_native(
            db, intent, phase="teardown", teardown_at=operations.now()
        )
        db.commit()
    if intent.phase in {"teardown", "pending"}:
        work = {
            "context": _cleanup_context(intent),
            "intent": intent.model_dump(mode="json"),
        }
        db.commit()
        # Every recovery independently observes whether original conditional steps
        # remain missing. An accepted/deleting original is never submitted twice.
        power.with_client(provider, submit_native_teardown, work)
        project, rows, intent = _checked_native(db, project_id, intent_id)
        intent = _save_native(db, intent, phase="pending")
        db.commit()
        work = {
            "context": _cleanup_context(intent),
            "intent": intent.model_dump(mode="json"),
        }
        ready = power.with_client(provider, namespace_teardown_ready, work)
        if ready:
            project, rows, intent = _checked_native(db, project_id, intent_id)
            if intent.namespace_delete_at is None:
                intent = _save_native(db, intent, namespace_delete_at=operations.now())
            db.commit()
            power.with_client(
                provider,
                submit_namespace_teardown,
                {
                    "context": _cleanup_context(intent),
                    "intent": intent.model_dump(mode="json"),
                },
            )
        project, rows, intent = _checked_native(db, project_id, intent_id)
        _settle_native(db, project, rows, intent)


def _original_namespace(api, intent):
    from app.services.native_managed_power import helpers

    module = helpers()
    module.require(
        api.get("Namespace", None, "kube-system")["metadata"]["uid"]
        == intent.root.cluster_key,
        "cleanup_cluster_changed",
    )
    value = api.find("Namespace", None, intent.root.namespace)
    if value is not None:
        module.require(
            value["metadata"]["uid"] == intent.root.namespace_uid,
            "cleanup_namespace_changed",
        )
    return value


def _checked_cleanup_rows(api, intent):
    """Worker's complete custody check, including Secrets, before deletion."""
    return _validate_cleanup_rows(
        api, intent, _namespace_rows(api, intent.root.namespace)
    )


def checked_collector_cleanup_rows(api, intent):
    """Storage observation only; never grants authority to delete the namespace.

    The worker seals and rechecks Secret custody. Collector settlement still
    requires namespace absence, so omitted Secrets are not absence evidence.
    """
    rows = _namespace_rows(
        api, intent.root.namespace, excluded_kinds=frozenset({"Secret"})
    )
    return _validate_cleanup_rows(api, intent, rows)


def _validate_cleanup_rows(api, intent, rows):
    from app.services.native_managed_power import helpers

    module = helpers()
    original_root = next(v for v in intent.objects if v.kind == "TroshkaProject")
    root = _cleanup_root(api, intent, original_root)
    expected = {(v.kind, v.name): v for v in intent.objects if v.namespace}
    by_uid = {v["metadata"]["uid"]: v for values in rows.values() for v in values}
    if root is not None:
        by_uid[root["metadata"]["uid"]] = root
    for kind, values in rows.items():
        for value in values:
            meta = value["metadata"]
            original = expected.get((kind, meta["name"]))
            module.require(
                original is not None
                and original.uid == meta["uid"]
                and meta.get("namespace") == intent.root.namespace,
                "cleanup_original_inventory_changed",
            )
            if original is None:
                raise accounting.AccountingUnavailable(
                    "Original cleanup object is unavailable"
                )
            if original.namespace_bootstrap:
                module.require(
                    module.cleanup_bootstrap_matches(
                        value, original.namespace_bootstrap, by_uid
                    ),
                    "cleanup_namespace_bootstrap_changed",
                )
    return rows


def _cleanup_marker(intent):
    # Submission identity is stable across retries and advancing DB-only receipts.
    payload = intent.model_dump(mode="json")
    payload.update(
        phase="teardown",
        namespace_delete_at=None,
        settlement_report_id=None,
        settled_at=None,
        last_error=None,
    )
    return json.dumps(payload, sort_keys=True)


def _cleanup_root(api, intent, original):
    from app.services.native_managed_power import helpers

    root = api.find("TroshkaProject", intent.root.namespace, original.name)
    if root is not None:
        helpers().require(
            root["metadata"]["uid"] == original.uid,
            "cleanup_root_changed",
        )
        helpers().validate_cleanup_root(root, intent.model_dump(mode="json"))
    return root


def _fresh_cleanup_inventory(api, work, intent):
    objects, _, _ = seal_native_inventory(api, work)
    expected = {
        (v.kind, v.name): v.model_dump(exclude={"resource_version"})
        for v in intent.objects
    }
    actual = {
        (v.kind, v.name): v.model_dump(exclude={"resource_version"}) for v in objects
    }
    from app.services.native_managed_power import helpers

    helpers().require(actual == expected, "cleanup_original_inventory_changed")


def _ensure_cleanup_marker(api, value, marker):
    from app.services.native_managed_power import helpers

    key = helpers().CLEANUP_ANNOTATION
    existing = value["metadata"].get("annotations", {}).get(key)
    helpers().require(existing is None or existing == marker, "cleanup_marker_changed")
    if existing is None:
        annotations = {**value["metadata"].get("annotations", {}), key: marker}
        api.patch(
            value,
            [{"op": "add", "path": "/metadata/annotations", "value": annotations}],
        )


def _cleanup_child(api, child, original):
    from app.services.native_managed_power import helpers

    value = api.get(child.kind, child.namespace, child.name)
    meta = value["metadata"]
    refs = meta.get("ownerReferences", [])
    helpers().require(
        meta["uid"] == child.uid
        and len(refs) == 1
        and refs[0].get("uid") == original.uid
        and refs[0].get("kind") == "TroshkaProject"
        and refs[0].get("name") == original.name,
        "cleanup_child_parent_changed",
    )
    return value


def submit_native_teardown(api, work):
    from app.services.native_managed_power import helpers

    intent = NativeCleanupIntent.model_validate(work["intent"])
    module = helpers()
    namespace = _original_namespace(api, intent)
    if namespace is None:
        return
    original = next(v for v in intent.objects if v.kind == "TroshkaProject")
    root = _cleanup_root(api, intent, original)
    if root is None:
        return
    marker = _cleanup_marker(intent)
    if root["metadata"].get("deletionTimestamp"):
        module.require(
            root["metadata"].get("annotations", {}).get(module.CLEANUP_ANNOTATION)
            == marker,
            "cleanup_root_marker_changed",
        )
        module.cleanup_intent(root)
        return
    module.require(
        not namespace["metadata"].get("deletionTimestamp"),
        "cleanup_namespace_terminating",
    )
    _fresh_cleanup_inventory(api, work, intent)
    _ensure_cleanup_marker(api, root, marker)
    children = [
        v
        for v in intent.objects
        if v.kind in {"TroshkaVM", "TroshkaNetwork", "TroshkaCeph"}
    ]
    for child in children:
        _ensure_cleanup_marker(api, _cleanup_child(api, child, original), marker)
    # Marker writes may fail ambiguously. Independently observe all originals,
    # including their fresh full off proof, before conditional parent deletion.
    _fresh_cleanup_inventory(api, work, intent)
    for child in children:
        value = _cleanup_child(api, child, original)
        module.require(
            value["metadata"].get("annotations", {}).get(module.CLEANUP_ANNOTATION)
            == marker,
            "cleanup_child_marker_changed",
        )
    root = _cleanup_root(api, intent, original)
    module.require(
        root is not None
        and root["metadata"].get("annotations", {}).get(module.CLEANUP_ANNOTATION)
        == marker,
        "cleanup_root_marker_changed",
    )
    assert root is not None
    if root["metadata"].get("deletionTimestamp"):
        return
    api.request(
        "DELETE",
        "TroshkaProject",
        intent.root.namespace,
        original.name,
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Foreground",
            "preconditions": {
                "uid": original.uid,
                "resourceVersion": root["metadata"]["resourceVersion"],
            },
        },
    )


def namespace_teardown_ready(api, work):
    from app.services.native_managed_power import helpers

    intent = NativeCleanupIntent.model_validate(work["intent"])
    module = helpers()
    namespace = _original_namespace(api, intent)
    if namespace is None:
        return False
    original = next(v for v in intent.objects if v.kind == "TroshkaProject")
    root = api.find("TroshkaProject", intent.root.namespace, original.name)
    if root is not None:
        module.require(root["metadata"]["uid"] == original.uid, "cleanup_root_changed")
        return False
    rows = _checked_cleanup_rows(api, intent)
    # Wait for all workload/network/service finalizers. Namespace-owned bootstrap
    # objects may remain; every one was sealed before original CR deletion.
    allowed = {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Secret"}
    return not any(values for kind, values in rows.items() if kind not in allowed)


def submit_namespace_teardown(api, work):
    intent = NativeCleanupIntent.model_validate(work["intent"])
    if not namespace_teardown_ready(api, work):
        return
    namespace = _original_namespace(api, intent)
    if namespace is None or namespace["metadata"].get("deletionTimestamp"):
        return
    api.request(
        "DELETE",
        "Namespace",
        None,
        intent.root.namespace,
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {
                "uid": intent.root.namespace_uid,
                "resourceVersion": namespace["metadata"]["resourceVersion"],
            },
        },
    )


def _settle_native(db, project, rows, intent):
    from app.services.native_collection_jobs import assert_collection_lease

    assert_collection_lease()
    from app.models.kubevirt_storage import (
        NativeStorageSnapshot,
    )
    from app.schemas.kubevirt_storage import NativeStorageReport
    from app.services import quota_native, quota_storage

    if intent.pending_initialization is not None and not intent.bindings:
        from app.services.native_pending_cleanup import require_no_known_storage

        require_no_known_storage(db, str(intent.domain_id), intent.root)

    identity = quota_storage.measurement_row(rows).dispatch_metadata[
        "native_storage_identity"
    ]
    measurement = quota_native.storage_measurement(
        db, project, intent.generation, identity, operations.now()
    )
    report = db.get(NativeStorageSnapshot, measurement.report_id)
    source = NativeStorageReport.model_validate(report.payload)
    proofs = [p for p in source.retiring_roots if p.intent_id == intent.intent_id]
    quota_storage.apply_measurement(db, project, rows, measurement, operations.now())
    absent = set(image.key for image in source.images if image.absence is not None)
    if (
        measurement.allocated_bytes != 0
        or len(proofs) != 1
        or not proofs[0].objects_absent
        or not set(intent.owned_images) <= absent
    ):
        db.commit()
        return False
    proof = proofs[0]
    if (
        proof.root != intent.root
        or proof.inventory_digest != intent.inventory_digest
        or proof.observed_at < intent.teardown_at
        or measurement.observed_at < intent.teardown_at
    ):
        raise accounting.AccountingUnavailable(
            "Cleanup settlement observation predates original teardown"
        )
    # Identity/operation locks and the current domain lock are still held. This
    # receipt survives Project deletion on the existing FK-free reservation row.
    _save_native(
        db,
        intent,
        phase="settled",
        settlement_report_id=report.id,
        settled_at=measurement.observed_at,
        last_error=None,
    )
    finalize_absence(db, project, measurement.observed_at)
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, measurement.observed_at, refresh_live=False)
    # These existing helpers may commit; the settlement receipt must already be
    # durable before their external cleanup is attempted.
    db.commit()
    _finish_native(db, project, intent)
    return True


def _finish_native(db, project, intent):
    from app.services.deploy_service import _destroy_cleanup_dns, _destroy_cleanup_eips
    from app.services.ws_pubsub import notify_project

    project, rows, intent = _checked_native(db, project.id, str(intent.intent_id))
    if intent.phase == "settled":
        project_id = project.id
        _destroy_cleanup_dns(
            db,
            {
                "dns_provider_id": project.dns_provider_id,
                "domain": project.domain,
                "topology": project.deployed_topology or project.topology or {},
            },
            project_id,
        )
        _destroy_cleanup_eips(db, project.id)
        project, rows, intent = _checked_native(db, project.id, str(intent.intent_id))
        _save_native(db, intent, phase="finalized")
        if intent.mode == "delete":
            db.delete(project)
        else:
            project.state, project.host_id, project.vni_map, project.deploy_error = (
                "draft",
                None,
                None,
                None,
            )
            project.deployed_topology = None
        db.commit()
        notify_project(
            project_id,
            (
                {"type": "project-deleted"}
                if intent.mode == "delete"
                else {"type": "project-state", "state": "draft"}
            ),
        )


def guard_supported_cleanup(db, project):
    """Never fall back to unverified native deletion, even with quotas disabled."""
    if operations.select_adapter(db, project) != "kubevirt":
        return
    if (
        project.host_id
        or project.state != "draft"
        or operations.current_generation(operations._rows(db, project.id))
    ):
        operations.unavailable(
            "Native Delete/Undeploy requires verified original ownership and backend-storage settlement"
        )


def fence(db, project_id):
    """Prevent every unclaimed/next provider dispatch before cleanup begins."""
    rows = db.query(QuotaReservation).filter_by(project_id=project_id).all()
    admission.lock_operations(db, [row.operation_id for row in rows])
    from app.services.quota_locks import lock_retained_project

    lock_retained_project(db, project_id)
    current = (
        db.query(QuotaReservation)
        .filter_by(project_id=project_id)
        .populate_existing()
        .all()
    )
    if {row.operation_id for row in current} != {row.operation_id for row in rows}:
        raise accounting.AccountingUnavailable(
            "Allocation changed before cleanup; retry cleanup"
        )
    rows = current
    for row in rows:
        row.dispatch_metadata = {**row.dispatch_metadata, "cancelled": True}
    db.flush()


def _provider_work_settled(db, project_id):
    rows = (
        db.query(QuotaReservation)
        .filter_by(project_id=project_id)
        .populate_existing()
        .all()
    )
    for row in rows:
        if not row.dispatch_metadata.get("cancelled"):
            return False
        if row.dispatch_metadata.get("claim") and not row.dispatch_metadata.get(
            "worker_finished"
        ):
            return False
        calls = _settled_calls(db, row.dispatch_metadata.get("calls", {}))
        if calls is None:
            return False
        row.dispatch_metadata = {**row.dispatch_metadata, "calls": calls}
    db.flush()
    return True


def _settled_calls(db, previous):
    from app.models.host import Host
    from app.services.troshkad_client import poll_job

    calls = dict(previous)
    for key, call in calls.items():
        if call.get("terminal"):
            continue
        if not call.get("job_id"):
            return None  # Ambiguous HTTP dispatch cannot be inferred absent.
        host = db.get(Host, call["host_id"])
        if not host:
            return None
        result = poll_job(host, call["job_id"])
        if result.get("status") not in {"completed", "failed", "cancelled"}:
            return None
        calls[key] = {**call, "terminal": True, "status": result["status"]}
    return calls


def verify_absence(db, project, host):
    """Query the agent after all accepted creation jobs have reached a terminal state."""
    from app.services.deploy_topology import _vm_dir
    from app.services.troshkad_client import get_all_vm_states, start_job, wait_for_job

    if not _provider_work_settled(db, project.id):
        return None
    # An unsupported adapter can still clean up, but must retain ledger/history
    # until an adapter can prove all resources absent (not merely a missing host).
    try:
        operations.supported(db, project, host_id=host.id, deployed=True)
    except accounting.AccountingUnavailable:
        return None
    timestamp = operations.now()  # Original request-start time, never receipt time.
    states = get_all_vm_states(host)
    if states is None or any(
        name.startswith(f"troshka-{project.id[:8]}-") for name in states
    ):
        return None
    from types import SimpleNamespace

    # Check both placements: old metadata alone cannot prove which path exists.
    for pool in (None, SimpleNamespace(mode="shared")):
        job_id = start_job(host, "/files/stat", {"path": _vm_dir(project.id, pool)})
        result = wait_for_job(host, job_id, timeout=30)
        if (
            result.get("status") != "completed"
            or result.get("result", {}).get("exists") is not False
        ):
            return None
    return timestamp


def finalize_absence(db, project, timestamp):
    """Caller supplies verified absence after fencing and settling provider work.

    Does not require accounting readiness or a spending allowance. The transaction
    atomically closes meters, retains charges, and releases the retained ledger.
    """
    from app.models.metering import MeteringInterval
    from app.services.quota_locks import lock_retained_project

    rows = db.query(QuotaReservation).filter_by(project_id=project.id).all()
    admission.lock_operations(db, [row.operation_id for row in rows])
    lock_retained_project(db, project.id)
    for row in rows:
        if not row.dispatch_metadata.get("cancelled"):
            raise accounting.AccountingUnavailable(
                "Cleanup must fence queued allocation before release"
            )
        admission.confirm(
            db,
            row.operation_id,
            Resources(),
            f"destroy:{timestamp.isoformat()}",
            timestamp,
        )
    for row in (
        db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .with_for_update()
    ):
        row.ended_at = timestamp
    accounting.accrue(db, project.id, timestamp, retained=True)
    db.flush()


def complete(db, project, host):
    timestamp = verify_absence(db, project, host)
    if timestamp is None:
        return False
    finalize_absence(db, project, timestamp)
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, timestamp, refresh_live=False)
    db.commit()
    return True


def complete_empty(db, project):
    """A never-dispatched empty draft needs no hypothetical provider observation."""
    from app.models.metering import MeteringInterval

    rows = operations.current_generation(
        db.query(QuotaReservation).filter_by(project_id=project.id).all()
    )
    if (
        project.host_id
        or db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .first()
    ):
        return False
    if any(
        row.dispatch_metadata.get("claim")
        or row.dispatch_metadata.get("calls")
        or any(row.observed.values())
        for row in rows
    ):
        return False
    fence(db, project.id)
    timestamp = operations.now()
    finalize_absence(db, project, timestamp)
    from app.services.metering_service import _freeze_quota_invoice

    _freeze_quota_invoice(db, project, timestamp, refresh_live=False)
    return True
