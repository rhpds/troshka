"""Bounded managed power lifecycle for a single active, standalone operator.

Annotations coordinate trusted Troshka producers; they are not a Kubernetes
security boundary. Status is a durable intent snapshot, never shutdown proof.
"""

import asyncio
import inspect
import json
import time
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from kubernetes import client

GROUP = "troshka.redhat.com"
VERSION = "v1alpha1"
ANNOTATION = GROUP + "/managed-power"
KINDS = {
    "VirtualMachine": ("kubevirt.io/v1", "virtualmachines"),
    "VirtualMachineInstance": ("kubevirt.io/v1", "virtualmachineinstances"),
    "TroshkaProject": (GROUP + "/" + VERSION, "troshkaprojects"),
    "TroshkaVM": (GROUP + "/" + VERSION, "troshkavms"),
    "TroshkaNetwork": (GROUP + "/" + VERSION, "troshkanetworks"),
    "TroshkaCeph": (GROUP + "/" + VERSION, "troshkancephs"),
    "DataVolume": ("cdi.kubevirt.io/v1beta1", "datavolumes"),
    "Pod": ("v1", "pods"),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims"),
    "PersistentVolume": ("v1", "persistentvolumes"),
    "Namespace": ("v1", "namespaces"),
    "ServiceAccount": ("v1", "serviceaccounts"),
    "Service": ("v1", "services"),
    "Route": ("route.openshift.io/v1", "routes"),
    "Role": ("rbac.authorization.k8s.io/v1", "roles"),
    "RoleBinding": ("rbac.authorization.k8s.io/v1", "rolebindings"),
    **{
        k: ("apps/v1", p)
        for k, p in (
            ("Deployment", "deployments"),
            ("ReplicaSet", "replicasets"),
            ("StatefulSet", "statefulsets"),
            ("DaemonSet", "daemonsets"),
        )
    },
    "Job": ("batch/v1", "jobs"),
    "CronJob": ("batch/v1", "cronjobs"),
}
SUPPORT_KINDS = {"ServiceAccount", "Service", "Route", "Role", "RoleBinding"}
INVENTORY = tuple(
    k
    for k in KINDS
    if k not in {"TroshkaProject", "PersistentVolume", "Namespace"} | SUPPORT_KINDS
)
_locks = {}
_context: ContextVar[tuple[dict, str, Any] | None] = ContextVar(
    "managed_power_producer", default=None
)


class ManagedPowerError(ValueError):
    """Only fixed reason codes may cross the API boundary."""


def require(condition, reason="managed_profile_unsupported"):
    if not condition:
        raise ManagedPowerError(reason)


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def intent(root):
    raw = root.get("metadata", {}).get("annotations", {}).get(ANNOTATION)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        require(
            value["version"] == 1 and value["mode"] in {"stopped", "running", "deploy"}
        )
        require(
            all(
                type(value[k]) is str and 0 < len(value[k]) <= 256
                for k in ("requestId", "allocationGeneration")
            )
        )
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedPowerError("managed_intent_invalid") from error


