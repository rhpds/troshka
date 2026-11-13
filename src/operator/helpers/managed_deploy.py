"""Bounded blank-PVC first deployment, behind the managed producer guard.

The pending phase cannot create children. Initialization creates only blank PVCs
and halted definitions. A separate application CAS admits runtime after physical
accounting; ordinary producers never reopen for this managed root.
"""

import re
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal

from kubernetes.utils.quantity import parse_quantity

from helpers import managed_power as power

PROFILE = "configured-blank-pvc-v1"
CLONE_PROFILE = "configured-managed-clone-v1"

# Only these static API defaults are equivalent to omission in this profile.
# Environment-dependent defaults (CPU models, machine types, injected requests,
# sidecars, selectors, etc.) must be explicitly rendered or remain unsupported.
_POD_DEFAULTS = {
    ("restartPolicy",): "Always",
    ("dnsPolicy",): "ClusterFirst",
    ("schedulerName",): "default-scheduler",
    ("terminationGracePeriodSeconds",): 30,
    ("hostNetwork",): False,
    ("hostPID",): False,
    ("hostIPC",): False,
    ("containers", "[]", "terminationMessagePath"): "/dev/termination-log",
    ("containers", "[]", "terminationMessagePolicy"): "File",
}
_CONFIGURATION_DEFAULTS = {
    "VirtualMachine": {
        ("template", "spec", "domain", "cpu", "sockets"): 1,
        ("template", "spec", "domain", "cpu", "threads"): 1,
    },
    "Deployment": {
        ("revisionHistoryLimit",): 10,
        ("progressDeadlineSeconds",): 600,
        ("strategy", "type"): "RollingUpdate",
        ("strategy", "rollingUpdate", "maxSurge"): "25%",
        ("strategy", "rollingUpdate", "maxUnavailable"): "25%",
        **{("template", "spec", *path): value for path, value in _POD_DEFAULTS.items()},
    },
    "Pod": _POD_DEFAULTS,
    "PersistentVolumeClaim": {("volumeMode",): "Filesystem"},
    "Service": {
        ("spec", "sessionAffinity"): "None",
        ("spec", "internalTrafficPolicy"): "Cluster",
        ("spec", "ipFamilyPolicy"): "SingleStack",
    },
    "Route": {("spec", "wildcardPolicy"): "None", ("spec", "to", "weight"): 100},
}
_SERVER_ALLOCATIONS = {
    "Service": ("clusterIP", "clusterIPs", "ipFamilies"),
    "Route": ("host",),
}


def validate_topology(topology, *, clone=False):
    forbidden = (
        "clusters",
        "ocp",
        "ocpClusters",
        "workloads",
        "showroom",
        "recert",
        "bastion",
        "externalIps",
        "projectCephCapture",
        "startOrder",
    )
    power.require(not any(topology.get(k) for k in forbidden))
    nodes = topology.get("nodes", [])
    ids = [n.get("id") for n in nodes]
    power.require(all(ids) and len(set(ids)) == len(ids))
    for node in nodes:
        power.require(node.get("type") in {"vmNode", "containerNode", "storageNode"})
        data = node.get("data", {})
        flags = (
            "isPod",
            "podContainers",
            "initContainers",
            "tpm",
            "tpmEnabled",
            "bmc",
            "bmcEnabled",
            "recertEnabled",
            "isShowroom",
            "guestfishCommands",
            "patternId",
            "patternDiskId",
            "libraryItemId",
            "snapshotItemId",
            "imageId",
            "deferred",
            "ocpRole",
            "ocpCluster",
            "nics",
            "pxeBootIsoId",
            "pxeBootIsoS3Path",
            "deferOcpInstall",
            "clusterId",
            "cdrom",
            "disks",
            "resolvedS3Path",
            "sourceSizeGb",
            "diskSource",
            "centralSource",
        )
        allowed = (
            {"libraryItemId", "resolvedS3Path", "sourceSizeGb", "centralSource"}
            if clone and node["type"] == "storageNode"
            else set()
        )
        power.require(not any(data.get(k) for k in flags if k not in allowed))
        power.require(not data.get("firmware", "bios").startswith("uefi"))
        if node["type"] == "storageNode":
            if clone and data.get("source") == "library":
                power.require(
                    bool(data.get("libraryItemId"))
                    and data.get("format", "qcow2") == "qcow2"
                    and not data.get("s3Path")
                )
                continue
            power.require(
                not data.get("image")
                and not data.get("s3Path")
                and data.get("source", "") in {"", "blank"}
                and data.get("format", "qcow2") != "iso",
                "managed_import_unsupported",
            )
    power.require(any(n["type"] in {"vmNode", "containerNode"} for n in nodes))


