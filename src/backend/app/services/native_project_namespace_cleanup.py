"""One retained namespace teardown flow, separate from storage settlement."""

import errno
import json
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from app.models.native_project_cleanup import NativeProjectNamespaceCleanup as State
from app.models.native_project_cleanup import NativeProjectNamespaceEvidence as Evidence
from app.models.project import Project
from app.schemas.native_project_cleanup import (
    CleanupStorageScope,
    ProjectNamespaceCleanup,
    VMTarget,
)
from app.services import native_cleanup_authority as authority
from app.services import native_deployment_origin as origin
from app.services import quota_operations
from app.services.kubevirt_storage_enrollment import digest
from app.services.native_namespace_client import (
    ACK,
    ANNOTATION,
    OWN_KINDS,
    NamespaceClient,
)

# Reconcile poll while waiting on collector proof after the namespace is gone.
SETTLEMENT_POLL_SECONDS = 10
# Broader teardown phases (fencing, VM/CR drain, NS terminating).
CLEANUP_POLL_SECONDS = 60


def request_settlement_collection(work):
    """Kick a cleanup-priority scan as soon as settlement can consume it."""
    selected = work.authority
    if selected.domain_id is None or selected.project_id is None:
        return False
    from app.services.native_collection_jobs import enqueue_collection

    return enqueue_collection(
        str(selected.domain_id), str(selected.project_id), cleanup=True
    )


def nudge_settlement(project_id):
    """Re-run settle soon after a fresh cleanup-priority report lands."""
    from app.core.redis import enqueue_job, get_redis_raw

    with_pending = [
        work
        for work in states_for_project(project_id)
        if work.phase in {"namespace_absent", "settled"}
    ]
    if not with_pending:
        return 0
    redis = get_redis_raw()
    redis.ping()
    count = 0
    for work in with_pending:
        key = "native:namespace-cleanup:" + str(work.id)
        redis.delete(key)
        if redis.set(key, "nudge", nx=True, ex=SETTLEMENT_POLL_SECONDS):
            enqueue_job(reconcile, str(work.id))
            count += 1
    return count


def states_for_project(project_id):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        return list(states(db, project_id))


def load(row):
    origin.require(
        row is not None and digest(row.payload) == row.digest,
        "Namespace cleanup state changed",
    )
    if TYPE_CHECKING:
        assert row is not None
    return ProjectNamespaceCleanup.model_validate(row.payload)


def states(db, project_id=None):
    query = db.query(State)
    if project_id is not None:
        query = query.filter_by(project_id=str(project_id))
    return [load(row) for row in query.populate_existing()]


def pending(db, project_id):
    return any(v.phase != "finalized" for v in states(db, project_id))


def _save(db, work, **updates):
    value = ProjectNamespaceCleanup.model_validate({**work.model_dump(), **updates})
    row = db.get(State, str(value.id))
    row.payload = value.model_dump(mode="json")
    row.digest = digest(row.payload)
    project = db.get(Project, str(value.authority.project_id))
    if project is not None and value.last_error:
        project.deploy_error = value.last_error
    db.flush()
    return value


def _evidence(db, work, phase, payload):
    row = (
        db.query(Evidence).filter_by(cleanup_id=str(work.id), phase=phase).one_or_none()
    )
    if row is None:
        db.add(
            Evidence(
                id=str(uuid4()),
                cleanup_id=str(work.id),
                phase=phase,
                digest=digest(payload),
                payload=payload,
            )
        )
    else:
        origin.require(
            row.payload == payload and row.digest == digest(payload),
            "Namespace cleanup evidence changed",
        )
    db.flush()


def _locked(db, cleanup_id, previous=None):
    work = load(
        db.query(State)
        .filter_by(id=str(cleanup_id))
        .populate_existing()
        .with_for_update()
        .one()
    )
    project = db.get(Project, str(work.authority.project_id))
    # Post-release phases own the cleanup row only; undeploy may still have a
    # draft Project without a native provider assignment.
    if work.phase in {"orphan_reclaim", "finalized"} or project is None:
        origin.require(
            work.phase in {"orphan_reclaim", "finalized"} or project is not None,
            "Cleanup project unavailable before finalization",
        )
        origin.require(
            previous is None or work == previous,
            "Namespace cleanup changed during unlocked I/O",
        )
        return project, work
    authority.lock(db, project, work.authority)
    work = load(db.query(State).filter_by(id=str(cleanup_id)).populate_existing().one())
    origin.require(
        previous is None or work == previous,
        "Namespace cleanup changed during unlocked I/O",
    )
    boundary = authority.enrollment_boundary(db, work.authority)
    if previous is not None:
        origin.require(
            boundary == previous.enrollment_boundary,
            "Cleanup live enrollment changed during unlocked I/O",
        )
    elif work.enrollment_boundary != boundary:
        work = _save(db, work, enrollment_boundary=boundary)
    return project, work


