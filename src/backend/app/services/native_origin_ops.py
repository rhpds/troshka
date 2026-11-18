"""Exact native support dispatch and active-project replacement evidence.

Whole-project teardown belongs to normal namespace deletion. Resource absence
here permits active healing; it never resolves historical remote/storage debt.
"""

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from app.services import native_deployment_origin as origin
from app.services import native_origin_ops_state as state
from app.services import native_origin_support as support
from app.services.kubevirt_storage_enrollment import digest

OPERATION = "troshka.redhat.com/ops-support-operation"
BODY = "troshka.redhat.com/ops-support-body"


def _session():
    from app.core.database import SessionLocal

    return SessionLocal()


def participates(project_id):
    with _session() as db:
        return origin.active(db, project_id) is not None


def client(provider):
    from app.services.native_origin_ops_client import OpsClient
    from app.services.providers.kubevirt import _get_k8s_clients

    return OpsClient(_get_k8s_clients(provider)[2])


def for_core(core):
    from app.services.native_origin_ops_client import OpsClient

    return OpsClient(core.api_client)


def create_pair(provider, project_id, pod, secret):
    with _session() as db:
        intent, namespace = cast(
            state.CurrentSupport, state.current(db, project_id, creating=True)
        )
        origin.require(
            str(provider.id) == str(intent.provider_id),
            "Native support provider changed",
        )
        for body in (secret, pod):
            _validate_body(intent, body)
            origin.require(
                state.latest(db, str(intent.id), body["kind"], body["metadata"]["name"])
                is None,
                "Native initial support request already retained; replay denied",
            )
    api = client(provider)
    support.bracket(api, intent, namespace)
    for body in (secret, pod):
        origin.require(
            api.get(body["kind"], intent.namespace, body["metadata"]["name"]) is None,
            "Native initial support target already exists",
        )
    create(project_id, api, secret)
    create(project_id, api, pod)
    return intent.namespace


def _content(body):
    if body["kind"] == "Secret":
        return support._content(body)
    spec = deepcopy(body["spec"])
    spec.pop("nodeName", None)  # Scheduler assignment is not a body replacement.
    return spec


def _validate_body(intent, body):
    meta = body["metadata"]
    prefix = f"troshka-{str(intent.project_id)[:8]}-ops"
    origin.require(
        body["kind"] in {"Secret", "Pod"}
        and body.get("apiVersion") == "v1"
        and meta.get("namespace") == intent.namespace
        and meta.get("name") in {prefix, prefix + "-config"}
        and not meta.get("ownerReferences"),
        "Native support body scope unavailable",
    )
    if body["kind"] == "Pod":
        spec = body["spec"]
        origin.require(
            spec.get("restartPolicy") in {"Never", "Always", "OnFailure"}
            and [c["name"] for c in spec.get("containers", [])] == ["ops"]
            and not spec.get("initContainers")
            and not spec.get("ephemeralContainers")
            and not spec.get("nodeName")
            and not spec.get("hostPID"),
            "Native support Pod execution policy unsupported",
        )
        origin.require(
            not any(
                "persistentVolumeClaim" in v or "hostPath" in v
                for v in spec.get("volumes", [])
            ),
            "Native support Pod storage scope unsupported",
        )


def _identity(operation, value):
    origin.require(value is not None, "Native support original object unavailable")
    request, meta = operation.payload["request"], value.get("metadata", {})
    origin.require(
        value.get("kind") == request["kind"]
        and meta.get("name") == request["name"]
        and meta.get("namespace") == operation.payload["namespace"]
        and meta.get("uid")
        and meta.get("resourceVersion")
        and not meta.get("ownerReferences"),
        "Native support original identity changed",
    )
    annotations = meta.get("annotations", {})
    origin.require(
        annotations.get(OPERATION) == operation.id
        and annotations.get(BODY) == request["body_digest"],
        "Native support original provenance changed",
    )
    return dict(
        kind=request["kind"],
        name=request["name"],
        uid=meta["uid"],
        content_digest=digest(_content(value)),
    )


