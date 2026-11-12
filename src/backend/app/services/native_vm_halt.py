"""Standalone maintenance halt requests. No quota/runtime integration.

The admission import is provided by the standalone CLI's operator source path.
Separate reads are not an atomic retention fence; apply requires external writer
maintenance. No method here deletes or changes storage or ownership.
"""

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from app.services.native_workload_observations import _identity, observe_workloads


class UnsafeProfileError(ValueError):
    """Fixed reason only; never include an API body."""


def require(condition, reason="unsupported_profile"):
    if not condition:
        raise UnsafeProfileError(reason)


class HaltJournal:
    """Create-new, fsynced JSONL. An unfinished attempt means DO NOT redispatch."""

    def __init__(self, path):
        self.path = path
        self.stream = None

    def __enter__(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "w")
        # Persist creation before any network effect, as well as every record.
        parent = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
        try:
            os.fsync(parent)
        except BaseException:
            self.stream.close()
            raise
        finally:
            os.close(parent)
        return self

    def append(self, event, **fields):
        assert self.stream is not None
        self.stream.write(
            json.dumps({"event": event, "at": datetime.now(UTC).isoformat(), **fields})
            + "\n"
        )
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def __exit__(self, *args):
        assert self.stream is not None
        self.stream.close()


def _object(reader, kind, namespace, name, uid=None):
    value = reader.get(kind, namespace, name)
    meta = value["metadata"]
    require(value.get("kind") == kind and meta.get("name") == name)
    require(namespace is None or meta.get("namespace") == namespace)
    require(bool(meta.get("uid")) and bool(meta.get("resourceVersion")))
    require(uid is None or meta["uid"] == uid, "resource_identity_changed")
    require(not meta.get("deletionTimestamp"), "resource_deleting")
    return value


def _owner(value):
    owners = value["metadata"].get("ownerReferences", [])
    require(len(owners) == 1, "unverified_ownership")
    owner = owners[0]
    require(
        owner.get("controller") is True
        and bool(owner.get("name"))
        and bool(owner.get("uid")),
        "unverified_ownership",
    )
    return owner


def _owned_by(value, owner):
    ref = _owner(value)
    require(
        all(ref.get(key) == owner.get(key) for key in ("apiVersion", "kind")),
        "unverified_ownership",
    )
    require(
        all(ref.get(key) == owner["metadata"].get(key) for key in ("name", "uid")),
        "unverified_ownership",
    )


def _root(reader, root):
    require(_identity(reader, root) is None, "root_unverified")
    projects = reader.projects(root.namespace)
    require(len(projects) == 1, "root_unverified")
    project = projects[0]
    require(
        project["metadata"].get("uid") == root.project_cr_uid
        and not project["metadata"].get("deletionTimestamp"),
        "root_unverified",
    )
    require(
        project["metadata"].get("generation") == root.deployment_generation
        and project.get("spec", {}).get("projectId") == str(root.project_id),
        "root_unverified",
    )
    ns = reader.namespace(root.namespace).get("metadata", {})
    require(
        ns.get("uid") == root.namespace_uid and not ns.get("deletionTimestamp"),
        "root_unverified",
    )
    return project


def _retained_pvc(reader, namespace, name, owner, project):
    pvc = _object(reader, "PersistentVolumeClaim", namespace, name)
    ref = _owner(pvc)
    retained = {v["metadata"]["uid"]: v for v in (owner, project)}
    if ref.get("kind") == "DataVolume":
        dv = _object(reader, "DataVolume", namespace, ref["name"], ref["uid"])
        _owned_by(pvc, dv)
        _owned_by(dv, owner)
    else:
        require(ref.get("uid") in retained, "unverified_pvc_retention")
        _owned_by(pvc, retained[ref["uid"]])
    require(pvc.get("status", {}).get("phase") == "Bound", "pvc_not_bound")
    pv = _object(reader, "PersistentVolume", None, pvc["spec"]["volumeName"])
    claim = pv.get("spec", {}).get("claimRef", {})
    require(
        all(
            claim.get(k) == v
            for k, v in {
                "name": name,
                "namespace": namespace,
                "uid": pvc["metadata"]["uid"],
            }.items()
        ),
        "pv_claim_mismatch",
    )
    require(not pv["metadata"].get("ownerReferences"), "unverified_pv_retention")
    return pvc


def _volume_claims(spec):
    volumes = spec.get("volumes", [])
    require(type(volumes) is list and len(volumes) <= 128)
    claims, names = set(), set()
    for volume in cast(list[dict[str, Any]], volumes):
        require(type(volume) is dict and bool(volume.get("name")))
        require(volume["name"] not in names)
        names.add(volume["name"])
        source = set(volume) - {"name"}
        require(
            source in ({"persistentVolumeClaim"}, {"cloudInitNoCloud"}),
            "unsupported_volume",
        )
        if source == {"persistentVolumeClaim"}:
            pvc = volume["persistentVolumeClaim"]
            require(
                set(pvc) <= {"claimName", "readOnly"} and bool(pvc.get("claimName"))
            )
            claims.add(pvc["claimName"])
    return claims