def request(db, project, mode):
    from app.core.redis import enqueue_job

    if quota_operations.select_adapter(db, project) != "kubevirt":
        return None
    resolved = authority.lock(db, project)
    if resolved is None:
        return None
    current = [v for v in states(db, project.id) if v.phase != "finalized"]
    if current:
        origin.require(
            len(current) == 1 and current[0].mode == mode,
            "Original cleanup mode already pending",
        )
        _, work = _locked(db, current[0].id)
    else:
        selected, liabilities = resolved
        work = ProjectNamespaceCleanup(
            id=uuid4(),
            authority=selected,
            mode=mode,
            liabilities=liabilities,
            liability_digest=digest(liabilities.model_dump(mode="json")),
            prepared_at=quota_operations.now(),
        )
        payload = work.model_dump(mode="json")
        db.add(
            State(
                id=str(work.id),
                project_id=project.id,
                payload=payload,
                digest=digest(payload),
            )
        )
        if selected.source_kind == "origin":
            origin.cancel(db, project.id)
        project.state, project.deploy_error = (
            "cleanup_pending",
            "Namespace cleanup and known-storage orphan reclaim pending",
        )
        db.flush()
        _evidence(db, work, "requested", payload)
    from app.services.quota_cleanup import freeze_cleanup_metering

    freeze_cleanup_metering(db, project)
    db.commit()
    from app.core.redis import mark_cancelled

    mark_cancelled(project.id)
    enqueue_job(reconcile, str(work.id))
    return dict(
        status="cleanup_pending", id=project.id, mode=mode, cleanup_id=str(work.id)
    )


def _namespace(api, selected):
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(
        cluster and cluster["metadata"]["uid"] == selected.cluster_uid,
        "Cleanup cluster changed",
    )
    namespace = api.get("Namespace", None, selected.namespace)
    if namespace is not None:
        origin.require(
            namespace["metadata"]["uid"] == selected.namespace_uid,
            "Cleanup namespace replaced",
        )
    cluster = api.get("Namespace", None, "kube-system")
    origin.require(
        cluster and cluster["metadata"]["uid"] == selected.cluster_uid,
        "Cleanup absence cluster changed",
    )
    return namespace


def _marker(work, pins, identity):
    selected = work.authority
    return dict(
        version=work.version,
        cleanupId=str(work.id),
        sourceKind=selected.source_kind,
        sourceId=str(selected.source_id),
        sourceDigest=selected.source_digest,
        projectId=str(selected.project_id),
        clusterUid=selected.cluster_uid,
        namespace=selected.namespace,
        namespaceUid=selected.namespace_uid,
        rootUid=selected.root_uid,
        qualification=pins,
        operator=identity,
    )


def _adopt_recovered_origin_root(db, work, root_receipt):
    """Refresh sealed cleanup authority after a late cancelled root receipt."""
    project = db.get(Project, str(work.authority.project_id))
    # Do not pass the stale null-root authority as ``expected``: resolve already
    # reflects the sealed receipt, which is the gap this recovery closes.
    resolved = authority.lock(db, project)
    origin.require(
        resolved is not None, "Origin cleanup authority unavailable after root recovery"
    )
    if TYPE_CHECKING:
        assert resolved is not None
    selected, liabilities = resolved
    origin.require(
        selected.source_kind == "origin"
        and str(selected.source_id) == str(work.authority.source_id)
        and selected.namespace_uid == work.authority.namespace_uid
        and selected.cluster_uid == work.authority.cluster_uid
        and work.authority.root_uid is None
        and selected.root_uid == root_receipt.root_uid,
        "Recovered origin root authority changed",
    )
    current = load(db.query(State).filter_by(id=str(work.id)).populate_existing().one())
    origin.require(
        current.phase == work.phase
        and current.marker is None
        and current.authority.root_uid is None
        and str(current.authority.source_id) == str(work.authority.source_id)
        and current.authority.namespace_uid == work.authority.namespace_uid,
        "Namespace cleanup changed during origin root recovery",
    )
    evidence = dict(
        previous=current.authority.model_dump(mode="json"),
        authority=selected.model_dump(mode="json"),
        root_uid=root_receipt.root_uid,
        generation=root_receipt.generation,
    )
    _evidence(db, current, "origin_root_recovered:" + digest(evidence)[:32], evidence)
    work = _save(
        db,
        current,
        authority=selected,
        liabilities=liabilities,
        liability_digest=digest(liabilities.model_dump(mode="json")),
        last_error=None,
    )
    db.commit()
    return work


