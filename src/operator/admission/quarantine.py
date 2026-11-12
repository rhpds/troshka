"""Exact cleanup whitelist. No caller identity confers authority here.

Namespace lifetime authenticity is supplied by verified immutable enrollment and
the API-local namespace tombstone, not mutable labels or AdmissionReview fields.
"""

import json
from copy import deepcopy

from .models import ValidationError, dns_name, full_uid, require, validate_tree

_KINDS = {
    ("", "v1", "pods"): "Pod",
    ("", "v1", "persistentvolumeclaims"): "PersistentVolumeClaim",
    ("", "v1", "secrets"): "Secret",
    ("", "v1", "configmaps"): "ConfigMap",
    ("", "v1", "services"): "Service",
    ("apps", "v1", "deployments"): "Deployment",
    ("apps", "v1", "statefulsets"): "StatefulSet",
    ("apps", "v1", "replicasets"): "ReplicaSet",
    ("batch", "v1", "jobs"): "Job",
    ("kubevirt.io", "v1", "virtualmachines"): "VirtualMachine",
    ("kubevirt.io", "v1", "virtualmachineinstances"): "VirtualMachineInstance",
}
_SCALABLE = frozenset(
    {("apps", "v1", name) for name in ("deployments", "statefulsets", "replicasets")}
)
_STATUS = _SCALABLE | {("", "v1", "pods"), ("batch", "v1", "jobs")}
_REQUIRED_REQUEST = {
    "uid",
    "kind",
    "resource",
    "name",
    "namespace",
    "operation",
    "userInfo",
}
_OPTIONAL_REQUEST = {
    "requestKind",
    "requestResource",
    "subResource",
    "requestSubResource",
    "object",
    "oldObject",
    "dryRun",
    "options",
}


def _request_shape(request):
    require(
        _REQUIRED_REQUEST <= set(request) <= _REQUIRED_REQUEST | _OPTIONAL_REQUEST,
        "invalid_request",
    )
    require(
        "dryRun" not in request or type(request["dryRun"]) is bool, "invalid_request"
    )
    require(
        request.get("options") is None or type(request["options"]) is dict,
        "invalid_request",
    )
    user = request["userInfo"]
    require(
        type(user) is dict and set(user) <= {"username", "uid", "groups", "extra"},
        "invalid_request",
    )
    require(
        type(user.get("username")) is str and bool(user["username"]), "invalid_request"
    )
    require("uid" not in user or type(user["uid"]) is str, "invalid_request")
    groups = user.get("groups", [])
    require(
        type(groups) is list and all(type(group) is str for group in groups),
        "invalid_request",
    )
    extra = user.get("extra", {})
    require(type(extra) is dict, "invalid_request")
    require(
        all(
            type(values) is list and all(type(value) is str for value in values)
            for values in extra.values()
        ),
        "invalid_request",
    )


def _resource(value):
    require(
        type(value) is dict and set(value) == {"group", "version", "resource"},
        "invalid_resource",
    )
    require(all(type(part) is str for part in value.values()), "invalid_resource")
    return value["group"], value["version"], value["resource"]


def _expected_kind(resource, subresource):
    require(resource in _KINDS, "unsupported_resource")
    if subresource == "scale":
        require(resource in _SCALABLE, "unsupported_subresource")
        return {"group": "autoscaling", "version": "v1", "kind": "Scale"}
    require(subresource in ("", "status"), "unsupported_subresource")
    return {"group": resource[0], "version": resource[1], "kind": _KINDS[resource]}


def _validate_request(document, enrollments):
    validate_tree(document)
    require(
        document.get("apiVersion") == "admission.k8s.io/v1"
        and document.get("kind") == "AdmissionReview",
        "invalid_review",
    )
    require(set(document) == {"apiVersion", "kind", "request"}, "invalid_review")
    request = document["request"]
    _request_shape(request)
    require(
        request.get("operation") in ("CREATE", "UPDATE", "DELETE", "CONNECT"),
        "invalid_operation",
    )
    require(
        any(entry.namespace_name == request.get("namespace") for entry in enrollments),
        "not_enrolled",
    )
    resource = _resource(request.get("resource"))
    kind = _expected_kind(resource, request.get("subResource", ""))
    require(request.get("kind") == kind, "identity_mismatch")
    for original, effective in (
        ("requestKind", "kind"),
        ("requestResource", "resource"),
        ("requestSubResource", "subResource"),
    ):
        require(
            original not in request or request[original] == request.get(effective, ""),
            "identity_mismatch",
        )
    require(dns_name(request.get("name")), "invalid_identity")
    return request, resource, kind


def _validate_object(value, request, kind):
    require(
        type(value) is dict and type(value.get("metadata")) is dict, "invalid_object"
    )
    version = f"{kind['group']}/{kind['version']}" if kind["group"] else kind["version"]
    require(
        value.get("apiVersion") == version and value.get("kind") == kind["kind"],
        "identity_mismatch",
    )
    metadata = value["metadata"]
    require(
        metadata.get("name") == request["name"]
        and metadata.get("namespace") == request["namespace"],
        "identity_mismatch",
    )
    require(full_uid(metadata.get("uid")), "invalid_identity")


