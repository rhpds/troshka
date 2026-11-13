"""Narrow bare Project-owned Pod graceful DELETE maintenance profile."""

from app.services.native_controller_halt import _claims, _retain_claim, _runtime_storage
from app.services.native_vm_halt import _object, _owned_by, _root, require


def preflight_pod(reader, namespace, target, project):
    pod = _object(reader, "Pod", namespace, target.name, target.uid)
    require(pod.get("apiVersion") == "v1")
    require(
        pod["metadata"]["resourceVersion"] == target.resource_version,
        "resource_version_changed",
    )
    # This direct Project owner excludes controller and VM launcher Pods.
    _owned_by(pod, project)
    claims = _claims(pod.get("spec", {}))
    _runtime_storage(reader, namespace, {target.uid}, project)
    for name in sorted(claims):
        _retain_claim(reader, namespace, name, [project], project)
    return pod, {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {
            "uid": target.uid,
            "resourceVersion": target.resource_version,
        },
    }


def _verify_response(returned, namespace, target):
    require(returned.get("apiVersion") == "v1")
    if returned.get("kind") == "Pod":
        meta = returned["metadata"]
        require(
            meta.get("name") == target.name
            and meta.get("namespace") == namespace
            and meta.get("uid") == target.uid
            and bool(meta.get("resourceVersion"))
        )
        return
    require(returned.get("kind") == "Status")
    require(returned.get("status") == "Success" and returned.get("code") == 200)
    details = returned["details"]
    require(
        details.get("name") == target.name
        and details.get("uid") == target.uid
        and details.get("kind") == "pods"
        and details.get("group", "") == ""
    )


def _followup(reader, manifest, target):
    try:
        _root(reader, manifest.root)
        value = reader.read_pod_after_delete(manifest.root.namespace, target.name)
        # Namespace absence is never evidence of the target Pod's absence.
        _root(reader, manifest.root)
    except Exception:
        return "delete_request_accepted"
    if value is None:
        return "absence_observed"
    try:
        _verify_response(value, manifest.root.namespace, target)
        require(value.get("kind") == "Pod")
    except Exception:
        return "ambiguous"
    return "delete_request_accepted"


def request_pod(reader, manifest, target, options, *, apply):
    try:
        returned = reader.delete_pod(
            manifest.root.namespace, target.name, options, dry_run=not apply
        )
        _verify_response(returned, manifest.root.namespace, target)
    except Exception:
        return {"target": target.model_dump(), "status": "ambiguous"}
    status = _followup(reader, manifest, target) if apply else "dry_run_accepted"
    return {"target": target.model_dump(), "status": status}