def _recover_cancelled_origin_root(db, api, work):
    """Seal a late cancelled root POST and adopt it before producer fencing.

    Delete can cancel origin after the TroshkaProject create lands but before the
    root receipt is bound. Cleanup then freezes ``root_uid=None`` and fencing
    refuses to proceed while the CR remains. ``reconcile_cancelled`` already
    retains that exact late result; cleanup must invoke it and refresh authority
    before the strict lock/marker path. This does not invent UIDs or skip proof.
    """
    selected = work.authority
    if (
        selected.source_kind != "origin"
        or selected.root_uid is not None
        or work.marker is not None
        or work.phase != "fencing"
    ):
        return work
    intent = origin.active(db, str(selected.project_id))
    if intent is None or str(intent.id) != str(selected.source_id):
        return work
    from app.models.native_deployment_origin import NativeDeploymentOriginActive

    pointer = db.get(NativeDeploymentOriginActive, str(selected.project_id))
    if pointer is None or not pointer.cancelled:
        return work
    _, root_receipt = origin.receipts(db, str(intent.id))
    if root_receipt is None:
        # Unlock before remote readback / worker-idle checks.
        db.commit()
        origin.reconcile_cancelled(db, api, intent)
        _, root_receipt = origin.receipts(db, str(intent.id))
    if root_receipt is None:
        return work
    return _adopt_recovered_origin_root(db, work, root_receipt)


def _publish_marker(db, api, work, namespace, operator_namespace):
    if work.authority.root_uid is None:
        origin.require(
            not api.rows("TroshkaProject", work.authority.namespace),
            "Original root UID unresolved; producer boundary unavailable",
        )
    pins, identity = api.operator_profile(
        operator_namespace, work.authority.cluster_uid
    )
    marker = _marker(work, pins, identity)
    previous = work.marker
    history = None
    if previous is not None and previous != marker:
        history = _replacement_boundary(api, work)
    _, current = _locked(db, work.id, work)
    if history is not None:
        origin.require(
            db.query(Evidence).filter_by(cleanup_id=str(work.id)).count() < 256,
            "Cleanup operator requalification history limit",
        )
        _evidence(
            db,
            current,
            "operator_successor:" + digest(marker)[:32],
            dict(previous=previous, marker=marker, executions=history),
        )
    _evidence(db, current, "fence:" + digest(marker)[:32], marker)
    work = _save(db, current, marker=marker)
    db.commit()
    annotations = dict(namespace["metadata"].get("annotations", {}))
    existing = annotations.get(ANNOTATION)
    origin.require(
        existing is None
        or json.loads(existing) == marker
        or _marker_predecessor(db, work, json.loads(existing)),
        "Namespace has another cleanup marker",
    )
    annotations.pop(ACK, None)
    annotations[ANNOTATION] = json.dumps(marker, sort_keys=True, separators=(",", ":"))
    api.patch_namespace(
        work.authority.namespace,
        work.authority.namespace_uid,
        namespace["metadata"]["resourceVersion"],
        annotations,
    )
    return work


def _marker_predecessor(db, work, marker):
    current = work.marker
    for _ in range(256):
        row = (
            db.query(Evidence)
            .filter_by(
                cleanup_id=str(work.id),
                phase="operator_successor:" + digest(current)[:32],
            )
            .one_or_none()
        )
        if (
            row is None
            or row.digest != digest(row.payload)
            or row.payload["marker"] != current
        ):
            return False
        current = row.payload["previous"]
        if current == marker:
            return True
    return False


