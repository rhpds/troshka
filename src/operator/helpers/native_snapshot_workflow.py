"""Origin operation retry position; storage authority remains signed custody."""

import json
from copy import deepcopy

from helpers import managed_power as power
from helpers import native_origin_checkpoint as checkpoint


def _context():
    root, namespace, api = power._context.get()
    return power.root_for(root, namespace, api), namespace, api


def capture_resumable(root):
    """Dispatch only a retained checkpoint/cleanup for this origin operation.

    This is eligibility, never POST authority: the producer still verifies the
    signed ACK, submission history and original UID on every continuation.
    """
    origin = root.get("spec", {}).get("originCheckpoint")
    annotations = root.get("metadata", {}).get("annotations", {}) or {}
    if not origin or not annotations.get("troshka.redhat.com/capture-request"):
        return False
    operation = checkpoint.operation(root)
    cleanup = root.get("status", {}).get("originSnapshotCleanup", {}).get("capture", {})
    if cleanup.get("operation") == operation:
        return True
    try:
        request = json.loads(annotations.get(checkpoint.REQUEST, "{}"))
    except (ValueError, TypeError):
        return False
    return (isinstance(request, dict)
            and request.get("intentId") == origin.get("intentId")
            and request.get("operationDigest") == operation
            and request.get("root", {}).get("uid") == root["metadata"].get("uid"))


def pending(stage):
    if not checkpoint.participating():
        return None
    root, _, _ = _context()
    saved = root.get("status", {}).get("originSnapshotCleanup", {}).get(stage)
    if saved is None or saved.get("operation") != checkpoint.operation(root):
        return None
    return saved["resources"]


def outcome(stage):
    root, _, _ = _context()
    saved = root.get("status", {}).get("originSnapshotCleanup", {}).get(stage, {})
    checkpoint.require(saved.get("operation") == checkpoint.operation(root), "origin_snapshot_cleanup_operation_changed")
    return saved.get("outcome")


def begin(stage, resources, *, outcome="complete"):
    if not checkpoint.participating():
        return
    root, _, api = _context()
    checkpoint.require(outcome in {"complete", "failed"}, "origin_snapshot_cleanup_outcome_invalid")
    status = deepcopy(root.get("status", {}))
    entries = status.setdefault("originSnapshotCleanup", {})
    # Only known non-secret selectors; never persist a Job spec or launch URLs.
    fields = {"jobName", "podName", "tempPvcName", "scratchPvcName", "snapName"}
    entries[stage] = dict(operation=checkpoint.operation(root), outcome=outcome, resources=[
        {k: v for k, v in row.items() if k in fields} for row in resources])
    checkpoint.require(len(resources) <= 100 and len(entries) <= 3, "origin_snapshot_cleanup_limit")
    api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)


def recorded_resources():
    """Selectors for original observed producers, including partial pipelines.

    These selectors only schedule cleanup; every DELETE still obtains its own
    signed custody checkpoint. Never select an unobserved name-only allocation.
    """
    root, _, _ = _context()
    status = root.get("status", {})
    originals = status.get("originProducerObjects", {})
    fields = {"snapshot": "snapName", "restore": "tempPvcName", "scratch": "scratchPvcName",
              "export": "jobName", "filepull": "podName"}
    selected = {}
    for row in status.get("originCheckpointReceipts", {}).values():
        intent = row.get("request", {}).get("producerIntent", {})
        if (intent.get("operation_digest") != checkpoint.operation(root)
                or not originals.get(intent.get("id"), {}).get("uid")):
            continue
        field = fields.get(intent.get("role"))
        if field:
            selected[intent["id"]] = {field: intent["name"]}
    return list(selected.values())


def _remove(api, namespace, kind, name):
    if name:
        checkpoint.remove(kind, namespace, name, "delete-" + kind)


def cleanup(resources):
    _, namespace, api = _context()
    for row in resources:
        _remove(api, namespace, "Job", row.get("jobName"))
        _remove(api, namespace, "Pod", row.get("podName"))
    claims = {r.get(k) for r in resources for k in ("tempPvcName", "scratchPvcName")} - {None}
    consumers = [p for p in api.rows("Pod", namespace) if any(
        v.get("persistentVolumeClaim", {}).get("claimName") in claims for v in p.get("spec", {}).get("volumes", []))]
    checkpoint.require(not consumers, "origin_snapshot_helper_writer_remains")
    for row in resources:
        _remove(api, namespace, "PersistentVolumeClaim", row.get("tempPvcName"))
        _remove(api, namespace, "PersistentVolumeClaim", row.get("scratchPvcName"))
    for row in resources:
        _remove(api, namespace, "VolumeSnapshot", row.get("snapName"))


def resume(stage):
    resources = pending(stage)
    if resources is None:
        return False
    cleanup(resources)
    return True
