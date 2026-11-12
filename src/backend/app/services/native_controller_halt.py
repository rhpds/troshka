"""Narrow retained controller profiles for the existing maintenance engine."""

from typing import Any, cast

from app.services.native_vm_halt import (
    _object,
    _owned_by,
    _owner,
    _references,
    _retained_pvc,
    _runtime_rows,
    require,
)

CONTROLLERS = {
    "Deployment": ("apps/v1", "deployments"),
    "ReplicaSet": ("apps/v1", "replicasets"),
    "StatefulSet": ("apps/v1", "statefulsets"),
    "Job": ("batch/v1", "jobs"),
}
_ROOTS = {"TroshkaVM", "TroshkaNetwork"}


def _chain(reader, namespace, value, project):
    """At most RS -> Deployment -> generated CR -> pinned Project."""
    retained = [value, project]
    ref = _owner(value)
    if value["kind"] == "ReplicaSet" and ref["kind"] == "Deployment":
        parent = _object(reader, "Deployment", namespace, ref["name"], ref["uid"])
        require(parent.get("apiVersion") == "apps/v1")
        _owned_by(value, parent)
        value, ref = parent, _owner(parent)
        retained.append(parent)
    if ref["kind"] in _ROOTS:
        parent = _object(reader, ref["kind"], namespace, ref["name"], ref["uid"])
        require(parent.get("apiVersion") == "troshka.redhat.com/v1alpha1")
        _owned_by(value, parent)
        _owned_by(parent, project)
        retained.append(parent)
    else:
        _owned_by(value, project)
    return retained


def _claims(spec):
    volumes = spec.get("volumes", [])
    require(type(volumes) is list and len(volumes) <= 128)
    claims, names = set(), set()
    for volume in cast(list[dict[str, Any]], volumes):
        require(type(volume) is dict and bool(volume.get("name")))
        require(volume["name"] not in names)
        names.add(volume["name"])
        source = set(volume) - {"name"}
        require(
            len(source) == 1
            and source
            <= {
                "emptyDir",
                "persistentVolumeClaim",
                "secret",
                "configMap",
                "projected",
                "downwardAPI",
            },
            "unsupported_controller_volume",
        )
        if "persistentVolumeClaim" in source:
            claim = volume["persistentVolumeClaim"]
            require(type(claim) is dict and set(claim) <= {"claimName", "readOnly"})
            require(type(claim.get("claimName")) is str and bool(claim["claimName"]))
            claims.add(claim["claimName"])
    return claims


def _template(value):
    require(
        not value["spec"].get("volumeClaimTemplates"), "claim_templates_unsupported"
    )
    policy = value["spec"].get("persistentVolumeClaimRetentionPolicy", {})
    require(type(policy) is dict and set(policy) <= {"whenScaled", "whenDeleted"})
    require(all(v in {"Retain", "Delete"} for v in policy.values()))
    require(policy.get("whenScaled", "Retain") == "Retain", "scale_deletes_storage")
    return _claims(value["spec"]["template"]["spec"])


def _runtime(reader, namespace, controller):
    runtimes = []
    parents = [controller]
    if controller["kind"] == "Deployment":
        for rs in _runtime_rows(reader, "ReplicaSet", namespace):
            if _references(rs, controller["metadata"]["uid"]):
                _runtime_identity(rs, "ReplicaSet", "apps/v1")
                _owned_by(rs, controller)
                require(rs.get("apiVersion") == "apps/v1")
                require(not rs["metadata"].get("deletionTimestamp"), "runtime_deleting")
                parents.append(rs)
                runtimes.append(rs)
    for pod in _runtime_rows(reader, "Pod", namespace):
        matches = [p for p in parents if _references(pod, p["metadata"]["uid"])]
        if matches:
            _runtime_identity(pod, "Pod", "v1")
            require(len(matches) == 1)
            _owned_by(pod, matches[0])
            require(not pod["metadata"].get("deletionTimestamp"), "runtime_deleting")
            runtimes.append(pod)
    return runtimes


def _runtime_identity(value, kind, version):
    require(value.get("kind") == kind and value.get("apiVersion") == version)
    require(all(value["metadata"].get(k) for k in ("name", "uid", "resourceVersion")))


def _retain_claim(reader, namespace, name, retained, project):
    pvc = _object(reader, "PersistentVolumeClaim", namespace, name)
    ref = _owner(pvc)
    if ref["kind"] == "DataVolume":
        dv = _object(reader, "DataVolume", namespace, ref["name"], ref["uid"])
        ref = _owner(dv)
    owners = [v for v in retained if v["metadata"]["uid"] == ref["uid"]]
    require(len(owners) == 1, "unverified_pvc_retention")
    _retained_pvc(reader, namespace, name, owners[0], project)


def _storage_ancestry(reader, namespace, value, project, runtime_uids, cache):
    versions = {kind: version for kind, (version, _) in CONTROLLERS.items()}
    versions.update({kind: "troshka.redhat.com/v1alpha1" for kind in _ROOTS})
    versions["DataVolume"] = "cdi.kubevirt.io/v1beta1"
    seen = set()
    for _ in range(6):
        uid = value["metadata"]["uid"]
        require(uid not in seen and uid not in runtime_uids, "unsafe_storage_ancestry")
        seen.add(uid)
        ref = _owner(value)
        if ref["uid"] == project["metadata"]["uid"]:
            _owned_by(value, project)
            return
        require(ref["kind"] in versions, "unknown_storage_ancestry")
        key = (ref["kind"], ref["name"], ref["uid"])
        if key not in cache:
            require(len(cache) < reader.max_objects * 8, "storage_graph_limit")
            cache[key] = _object(
                reader, ref["kind"], namespace, ref["name"], ref["uid"]
            )
        parent = cache[key]
        require(parent.get("apiVersion") == versions[ref["kind"]])
        _owned_by(value, parent)
        value = parent
    require(False, "storage_graph_depth")


def _runtime_storage(reader, namespace, runtime_uids, project):
    # CDI intermediaries can cause indirect PVC garbage collection as well.
    cache = {}
    for kind in ("DataVolume", "PersistentVolumeClaim"):
        for value in _runtime_rows(reader, kind, namespace):
            version = (
                "v1" if kind == "PersistentVolumeClaim" else "cdi.kubevirt.io/v1beta1"
            )
            _runtime_identity(value, kind, version)
            require(not value["metadata"].get("deletionTimestamp"), "storage_deleting")
            _storage_ancestry(reader, namespace, value, project, runtime_uids, cache)


def preflight_controller(reader, namespace, target, project):
    controller = _object(reader, target.kind, namespace, target.name, target.uid)
    require(controller.get("apiVersion") == CONTROLLERS[target.kind][0])
    require(
        controller["metadata"]["resourceVersion"] == target.resource_version,
        "resource_version_changed",
    )
    retained = _chain(reader, namespace, controller, project)
    claims = _template(controller)
    runtimes = _runtime(reader, namespace, controller)
    runtime_uids = {v["metadata"]["uid"] for v in runtimes}
    for runtime in runtimes:
        if runtime["kind"] == "ReplicaSet":
            claims |= _template(runtime)
        else:
            claims |= _claims(runtime.get("spec", {}))
    # Scaling/suspension can garbage-collect even an unmounted runtime-owned PVC.
    _runtime_storage(reader, namespace, runtime_uids, project)
    for name in sorted(claims):
        _retain_claim(reader, namespace, name, retained, project)
    return controller