def _replacement_boundary(api, work):
    """A new qualified singleton cannot stand in for interrupted old work."""
    origin.require(
        not any(
            v.startswith("interrupted_operator_execution:")
            for v in work.liabilities.unresolved
        ),
        "Original operator execution remains unresolved",
    )
    rows = [
        row for kind in OWN_KINDS for row in api.rows(kind, work.authority.namespace)
    ]
    original = work.authority.root_uid
    origin.require(
        original is None
        or any(row["metadata"]["uid"] == original for row in rows)
        or any(
            target.kind == "TroshkaProject" and target.uid == original
            for target in work.cr_targets
        ),
        "Original operator root history unavailable before replacement",
    )
    history = [_diagnostic(row) for row in rows]
    origin.require(
        all(
            execution.get("state") == "complete"
            for row in history
            for execution in row["executions"].values()
        ),
        "Original operator execution remains unresolved",
    )
    return history


def _acknowledged(api, work, namespace):
    annotations = namespace["metadata"].get("annotations", {})
    origin.require(
        json.loads(annotations.get(ANNOTATION, "null")) == work.marker,
        "Namespace marker changed",
    )
    ack = json.loads(annotations.get(ACK, "null"))
    if ack is None:
        return False
    identity = ack.get("operator", {})
    origin.require(
        ack.get("markerDigest") == digest(work.marker)
        and ack.get("namespaceUid") == work.authority.namespace_uid
        and identity.get("incarnation")
        and {k: v for k, v in identity.items() if k != "incarnation"}
        == work.marker["operator"],
        "Namespace producer acknowledgement changed",
    )
    api.qualify_operator(work.marker["qualification"], identity)
    return True


def _targets(api, work, kinds):
    return [
        VMTarget(
            kind=kind,
            name=row["metadata"]["name"],
            uid=row["metadata"]["uid"],
            resource_version=row["metadata"]["resourceVersion"],
        )
        for kind in kinds
        for row in api.rows(kind, work.authority.namespace)
    ]


def _delete_targets(api, work, targets, *, orphan=False):
    for target in targets:
        current = api.get(target.kind, work.authority.namespace, target.name)
        if current is None:
            continue
        meta = current["metadata"]
        origin.require(meta["uid"] == target.uid, "Original cleanup target replaced")
        if not meta.get("deletionTimestamp"):
            # Retry only the original persisted UID/RV. Conflicts require a fresh
            # request evidence record rather than widening a previous DELETE.
            api.delete(
                target.kind,
                work.authority.namespace,
                target.name,
                target.uid,
                target.resource_version,
                orphan=orphan,
            )


def _inventory_evidence(db, work):
    return [
        row
        for row in db.query(Evidence).filter_by(cleanup_id=str(work.id)).all()
        if row.phase.startswith("inventory:")
    ]


def _retain_inventory(db, api, work):
    # Dedicated namespace authority does not require LIST on unrelated children.
    # These exact storage and participating-controller lists remain mandatory.
    # Skip once evidence exists: re-running after live-map soft-seal (empty
    # bindings) would re-add unresolved_pvc debts and block require_coverage.
    if _inventory_evidence(db, work):
        return work
    from app.services.native_teardown_map import hard_debts, map_sealed

    rows = _required_inventory(api, work.authority.namespace)
    _, current = _locked(db, work.id, work)
    diagnostics = [_diagnostic(r) for r in rows]
    _evidence(db, current, "inventory:" + digest(diagnostics)[:32], diagnostics)
    # Soft-sealed live maps already authorized teardown. Snapshot finalizers for
    # orphan completion proof, but do not reintroduce PVC/snapshot hard debts.
    if map_sealed(current) and not hard_debts(current.liabilities):
        db.commit()
        return current
    liabilities = _support_liabilities(db, current)
    known_pvcs = {b.pvc_uid for b in liabilities.bindings}
    known_snapshots = {s.snapshot_uid for s in liabilities.snapshots}
    debt = set(liabilities.unresolved)
    for row in rows:
        kind, uid = row["kind"], row["metadata"]["uid"]
        if kind == "PersistentVolumeClaim" and uid not in known_pvcs:
            debt.add("unresolved_pvc:" + uid)
        if kind == "VolumeSnapshot" and uid not in known_snapshots:
            debt.add("unresolved_snapshot:" + uid)
        for execution_id, execution in (
            row.get("status", {}).get("originExecutions", {}).items()
        ):
            if execution.get("state") != "complete":
                debt.add("interrupted_operator_execution:" + execution_id)
    liabilities = liabilities.model_copy(update={"unresolved": sorted(debt)})
    work = _save(
        db,
        current,
        liabilities=liabilities,
        liability_digest=digest(liabilities.model_dump(mode="json")),
    )
    db.commit()
    return work