def _runtime_rows(reader, kind, namespace):
    payload = reader.workloads(kind, namespace)
    rows: list[dict[str, Any]] = payload["items"]
    require(
        type(rows) is list
        and len(rows) <= reader.max_objects
        and not payload["metadata"].get("continue"),
        "runtime_inventory_incomplete",
    )
    checked = cast(list[dict[str, Any]], rows)
    require(len({r["metadata"]["uid"] for r in checked}) == len(checked))
    require(all(r["metadata"].get("namespace") == namespace for r in checked))
    return checked


def _references(value, uid):
    return any(
        ref.get("uid") == uid for ref in value["metadata"].get("ownerReferences", [])
    )


def _pod_volumes(pod, claims):
    for volume in pod.get("spec", {}).get("volumes", []):
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
            "unsupported_launcher_volume",
        )
        if "persistentVolumeClaim" in source:
            require(
                volume["persistentVolumeClaim"].get("claimName") in claims,
                "unverified_launcher_pvc",
            )


def _runtime(reader, namespace, vm, claims):
    vmis = _runtime_rows(reader, "VirtualMachineInstance", namespace)
    selected = []
    for vmi in vmis:
        if vmi["metadata"]["name"] == vm["metadata"]["name"] or _references(
            vmi, vm["metadata"]["uid"]
        ):
            _owned_by(vmi, vm)
            require(
                _volume_claims(vmi.get("spec", {})) <= claims, "unverified_runtime_pvc"
            )
            selected.append(vmi)
    for pod in _runtime_rows(reader, "Pod", namespace):
        for vmi in selected:
            if _references(pod, vmi["metadata"]["uid"]):
                _owned_by(pod, vmi)
                _pod_volumes(pod, claims)


def _verify_cleanup(vm, new, namespace):
    # Only the repository entry point adds operator sources to the import path.
    review = import_module("admission.quarantine").review
    from app.services.native_controller_halt import CONTROLLERS

    version, resource = CONTROLLERS.get(
        vm["kind"], ("kubevirt.io/v1", "virtualmachines")
    )
    group, version = version.split("/")
    request = {
        "uid": str(uuid4()),
        "kind": {"group": group, "version": version, "kind": vm["kind"]},
        "resource": {
            "group": group,
            "version": version,
            "resource": resource,
        },
        "name": vm["metadata"]["name"],
        "namespace": namespace,
        "operation": "UPDATE",
        "userInfo": {"username": "local-verification"},
        "oldObject": vm,
        "object": new,
    }
    decision = review(
        {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "request": request,
        },
        [SimpleNamespace(namespace_name=namespace)],
    )
    require(decision["response"]["allowed"], "cleanup_classifier_denied")