def _pair(request, kind):
    old, new = request.get("oldObject"), request.get("object")
    _validate_object(old, request, kind)
    if request["operation"] == "DELETE":
        require(new is None, "invalid_object")
        return old, None
    _validate_object(new, request, kind)
    require(old["metadata"]["uid"] == new["metadata"]["uid"], "identity_mismatch")
    return old, new


def _same_except(old, new, section, field, *, root_generation=False):
    pair = [deepcopy(old), deepcopy(new)]
    for value in pair:
        metadata = value["metadata"]
        metadata.pop("resourceVersion", None)
        metadata.pop("managedFields", None)
        if root_generation:
            metadata.pop("generation", None)
        if section is None:
            value.pop(field, None)
        elif type(value.get(section)) is dict:
            value[section].pop(field, None)
    return json.dumps(pair[0], sort_keys=True) == json.dumps(pair[1], sort_keys=True)


def _finalizer_cleanup(old, new):
    before, after = (
        old["metadata"].get("finalizers", []),
        new["metadata"].get("finalizers", []),
    )
    if (
        not old["metadata"].get("deletionTimestamp")
        or type(before) is not list
        or type(after) is not list
    ):
        return False
    if not all(type(value) is str for value in before + after):
        return False
    # Preserve ordering and disallow duplicates; at least one removal is needed.
    after_set = set(after)
    remaining = [value for value in before if value in after_set]
    return (
        len(set(before)) == len(before)
        and len(set(after)) == len(after)
        and remaining == after
        and len(after) < len(before)
        and _same_except(old, new, "metadata", "finalizers")
    )


def _root_generation(old, new, changed):
    """API strategies increment generation once for a changed root spec."""
    before = old["metadata"].get("generation")
    after = new["metadata"].get("generation")
    return (
        type(before) is int
        and type(after) is int
        and 0 < before < 2**63
        and 0 < after < 2**63
        and after == before + int(changed)
    )


def _spec_delta(old, new, field, target, *, root_generation=False):
    before, after = old.get("spec"), new.get("spec")
    if type(before) is not dict or type(after) is not dict:
        return False
    return (
        _valid_old_field(before, field)
        and type(after.get(field)) is type(target)
        and after.get(field) == target
        and (not root_generation or _root_generation(old, new, before != after))
        and _same_except(old, new, "spec", field, root_generation=root_generation)
    )


def _valid_old_field(spec, field):
    if field == "replicas":
        return field not in spec or (
            type(spec[field]) is int and 0 <= spec[field] < 2**31
        )
    if field == "suspend":
        return field not in spec or type(spec[field]) is bool
    if field == "running":
        return type(spec.get(field)) is bool
    return spec.get(field) in ("Always", "Halted", "Manual", "Once", "RerunOnFailure")


def _vm_cleanup(old, new):
    before, after = old.get("spec", {}), new.get("spec", {})
    if type(before) is not dict or type(after) is not dict:
        return False
    keys = {"running", "runStrategy"}
    modes = keys.intersection(before)
    if len(modes) != 1 or modes != keys.intersection(after):
        return False
    field = next(iter(modes))
    target = False if field == "running" else "Halted"
    return _spec_delta(old, new, field, target, root_generation=True)


def _cleanup(request, resource, old, new):
    subresource = request.get("subResource", "")
    if subresource == "status":
        return resource in _STATUS and _same_except(old, new, None, "status")
    if subresource == "scale":
        return _spec_delta(old, new, "replicas", 0)
    if _finalizer_cleanup(old, new):
        return True
    if resource in _SCALABLE:
        return _spec_delta(old, new, "replicas", 0, root_generation=True)
    if resource == ("batch", "v1", "jobs"):
        return _spec_delta(old, new, "suspend", True, root_generation=True)
    if resource == ("kubevirt.io", "v1", "virtualmachines"):
        return _vm_cleanup(old, new)
    return False


def _decision(document, enrollments):
    request, resource, kind = _validate_request(document, enrollments)
    if request["operation"] in ("CREATE", "CONNECT"):
        return False, "growth_denied"
    old, new = _pair(request, kind)
    if request["operation"] == "DELETE":
        return True, "delete_outside_quarantine"
    permitted = _cleanup(request, resource, old, new)
    return permitted, "cleanup_only" if permitted else "mutation_denied"


def review(document, enrollments):
    """Return v1 decisions; missing usable UID raises a content-free error."""
    require(
        type(document) is dict and type(document.get("request")) is dict,
        "invalid_review",
    )
    uid = document["request"].get("uid")
    require(full_uid(uid), "invalid_request_uid")
    try:
        allowed, code = _decision(document, enrollments)
    except ValidationError as error:
        allowed, code = False, str(error)
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": allowed,
            "status": {"code": 200 if allowed else 403, "reason": code},
        },
    }