def _required_inventory(api, namespace):
    rows, seen = [], set()
    for kind in ("PersistentVolumeClaim", "VolumeSnapshot", *OWN_KINDS):
        values = api.rows(kind, namespace)
        origin.require(
            isinstance(values, list) and len(values) <= 1000,
            "Namespace required inventory incomplete",
        )
        for row in values:
            meta = row.get("metadata", {})
            key = kind, meta.get("uid")
            origin.require(
                row.get("kind") == kind
                and meta.get("name")
                and meta.get("uid")
                and meta.get("namespace") == namespace
                and key not in seen,
                "Namespace required inventory identity incomplete",
            )
            seen.add(key)
            rows.append(row)
    return rows


def _support_liabilities(db, work):
    if work.authority.source_kind != "origin":
        return work.liabilities
    from app.services.native_origin_ops_state import cleanup_obligations

    debt, boundaries = cleanup_obligations(db, str(work.authority.source_id))
    unresolved = [
        v
        for v in work.liabilities.unresolved
        if not v.startswith("support_outcome_unresolved:")
    ]
    result = work.liabilities.model_copy(
        update={
            "unresolved": sorted(set(unresolved + debt)),
            "support_boundaries": boundaries,
        }
    )
    payload = result.model_dump(mode="json")
    _evidence(db, work, "support_scope:" + digest(payload)[:32], payload)
    return result


def _diagnostic(row):
    executions = row.get("status", {}).get("originExecutions", {})
    return dict(
        kind=row["kind"],
        name=row["metadata"]["name"],
        uid=row["metadata"]["uid"],
        finalizers=row["metadata"].get("finalizers", []),
        volume_name=row.get("spec", {}).get("volumeName"),
        executions={
            key: {
                field: value.get(field)
                for field in ("executionId", "state", "outcome", "operator")
            }
            for key, value in executions.items()
        },
    )


def _remove_vms(db, api, work):
    targets = _targets(api, work, ("VirtualMachine",))
    previous = {t.name: t.uid for t in work.vm_targets}
    origin.require(
        work.phase == "fencing" or all(previous.get(t.name) == t.uid for t in targets),
        "Cleanup VM replaced or recreated",
    )
    _, current = _locked(db, work.id, work)
    _evidence(
        db,
        current,
        "vm_request:" + digest([t.model_dump() for t in targets])[:32],
        [t.model_dump() for t in targets],
    )
    retained = {t.name: t for t in work.vm_targets}
    retained.update({t.name: t for t in targets})
    work = _save(
        db,
        current,
        phase="vm_delete_requested",
        vm_targets=list(retained.values()),
        last_error="Waiting for normal VM deletion and VM/VMI absence",
    )
    db.commit()
    _delete_targets(api, work, targets)
    empty = not api.rows("VirtualMachine", work.authority.namespace)
    empty &= not api.rows("VirtualMachineInstance", work.authority.namespace)
    return work, empty


def _remove_own_crs(db, api, work):
    """Finish only our finalizers before namespace GC removes their RBAC."""
    targets = _targets(api, work, OWN_KINDS)
    previous = {(t.kind, t.name): t.uid for t in work.cr_targets}
    origin.require(
        not previous or all(previous.get((t.kind, t.name)) == t.uid for t in targets),
        "Cleanup CR replaced",
    )
    # A terminating object discovered for the first time is not our retained
    # DELETE target, even if an older inventory happened to see its finalizer.
    for target in targets:
        row = api.get(target.kind, work.authority.namespace, target.name)
        origin.require(
            row is not None and row["metadata"]["uid"] == target.uid,
            "Cleanup CR observation changed",
        )
        if TYPE_CHECKING:
            assert row is not None
        origin.require(
            not row["metadata"].get("deletionTimestamp")
            or previous.get((target.kind, target.name)) == target.uid,
            "Terminating cleanup CR has no persisted target",
        )
    _, current = _locked(db, work.id, work)
    payload = [t.model_dump() for t in targets]
    _evidence(db, current, "cr_request:" + digest(payload)[:32], payload)
    retained = {(t.kind, t.name): t for t in work.cr_targets}
    retained.update({(t.kind, t.name): t for t in targets})
    work = _save(
        db,
        current,
        cr_targets=list(retained.values()),
        last_error="Waiting for Troshka CR external duties and finalizers",
    )
    db.commit()
    _delete_targets(api, work, targets, orphan=True)
    return work, _own_cr_completion(db, api, work) is not None