def render(root, storage_class):
    """The same real builders run before reservation and inside the operator."""
    from helpers import containers, k8s, kubevirt, topology, vnc

    topo = deepcopy(root["spec"].get("topology", {}))
    sources = root["spec"].get("managedSources", {})
    validate_topology(topo, clone=bool(sources))
    namespace = root["metadata"]["namespace"]
    disks, cdroms = topology.resolve_vm_disks(topo)
    power.require(not any(cdroms.values()), "managed_import_unsupported")
    result = []
    for vm in topology.extract_vms(topo):
        child = topology.build_vm_cr(
            vm,
            disks,
            cdroms,
            {},
            None,
            namespace,
            root["metadata"]["name"],
            root,
            topo,
        )
        result.append(child)
        mapping = {}
        for disk in child["spec"]["disks"]:
            power.require(not disk.get("patternImage"))
            name = k8s.vm_disk_pvc_name(child["metadata"]["name"], disk["id"])
            mapping[disk["id"]] = name
            pvc = disk_manifest(disk, sources, name, namespace, storage_class)
            pvc["metadata"]["ownerReferences"] = [k8s.owner_ref(root)]
            result.append(pvc)
        # Inline NoCloud is deterministic; never create a secret with random
        # instance metadata after reservation.
        value = kubevirt.build_kubevirt_vm(child, mapping, {}, None)
        value["metadata"]["ownerReferences"] = [
            {
                "apiVersion": child["apiVersion"],
                "kind": "TroshkaVM",
                "name": child["metadata"]["name"],
                "uid": "pending",
                "controller": True,
            }
        ]
        ci = child["spec"].get("cloudInit", {})
        if ci.get("userData") or ci.get("networkConfig"):
            source = {"userData": ci.get("userData", "")}
            if ci.get("networkConfig"):
                source["networkData"] = ci["networkConfig"]
            pod = value["spec"]["template"]["spec"]
            pod["volumes"].append({"name": "cloudinit", "cloudInitNoCloud": source})
            pod["domain"]["devices"]["disks"].append(
                {"name": "cloudinit", "disk": {"bus": "virtio"}}
            )
        result.append(value)
    pvcs = {}
    for ctr, disk, size in topology.collect_container_disk_mounts(topo):
        name = topology.container_disk_pvc_name(ctr, disk)
        power.require(disk not in pvcs, "managed_disk_alias_unsupported")
        pvcs[disk] = name
        pvc = kubevirt.build_blank_pvc(name, namespace, size)
        pvc["spec"]["storageClassName"] = storage_class
        pvc["metadata"]["ownerReferences"] = [k8s.owner_ref(root)]
        result.append(pvc)
    for ctr in topology.extract_containers(topo):
        result.append(
            containers.build_single_container_pod(
                namespace,
                ctr,
                {},
                k8s.owner_ref(root),
                pvcs,
            )
        )
    result.extend(vnc.build_vnc_support(root))
    helper = vnc.build_vnc_proxy_deployment(
        root["metadata"]["name"],
        namespace,
        owner_body=root,
    )
    if root["spec"].get("managedVncImage"):
        helper["spec"]["template"]["spec"]["containers"][0]["image"] = root["spec"][
            "managedVncImage"
        ]
    result.append(helper)
    keys = [(v["kind"], v["metadata"]["name"]) for v in result]
    power.require(len(set(keys)) == len(keys), "managed_resource_collision")
    power.require(
        {
            v["spec"]["source"]["pvc"]["name"]
            for v in result
            if v["kind"] == "DataVolume"
        }
        == {v["name"] for v in sources.values()},
        "managed_source_not_cloned",
    )
    return result


def disk_manifest(disk, sources, name, namespace, storage_class):
    from helpers.kubevirt import build_blank_pvc

    if disk.get("libraryImage"):
        power.require(disk["id"] in sources, "managed_source_required")
        return clone_manifest(
            name, namespace, sources[disk["id"]], disk["sizeGb"], storage_class
        )
    value = build_blank_pvc(name, namespace, disk["sizeGb"])
    value["spec"]["storageClassName"] = storage_class
    return value


