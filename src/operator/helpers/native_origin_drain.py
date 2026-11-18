"""Origin producer cancellation fence for the qualified single operator profile.

Registrations survive process restarts. Completed handler executions are terminal;
an interrupted incarnation requires independently retained termination evidence.
Neither these records nor the drain acknowledgement are a managed off proof.
"""

import asyncio
import json
import os
from contextvars import ContextVar
from copy import deepcopy
from types import MethodType
from uuid import uuid4

from helpers import managed_power as power

ANNOTATION = "troshka.redhat.com/origin-drain"
INCARNATION = str(uuid4())
_children = ContextVar("origin_execution_children", default=None)
_unfinished_children = {}


def require(condition, reason):
    from helpers.native_origin_checkpoint import CheckpointHold

    if not condition:
        raise CheckpointHold(reason)


def assert_open(api, root, namespace):
    from helpers.native_namespace_cleanup import assert_project_open

    assert_project_open(api, namespace)
    current = power.root_for(root, namespace, api)
    annotations = current["metadata"].get("annotations", {})
    require(not any(k in annotations for k in (ANNOTATION, power.ANNOTATION, power.CLEANUP_ANNOTATION)),
            "origin_producer_stopped_or_draining")
    return current


class FenceClient:
    """Fence ancillary native producer writes without granting storage authority."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        original = getattr(self.client, name)
        if not name.startswith(("create_", "replace_", "patch_", "delete_", "connect_")):
            return original

        def call(bound_client, *args, **kwargs):
            context = power._context.get()
            if context is not None:
                from helpers.native_namespace_cleanup import assert_project_open

                assert_project_open(context[2], context[1])
            if context is not None and "originCheckpoint" in context[0].get("spec", {}):
                assert_open(context[2], context[0], context[1])
            return original(*args, **kwargs)

        # kubernetes.stream requires api_method.__self__.api_client and swaps
        # that exact client's request transport before invoking the method.
        return MethodType(call, self.client)


def operator_identity(api):
    namespace, name, uid = (os.environ.get(k) for k in ("POD_NAMESPACE", "POD_NAME", "POD_UID"))
    require(all((namespace, name, uid)), "origin_operator_identity_unavailable")
    pod = api.get("Pod", namespace, name)
    require(pod["metadata"]["uid"] == uid and not pod["metadata"].get("deletionTimestamp"),
            "origin_operator_incarnation_changed")
    containers = [c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == "operator"]
    require(len(containers) == 1 and containers[0].get("containerID") and containers[0].get("state", {}).get("running"),
            "origin_operator_container_unavailable")
    return {"namespace": namespace, "name": name, "uid": uid, "containerId": containers[0]["containerID"], "incarnation": INCARNATION}


def producer_digest(root):
    status = root.get("status", {})
    return power.digest({"objects": status.get("originProducerObjects", {}), "epochs": status.get("originCreateEpochs", {})})


def register(api, root, namespace):
    if "originCheckpoint" not in root.get("spec", {}):
        return None
    identity = operator_identity(api)
    current = assert_open(api, root, namespace)
    status = deepcopy(current.get("status", {}))
    executions = status.setdefault("originExecutions", {})
    execution_id = str(uuid4())
    previous = executions.get(identity["incarnation"])
    require(previous is None or previous.get("state") == "complete", "origin_prior_execution_unresolved")
    require(len(executions) < 1000 or previous is not None, "origin_execution_history_limit")
    executions[identity["incarnation"]] = {"operator": identity, "executionId": execution_id, "state": "active"}
    api.patch(current, [{"op": "add", "path": "/status", "value": status}], status=True)
    return execution_id, identity


def complete(api, root, namespace, execution, *, outcome="returned"):
    if execution is None:
        return
    execution_id, identity = execution
    current = power.root_for(root, namespace, api)
    status = deepcopy(current.get("status", {}))
    key = identity["incarnation"]
    saved = status.get("originExecutions", {}).get(key)
    require(saved == {"operator": identity, "executionId": execution_id, "state": "active"} and operator_identity(api) == identity,
            "origin_execution_identity_changed")
    status["originExecutions"][key] = {"operator": identity, "executionId": execution_id, "state": "complete", "outcome": outcome}
    api.patch(current, [{"op": "add", "path": "/status", "value": status}], status=True)


async def tracked_await(awaitable):
    """Keep the real task/thread future alive when its handler is cancelled."""
    children = _children.get()
    if children is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    children.append(task)
    return await asyncio.shield(task)


async def execute(api, root, namespace, function, args, kwargs):
    require(not any(not child.done() for child in _unfinished_children.get(root["metadata"]["uid"], [])),
            "origin_prior_local_child_unresolved")
    execution = register(api, root, namespace)
    children = []
    token = _children.set(children)
    outcome = "failed"
    try:
        result = await function(*args, **kwargs)
        outcome = "returned"
        return result
    finally:
        _children.reset(token)
        # shield prevents a cancelled handler from cancelling a to_thread future;
        # the timeout leaves the durable execution active instead of inventing exit.
        pending = [child for child in children if not child.done()]
        _unfinished_children[root["metadata"]["uid"]] = pending
        if pending:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending, return_exceptions=True)), timeout=55)
        _unfinished_children.pop(root["metadata"]["uid"], None)
        complete(api, root, namespace, execution, outcome=outcome)


def reconcile(api, root, namespace):
    """Called only inside the per-root producer lock, after earlier handlers exit."""
    raw = root["metadata"].get("annotations", {}).get(ANNOTATION)
    if raw is None:
        return False
    try:
        request = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise power.ManagedPowerError("origin_drain_invalid") from error
    identity = operator_identity(api)
    _validate(api, root, namespace, request, identity)
    status = deepcopy(root.get("status", {}))
    ack = {"drainId": request["drainId"], "epoch": request["epoch"], "intentId": request["intentId"],
               "rootUid": request["rootUid"], "namespaceUid": request["namespaceUid"], "operator": identity,
               "requestDigest": power.digest(request), "executionsDigest": power.digest(status.get("originExecutions", {})),
               "producerDigest": producer_digest(root)}
    if status.get("originDrain") != ack:
        status["originDrain"] = ack
        api.patch(root, [{"op": "add", "path": "/status", "value": status}], status=True)
    return True


def _validate(api, root, namespace, request, identity):
    from helpers.native_origin_operator import qualify

    require(request.get("version") == 1 and request.get("drainId") and request.get("epoch"), "origin_drain_invalid")
    require(request.get("intentId") == root.get("spec", {}).get("originCheckpoint", {}).get("intentId")
            and request.get("rootUid") == root["metadata"]["uid"]
            and request.get("namespaceUid") == api.get("Namespace", None, namespace)["metadata"]["uid"],
            "origin_drain_identity_changed")
    require(request.get("operator") == {k: v for k, v in identity.items() if k != "incarnation"},
            "origin_drain_operator_changed")
    qualify(api, request.get("qualification"), identity)
    executions = root.get("status", {}).get("originExecutions", {})
    require(request.get("executionsDigest") == power.digest(executions), "origin_drain_execution_set_changed")
    for execution in executions.values():
        # Active old incarnations stay held until the backend's independently
        # verified termination evidence transport is implemented and reviewed.
        require(execution.get("state") == "complete",
                "origin_drain_prior_execution_unresolved")
