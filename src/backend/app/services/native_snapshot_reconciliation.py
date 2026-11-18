"""Resolve/retire existing debt, including after cancellation; never authorize POST."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from app.models.native_deployment_origin import NativeDeploymentOriginActive
from app.models.native_origin_custody import (
    NativeOriginCustodyCurrent,
    NativeOriginCustodyReceipt,
    NativeOriginCustodyRequest,
)
from app.models.project import Project
from app.schemas.native_origin_custody import (
    OriginCustodyRequest,
    VerifiedSnapshotReconciliation,
)
from app.schemas.native_snapshot_custody import SnapshotFacts
from app.services import native_origin_custody as custody
from app.services import native_snapshot_custody as snapshots
from app.services.kubevirt_storage_enrollment import digest, utc


def _state(db, intent):
    custody._active_identity(db, intent)
    pointer = db.get(NativeDeploymentOriginActive, str(intent.project_id))
    return dict(
        cancelled=pointer.cancelled,
        io_phase=pointer.io_phase,
        io_token=pointer.io_token,
        project_state=db.query(Project)
        .filter_by(id=str(intent.project_id))
        .populate_existing()
        .one()
        .state,
    )


def validate_state(db, request):
    custody.require(
        request.mode == "snapshot-reconcile"
        and request.snapshot_read_state == _state(db, request.intent),
        "Snapshot cancellation or writer state changed",
    )


def prepare(db, intent_id):
    current = custody._current(db, intent_id)
    custody.require(
        current is not None and bool(current[1].producers),
        "Snapshot existing debt required",
    )
    if TYPE_CHECKING:
        assert current is not None and bool(current[1].producers)
    row, receipt = current
    custody.retained(db, db.get(Project, row.project_id))
    domain, manifest = custody._domain(db, row.domain_id)
    pins = custody._extended_pins(
        manifest,
        domain.enrollment_id,
        receipt.publication.root,
        retired=custody._settled_roots(db),
    )
    enrollment = custody.enrollment.prepare_native_enrollment(
        db, [str(r.project_id) for r in pins.project_roots]
    )
    request = receipt.publication.model_copy(
        update={
            **{key: getattr(receipt, key) for key in SnapshotFacts.model_fields},
            "request_id": uuid4(),
            "mode": "snapshot-reconcile",
            "pvc_uids": [],
            "pins": pins,
            "enrollment": enrollment,
            "previous_enrollment_id": UUID(domain.enrollment_id),
            "previous_enrollment_digest": manifest.digest,
            "predecessor_id": UUID(row.id),
            "predecessor_digest": row.digest,
            "snapshot_read_state": _state(db, receipt.publication.intent),
        }
    )
    db.add(
        NativeOriginCustodyRequest(
            id=str(request.request_id),
            digest=digest(request),
            payload=request.model_dump(mode="json"),
        )
    )
    db.flush()
    return request


def _root(kube, prepared):
    root = prepared.root
    custody.require(
        kube.cluster_identity() == root.cluster_key, "Snapshot cluster changed"
    )
    namespace = kube.namespace(root.namespace)
    custody.require(
        namespace["metadata"]["uid"] == root.namespace_uid, "Snapshot namespace changed"
    )
    values = kube.projects(root.namespace)
    if not values:
        custody.require(
            prepared.snapshot_read_state["cancelled"],
            "Snapshot root absent before cancellation",
        )
        return None
    custody.require(len(values) == 1, "Snapshot root ambiguous")
    value = values[0]
    custody.require(
        value["metadata"]["uid"] == root.project_cr_uid
        and value["metadata"].get("generation") == root.deployment_generation
        and digest(value["spec"]) == prepared.intent.cr_spec_digest,
        "Snapshot original root changed",
    )
    return value


def _observe(prepared, kube, backend, root):
    facts = SnapshotFacts(
        **{key: getattr(prepared, key) for key in SnapshotFacts.model_fields}
    )
    result, producers = {p.producer_id: p for p in facts.snapshots}, []
    api = snapshots._api(kube)
    for intent in facts.producers:
        if intent.kind != "VolumeSnapshot" or intent.id in snapshots.retired_ids(facts):
            producers.append(intent)
            continue
        # A disappeared root cannot supply an unobserved original child UID.
        original = root or {
            "status": {
                "originProducerObjects": {
                    str(intent.id): {
                        "uid": intent.uid,
                        "specDigest": intent.admitted_spec_digest,
                    }
                }
            }
        }
        intent, live = snapshots._producer(prepared, intent, api, original)
        if live is not None:
            intent = snapshots._content_intent(prepared, intent, live, api)
            proof = snapshots._resolve(
                prepared, intent, live, [intent.source_binding], kube, backend, api
            )
            if proof is not None:
                old = result.get(intent.id)
                custody.require(
                    old is None or snapshots.same_allocation(old, proof),
                    "Snapshot original allocation changed",
                )
                result[intent.id] = proof
        producers.append(intent)
    return facts.model_copy(
        update={"producers": producers, "snapshots": list(result.values())}
    )


def _retry_bookkeeping(value):
    if (
        isinstance(value, dict)
        and value.get("success") is False
        and value.get("failure") is False
    ):
        return {k: v for k, v in value.items() if k not in {"delayed", "retries"}}
    return value


def _comparison_root(root):
    """Ignore only retry bookkeeping; producer and writer state remain pinned."""
    if root is None:
        return None
    value = deepcopy(root)
    meta = value["metadata"]
    meta.pop("resourceVersion", None)
    meta.pop("managedFields", None)
    annotations = meta.get("annotations", {})
    status = value.get("status", {})
    progress = status.get("kopf", {}).get("progress", {})
    for handler in ("project_update", "project_managed_power_resume"):
        key = "kopf.zalando.org/" + handler
        if key in annotations:
            annotations[key] = _retry_bookkeeping(json.loads(annotations[key]))
        if handler in progress:
            progress[handler] = _retry_bookkeeping(progress[handler])
    for execution in status.get("originExecutions", {}).values():
        if execution.get("state") == "complete":
            execution.pop("executionId", None)
    return value


def verify(request, kube, backend):
    from app.services.native_snapshot_retirement import observe

    prepared = OriginCustodyRequest.model_validate(request)
    custody.require(
        prepared.mode == "snapshot-reconcile"
        and prepared.snapshot_read_state is not None,
        "Snapshot read-only reconciliation request required",
    )
    root = _root(kube, prepared)
    facts = _observe(prepared, kube, backend, root)
    repeated = _observe(prepared, kube, backend, root)
    custody.require(
        snapshots.stable_facts(facts) == snapshots.stable_facts(repeated),
        "Snapshot facts changed during read",
    )
    facts = observe(prepared, facts, kube, backend)
    from app.services.native_snapshot_observation import verify_graph

    verify_graph(facts, kube, backend)
    custody.require(
        _comparison_root(root) == _comparison_root(_root(kube, prepared)),
        "Snapshot root changed during read",
    )
    value = VerifiedSnapshotReconciliation(
        **facts.model_dump(),
        prepared=prepared,
        verified_at=datetime.now(UTC),
        digest="",
    )
    return value.model_copy(
        update={"digest": digest(value.model_dump(mode="json", exclude={"digest"}))}
    )


def apply(db, verified):
    from app.services.native_custody_reconciliation import (
        _invalidate_retirement_observations,
    )

    value = VerifiedSnapshotReconciliation.model_validate(verified)
    custody.require(
        value.digest == digest(value.model_dump(mode="json", exclude={"digest"})),
        "Snapshot readback changed",
    )
    request = value.prepared
    domain = custody._publication_locks(db, request)
    _, current = custody._publication_request(db, request, domain)
    custody.require(
        current is not None
        and 0 <= (datetime.now(UTC) - utc(value.verified_at)).total_seconds() <= 60,
        "Snapshot readback stale",
    )
    if TYPE_CHECKING:
        assert (
            current is not None
            and 0 <= (datetime.now(UTC) - utc(value.verified_at)).total_seconds() <= 60
        )
    receipt = current[1].model_copy(
        update={
            **{key: getattr(value, key) for key in SnapshotFacts.model_fields},
            "publication": request,
            "complete": False,
        }
    )
    row = NativeOriginCustodyReceipt(
        id=str(uuid4()),
        intent_id=str(request.intent.id),
        project_id=str(request.intent.project_id),
        domain_id=domain.id,
        request_id=str(request.request_id),
        digest=digest(receipt),
        payload=receipt.model_dump(mode="json"),
    )
    db.add(row)
    db.get(NativeOriginCustodyCurrent, str(request.intent.id)).receipt_id = row.id
    _invalidate_retirement_observations(db, domain.id)
    db.flush()
    return row