def _pin(project_id, body, replacement=None):
    with _session() as db:
        intent, namespace = cast(
            state.CurrentSupport, state.current(db, project_id, creating=True)
        )
        _validate_body(intent, body)
        predecessor = state.latest(
            db, str(intent.id), body["kind"], body["metadata"]["name"]
        )
        origin.require(
            predecessor is None or state.fact(db, predecessor, "absent") is not None,
            "Native support existing or unknown predecessor requires exact removal",
        )
        if replacement is not None:
            origin.require(
                predecessor is not None
                and predecessor.id == replacement.payload["request"]["predecessor"]
                and digest(body) == replacement.payload["request"]["body_digest"],
                "Native support replacement predecessor or body changed",
            )
        request = dict(
            kind=body["kind"],
            name=body["metadata"]["name"],
            body_digest=digest(body),
            predecessor=predecessor.id if predecessor else None,
            replacement=replacement.id if replacement else None,
        )
        operation = state.begin(db, intent, "create", request)
        return intent, namespace, operation


def _record(operation, stage, value):
    with _session() as db:
        state.retain(db, operation, stage, value)
        db.commit()


def _fact(operation, stage):
    with _session() as db:
        return state.fact(db, operation, stage)


def _dispatch(operation):
    with _session() as db:
        state.dispatch(db, operation)


def _body(body):
    body = deepcopy(body)
    for key in (OPERATION, BODY):
        body["metadata"].get("annotations", {}).pop(key, None)
    return body


def create(project_id, api, body, *, replacement=None):
    """One POST only. Ambiguous first writes can never be adopted by GET."""
    body = _body(body)
    intent, namespace, operation = _pin(project_id, body, replacement)
    meta = body["metadata"]
    meta.setdefault("annotations", {}).update(
        {OPERATION: operation.id, BODY: operation.payload["request"]["body_digest"]}
    )
    support.bracket(api, intent, namespace)
    _dispatch(operation)
    try:
        created = api.post(body["kind"], body)
        returned = _identity(operation, created)
        # The original returned UID is durable even if GET fails or cancellation won.
        _record(operation, "returned", returned)
    finally:
        _record(
            operation,
            "local_exit",
            {"dispatch_digest": digest(operation.payload["request"])},
        )
    _admitted(body, created)
    _record(operation, "admitted", returned)
    observed = api.get(body["kind"], intent.namespace, meta["name"])
    origin.require(
        _identity(operation, observed) == returned,
        "Native support POST/readback changed",
    )
    support.bracket(api, intent, namespace)
    _record(operation, "verified", returned)
    return operation