def _desired_patch(vm, namespace, field, value):
    new = deepcopy(vm)
    new["spec"][field] = value
    changed = new["spec"] != vm["spec"]
    new["metadata"]["generation"] += int(changed)
    _verify_cleanup(vm, new, namespace)
    return [
        {"op": "test", "path": "/metadata/uid", "value": vm["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": vm["metadata"]["resourceVersion"],
        },
        {
            "op": "replace" if field in vm["spec"] else "add",
            "path": "/spec/" + field,
            "value": value,
        },
    ]


def _cleanup_patch(vm, namespace):
    modes = {"running", "runStrategy"}.intersection(vm["spec"])
    require(len(modes) == 1, "unsupported_vm_mode")
    field = next(iter(modes))
    value = False if field == "running" else "Halted"
    return _desired_patch(vm, namespace, field, value)


def preflight(reader, manifest, target):
    namespace = manifest.root.namespace
    project = _root(reader, manifest.root)
    if target.kind != "VirtualMachine":
        from app.services.native_controller_halt import preflight_controller

        controller = preflight_controller(reader, namespace, target, project)
        field, value = ("suspend", True) if target.kind == "Job" else ("replicas", 0)
        return controller, _desired_patch(controller, namespace, field, value)
    vm = _object(reader, "VirtualMachine", namespace, target.name, target.uid)
    require(
        vm["metadata"]["resourceVersion"] == target.resource_version,
        "resource_version_changed",
    )
    ref = _owner(vm)
    require(ref.get("kind") == "TroshkaVM", "unverified_ownership")
    owner = _object(reader, "TroshkaVM", namespace, ref["name"], ref["uid"])
    _owned_by(vm, owner)
    _owned_by(owner, project)
    require(not vm["spec"].get("dataVolumeTemplates"), "generated_storage_unsupported")
    require(not vm.get("status", {}).get("volumeRequests"), "hotplug_unsupported")
    claims = _volume_claims(vm["spec"]["template"]["spec"])
    for name in sorted(claims):
        _retained_pvc(reader, namespace, name, owner, project)
    _runtime(reader, namespace, vm, claims)
    return vm, _cleanup_patch(vm, namespace)


def _request(reader, manifest, target, journal, apply):
    vm, patch = preflight(reader, manifest, target)
    _root(reader, manifest.root)
    journal.append(
        "attempt",
        target=target.model_dump(),
        mode="apply" if apply else "server_dry_run",
    )
    try:
        if target.kind == "VirtualMachine":
            returned = reader.patch_vm(
                manifest.root.namespace, target.name, patch, dry_run=not apply
            )
        else:
            returned = reader.patch_controller(
                target.kind,
                manifest.root.namespace,
                target.name,
                patch,
                dry_run=not apply,
            )
        _verify_returned(returned, manifest.root.namespace, target, patch)
        _verify_cleanup(vm, returned, manifest.root.namespace)
    except Exception:
        # Includes response loss, HTTP failure and ambiguous admission/transport.
        # Conservatively never retry or proceed to another target.
        return {"target": target.model_dump(), "status": "ambiguous"}
    if not apply:
        return {"target": target.model_dump(), "status": "dry_run_accepted"}
    controller = target.kind != "VirtualMachine"
    status = "stop_request_accepted" if controller else "halt_request_accepted"
    try:
        _root(reader, manifest.root)
        vm = _object(
            reader, target.kind, manifest.root.namespace, target.name, target.uid
        )
        field = patch[-1]["path"].split("/")[-1]
        if (
            type(vm["spec"].get(field)) is type(patch[-1]["value"])
            and vm["spec"].get(field) == patch[-1]["value"]
        ):
            status = "desired_stop_observed" if controller else "desired_halt_observed"
    except Exception:
        pass
    return {"target": target.model_dump(), "status": status}


def _verify_returned(returned, namespace, target, patch):
    from app.services.native_controller_halt import CONTROLLERS

    version = CONTROLLERS.get(target.kind, ("kubevirt.io/v1", "virtualmachines"))[0]
    require(
        returned.get("apiVersion") == version and returned.get("kind") == target.kind
    )
    meta = returned["metadata"]
    require(
        meta.get("name") == target.name
        and meta.get("namespace") == namespace
        and meta.get("uid") == target.uid
    )
    require(bool(meta.get("resourceVersion")) and not meta.get("deletionTimestamp"))
    field = patch[-1]["path"].split("/")[-1]
    value = returned["spec"].get(field)
    require(type(value) is type(patch[-1]["value"]) and value == patch[-1]["value"])


def halt_vms(reader, manifest, journal, *, apply=False):
    report = {
        "status": "blocked",
        "mode": "apply" if apply else "server_dry_run",
        "results": [],
        "settlement_verified": False,
        "physical_stop_verified": False,
        "capacity_release_allowed": False,
        "storage_absence_verified": False,
        "blockers": [],
    }
    journal.append(
        "start", manifest=manifest.model_dump(mode="json"), mode=report["mode"]
    )
    try:
        initial = observe_workloads(
            reader,
            manifest.root,
            collector_id=manifest.collector_id,
            max_objects=reader.max_objects,
        )
        require(initial.inventory_complete, "initial_inventory_incomplete")
        for target in manifest.targets:
            preflight(reader, manifest, target)
        for target in manifest.targets:
            result = _request(reader, manifest, target, journal, apply)
            # Any journal failure escapes; leave the preceding attempt unresolved.
            journal.append("result", **result)
            report["results"].append(result)
            if result["status"] not in {
                "dry_run_accepted",
                "desired_halt_observed",
                "desired_stop_observed",
            }:
                break
        report["status"] = (
            "complete"
            if len(report["results"]) == len(manifest.targets)
            and all(
                r["status"]
                in {
                    "dry_run_accepted",
                    "desired_halt_observed",
                    "desired_stop_observed",
                }
                for r in report["results"]
            )
            else "incomplete"
        )
    except OSError:
        raise
    except UnsafeProfileError as error:
        report["blockers"].append(str(error))
    except Exception:
        report["blockers"].append("preflight_unavailable_or_unsafe")
    report["observation"] = observe_workloads(
        reader,
        manifest.root,
        collector_id=manifest.collector_id,
        max_objects=reader.max_objects,
    ).model_dump(mode="json")
    journal.append("summary", report=report)
    return report