def _normal_deletion_timestamp(value):
    try:
        return (
            isinstance(value, str)
            and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset()
            is not None
        )
    except ValueError:
        return False


def _cr_registration(db, work, target):
    """Use existing immutable pre-delete diagnostics, including pending requests."""
    inventory, request = None, None
    for row in (
        db.query(Evidence).filter_by(cleanup_id=str(work.id)).order_by(Evidence.phase)
    ):
        if not row.phase.startswith(("inventory:", "cr_request:")):
            continue
        origin.require(
            row.digest == digest(row.payload),
            "Cleanup CR registration evidence changed",
        )
        matches = [
            v
            for v in row.payload
            if all(v.get(k) == target[k] for k in ("kind", "name", "uid"))
        ]
        if row.phase.startswith("inventory:") and any(
            "troshka.redhat.com/finalizer" in v.get("finalizers", []) for v in matches
        ):
            inventory = row
        if row.phase.startswith("cr_request:") and matches:
            request = row
    origin.require(
        inventory is not None and request is not None,
        "Cleanup CR finalizer registration proof unavailable",
    )
    if TYPE_CHECKING:
        assert inventory is not None and request is not None
    return dict(
        registration_phase=inventory.phase,
        registration_digest=inventory.digest,
        request_phase=request.phase,
        request_digest=request.digest,
    )


def _own_cr_completion(db, api, work):
    """Only a proven finalizer transition may leave Kubernetes orphan GC pending."""
    targets = {(t.kind, t.name): t.uid for t in work.cr_targets}
    observations = []
    for kind in OWN_KINDS:
        for row in api.rows(kind, work.authority.namespace):
            meta = row["metadata"]
            origin.require(
                row.get("kind") == kind
                and meta.get("namespace") == work.authority.namespace
                and targets.get((kind, meta.get("name"))) == meta.get("uid")
                and meta.get("uid"),
                "Cleanup CR replaced or not retained",
            )
            if meta.get("finalizers") != ["orphan"] or not _normal_deletion_timestamp(
                meta.get("deletionTimestamp")
            ):
                return None
            observed = dict(
                kind=kind,
                name=meta["name"],
                uid=meta["uid"],
                resource_version=meta["resourceVersion"],
                deletion_timestamp=meta["deletionTimestamp"],
                finalizers=meta["finalizers"],
            )
            observations.append({**observed, **_cr_registration(db, work, observed)})
    return observations


def _delete_namespace(db, api, work):
    from app.services.native_cleanup_storage import require_coverage

    namespace = _namespace(api, work.authority)
    origin.require(
        namespace is not None and _acknowledged(api, work, namespace),
        "Namespace fence acknowledgement unavailable",
    )
    if TYPE_CHECKING:
        assert namespace is not None
    origin.require(
        not api.rows("VirtualMachine", work.authority.namespace)
        and not api.rows("VirtualMachineInstance", work.authority.namespace),
        "Project VM/VMI remains",
    )
    completed = _own_cr_completion(db, api, work)
    origin.require(completed is not None, "Project own finalizer remains")
    rv = namespace["metadata"]["resourceVersion"]
    _, current = _locked(db, work.id, work)
    authority.local_writers_closed(db, current.authority)
    require_coverage(current)
    if completed:
        _evidence(
            db, current, "cr_external_complete:" + digest(completed)[:32], completed
        )
    payload = dict(
        namespace_uid=work.authority.namespace_uid,
        resource_version=rv,
        liability_digest=work.liability_digest,
        marker_digest=digest(work.marker),
        cr_completion_digest=digest(completed),
    )
    _evidence(db, current, "namespace_request:" + digest(payload)[:32], payload)
    work = _save(
        db,
        current,
        phase="namespace_delete_requested",
        namespace_resource_version=rv,
        namespace_delete_at=quota_operations.now(),
        last_error="Waiting for namespace termination",
    )
    db.commit()
    api.delete_namespace(work.authority.namespace, work.authority.namespace_uid, rv)
    # Cleanup scopes become visible here; start priority collection during NS GC.
    request_settlement_collection(work)
    return work