class PowerClient:
    """Small, bounded JSON transport using the operator/provider's existing client."""

    def __init__(self, api=None):
        self.api = api or client.ApiClient()
        self.deadline = None

    def begin_inventory(self):
        self.deadline = time.monotonic() + 55

    def timeout(self):
        remaining = 5 if self.deadline is None else self.deadline - time.monotonic()
        require(remaining > 0, "managed_observation_timeout")
        return min(5, remaining)

    def request(self, method, kind, namespace, name=None, body=None, status=False):
        version, plural = KINDS[kind]
        prefix = "/api/" if version == "v1" else "/apis/"
        path = prefix + version
        if namespace is not None:
            path += "/namespaces/" + quote(namespace, safe="")
        path += "/" + plural
        if name:
            path += "/" + quote(name, safe="")
        if status:
            path += "/status"
        # No retry of ambiguous mutations. Kubernetes identifiers are validated
        # by the API; provider/root identities originate from retained objects.
        from urllib3 import Timeout

        headers = {**self.api.default_headers, "Accept": "application/json"}
        headers["Content-Type"] = (
            "application/json-patch+json" if method == "PATCH" else "application/json"
        )
        query = [("limit", "1001")] if method == "GET" and name is None else []
        self.api.update_params_for_auth(headers, query, ["BearerToken"])
        response = self.api.rest_client.pool_manager.request(
            method,
            self.api.configuration.host + path,
            fields=query if body is None else None,
            headers=headers,
            body=json.dumps(body).encode() if body is not None else None,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=self.timeout()),
        )
        try:
            require(200 <= response.status < 300, "managed_api_unavailable")
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, "managed_response_limit")
            value = json.loads(raw)
            require(isinstance(value, dict), "managed_response_invalid")
            return value
        finally:
            response.close()

    def get(self, kind, namespace, name):
        return self.request("GET", kind, namespace, name)

    def rows(self, kind, namespace):
        value = self.request("GET", kind, namespace)
        rows = value["items"]
        require(
            isinstance(rows, list)
            and len(rows) <= 1000
            and not value["metadata"].get("continue"),
            "managed_inventory_incomplete",
        )
        require(len({r["metadata"]["uid"] for r in rows}) == len(rows))
        for row in rows:
            require(row["metadata"]["namespace"] == namespace)
            row.setdefault("kind", kind)
            row.setdefault("apiVersion", KINDS[kind][0])
            require(row["kind"] == kind and row["apiVersion"] == KINDS[kind][0])
        return rows

    def patch(self, value, changes, *, status=False):
        meta = value["metadata"]
        tests = [
            {"op": "test", "path": "/metadata/" + k, "value": meta[k]}
            for k in ("uid", "resourceVersion")
        ]
        return self.request(
            "PATCH",
            value["kind"],
            meta["namespace"],
            meta["name"],
            tests + changes,
            status=status,
        )


def root_for(body, namespace, api):
    if body.get("kind") == "TroshkaProject":
        meta = body["metadata"]
    else:
        refs = body.get("metadata", {}).get("ownerReferences", [])
        require(
            len(refs) == 1 and refs[0].get("kind") == "TroshkaProject",
            "managed_parent_unverified",
        )
        meta = refs[0]
    root = api.get("TroshkaProject", namespace, meta["name"])
    require(root["metadata"]["uid"] == meta["uid"], "managed_root_changed")
    require(not root["metadata"].get("deletionTimestamp"), "managed_root_deleting")
    return root


def assert_running(namespace, owner_uid):
    context = _context.get()
    if context is None:
        raise ManagedPowerError("managed_producer_context_required")
    require(
        context[1] == namespace and context[0]["metadata"]["uid"] == owner_uid,
        "managed_producer_context_required",
    )
    root, _, api = context
    current = root_for(root, namespace, api)
    require(intent(current) is None, "managed_producer_stopped")


async def checked_await(awaitable):
    """Recheck external annotation changes after yielding during provisioning."""
    result = await awaitable
    context = _context.get()
    if context is None:
        raise ManagedPowerError("managed_producer_context_required")
    root, namespace, _ = context
    assert_running(namespace, root["metadata"]["uid"])
    return result


