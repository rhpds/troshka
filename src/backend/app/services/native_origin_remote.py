"""Native remote dispatch identities; transport success is never settlement."""

import json
from typing import TYPE_CHECKING, Any, cast

from app.services import native_deployment_origin as origin
from app.services import native_origin_ops as ops
from app.services import native_origin_ops_state as state
from app.services import native_origin_support as support
from app.services.kubevirt_storage_enrollment import digest

FILE_PULL = "troshka.redhat.com/file-pull-request"
CAPTURE = "troshka.redhat.com/capture-request"


def exec_result(core, namespace, name, argv, timeout):
    """Observe the exec channel's terminal status; partial output is not a result."""
    from kubernetes.stream import stream

    response = stream(
        core.connect_get_namespaced_pod_exec,
        name,
        namespace,
        container="ops",
        command=argv,
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=False,
        _request_timeout=timeout,
    )
    try:
        response.run_forever(timeout=timeout)
        origin.require(
            not response.is_open() and isinstance(response.returncode, int),
            "Native support exec terminal result unavailable",
        )
        return response.read_all() or ""
    finally:
        response.close()


def exec_command(
    project_id, api, argv, invoke, *, target_namespace=None, target_name=None
):
    name = f"troshka-{project_id[:8]}-ops"
    intent, namespace, pod_operation = ops._load(project_id, "Pod", name)
    origin.require(
        target_namespace in {None, intent.namespace} and target_name in {None, name},
        "Native exec caller target changed",
    )
    origin.require(pod_operation is not None, "Native exec original Pod unavailable")
    if TYPE_CHECKING:
        assert pod_operation is not None
    pod = ops._observed(api, intent, namespace, pod_operation)
    origin.require(
        pod is not None and not pod["metadata"].get("deletionTimestamp"),
        "Native exec Pod terminating",
    )
    if TYPE_CHECKING:
        assert pod is not None
    request = dict(
        pod_operation=pod_operation.id,
        pod_digest=pod_operation.digest,
        pod_uid=pod["metadata"]["uid"],
        command_digest=digest(argv),
    )
    with ops._session() as db:
        state.current(db, project_id, creating=True)
        operation = state.begin(db, intent, "exec", request)
    ops._dispatch(operation)
    try:
        result = invoke(argv)
        ops._record(
            operation,
            "returned",
            {"pod_uid": pod["metadata"]["uid"], "command_digest": digest(argv)},
        )
        return result
    finally:
        # Only this local call has unwound. The remote command can outlive it;
        # retain its liability against the whole original Pod lifetime.
        ops._record(operation, "local_exit", {"dispatch_digest": digest(request)})


def request_file(provider, project_id, payload):
    api = ops.client(provider)
    with ops._session() as db:
        intent, namespace = cast(
            state.CurrentSupport, state.current(db, project_id, creating=True)
        )
        origin.require(
            str(provider.id) == str(intent.provider_id),
            "Native file-pull provider changed",
        )
        _, receipt = origin.receipts(db, str(intent.id))
        origin.require(receipt is not None, "Native file-pull root receipt unavailable")
    support.bracket(api, intent, namespace)
    root = api.root(intent.namespace, intent.root_name)
    origin.require(
        root and root["metadata"].get("uid") == receipt.root_uid,
        "Native file-pull original root changed",
    )
    meta = cast(dict[str, Any], root)["metadata"]
    annotations = dict(meta.get("annotations", {}))
    origin.require(
        not annotations.get(FILE_PULL) and not meta.get("deletionTimestamp"),
        "Native file-pull outstanding request or deleting root",
    )
    annotations[FILE_PULL] = json.dumps(payload)
    request = dict(
        request_id=payload["requestId"],
        root_uid=receipt.root_uid,
        root_name=intent.root_name,
        root_rv=meta["resourceVersion"],
        request_digest=digest(payload),
        operation_digest=digest(
            {
                "capture-request": annotations.get(CAPTURE),
                "file-pull-request": annotations[FILE_PULL],
            }
        ),
    )
    with ops._session() as db:
        state.current(db, project_id, creating=True)
        origin.require(
            not any(
                r.payload["kind"] == "filepull"
                and r.payload["request"]["request_id"] == payload["requestId"]
                for r in state.operations(db, str(intent.id))
            ),
            "Native file-pull request replay denied",
        )
        operation = state.begin(db, intent, "filepull", request)
    support.bracket(api, intent, namespace)
    ops._dispatch(operation)
    try:
        api.request_file(
            intent.namespace,
            intent.root_name,
            receipt.root_uid,
            meta["resourceVersion"],
            annotations,
        )
        ops._record(
            operation,
            "submitted",
            {"root_uid": receipt.root_uid, "request_id": payload["requestId"]},
        )
    finally:
        ops._record(operation, "local_exit", {"dispatch_digest": digest(request)})
    return operation


def file_result(operation, root):
    """Retain a successful result from the original root; storage still unsettled."""
    request = operation.payload["request"]
    result = root.get("status", {}).get("filePull", {})
    origin.require(
        root.get("metadata", {}).get("uid") == request["root_uid"]
        and result.get("requestId") == request["request_id"]
        and result.get("ready")
        and not result.get("error"),
        "Native file-pull result identity changed",
    )
    ops._record(
        operation,
        "returned",
        {"root_uid": request["root_uid"], "request_id": request["request_id"]},
    )