def advance(db, api, cleanup_id, *, operator_namespace="troshka-operator"):
    work = load(db.get(State, cleanup_id))
    # Adopt a late sealed root before the strict predecessor lock; otherwise a
    # cleanup frozen at root_uid=None can never match resolve() after binding.
    if work.phase == "fencing" and work.authority.root_uid is None:
        work = _recover_cancelled_origin_root(db, api, work)
    _, work = _locked(db, cleanup_id)
    authority.local_writers_closed(db, work.authority)
    db.commit()
    if work.phase in {
        "namespace_absent",
        "orphan_reclaim",
        "settled",
        "finalized",
    }:
        return work
    namespace = _namespace(api, work.authority)
    if namespace is None:
        origin.require(
            work.phase == "namespace_delete_requested",
            "Namespace disappeared without retained DELETE request",
        )
        _, current = _locked(db, work.id, work)
        _evidence(
            db,
            current,
            "namespace_absent",
            dict(
                namespace_uid=work.authority.namespace_uid,
                liability_digest=work.liability_digest,
                cleanup_id=str(work.id),
            ),
        )
        work = _save(
            db,
            current,
            phase="namespace_absent",
            last_error="Namespace absent; releasing project and reclaiming known orphans",
        )
        db.commit()
        return work
    if namespace["metadata"].get("deletionTimestamp"):
        origin.require(
            work.phase == "namespace_delete_requested",
            "Unexpected namespace termination",
        )
        return work
    if work.marker is None or ANNOTATION not in namespace["metadata"].get(
        "annotations", {}
    ):
        return _publish_marker(db, api, work, namespace, operator_namespace)
    published = json.loads(namespace["metadata"]["annotations"][ANNOTATION])
    if published != work.marker and _marker_predecessor(db, work, published):
        return _publish_marker(db, api, work, namespace, operator_namespace)
    pins, identity = api.operator_profile(
        operator_namespace, work.authority.cluster_uid
    )
    if _marker(work, pins, identity) != work.marker:
        return _publish_marker(db, api, work, namespace, operator_namespace)
    if not _acknowledged(api, work, namespace):
        # Raw event handlers add no finalizer and have no durable retry. A new
        # non-authoritative nonce resends the same committed marker after failure.
        annotations = dict(namespace["metadata"].get("annotations", {}))
        annotations[ANNOTATION + "-retry"] = str(uuid4())
        api.patch_namespace(
            work.authority.namespace,
            work.authority.namespace_uid,
            namespace["metadata"]["resourceVersion"],
            annotations,
        )
        return work
    work = _ensure_teardown_coverage(db, api, work)
    work, empty = _remove_vms(db, api, work)
    if not empty:
        return work
    work, empty = _remove_own_crs(db, api, work)
    return _delete_namespace(db, api, work) if empty else work


_SOFT_RECOVERY_TOKENS = (
    "storage coverage",
    "capture unavailable",
    "read failed",
    "unavailable",
    "timeout",
    "no such file",
    "authorized-pins",
)


def _soft_recovery_failure(error):
    """True when origin recover failed for collector/coverage availability."""
    message = str(error).lower()
    return any(token in message for token in _SOFT_RECOVERY_TOKENS)


def _ensure_teardown_coverage(db, api, work):
    """Retain or seal storage coverage before VM/CR/NS drain.

    Origin recover needs collection pins (mounted on collection-worker, not the
    lifecycle worker). Missing pins and other soft coverage failures seal a
    live-namespace teardown map instead. Once sealed, later phases must not
    re-enter recover — that reintroduced the pins FileNotFoundError hang.

    Always snapshot inventory first. Origin soft-seal (domain_id null) used to
    skip this, so orphan CR completion could not prove
    ``troshka.redhat.com/finalizer`` registration and hung forever.
    """
    from app.schemas.quota import AccountingUnavailable
    from app.services.native_cleanup_storage import recover, require_coverage
    from app.services.native_teardown_map import hard_debts, map_sealed, seal

    work = _retain_inventory(db, api, work)

    if work.authority.domain_id is not None:
        work = seal(db, api, work)
        require_coverage(work)
        return work

    if work.authority.source_kind != "origin":
        work = seal(db, api, work)
        require_coverage(work)
        return work

    if map_sealed(work) and not hard_debts(work.liabilities):
        require_coverage(work)
        return work

    if work.phase == "fencing" and not map_sealed(work):
        soft_error = None
        try:
            work = recover(db, api, work)
            require_coverage(work)
            return work
        except AccountingUnavailable as error:
            if not _soft_recovery_failure(error):
                raise
            soft_error = error
        except FileNotFoundError as error:
            soft_error = AccountingUnavailable(
                "storage coverage capture unavailable: missing collection pins "
                f"({error})"
            )
        except OSError as error:
            if getattr(error, "errno", None) != errno.ENOENT:
                raise
            soft_error = AccountingUnavailable(
                "storage coverage capture unavailable: missing collection pins "
                f"({error})"
            )
        work = seal(db, api, work)
        if not (map_sealed(work) and not hard_debts(work.liabilities)):
            raise soft_error
        require_coverage(work)
        return work

    if not map_sealed(work):
        work = seal(db, api, work)
    require_coverage(work)
    return work


