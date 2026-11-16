"""Read-only, fail-closed checks before Apply Changes removes networks."""

import json
import re

from app.services.providers.kubevirt import CRD_GROUP, CRD_VERSION

_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
_UNVERIFIED = (
    "Unable to verify live network consumers; no network changes were applied."
)
_IN_USE = (
    "A removed network is still used by a live workload or helper. Detach/remove "
    "consumers in a prior Apply Changes operation or use full redeploy."
)


def _items(value, api):
    if not isinstance(value, dict):
        value = api.api_client.sanitize_for_serialization(value)
    if not isinstance(value, dict) or value.get("metadata", {}).get("continue"):
        raise ValueError(_UNVERIFIED)
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError(_UNVERIFIED)
    return items


def _network_ref(value, namespace):
    if not isinstance(value, str) or not isinstance(namespace, str) or not namespace:
        raise ValueError(_UNVERIFIED)
    if not re.fullmatch(
        r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)?(?:@[a-zA-Z0-9_.-]+)?", value
    ):
        raise ValueError(_UNVERIFIED)
    value = value.split("@", 1)[0]
    return value if "/" in value else f"{namespace}/{value}"


def _attachments(metadata, namespace):
    annotation = metadata.get("annotations", {}).get(_ANNOTATION)
    if not annotation:
        return set()
    if not isinstance(annotation, str):
        raise ValueError(_UNVERIFIED)
    entries = (
        json.loads(annotation)
        if annotation.lstrip().startswith("[")
        else annotation.split(",")
    )
    if not isinstance(entries, list):
        raise ValueError(_UNVERIFIED)
    result = set()
    for entry in entries:
        if isinstance(entry, dict):
            entry_namespace = entry.get("namespace", "")
            if not isinstance(entry_namespace, str):
                raise ValueError(_UNVERIFIED)
            result.add(_network_ref(entry["name"], entry_namespace or namespace))
        else:
            result.add(_network_ref(entry.strip(), namespace))
    return result


def _owned_by(obj, uid, kind):
    owners = obj.get("metadata", {}).get("ownerReferences", [])
    return (
        bool(uid)
        and len(owners) == 1
        and owners[0].get("uid") == uid
        and owners[0].get("kind") == kind
        and owners[0].get("controller") is True
    )


def _dns_dependents(deployments, replicas, name, uid):
    deployments = [
        obj
        for obj in deployments
        if obj.get("metadata", {}).get("name") == f"dnsmasq-{name}"
        and _owned_by(obj, uid, "TroshkaNetwork")
    ]
    dep_uids = {obj["metadata"]["uid"] for obj in deployments}
    rs_uids = {
        obj["metadata"]["uid"]
        for obj in replicas
        if any(_owned_by(obj, dep, "Deployment") for dep in dep_uids)
    }
    return dep_uids, rs_uids


def _check_consumers(namespace, nad, name, uid, inventory):
    deployments, replicas, pods, vms, vmis, desired_vms = inventory
    dep_uids, rs_uids = _dns_dependents(deployments, replicas, name, uid)
    target = _network_ref(nad, namespace)
    if any(
        nic.get("networkRef") == name
        for vm in desired_vms
        for nic in vm.get("spec", {}).get("nics", [])
    ):
        raise ValueError(_IN_USE)
    retained = _retained_replicas(replicas, deployments)
    _check_templates(namespace, target, deployments + retained, dep_uids | rs_uids)
    _check_pods(namespace, target, pods, rs_uids)
    _check_vm_specs(namespace, target, vms, vmis)


def _check_templates(namespace, target, objects, exempt_uids):
    for obj in objects:
        refs = _attachments(
            obj.get("spec", {}).get("template", {}).get("metadata", {}), namespace
        )
        if target in refs and obj.get("metadata", {}).get("uid") not in exempt_uids:
            raise ValueError(_IN_USE)


def _retained_replicas(replicas, deployments):
    """Completed rollouts retain inert history; orphan/active replicas still count."""
    deployment_uids = {obj.get("metadata", {}).get("uid") for obj in deployments}
    return [
        obj
        for obj in replicas
        if not (
            obj.get("spec", {}).get("replicas") == 0
            and obj.get("status", {}).get("replicas", 0) == 0
            and any(_owned_by(obj, uid, "Deployment") for uid in deployment_uids)
        )
    ]


def _check_pods(namespace, target, pods, rs_uids):
    for obj in pods:
        if target in _attachments(obj.get("metadata", {}), namespace) and not any(
            _owned_by(obj, rs, "ReplicaSet") for rs in rs_uids
        ):
            raise ValueError(_IN_USE)


def _check_vm_specs(namespace, target, vms, vmis):
    specs = [obj.get("spec", {}).get("template", {}).get("spec", {}) for obj in vms]
    specs += [obj.get("spec", {}) for obj in vmis]
    for spec in specs:
        refs = {
            _network_ref(net["multus"]["networkName"], namespace)
            for net in spec.get("networks", [])
            if "multus" in net
        }
        if target in refs:
            raise ValueError(_IN_USE)


def _inventory(custom, core, apps, namespace):
    options = {"namespace": namespace, "_request_timeout": 15}
    rows = [
        _items(apps.list_namespaced_deployment(**options), apps),
        _items(apps.list_namespaced_replica_set(**options), apps),
        _items(core.list_namespaced_pod(**options), core),
    ]
    for plural in ("virtualmachines", "virtualmachineinstances"):
        rows.append(
            _items(
                custom.list_namespaced_custom_object(
                    group="kubevirt.io", version="v1", plural=plural, **options
                ),
                custom,
            )
        )
    rows.append(
        _items(
            custom.list_namespaced_custom_object(
                group=CRD_GROUP, version=CRD_VERSION, plural="troshkavms", **options
            ),
            custom,
        )
    )
    return rows


def validate_network_removal(custom, core, apps, namespace, removed):
    """Validate every removal before the caller performs any cluster mutation."""
    candidates = [
        node for node in removed if node.get("data", {}).get("subtype") != "gateway"
    ]
    if not candidates:
        return
    try:
        inventory = _inventory(custom, core, apps, namespace)
        for node in candidates:
            name = f"net-{node['id'][:8]}"
            try:
                network = custom.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=namespace,
                    plural="troshkanetworks",
                    name=name,
                    _request_timeout=15,
                )
            except Exception as error:
                if getattr(error, "status", None) != 404:
                    raise
                network = {}
            nad = f"{name}-nad"
            if network.get("status", {}).get("nadName", nad) != nad:
                raise ValueError(_UNVERIFIED)
            _check_consumers(
                namespace, nad, name, network.get("metadata", {}).get("uid"), inventory
            )
    except Exception as error:
        message = (
            _IN_USE
            if isinstance(error, ValueError) and str(error) == _IN_USE
            else _UNVERIFIED
        )
        raise ValueError(message) from None