def clone_manifest(name, namespace, source, size, storage_class):
    from helpers.kubevirt import build_clone_datavolume

    value = build_clone_datavolume(
        name,
        namespace,
        source["name"],
        source["namespace"],
        size,
        source["sourceCapacityGiB"],
    )
    value["spec"]["pvc"]["storageClassName"] = storage_class
    value["spec"]["pvc"]["volumeMode"] = "Filesystem"
    value["metadata"]["annotations"] = {"cdi.kubevirt.io/storage.usePopulator": "false"}
    if source.get("storage_request"):
        power.require(
            value["spec"]["pvc"]["resources"]["requests"]["storage"]
            == source["storage_request"],
            "managed_source_capacity_changed",
        )
    return value


def demand(manifests):
    cpu, ram = Decimal(0), 0
    for value in manifests:
        if (
            value["kind"] == "VirtualMachine"
            and value["spec"]["runStrategy"] != "Halted"
        ):
            domain = value["spec"]["template"]["spec"]["domain"]
            cfg = domain["cpu"]
            cpu += Decimal(
                cfg.get("cores", 1) * cfg.get("sockets", 1) * cfg.get("threads", 1)
            )
            ram += int(parse_quantity(domain["resources"]["requests"]["memory"]))
        elif value["kind"] == "Pod":
            resources = value["spec"]["containers"][0]["resources"]["requests"]
            cpu += parse_quantity(resources["cpu"])
            ram += int(parse_quantity(resources["memory"]))
    return {
        "cpu": int(cpu.to_integral_value(rounding=ROUND_CEILING)),
        "ram_bytes": ram,
        "configured_cpu": str(cpu),
    }