def storage_scopes(db, domain_id=None, *, settled_only=False):
    result = []
    for work in states(db):
        if settled_only and work.phase not in {"settled", "finalized"}:
            continue
        selected = work.authority
        if (
            selected.root is None
            or selected.domain_id is None
            or work.namespace_delete_at is None
        ):
            continue
        if domain_id is not None and str(selected.domain_id) != str(domain_id):
            continue
        if work.phase not in {"orphan_reclaim", "settled", "finalized"}:
            project = db.get(Project, str(selected.project_id))
            if project is None:
                continue
            resolved = authority.resolve(db, project)
            origin.require(
                resolved is not None
                and resolved[0] == selected
                and resolved[1].model_dump(exclude={"unresolved"})
                == work.liabilities.model_dump(exclude={"unresolved"}),
                "Cleanup storage scope predecessor changed",
            )
        result.append(
            CleanupStorageScope(
                intent_id=work.id,
                operation_id="namespace-cleanup:" + str(work.id),
                domain_id=selected.domain_id,
                root=selected.root,
                phase=work.phase,
                inventory_digest=work.liability_digest,
                owned_images=work.liabilities.private_images,
                bindings=work.liabilities.bindings,
                teardown_at=work.namespace_delete_at,
                liabilities=work.liabilities,
                settlement_report_id=work.settlement_report_id,
                settled_at=work.settled_at,
            )
        )
    return result


def reconcile(cleanup_id):
    from app.core.database import SessionLocal
    from app.models.provider import Provider
    from app.services.providers.kubevirt import _get_k8s_clients

    with SessionLocal() as db:
        work = load(db.get(State, cleanup_id))
        if work.phase == "finalized":
            return
        client = None
        try:
            provider = db.get(Provider, str(work.authority.provider_id))
            operator_namespace = (
                cast(Provider, provider)
                .get_credentials()
                .get("namespace", "troshka-operator")
            )
            db.commit()
            _, _, client = _get_k8s_clients(provider)
            api = NamespaceClient(client)
            work = advance(
                db,
                api,
                cleanup_id,
                operator_namespace=operator_namespace,
            )
            if work.phase in {"namespace_absent", "orphan_reclaim", "settled"}:
                from app.services.native_project_retirement import settle

                settle(db, cleanup_id, api=api)
        except Exception as error:
            from app.schemas.quota import AccountingUnavailable

            db.rollback()
            _, work = _locked(db, cleanup_id)
            reason = (
                str(error)
                if isinstance(error, AccountingUnavailable)
                else "Namespace cleanup or storage evidence remains unavailable; retry required"
            )
            _save(db, work, last_error=reason)
            db.commit()
            if work.phase in {"namespace_absent", "orphan_reclaim"}:
                # Keep polling orphan reclaim; optional collector audit may still help.
                request_settlement_collection(work)
            raise
        finally:
            if client is not None:
                client.close()


def dispatch_pending(redis, pending):
    """Resume retained cleanup after restart, including scopes without a domain.

    ``pending`` is an iterable of ``(cleanup_id, phase)``. Settlement phases poll
    faster so a complete report is consumed soon after it lands.
    """
    from app.core.redis import enqueue_job

    count = 0
    for cleanup_id, phase in pending:
        key = "native:namespace-cleanup:" + cleanup_id
        ttl = (
            SETTLEMENT_POLL_SECONDS
            if phase in {"namespace_absent", "orphan_reclaim", "settled"}
            else CLEANUP_POLL_SECONDS
        )
        if redis.set(key, "queued", nx=True, ex=ttl):
            enqueue_job(reconcile, cleanup_id)
            count += 1
    return count