def _contains(expected, observed):
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            k in observed and _contains(v, observed[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(observed, list)
            and len(expected) == len(observed)
            and all(_contains(a, b) for a, b in zip(expected, observed, strict=True))
        )
    return expected == observed


def _admitted(body, observed):
    if body["kind"] == "Secret":
        origin.require(
            _content(body) == _content(observed),
            "Native support admitted Secret changed",
        )
        return
    spec = _admitted_spec(body["spec"], observed.get("spec", {}))
    origin.require(
        _contains(body["spec"], spec)
        and not spec.get("initContainers")
        and not spec.get("ephemeralContainers")
        and not spec.get("hostPID"),
        "Native support admitted Pod changed",
    )


def _service_account_volume(volume):
    """Recognize only Kubernetes' standard bound service-account projection."""
    value = deepcopy(volume)
    name = value.pop("name", "")
    if not isinstance(name, str) or not name.startswith("kube-api-access-"):
        return False
    projected = value.get("projected", {})
    if projected.get("defaultMode", 420) != 420:
        return False
    projected.pop("defaultMode", None)
    sources = projected.get("sources", [])
    if len(sources) != 3:
        return False
    token = sources[0].get("serviceAccountToken", {})
    expiration = token.pop("expirationSeconds", 3607)
    return (
        type(expiration) is int
        and expiration > 0
        and value
        == {
            "projected": {
                "sources": [
                    {"serviceAccountToken": {"path": "token"}},
                    {
                        "configMap": {
                            "name": "kube-root-ca.crt",
                            "items": [{"key": "ca.crt", "path": "ca.crt"}],
                        }
                    },
                    {
                        "downwardAPI": {
                            "items": [
                                {
                                    "path": "namespace",
                                    "fieldRef": {
                                        "apiVersion": "v1",
                                        "fieldPath": "metadata.namespace",
                                    },
                                }
                            ]
                        }
                    },
                ]
            }
        }
    )


def _admitted_spec(expected, observed):
    spec = deepcopy(observed)
    containers = spec.get("containers", [])
    origin.require(len(containers) == 1, "Native support admitted containers changed")
    volumes = spec.get("volumes", [])
    mounts = containers[0].get("volumeMounts", [])
    if len(volumes) == len(expected.get("volumes", [])) + 1:
        volume = volumes[-1]
        mount = dict(
            name=volume.get("name"),
            mountPath="/var/run/secrets/kubernetes.io/serviceaccount",
            readOnly=True,
        )
        origin.require(
            expected.get("automountServiceAccountToken") is not False
            and _service_account_volume(volume)
            and mounts
            and mounts[-1] == mount,
            "Native support admitted service-account projection changed",
        )
        volumes.pop()
        mounts.pop()
    container = expected["containers"][0]
    origin.require(
        _contains(expected.get("volumes", []), volumes)
        and _contains(container.get("volumeMounts", []), mounts)
        and container.get("command", []) == containers[0].get("command", [])
        and container.get("args", []) == containers[0].get("args", []),
        "Native support admitted volumes, mounts or command changed",
    )
    return spec


def _load(project_id, kind, name, operation_id=None):
    with _session() as db:
        intent, namespace = cast(state.CurrentSupport, state.current(db, project_id))
        operation = (
            db.get(state.Operation, operation_id)
            if operation_id
            else state.latest(db, str(intent.id), kind, name)
        )
        if operation is not None:
            origin.require(
                operation.intent_id == str(intent.id)
                and operation.payload["kind"] == "create"
                and operation.payload["request"]["kind"] == kind
                and operation.payload["request"]["name"] == name,
                "Native support target changed",
            )
            db.expunge(operation)
        return intent, namespace, operation


def _observed(api, intent, namespace, operation):
    support.bracket(api, intent, namespace)
    request = operation.payload["request"]
    value = api.get(request["kind"], intent.namespace, request["name"])
    expected = _fact(operation, "returned")
    origin.require(expected is not None, "Native support original POST UID unresolved")
    origin.require(
        _fact(operation, "admitted") == expected,
        "Native support admitted body unresolved",
    )
    if value is not None:
        origin.require(
            _identity(operation, value) == expected,
            "Native support original UID or content changed",
        )
    support.bracket(api, intent, namespace)
    return value


def _delete(api, intent, operation, value):
    meta, request = value["metadata"], operation.payload["request"]
    action = dict(uid=meta["uid"], resource_version=meta["resourceVersion"])
    _record(operation, "delete-" + digest(action), action)
    # Removal preserves the original attempt but is permitted after cancellation.
    with _session() as db:
        origin._locked(db, operation.intent_id, allow_cancelled=True)
        origin.require(
            cast(dict[str, Any], state.fact(db, operation, "returned"))["uid"]
            == meta["uid"],
            "Native support removal changed",
        )
        db.commit()
    api.delete(
        request["kind"],
        intent.namespace,
        request["name"],
        meta["uid"],
        meta["resourceVersion"],
    )


def remove(project_id, api, kind, name, *, operation_id=None):
    """Resume exact graceful removal; unknown/replaced identities remain held."""
    intent, namespace, operation = _load(project_id, kind, name, operation_id)
    if operation is None:
        support.bracket(api, intent, namespace)
        origin.require(
            api.get(kind, intent.namespace, name) is None,
            "Native support unowned object cannot be removed",
        )
        return
    if kind == "Pod":
        with _session() as db:
            state.close_exec(db, operation)
    value = _observed(api, intent, namespace, operation)
    if value is not None:
        if not value["metadata"].get("deletionTimestamp"):
            _delete(api, intent, operation, value)
            value = _observed(api, intent, namespace, operation)
    origin.require(value is None, "Native support original removal pending")
    _record(
        operation,
        "absent",
        {"uid": cast(dict[str, Any], _fact(operation, "returned"))["uid"]},
    )


def liabilities(db, intent_id):
    """Exact retained support identities for 4c3; this is not cleanup authority."""
    return [
        dict(
            operation_id=r.id,
            operation_digest=r.digest,
            kind=r.payload["kind"],
            request=r.payload["request"],
            returned=state.fact(db, r, "returned"),
            verified=state.fact(db, r, "verified"),
            absent=state.fact(db, r, "absent"),
            local_exit=state.fact(db, r, "local_exit"),
        )
        for r in state.operations(db, intent_id)
    ]


def replacement(project_id, api, body):
    """Persist the encrypted desired successor before touching its predecessor."""
    body = _body(body)
    with _session() as db:
        intent, _ = cast(
            state.CurrentSupport, state.current(db, project_id, creating=True)
        )
        _validate_body(intent, body)
        old = state.latest(db, str(intent.id), "Pod", body["metadata"]["name"])
        origin.require(
            old is not None and state.fact(db, old, "verified"),
            "Native support predecessor unavailable",
        )
        if TYPE_CHECKING:
            assert old is not None and state.fact(db, old, "verified")
        pending = _pending(db, str(intent.id))
        origin.require(pending is None, "Native support replacement already pending")
        operation = state.begin(
            db,
            intent,
            "replace",
            dict(
                kind="Pod",
                name=body["metadata"]["name"],
                body_digest=digest(body),
                predecessor=old.id,
            ),
            body=body,
        )
    return _resume_replacement(project_id, api, operation)


def _pending(db, intent_id):
    rows = [
        r
        for r in state.operations(db, intent_id)
        if r.payload["kind"] == "replace"
        and state.fact(db, r, "successor") is None
        and state.fact(db, r, "cancelled") is None
    ]
    origin.require(len(rows) <= 1, "Native support replacement plans ambiguous")
    return rows[0] if rows else None


def resume_replacement(project_id, api):
    with _session() as db:
        intent, _ = cast(state.CurrentSupport, state.current(db, project_id))
        operation = _pending(db, str(intent.id))
        if operation is None:
            return None
        db.expunge(operation)
    return _resume_replacement(project_id, api, operation)


def _resume_replacement(project_id, api, operation):
    request = operation.payload["request"]
    with _session() as db:
        _, pointer = origin._locked(db, operation.intent_id, allow_cancelled=True)
        successor = state.successor(db, operation)
        if successor is not None:
            db.expunge(successor)
        elif pointer.cancelled:
            state.retain(
                db, operation, "cancelled", {"predecessor": request["predecessor"]}
            )
            db.commit()
            return {"action": "cancelled"}
    if successor is None:
        remove(
            project_id, api, "Pod", request["name"], operation_id=request["predecessor"]
        )
        successor = create(
            project_id, api, state.body(operation), replacement=operation
        )
    _verify_successor(project_id, api, operation, successor)
    _record(operation, "successor", {"id": successor.id, "digest": successor.digest})
    return {"action": "reschedule", "detail": "Exact original ops Pod replaced"}


def _verify_successor(project_id, api, replacement, successor):
    origin.require(
        successor.payload["request"]["predecessor"]
        == replacement.payload["request"]["predecessor"]
        and successor.payload["request"]["body_digest"]
        == replacement.payload["request"]["body_digest"],
        "Native support successor identity changed",
    )
    with _session() as db:
        intent, namespace = cast(state.CurrentSupport, state.current(db, project_id))
    observed = _observed(api, intent, namespace, successor)
    origin.require(observed is not None, "Native support successor unavailable")
    _record(successor, "verified", _fact(successor, "returned"))