def producer(*, project=False, reconcile=False):
    """Serialize entry points, reread intent, drain before acknowledging a stop."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs).arguments
            extra = bound.get("_", {})
            namespace = bound.get("namespace", extra.get("namespace"))
            body = bound.get("body", extra.get("body"))
            if body is None:
                body = {
                    "kind": "TroshkaProject" if project else "",
                    "metadata": bound["meta"],
                }
            api = PowerClient()
            root = root_for(body, namespace, api)
            key = root["metadata"]["uid"]
            async with _locks.setdefault(key, asyncio.Lock()):
                root = root_for(body, namespace, api)
                token = _context.set((root, namespace, api))
                try:
                    if (
                        root.get("metadata", {}).get("annotations", {}).get(ANNOTATION)
                        is not None
                    ):
                        reconcile_intent(api, root, reconcile)
                        return None
                    return await function(*args, **kwargs)
                finally:
                    _context.reset(token)

        return wrapped

    return decorate


def reconcile_intent(api, root, reconcile):
    request = intent(root)
    if request is None:
        raise ManagedPowerError("managed_intent_required")
    if request["mode"] == "deploy":
        from helpers.managed_deploy import reconcile as deploy

        deploy(api, root)
    elif reconcile:
        reconcile_stop(api, root)


def inventory(api, root, *, deployment_support=True):
    namespace = root["metadata"]["namespace"]
    rows = {kind: api.rows(kind, namespace) for kind in INVENTORY}
    all_rows = [root] + [value for values in rows.values() for value in values]
    require(len(all_rows) <= 1000, "managed_inventory_limit")
    by_uid = {value["metadata"]["uid"]: value for value in all_rows}
    require(len(by_uid) == len(all_rows), "managed_identity_duplicate")
    for value in all_rows[1:]:
        verify_chain(value, root, by_uid)
    if deployment_support and root.get("status", {}).get("managedDeployment", {}).get(
        "confirmedObservationId"
    ):
        from helpers import managed_deploy

        managed_deploy.retained_support(api, root, rows, by_uid)
    return rows, by_uid


def verify_chain(value, root, by_uid):
    seen = set()
    while value["metadata"]["uid"] != root["metadata"]["uid"]:
        uid = value["metadata"]["uid"]
        require(uid not in seen, "managed_owner_cycle")
        seen.add(uid)
        refs = value["metadata"].get("ownerReferences", [])
        require(
            len(refs) == 1 and refs[0].get("controller") is True,
            "managed_ownership_unverified",
        )
        ref = refs[0]
        parent = by_uid.get(ref["uid"])
        require(parent is not None, "managed_owner_missing")
        require(
            ref["name"] == parent["metadata"]["name"]
            and ref["kind"] == parent["kind"]
            and ref["apiVersion"] == parent["apiVersion"],
            "managed_owner_changed",
        )
        value = parent


def profile(root, rows, by_uid, api):
    status = root.get("status", {})
    require(status.get("phase") == "Running", "managed_project_not_settled")
    require(
        not any(status.get(k) for k in ("recertConfig", "capture", "filePull")),
        "managed_active_work_unsupported",
    )
    annotations = root["metadata"].get("annotations", {})
    require(
        not any("capture" in k or "file-pull" in k for k in annotations),
        "managed_active_work_unsupported",
    )
    topology = root["spec"].get("topology", {})
    require(not topology.get("projectCephCapture"), "managed_ceph_unsupported")
    for node in topology.get("nodes", []):
        require(
            node.get("type")
            in {"vmNode", "containerNode", "networkNode", "storageNode"}
        )
        data = node.get("data", {})
        require(
            not any(data.get(k) for k in ("bmcEnabled", "recertEnabled", "isShowroom"))
        )
    for kind in ("TroshkaCeph", "StatefulSet", "DaemonSet", "Job", "CronJob"):
        require(not rows[kind], "managed_controller_unsupported")
    for vm in rows["TroshkaVM"]:
        require(
            vm.get("status", {}).get("observedGeneration")
            == vm["metadata"]["generation"],
            "managed_child_not_settled",
        )
        require(
            not vm["spec"].get("bmcEnabled") and not vm["spec"].get("recertEnabled")
        )
    verify_storage(rows, by_uid, api)
    verify_workloads(root, rows, by_uid)


def verify_storage(rows, by_uid, api):
    runtime_kinds = {"Pod", "VirtualMachineInstance", "ReplicaSet"}
    for kind in ("PersistentVolumeClaim", "DataVolume"):
        for value in rows[kind]:
            require(
                not value["metadata"].get("deletionTimestamp"),
                "managed_storage_deleting",
            )
            parent = by_uid[value["metadata"]["ownerReferences"][0]["uid"]]
            require(
                parent["kind"] not in runtime_kinds | {"VirtualMachine", "Deployment"},
                "managed_storage_not_retained",
            )
            if kind == "DataVolume":
                require(
                    value.get("status", {}).get("phase") == "Succeeded",
                    "managed_import_active",
                )
            else:
                verify_pvc(value, api)
    verify_mounts(rows)


def verify_mounts(rows):
    claims = {v["metadata"]["name"] for v in rows["PersistentVolumeClaim"]}
    for kind in (
        "VirtualMachine",
        "VirtualMachineInstance",
        "Deployment",
        "ReplicaSet",
        "Pod",
    ):
        for value in rows[kind]:
            spec = value.get("spec", {})
            if kind in {"VirtualMachine", "Deployment", "ReplicaSet"}:
                spec = spec.get("template", {}).get("spec", {})
            verify_volumes(spec, claims)


def verify_volumes(spec, claims):
    allowed = {
        "emptyDir",
        "persistentVolumeClaim",
        "secret",
        "configMap",
        "projected",
        "downwardAPI",
        "cloudInitNoCloud",
    }
    volumes = spec.get("volumes", [])
    require(isinstance(volumes, list) and len(volumes) <= 128)
    for volume in volumes:
        sources = set(volume) - {"name"}
        require(len(sources) == 1 and sources <= allowed, "managed_volume_unsupported")
        if "persistentVolumeClaim" in volume:
            require(
                volume["persistentVolumeClaim"].get("claimName") in claims,
                "managed_claim_missing",
            )


def verify_pvc(pvc, api):
    require(pvc.get("status", {}).get("phase") == "Bound", "managed_pvc_not_bound")
    pv = api.get("PersistentVolume", None, pvc["spec"]["volumeName"])
    require(
        not pv["metadata"].get("deletionTimestamp")
        and not pv["metadata"].get("ownerReferences"),
        "managed_pv_not_retained",
    )
    claim = pv["spec"]["claimRef"]
    require(
        all(claim[k] == pvc["metadata"][k] for k in ("name", "namespace", "uid")),
        "managed_pv_changed",
    )


def parent_kind(value, by_uid):
    return by_uid[value["metadata"]["ownerReferences"][0]["uid"]]["kind"]


def verify_workloads(root, rows, by_uid):
    verify_virtual_machines(rows, by_uid)
    verify_helpers(rows, by_uid)
    verify_pods(root, rows, by_uid)


def verify_virtual_machines(rows, by_uid):
    require(len(rows["VirtualMachine"]) == len(rows["TroshkaVM"]), "managed_vm_missing")
    for value in rows["VirtualMachine"]:
        require(parent_kind(value, by_uid) == "TroshkaVM")
        require(
            not value["spec"].get("dataVolumeTemplates")
            and not value.get("status", {}).get("volumeRequests")
        )
        spec = value["spec"]
        require(
            len({"running", "runStrategy"} & set(spec)) == 1, "managed_vm_mode_invalid"
        )
        require(
            spec.get("runStrategy", "Halted")
            in {"Always", "Halted", "RerunOnFailure", "Manual", "Once"}
        )


def verify_helpers(rows, by_uid):
    for value in rows["Deployment"]:
        require(parent_kind(value, by_uid) in {"TroshkaProject", "TroshkaNetwork"})
        name = value["metadata"]["name"]
        require(
            name.startswith(("dnsmasq-", "gateway-", "exec-", "vnc-")),
            "managed_helper_unsupported",
        )
        require(
            type(value["spec"].get("replicas")) is int
            and value["spec"]["replicas"] >= 0
        )
    for value in rows["ReplicaSet"]:
        require(parent_kind(value, by_uid) == "Deployment")


def verify_pods(root, rows, by_uid):
    for value in rows["VirtualMachineInstance"]:
        require(parent_kind(value, by_uid) == "VirtualMachine")
    for value in rows["Pod"]:
        require(
            parent_kind(value, by_uid)
            in {"TroshkaProject", "ReplicaSet", "VirtualMachineInstance"}
        )
        if parent_kind(value, by_uid) == "TroshkaProject":
            manifest = container_manifests(root, rows).get(value["metadata"]["name"])
            if manifest is None:
                raise ManagedPowerError("managed_bare_pod_unsupported")
            require(
                manifest_matches(manifest["spec"], value["spec"]),
                "managed_container_changed",
            )


def manifest_matches(expected, actual, path=()):
    if isinstance(expected, dict):
        return manifest_dict_matches(expected, actual, path)
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                manifest_matches(a, b, path + ("[]",)) for a, b in zip(expected, actual)
            )
        )
    if len(path) >= 2 and path[-2] in {"limits", "requests"}:
        from kubernetes.utils.quantity import parse_quantity

        try:
            return parse_quantity(expected) == parse_quantity(actual)
        except (ValueError, TypeError):
            return False
    return expected == actual


def manifest_dict_matches(expected, actual, path):
    if not isinstance(actual, dict) or extra_intent_fields(expected, actual, path):
        return False
    for key, value in expected.items():
        if key not in actual:
            if value == [] or value == {}:
                continue  # Kubernetes omitempty preserves these empty defaults.
            return False
        if not manifest_matches(value, actual[key], path + (key,)):
            return False
    return True


def extra_intent_fields(expected, actual, path):
    # Ignore API-owned scheduling/default fields, but never silently accept new
    # commands, mounts, sidecars or resource demand absent from retained topology.
    fields = set()
    if not path:
        fields = {
            "containers",
            "initContainers",
            "volumes",
            "hostNetwork",
            "hostPID",
            "hostIPC",
        }
    elif path[-1] in {"resources", "limits", "requests"}:
        fields = set(actual)
    elif len(path) >= 2 and path[-2] in {"containers", "initContainers"}:
        fields = {
            "command",
            "args",
            "env",
            "envFrom",
            "volumeMounts",
            "volumeDevices",
            "resources",
            "ports",
            "workingDir",
            "lifecycle",
            "livenessProbe",
            "readinessProbe",
            "startupProbe",
            "restartPolicy",
        }
    return any(
        actual[k] not in ([], {}, None, False, "")
        for k in (set(actual) - set(expected)) & fields
    )


def container_manifests(root, rows):
    from helpers.containers import build_pod_group, build_single_container_pod
    from helpers.k8s import owner_ref
    from helpers.topology import (
        enrich_container_nics,
        enrich_showroom_infra_networks,
        extract_containers,
    )

    topology = deepcopy(root["spec"].get("topology", {}))
    containers = extract_containers(topology)
    enrich_container_nics(topology, containers)
    enrich_showroom_infra_networks(topology, containers)
    refs = {
        v["metadata"]["name"]: v["status"]["nadName"] for v in rows["TroshkaNetwork"]
    }
    # Matches _provision_container_pvcs without provisioning or resizing anything.
    from helpers.topology import collect_container_disk_mounts, container_disk_pvc_name

    pvcs = {
        disk: container_disk_pvc_name(ctr, disk)
        for ctr, disk, _ in collect_container_disk_mounts(topology)
    }
    result = {}
    for ctr in containers:
        builder = build_pod_group if ctr.get("isPod") else build_single_container_pod
        value = builder(root["metadata"]["namespace"], ctr, refs, owner_ref(root), pvcs)
        require(
            value["metadata"]["name"] not in result,
            "managed_container_identity_collision",
        )
        result[value["metadata"]["name"]] = value
    return result


def snapshot(root, rows, by_uid, request):
    manifests = container_manifests(root, rows)
    bare_names = {
        p["metadata"]["name"]
        for p in rows["Pod"]
        if parent_kind(p, by_uid) == "TroshkaProject"
    }
    require(set(manifests) == bare_names, "managed_container_not_settled")
    targets = []
    for kind in ("VirtualMachine", "Deployment", "Pod"):
        for value in rows[kind]:
            if kind == "Pod" and parent_kind(value, by_uid) != "TroshkaProject":
                continue
            meta = value["metadata"]
            target = {"kind": kind, "name": meta["name"], "uid": meta["uid"]}
            if kind == "Pod":
                target["manifestDigest"] = digest(manifests[meta["name"]])
                target["nodeId"] = container_node_id(root, value)
            else:
                target["desired"] = {
                    k: value["spec"][k]
                    for k in ("replicas", "running", "runStrategy")
                    if k in value["spec"]
                }
            targets.append(target)
    retained = retained_digest(rows)
    return {
        "requestId": request["requestId"],
        "allocationGeneration": request["allocationGeneration"],
        "mode": "stopped",
        "state": "stopping",
        "producerDrained": False,
        "topologyDigest": digest(root["spec"]),
        "targets": targets,
        "retainedDigest": retained,
        "inventoryDigest": digest({"targets": targets, "retainedDigest": retained}),
    }


def retained_digest(rows):
    result = []
    kinds = ["TroshkaVM", "TroshkaNetwork", "PersistentVolumeClaim", "DataVolume"]
    kinds.extend(sorted(SUPPORT_KINDS.intersection(rows)))
    for kind in kinds:
        for value in rows[kind]:
            meta = value["metadata"]
            result.append(
                {
                    "kind": kind,
                    "name": meta["name"],
                    "uid": meta["uid"],
                    "owners": meta.get("ownerReferences"),
                    "spec": {
                        k: v
                        for k, v in value.items()
                        if k not in {"apiVersion", "kind", "metadata", "status"}
                    }
                    if kind in SUPPORT_KINDS
                    else value["spec"],
                }
            )
    return digest(sorted(result, key=lambda v: (v["kind"], v["name"])))


def container_node_id(root, pod):
    from helpers.topology import extract_containers

    prefix = pod["metadata"].get("labels", {}).get("troshka-container")
    matches = [
        c["id"]
        for c in extract_containers(root["spec"].get("topology", {}))
        if c["id"][:8] == prefix
    ]
    require(len(matches) == 1, "managed_container_identity_changed")
    return matches[0]


def write_status(api, root, state):
    change = {"op": "add", "path": "/status/managedPower", "value": state}
    if "status" not in root:
        change = {"op": "add", "path": "/status", "value": {"managedPower": state}}
    return api.patch(root, [change], status=True)


def validate_snapshot(root, state, request):
    require(
        state.get("requestId") == request["requestId"]
        and state.get("allocationGeneration") == request["allocationGeneration"],
        "managed_episode_changed",
    )
    require(
        state.get("topologyDigest") == digest(root["spec"]), "managed_topology_changed"
    )
    require(
        state.get("inventoryDigest")
        == digest(
            {
                "targets": state.get("targets"),
                "retainedDigest": state.get("retainedDigest"),
            }
        ),
        "managed_snapshot_invalid",
    )


def reconcile_stop(api, root):
    try:
        begin = getattr(api, "begin_inventory", None)
        if begin is not None:
            begin()
        request = intent(root)
        if request is None:
            raise ManagedPowerError("managed_intent_missing")
        if request["mode"] == "running":
            reconcile_restore(api, root, request)
            return
        rows, by_uid = inventory(api, root)
        profile(root, rows, by_uid, api)
        state = deepcopy(root.get("status", {}).get("managedPower"))
        if state is None or "inventoryDigest" not in state:
            state = snapshot(root, rows, by_uid, request)
            root = write_status(api, root, state)  # Persist originals BEFORE mutation.
        elif state.get("requestId") != request["requestId"]:
            state = superseding_stop(root, rows, state, request)
            root = write_status(api, root, state)
        validate_snapshot(root, state, request)
        require(
            state.get("retainedDigest") == retained_digest(rows),
            "managed_retained_identity_changed",
        )
        stop_targets(api, root, rows, state)
        current = root_for(root, root["metadata"]["namespace"], api)
        require(intent(current) == request, "managed_episode_changed")
        state.update(
            state="off" if off_inventory(rows) else "stopping", producerDrained=True
        )
        write_status(api, current, state)
    except ManagedPowerError as error:
        current = root_for(root, root["metadata"]["namespace"], api)
        state = deepcopy(current.get("status", {}).get("managedPower", {}))
        state.update(state="blocked", producerDrained=False, reason=str(error))
        write_status(api, current, state)


def superseding_stop(root, rows, state, request):
    """Carry desired originals through partial restore; bind recreated Pod UIDs."""
    validate_snapshot(root, state, {**request, "requestId": state["requestId"]})
    validate_restore_inventory(rows, state)
    targets = deepcopy(state["targets"])
    pods = {p["metadata"]["name"]: p for p in rows["Pod"]}
    manifests = container_manifests(root, rows)
    for target in targets:
        if target["kind"] != "Pod" or target["name"] not in pods:
            continue
        pod = pods[target["name"]]
        manifest = manifests[target["name"]]
        require(
            digest(manifest) == target["manifestDigest"]
            and manifest_matches(manifest["spec"], pod["spec"]),
            "managed_container_changed",
        )
        uid = pod["metadata"]["uid"]
        if uid != target["uid"]:
            require(
                bool(state.get("resumeRequestId"))
                and state.get("restoredPods", {}).get(target["name"], uid) == uid,
                "managed_target_changed",
            )
            target["uid"] = uid
    result = {k: deepcopy(state[k]) for k in ("topologyDigest", "retainedDigest")}
    result.update(
        requestId=request["requestId"],
        allocationGeneration=request["allocationGeneration"],
        mode="stopped",
        state="stopping",
        producerDrained=False,
        targets=targets,
    )
    result["inventoryDigest"] = digest(
        {"targets": targets, "retainedDigest": result["retainedDigest"]}
    )
    return result


def validate_restore(root, state, request):
    require(
        request.get("profile") == "configured-workloads-v1",
        "managed_resume_profile_invalid",
    )
    require(
        isinstance(request.get("executionDigest"), str)
        and bool(request["executionDigest"])
        and request.get("resumeOf") == state.get("requestId")
        and request.get("targetDigest") == state.get("inventoryDigest"),
        "managed_resume_target_changed",
    )
    previous = {**request, "mode": "stopped", "requestId": request["resumeOf"]}
    validate_snapshot(root, state, previous)
    require(state.get("producerDrained") is True, "managed_producer_not_drained")
    require(
        state.get("resumeRequestId") in (None, request["requestId"]),
        "managed_episode_changed",
    )


def reconcile_restore(api, root, request):
    state = deepcopy(root.get("status", {}).get("managedPower", {}))
    validate_restore(root, state, request)
    rows, by_uid = inventory(api, root)
    profile(root, rows, by_uid, api)
    require(
        state["retainedDigest"] == retained_digest(rows),
        "managed_retained_identity_changed",
    )
    validate_restore_inventory(rows, state)
    require(
        configuration_digest(rows) == request.get("configurationDigest"),
        "managed_configuration_changed",
    )
    if state.get("state") == "running":
        require(
            state.get("confirmedOperationId") == request["requestId"]
            and bool(state.get("confirmedObservationId")),
            "managed_confirmation_invalid",
        )
        return  # Completed managed plans never reopen ordinary producer paths.
    state.update(state="restoring", resumeRequestId=request["requestId"])
    root = write_status(api, root, state)
    manifests = container_manifests(root, rows)
    for target in state["targets"]:
        restore_target(api, root, request, target, state, manifests)
    current = root_for(root, root["metadata"]["namespace"], api)
    require(intent(current) == request, "managed_episode_changed")
    write_status(api, current, state)


def validate_restore_inventory(rows, state):
    for kind in ("VirtualMachine", "Deployment"):
        expected = {t["name"]: t["uid"] for t in state["targets"] if t["kind"] == kind}
        actual = {v["metadata"]["name"]: v["metadata"]["uid"] for v in rows[kind]}
        require(actual == expected, "managed_inventory_changed")


def configuration_digest(rows):
    """Power transitions may change desired state, never workload declarations."""
    values = []
    for kind in ("VirtualMachine", "Deployment"):
        for value in rows[kind]:
            values.append(
                {
                    "kind": kind,
                    "name": value["metadata"]["name"],
                    "uid": value["metadata"]["uid"],
                    "spec": {
                        k: v
                        for k, v in value["spec"].items()
                        if k not in {"running", "runStrategy", "replicas"}
                    },
                }
            )
    return digest(sorted(values, key=lambda v: (v["kind"], v["name"])))


def restore_target(api, root, request, target, state, manifests):
    namespace = root["metadata"]["namespace"]
    require(
        intent(root_for(root, namespace, api)) == request, "managed_episode_changed"
    )
    if target["kind"] == "Pod":
        restore_pod(api, root, target, state, manifests)
        return
    value = api.get(target["kind"], namespace, target["name"])
    require(
        value["metadata"]["uid"] == target["uid"]
        and not value["metadata"].get("deletionTimestamp"),
        "managed_target_changed",
    )
    desired = target["desired"]
    changes = [
        {"op": "remove", "path": "/spec/" + k}
        for k in ("running", "runStrategy")
        if k in value["spec"] and k not in desired
    ]
    changes.extend(
        {"op": "add", "path": "/spec/" + k, "value": v}
        for k, v in desired.items()
        if value["spec"].get(k) != v
    )
    if changes:
        api.patch(value, changes)


def restore_pod(api, root, target, state, manifests):
    manifest = manifests.get(target["name"])
    require(
        manifest is not None and digest(manifest) == target.get("manifestDigest"),
        "managed_container_changed",
    )
    require(
        container_node_id(root, manifest) == target.get("nodeId"),
        "managed_container_identity_changed",
    )
    namespace = root["metadata"]["namespace"]
    values = {p["metadata"]["name"]: p for p in api.rows("Pod", namespace)}
    pod = values.get(target["name"])
    if pod is None:
        # An ambiguous create is never retried: next reconciliation reads it by
        # its exact saved name, owner and manifest before proceeding.
        pod = api.request("POST", "Pod", namespace, None, manifest)
    require(not pod["metadata"].get("deletionTimestamp"), "managed_target_deleting")
    require(
        manifest_matches(manifest["spec"], pod["spec"]), "managed_container_changed"
    )
    require(
        pod["metadata"].get("ownerReferences")
        == manifest["metadata"]["ownerReferences"],
        "managed_owner_changed",
    )
    restored = state.setdefault("restoredPods", {})
    uid = pod["metadata"]["uid"]
    require(
        restored.get(target["name"], uid) == uid and uid != target["uid"],
        "managed_target_changed",
    )
    restored[target["name"]] = uid


def off_inventory(rows):
    if rows["Pod"] or rows["VirtualMachineInstance"]:
        return False
    if any(
        v["metadata"].get("deletionTimestamp")
        for kind in ("VirtualMachine", "Deployment")
        for v in rows[kind]
    ):
        return False
    vms_off = all(
        v["spec"].get("runStrategy") == "Halted" and "running" not in v["spec"]
        for v in rows["VirtualMachine"]
    )
    helpers_off = all(
        v["spec"].get("replicas") == 0
        and v.get("status", {}).get("observedGeneration") == v["metadata"]["generation"]
        for v in rows["Deployment"]
    )
    replicas_off = all(v["spec"].get("replicas") == 0 for v in rows["ReplicaSet"])
    return vms_off and helpers_off and replicas_off


def stop_targets(api, root, rows, state):
    namespace = root["metadata"]["namespace"]
    indexed = {
        (v["kind"], v["metadata"]["name"]): v
        for values in rows.values()
        for v in values
    }
    expected = {(t["kind"], t["name"]): t for t in state["targets"]}
    for kind in ("VirtualMachine", "Deployment"):
        require(
            {v["metadata"]["name"] for v in rows[kind]}
            == {t["name"] for t in state["targets"] if t["kind"] == kind},
            "managed_inventory_changed",
        )
    for key, target in expected.items():
        value = indexed.get(key)
        if value is None:
            require(target["kind"] == "Pod", "managed_target_missing")
            continue
        require(value["metadata"]["uid"] == target["uid"], "managed_target_changed")
        require(
            intent(root_for(root, namespace, api)) == intent(root),
            "managed_episode_changed",
        )
        stop_target(api, value)


def stop_target(api, value):
    kind, spec = value["kind"], value.get("spec", {})
    if kind == "Pod":
        if not value["metadata"].get("deletionTimestamp"):
            api.request(
                "DELETE",
                "Pod",
                value["metadata"]["namespace"],
                value["metadata"]["name"],
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {
                        k: value["metadata"][k] for k in ("uid", "resourceVersion")
                    },
                },
            )
        return
    field, desired = (
        ("replicas", 0) if kind == "Deployment" else ("runStrategy", "Halted")
    )
    if spec.get(field) == desired:
        return
    changes = []
    if kind == "VirtualMachine" and "running" in spec:
        changes.append({"op": "remove", "path": "/spec/running"})
    changes.append({"op": "add", "path": "/spec/" + field, "value": desired})
    api.patch(value, changes)