def request(root) -> dict:
    value = power.intent(root)
    if value is None:
        raise power.ManagedPowerError("managed_deploy_intent_required")
    power.require(value["mode"] == "deploy")
    power.require(value.get("profile") in {PROFILE, CLONE_PROFILE})
    power.require(
        (value["profile"] == CLONE_PROFILE) == bool(root["spec"].get("managedSources"))
    )
    power.require(value.get("phase") in {"pending", "initializing", "runtime"})
    power.require(
        value.get("payloadDigest") == power.digest(root["spec"]),
        "managed_payload_changed",
    )
    power.require(value.get("allocationGeneration") == value.get("requestId"))
    power.require(
        isinstance(value.get("storageClass"), str) and bool(value["storageClass"])
    )
    profile_digest = value.get("profileDigest")
    power.require(
        isinstance(profile_digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", profile_digest) is not None
    )
    if value["phase"] != "pending":
        power.require(
            value.get("rootUid") == root["metadata"]["uid"], "managed_root_changed"
        )
        power.require(
            value.get("rootGeneration") == root["metadata"]["generation"],
            "managed_root_changed",
        )
        power.require(
            value.get("projectId") == root["spec"]["projectId"], "managed_root_changed"
        )
    return value


def state_for(root, wanted):
    state = deepcopy(root.get("status", {}).get("managedDeployment", {}))
    identity = {
        k: wanted[k]
        for k in (
            "requestId",
            "payloadDigest",
            "profileDigest",
            "rootUid",
            "rootGeneration",
            "projectId",
            "namespaceUid",
            "clusterKey",
            "storageClass",
        )
    }
    if state:
        power.require(
            all(state.get(k) == v for k, v in identity.items()),
            "managed_deploy_identity_changed",
        )
    return {**identity, **state}


def write_state(api, root, state):
    changes = [{"op": "add", "path": "/status/managedDeployment", "value": state}]
    if "status" not in root:
        changes = [
            {"op": "add", "path": "/status", "value": {"managedDeployment": state}}
        ]
    return api.patch(root, changes, status=True)


def reconcile(api, root):
    wanted = request(root)
    if wanted["phase"] == "pending":
        rows, _ = power.inventory(api, root)
        power.require(not any(rows.values()), "managed_pending_children_present")
        return
    namespace = root["metadata"]["namespace"]
    power.require(
        api.get("Namespace", None, namespace)["metadata"]["uid"]
        == wanted.get("namespaceUid"),
        "managed_namespace_changed",
    )
    power.require(
        api.get("Namespace", None, "kube-system")["metadata"]["uid"]
        == wanted.get("clusterKey"),
        "managed_cluster_changed",
    )
    planned = render(root, wanted["storageClass"])
    if root["spec"].get("managedPlanDigest"):
        power.require(
            wanted.get("planDigest")
            == root["spec"]["managedPlanDigest"]
            == plan_digest(planned),
            "managed_plan_changed",
        )
    state = state_for(root, wanted)
    if wanted["phase"] == "runtime":
        require_release(state, wanted)
        preflight_release(api, root, state)
        state["phase"] = "runtime"
        root = write_state(api, root, state)
    for value in planned:
        if value["kind"] == "Pod" and wanted["phase"] != "runtime":
            continue
        root, state = ensure_resource(api, root, state, wanted, value)
    if wanted["phase"] == "initializing":
        rows, _, _ = observe(api, root)
        retain_generated_claims(api, rows, state)
        state["initializationDigest"] = initialization_digest(rows)
        state["phase"] = "initialized"
    else:
        state["phase"] = "runtime"
    write_state(api, root, state)


def preflight_release(api, root, state):
    if state["phase"] == "initialized":
        rows, _, _ = observe(api, root)
        verify_retained_claims(api, rows, state)
        power.require(
            initialization_digest(rows) == state["initializationDigest"],
            "managed_initialization_changed",
        )
        return
    rows, by_uid, _ = observe(api, root, runtime=True, allow_missing_pods=True)
    verify_retained_claims(api, rows, state)
    for values in rows.values():
        for value in values:
            verify_runtime_target(value, by_uid, state)


def verify_runtime_target(value, by_uid, state):
    if value["kind"] in {"ReplicaSet", "VirtualMachineInstance"}:
        return
    if value["kind"] == "Pod" and power.parent_kind(value, by_uid) != "TroshkaProject":
        return
    if value["kind"] == "PersistentVolumeClaim" and value["metadata"][
        "name"
    ] in state.get("generatedClaims", {}):
        return  # Independently matched to the retained original DV/PVC/PV closure.
    key = value["kind"] + ":" + value["metadata"]["name"]
    saved = state["creates"].get(key, {})
    power.require(
        bool(saved)
        and saved.get("uid", value["metadata"]["uid"]) == value["metadata"]["uid"],
        "managed_target_changed",
    )


def generated_claim_identities(api, rows, state):
    claims = {v["metadata"]["name"]: v for v in rows["PersistentVolumeClaim"]}
    result = {}
    for dv in rows["DataVolume"]:
        name, uid = dv["metadata"]["name"], dv["metadata"]["uid"]
        original = state["creates"].get("DataVolume:" + name, {})
        power.require(original.get("uid") == uid, "managed_clone_dv_changed")
        claim = claims[name]
        pv = api.get("PersistentVolume", None, claim["spec"]["volumeName"])
        spec = pv["spec"]
        result[name] = {
            "uid": claim["metadata"]["uid"],
            "dvUid": uid,
            "spec": configuration(claim),
            "pv": {
                "uid": pv["metadata"]["uid"],
                "name": pv["metadata"]["name"],
                "claimRef": {
                    k: spec["claimRef"][k] for k in ("uid", "name", "namespace")
                },
                "storageClassName": spec.get("storageClassName"),
                "csi": deepcopy(spec.get("csi", {})),
            },
        }
    return result


def retain_generated_claims(api, rows, state):
    current = generated_claim_identities(api, rows, state)
    if not current:
        return
    if "generatedClaims" in state:
        power.require(
            state["generatedClaims"] == current, "managed_clone_binding_changed"
        )
    else:
        power.require(
            not state.get("initializationDigest"), "managed_clone_binding_unbound"
        )
        state["generatedClaims"] = current


def verify_retained_claims(api, rows, state):
    power.require(
        state.get("generatedClaims", {})
        == generated_claim_identities(api, rows, state),
        "managed_clone_binding_changed",
    )


def require_release(state, wanted):
    power.require(
        state.get("phase") in {"initialized", "runtime"},
        "managed_initialization_pending",
    )
    power.require(
        state.get("initializationDigest") == wanted.get("initializationDigest"),
        "managed_initialization_changed",
    )
    power.require(
        all(
            isinstance(wanted.get(k), str) and wanted[k]
            for k in ("physicalReportId", "physicalObservationId")
        ),
        "managed_physical_proof_required",
    )
    receipt = {
        k: wanted[k]
        for k in ("physicalReportId", "physicalObservationId", "initializationDigest")
    }
    power.require(state.get("release", receipt) == receipt, "managed_release_changed")
    state["release"] = receipt


def initialization_digest(rows):
    return power.digest(
        sorted(
            (
                {
                    "kind": v["kind"],
                    "uid": v["metadata"]["uid"],
                    "spec": configuration(v),
                }
                for values in rows.values()
                for v in values
            ),
            key=lambda v: (v["kind"], v["uid"]),
        )
    )


def ensure_resource(api, root, state, wanted, desired):
    """Persist each create intent before I/O; missing ambiguous creates stay held."""
    value = deepcopy(desired)
    if wanted["phase"] != "runtime":
        if value["kind"] == "VirtualMachine":
            value["spec"]["runStrategy"] = "Halted"
        if value["kind"] == "Deployment":
            value["spec"]["replicas"] = 0
    namespace = root["metadata"]["namespace"]
    bind_vm_owner(api, value, namespace)
    current = power.root_for(root, namespace, api)
    power.require(request(current) == wanted, "managed_episode_changed")
    key = value["kind"] + ":" + value["metadata"]["name"]
    journal = state.setdefault("creates", {})
    existing = [
        v
        for v in api.rows(value["kind"], namespace)
        if v["metadata"]["name"] == value["metadata"]["name"]
    ]
    power.require(len(existing) <= 1)
    if not existing:
        power.require(key not in journal, "managed_create_unresolved")
        journal[key] = {"intentDigest": power.digest(value)}
        current = write_state(api, current, state)
        actual = api.request("POST", value["kind"], namespace, None, value)
    else:
        actual = existing[0]
        power.require(key in journal, "managed_unrelated_resource")
        if value["kind"] == "DataVolume":
            power.require(journal[key].get("uid"), "managed_create_unresolved")
    power.require(not actual["metadata"].get("deletionTimestamp"))
    power.require(
        journal[key].get("uid", actual["metadata"]["uid"]) == actual["metadata"]["uid"],
        "managed_target_changed",
    )
    power.require(
        actual["metadata"].get("ownerReferences")
        == value["metadata"].get("ownerReferences"),
        "managed_owner_changed",
    )
    verify_spec(value, actual, allocations=journal[key].get("allocations"))
    if value["kind"] == "TroshkaVM":
        actual = acknowledge_vm(api, actual)
    if not journal[key].get("uid"):
        journal[key]["uid"] = actual["metadata"]["uid"]
        current = write_state(api, current, state)
    if bind_allocations(journal[key], value, actual):
        current = write_state(api, current, state)
    restore_resource(api, wanted, value, actual)
    return current, state


def restore_resource(api, wanted, value, actual):
    if wanted["phase"] == "runtime" and value["kind"] in {
        "VirtualMachine",
        "Deployment",
    }:
        field = "runStrategy" if value["kind"] == "VirtualMachine" else "replicas"
        if actual["spec"].get(field) != value["spec"][field]:
            api.patch(
                actual,
                [
                    {
                        "op": "add",
                        "path": "/spec/" + field,
                        "value": value["spec"][field],
                    }
                ],
            )


def acknowledge_vm(api, value):
    if (
        value.get("status", {}).get("observedGeneration")
        == value["metadata"]["generation"]
    ):
        return value
    status = {
        **value.get("status", {}),
        "observedGeneration": value["metadata"]["generation"],
    }
    return api.patch(
        value, [{"op": "add", "path": "/status", "value": status}], status=True
    )


def bind_vm_owner(api, value, namespace):
    if value["kind"] != "VirtualMachine":
        return
    ref = value["metadata"]["ownerReferences"][0]
    owners = [
        v
        for v in api.rows("TroshkaVM", namespace)
        if v["metadata"]["name"] == ref["name"]
    ]
    power.require(len(owners) == 1, "managed_parent_unverified")
    ref["uid"] = owners[0]["metadata"]["uid"]


def server_allocations(expected, actual):
    """Only initial omitted address fields are supplied by API allocation."""
    planned, observed = expected.get("spec", {}), actual.get("spec", {})
    return {
        field: deepcopy(observed[field])
        for field in _SERVER_ALLOCATIONS.get(expected["kind"], ())
        if field not in planned and field in observed
    }


def bind_allocations(saved, expected, actual):
    if expected["kind"] not in _SERVER_ALLOCATIONS or "allocations" in saved:
        return False
    saved["allocations"] = server_allocations(expected, actual)
    return True


def allocation_proof(root, expected):
    if expected["kind"] not in _SERVER_ALLOCATIONS:
        return None
    key = expected["kind"] + ":" + expected["metadata"]["name"]
    saved = (
        root.get("status", {})
        .get("managedDeployment", {})
        .get("creates", {})
        .get(key, {})
    )
    power.require("allocations" in saved, "managed_support_allocation_unbound")
    return saved["allocations"]


def verify_spec(expected, actual, *, allocations=None):
    planned, observed = configuration(expected), configuration(actual)
    if allocations is not None:
        power.require(
            server_allocations(expected, actual) == allocations,
            "managed_support_allocation_changed",
        )
    for field in _SERVER_ALLOCATIONS.get(expected["kind"], ()):
        if field not in expected.get("spec", {}):
            observed.get("spec", {}).pop(field, None)
    # Binding is checked separately against immutable PVC/PV identities by
    # verify_storage. It is not a pre-creation input in a blank PVC manifest.
    if expected["kind"] == "PersistentVolumeClaim" and "volumeName" not in planned:
        observed.pop("volumeName", None)
    power.require(
        power.digest(planned) == power.digest(observed),
        "managed_configuration_changed",
    )


def configuration(value) -> dict:
    if value["kind"] in power.SUPPORT_KINDS:
        spec = {
            k: deepcopy(v)
            for k, v in value.items()
            if k not in {"apiVersion", "kind", "metadata", "status"}
        }
    else:
        spec = deepcopy(value["spec"])
    if value["kind"] == "VirtualMachine":
        spec.pop("runStrategy", None)
    if value["kind"] == "Deployment":
        spec.pop("replicas", None)
    return canonical_fields(spec, _CONFIGURATION_DEFAULTS.get(value["kind"], {}), ())


def canonical_configuration(value, defaults, path=()):
    """Compare the complete spec, omitting explicit static defaults/omitempty.

    Resource quantities use exact quantity semantics. Unknown nonempty fields
    reject rather than inheriting container-only subset matching behavior.
    """
    if isinstance(value, dict):
        return canonical_fields(value, defaults, path)
    if isinstance(value, list):
        return [canonical_configuration(v, defaults, (*path, "[]")) for v in value]
    if len(path) >= 2 and path[-2] in {"requests", "limits"}:
        return str(parse_quantity(value).normalize())
    return value


def canonical_fields(value, defaults, path):
    result = {}
    for key, item in value.items():
        current = (*path, key)
        default = defaults.get(current)
        if current in defaults and type(default) is type(item) and item == default:
            continue
        normalized = canonical_configuration(item, defaults, current)
        # Kubernetes omitempty removes empty containers/maps and null optional
        # fields. False/zero are preserved unless explicitly allowlisted above.
        if normalized is not None and normalized != {} and normalized != []:
            result[key] = normalized
    return result


def observe(api, root, *, runtime=False, allow_missing_pods=False):
    """Independent full inventory, distinct from a retained stop snapshot."""
    wanted = request(root)
    power.require(
        wanted["phase"] == "runtime"
        if runtime
        else wanted["phase"] in {"initializing", "runtime"}
    )
    planned = render(root, wanted["storageClass"])
    rows, by_uid = power.inventory(api, root, deployment_support=False)
    support_inventory(api, root, planned, rows, by_uid)
    expected = {
        (v["kind"], v["metadata"]["name"]): v
        for v in planned
        if runtime or v["kind"] != "Pod"
    }
    generated = generated_claims(planned, rows)
    expected.update(generated)
    actual = {
        (v["kind"], v["metadata"]["name"]): v
        for values in rows.values()
        for v in values
        if v["kind"] not in {"ReplicaSet", "VirtualMachineInstance"}
        and not (
            v["kind"] == "Pod" and power.parent_kind(v, by_uid) != "TroshkaProject"
        )
    }
    missing = set(expected) - set(actual)
    power.require(
        not (set(actual) - set(expected))
        and (
            not missing or (allow_missing_pods and all(k[0] == "Pod" for k in missing))
        ),
        "managed_inventory_changed",
    )
    for key, value in actual.items():
        if key in generated:
            verify_generated_claim(value, generated[key], rows)
            continue
        verify_spec(
            expected[key], value, allocations=allocation_proof(root, expected[key])
        )
        power.require(not value["metadata"].get("deletionTimestamp"))
    power.verify_storage(rows, by_uid, api)
    if not runtime:
        power.require(
            not rows["Pod"]
            and not rows["VirtualMachineInstance"]
            and not rows["ReplicaSet"],
            "managed_runtime_present",
        )
        power.require(
            all(
                v["spec"].get("runStrategy") == "Halted" for v in rows["VirtualMachine"]
            )
        )
        power.require(all(v["spec"].get("replicas") == 0 for v in rows["Deployment"]))
    return rows, by_uid, planned


def generated_claims(planned, rows):
    return {
        ("PersistentVolumeClaim", value["metadata"]["name"]): value
        for value in planned
        if value["kind"] == "DataVolume"
    }


def verify_generated_claim(actual, dv, rows):
    """CDI child binding is distinct from deterministic original DV plan matching."""
    live = [
        v for v in rows["DataVolume"] if v["metadata"]["name"] == dv["metadata"]["name"]
    ]
    power.require(len(live) == 1, "managed_clone_dv_missing")
    owners = actual["metadata"].get("ownerReferences", [])
    power.require(
        len(owners) == 1
        and owners[0].get("kind") == "DataVolume"
        and owners[0].get("controller") is True
        and owners[0].get("uid") == live[0]["metadata"]["uid"],
        "managed_clone_owner_changed",
    )
    spec = deepcopy(actual["spec"])
    spec.pop("volumeName", None)
    # Supported direct CDI CSI clone. An unexpected populator/snapshot/selector
    # shape is never silently canonicalized away.
    for field in ("dataSource", "dataSourceRef"):
        source = spec.pop(field, None)
        if source is not None:
            expected = {
                "kind": "PersistentVolumeClaim",
                "name": dv["spec"]["source"]["pvc"]["name"],
            }
            if "apiGroup" in source:
                expected["apiGroup"] = ""
            if field == "dataSourceRef" and "namespace" in source:
                expected["namespace"] = dv["spec"]["source"]["pvc"]["namespace"]
            power.require(source == expected, "managed_clone_datasource_changed")
    expected = {"kind": "PersistentVolumeClaim", "spec": dv["spec"]["pvc"]}
    power.require(
        configuration({"kind": "PersistentVolumeClaim", "spec": spec})
        == configuration(expected),
        "managed_clone_pvc_changed",
    )
    power.require(not actual["metadata"].get("deletionTimestamp"))


def plan_digest(planned):
    result = []
    for value in planned:
        item = deepcopy(value)
        for ref in item["metadata"].get("ownerReferences", []):
            ref["uid"] = "pending"
        result.append(item)
    return power.digest(
        sorted(result, key=lambda v: (v["kind"], v["metadata"]["name"]))
    )


def support_inventory(api, root, planned, rows, by_uid):
    for kind in power.SUPPORT_KINDS:
        names = {v["metadata"]["name"] for v in planned if v["kind"] == kind}
        values = [
            v
            for v in api.rows(kind, root["metadata"]["namespace"])
            if v["metadata"]["name"] in names
        ]
        power.require(len(values) == len(names), "managed_support_missing")
        for value in values:
            power.require(
                value["metadata"]["uid"] not in by_uid, "managed_identity_duplicate"
            )
            by_uid[value["metadata"]["uid"]] = value
            power.verify_chain(value, root, by_uid)
        rows[kind] = values


def retained_support(api, root, rows, by_uid):
    """Only new managed deployments include fixed support in retained proofs."""
    state = root["status"]["managedDeployment"]
    planned = render(root, state["storageClass"])
    power.require(
        root["spec"].get("managedPlanDigest") == plan_digest(planned),
        "managed_plan_changed",
    )
    support_inventory(api, root, planned, rows, by_uid)
    for value in planned:
        if value["kind"] in power.SUPPORT_KINDS:
            actual = next(
                v
                for v in rows[value["kind"]]
                if v["metadata"]["name"] == value["metadata"]["name"]
            )
            verify_spec(value, actual, allocations=allocation_proof(root, value))
