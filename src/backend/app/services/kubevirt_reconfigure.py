"""KubeVirt-native Apply Changes helpers (parity with troshkad reconfigure)."""

import copy
import logging
import time

from app.services.providers.kubevirt import CRD_GROUP, CRD_VERSION

logger = logging.getLogger(__name__)

_TROSHKA_DOMAIN = CRD_GROUP
_NET_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
# Matches the operator's helpers.kubevirt.STORAGE_CLASS; used only as a fallback
# when the namespace has no existing PVC to copy the storage class from.
_DEFAULT_STORAGE_CLASS = "ocs-storagecluster-ceph-rbd-virtualization"


def _gateway_image() -> str:
    """Gateway image at the configured deploy tag (not a hardcoded ':stable' that
    doesn't exist in the registry). Matches what the operator deploys."""
    from app.services.app_updater import image_ref

    return image_ref("troshka-gateway")


def _gateway_ip_for_cidr(cidr: str) -> str:
    if not cidr:
        return ""
    base = cidr.split("/")[0]
    parts = base.split(".")
    if len(parts) != 4:
        return ""
    parts[3] = "1"
    return ".".join(parts)


def _networks_with_gateway(topology: dict) -> set[str]:
    gateway_ids = {
        n.get("id")
        for n in topology.get("nodes", [])
        if n.get("type") == "networkNode"
        and (n.get("data") or {}).get("subtype") == "gateway"
    }
    connected: set[str] = set()
    for edge in topology.get("edges", []):
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src in gateway_ids:
            connected.add(tgt)
        elif tgt in gateway_ids:
            connected.add(src)
    return connected


def _network_entry_from_node(node: dict, topology: dict) -> dict | None:
    data = node.get("data") or {}
    if node.get("type") != "networkNode" or data.get("subtype") == "gateway":
        return None
    if data.get("networkType") == "bmc":
        return None
    node_id = node.get("id", data.get("id", ""))
    if not node_id:
        return None
    cidr = data.get("cidr", "")
    gateway_ip = (
        data.get("gatewayIp") or data.get("gateway") or _gateway_ip_for_cidr(cidr)
    )
    has_gateway = node_id in _networks_with_gateway(topology)
    external_access = data.get("externalAccess", has_gateway)
    dns_forwarders = data.get("dnsForwarders", [])
    if not dns_forwarders and data.get("dns") and gateway_ip:
        dns_forwarders = [gateway_ip]
    entry = {
        "id": node_id,
        "cidr": cidr,
        "gateway": gateway_ip,
        "dhcpRange": data.get("dhcpRange", ""),
        "networkType": data.get("networkType", "standard"),
        "dnsForwarders": dns_forwarders,
        "externalAccess": external_access,
        "dnsRecords": data.get("dnsRecords", []),
        "pxeConfig": data.get("pxeConfig") or {},
    }
    mtu = data.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        entry["mtu"] = mtu
    return entry


def _extract_nic_id(handle: str) -> str:
    if not handle or "nic-" not in handle:
        return ""
    for suffix in ("-top", "-bottom", "-left", "-right"):
        if handle.endswith(suffix):
            handle = handle[: -len(suffix)]
            break
    if handle.startswith("nic-"):
        handle = handle[4:]
    if handle.startswith("nic-"):
        return handle
    return f"nic-{handle}" if handle else ""


def _static_leases_for_network(net_id: str, topology: dict) -> list[dict]:
    from app.services.vxlan import (
        _cluster_vip_reservations,
        _infra_ip_reservations,
    )

    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    node_map: dict[str, dict] = {}
    for node in nodes:
        data = node.get("data") or {}
        nid = node.get("id", data.get("id", ""))
        node_map[nid] = data

    leases: list[dict] = []
    reserved_ips: set[str] = set()
    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        src_data, tgt_data = node_map.get(src, {}), node_map.get(tgt, {})
        vm_data = net_target = nic_id = None
        if src_data.get("nics"):
            vm_data, net_target, nic_id = (
                src_data,
                tgt,
                _extract_nic_id(edge.get("sourceHandle", "")),
            )
        elif tgt_data.get("nics"):
            vm_data, net_target, nic_id = (
                tgt_data,
                src,
                _extract_nic_id(edge.get("targetHandle", "")),
            )
        if not vm_data or net_target != net_id or not nic_id:
            continue
        for nic in vm_data.get("nics", []):
            if nic.get("id") != nic_id:
                continue
            mac, ip = nic.get("mac", ""), nic.get("ip", "")
            if mac and ip:
                leases.append(
                    {
                        "mac": mac,
                        "ip": ip,
                        "hostname": vm_data.get("name", vm_data.get("label", "")),
                    }
                )
                reserved_ips.add(ip)

    for vip in _cluster_vip_reservations(net_id, nodes, edges, reserved_ips):
        ip = vip.get("ip", "")
        if ip:
            reserved_ips.add(ip)
        leases.append(
            {
                "mac": vip.get("mac", ""),
                "ip": ip,
                "hostname": vip.get("name", vip.get("hostname", "")),
            }
        )

    net_data = next(
        (n.get("data", {}) for n in nodes if n.get("id") == net_id),
        {},
    )
    for infra in _infra_ip_reservations(net_data, reserved_ips):
        ip = infra.get("ip", "")
        if ip:
            reserved_ips.add(ip)
        leases.append(
            {
                "mac": infra.get("mac", ""),
                "ip": ip,
                "hostname": infra.get("name", infra.get("hostname", "")),
            }
        )
    return leases


def build_troshkanetwork_spec(net_entry: dict, topology: dict) -> dict:
    """Build a TroshkaNetwork CR spec from a network entry dict."""
    net_id = net_entry["id"]
    spec = {
        "networkId": net_id,
        "cidr": net_entry.get("cidr", ""),
        "gateway": net_entry.get("gateway", ""),
        "dhcpRange": net_entry.get("dhcpRange", ""),
        "networkType": net_entry.get("networkType", "standard"),
        "dnsForwarders": net_entry.get("dnsForwarders", []),
        "externalAccess": net_entry.get("externalAccess", False),
        "staticLeases": _static_leases_for_network(net_id, topology),
        "dnsRecords": net_entry.get("dnsRecords", []),
    }
    if net_entry.get("pxeConfig"):
        spec["pxeConfig"] = net_entry["pxeConfig"]
    mtu = net_entry.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        spec["mtu"] = mtu
    return spec


def _build_troshkanetwork_cr(
    cr_name: str,
    ns: str,
    p_id: str,
    spec: dict,
    owner_refs: list[dict],
    labels: dict | None,
) -> dict:
    return {
        "apiVersion": f"{_TROSHKA_DOMAIN}/{CRD_VERSION}",
        "kind": "TroshkaNetwork",
        "metadata": {
            "name": cr_name,
            "namespace": ns,
            "ownerReferences": owner_refs or [],
            "labels": labels or {"troshka-project": p_id[:8]},
        },
        "spec": spec,
    }


def _create_troshkanetwork_cr(custom_api, ns: str, body: dict) -> None:
    from kubernetes.client.exceptions import ApiException

    try:
        custom_api.create_namespaced_custom_object(
            group=_TROSHKA_DOMAIN,
            version=CRD_VERSION,
            namespace=ns,
            plural="troshkanetworks",
            body=body,
        )
    except ApiException as e:
        if e.status != 409:
            raise


def _resolve_nad_refs(custom_api, ns: str) -> dict[str, str]:
    nad_refs: dict[str, str] = {}
    try:
        networks = custom_api.list_namespaced_custom_object(
            group=_TROSHKA_DOMAIN,
            version=CRD_VERSION,
            namespace=ns,
            plural="troshkanetworks",
        )
        for net in dict(networks).get("items", []):  # type: ignore[call-overload]
            net_name = net.get("metadata", {}).get("name", "")
            nad_name = net.get("status", {}).get("nadName", f"{net_name}-nad")
            nad_refs[net_name] = nad_name
    except Exception:
        pass
    return nad_refs


def _wait_kubevirt_networks_ready(
    custom_api, ns: str, pending_cr_names: list[str], deadline_secs: int = 120
) -> None:
    if not pending_cr_names:
        return
    pending = set(pending_cr_names)
    deadline = time.time() + deadline_secs
    while time.time() < deadline and pending:
        try:
            nets = custom_api.list_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkanetworks",
            )
            for net in dict(nets).get("items", []):  # type: ignore[call-overload]
                cr_name = net.get("metadata", {}).get("name", "")
                if cr_name not in pending:
                    continue
                if net.get("status", {}).get("ready"):
                    pending.discard(cr_name)
        except Exception:
            pass
        if not pending:
            break
        time.sleep(3)


def _find_changed_kubevirt_networks(current: dict, deployed: dict) -> list[str]:
    cur_nodes = {
        n["id"]: n
        for n in current.get("nodes", [])
        if n.get("type") == "networkNode"
        and (n.get("data") or {}).get("subtype") not in ("gateway",)
        and (n.get("data") or {}).get("networkType") != "bmc"
    }
    dep_nodes = {
        n["id"]: n
        for n in deployed.get("nodes", [])
        if n.get("type") == "networkNode"
        and (n.get("data") or {}).get("subtype") not in ("gateway",)
        and (n.get("data") or {}).get("networkType") != "bmc"
    }
    import json

    def _stable(obj: dict) -> str:
        return json.dumps(obj, sort_keys=True, default=str)

    changed: list[str] = []
    for nid, cur in cur_nodes.items():
        dep = dep_nodes.get(nid)
        if not dep:
            continue
        if _stable(cur.get("data", {})) != _stable(dep.get("data", {})):
            changed.append(nid)
    return changed


def _kubevirt_project_owner_refs(project_cr: dict) -> list[dict]:
    meta = project_cr.get("metadata") or {}
    return [
        {
            "apiVersion": project_cr.get(
                "apiVersion", f"{_TROSHKA_DOMAIN}/{CRD_VERSION}"
            ),
            "kind": project_cr.get("kind", "TroshkaProject"),
            "name": meta.get("name", ""),
            "uid": meta.get("uid", ""),
            "controller": True,
        }
    ]


def apply_kubevirt_network_changes(
    custom_api,
    ns: str,
    p_id: str,
    current: dict,
    deployed: dict,
    diff: dict,
    project_cr: dict,
    errors: list[str],
) -> bool:
    """Create/patch/delete TroshkaNetwork CRs. Returns True when gateway NADs changed."""
    owner_refs = _kubevirt_project_owner_refs(project_cr)
    labels = {"troshka-project": p_id[:8]}
    gateway_changed = False
    pending: list[str] = []

    for node in diff.get("removed_networks", []):
        if (node.get("data") or {}).get("subtype") == "gateway":
            continue
        cr_name = f"net-{node['id'][:8]}"
        try:
            custom_api.delete_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkanetworks",
                name=cr_name,
            )
            logger.info("Reconfigure %s: deleted TroshkaNetwork %s", p_id[:8], cr_name)
        except Exception:
            pass

    for node in diff.get("added_networks", []):
        entry = _network_entry_from_node(node, current)
        if not entry:
            continue
        cr_name = f"net-{entry['id'][:8]}"
        spec = build_troshkanetwork_spec(entry, current)
        body = _build_troshkanetwork_cr(cr_name, ns, p_id, spec, owner_refs, labels)
        try:
            _create_troshkanetwork_cr(custom_api, ns, body)
            logger.info("Reconfigure %s: created TroshkaNetwork %s", p_id[:8], cr_name)
            pending.append(cr_name)
            if entry.get("externalAccess"):
                gateway_changed = True
        except Exception as e:
            logger.warning(
                "Reconfigure %s: failed to create %s: %s", p_id[:8], cr_name, e
            )
            errors.append(f"Failed to add network {entry['id'][:8]}: {e}")

    for net_id in _find_changed_kubevirt_networks(current, deployed):
        node = next(
            (n for n in current.get("nodes", []) if n.get("id") == net_id), None
        )
        if not node:
            continue
        entry = _network_entry_from_node(node, current)
        if not entry:
            continue
        cr_name = f"net-{net_id[:8]}"
        spec = build_troshkanetwork_spec(entry, current)
        try:
            existing = custom_api.get_namespaced_custom_object(  # type: ignore[assignment]
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkanetworks",
                name=cr_name,
            )
            existing["spec"] = spec  # type: ignore[index]
            custom_api.replace_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkanetworks",
                name=cr_name,
                body=existing,
            )
            logger.info("Reconfigure %s: updated TroshkaNetwork %s", p_id[:8], cr_name)
            pending.append(cr_name)
        except Exception as e:
            logger.warning(
                "Reconfigure %s: failed to update %s: %s", p_id[:8], cr_name, e
            )
            errors.append(f"Failed to update network {net_id[:8]}: {e}")

    _wait_kubevirt_networks_ready(custom_api, ns, pending)
    return gateway_changed


def apply_kubevirt_ceph_changes(
    custom_api,
    ns: str,
    p_id: str,
    current: dict,
    diff: dict,
    project_cr: dict,
    errors: list[str],
) -> None:
    """Create, update, or delete TroshkaCeph when cephClusterNode changes."""
    from app.services.project_ceph import (
        TROSHKA_CEPH_CR_NAME,
        build_troshka_ceph_cr,
        extract_ceph_cluster_spec,
    )

    owner_refs = _kubevirt_project_owner_refs(project_cr)

    if diff.get("ceph_removed"):
        try:
            custom_api.delete_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkancephs",
                name=TROSHKA_CEPH_CR_NAME,
            )
            logger.info("Reconfigure %s: deleted TroshkaCeph", p_id[:8])
        except Exception as e:
            if "404" not in str(e) and getattr(e, "status", None) != 404:
                logger.warning(
                    "Reconfigure %s: failed to delete TroshkaCeph: %s", p_id[:8], e
                )
                errors.append(f"Failed to remove Ceph: {e}")
        return

    if not (diff.get("ceph_added") or diff.get("ceph_changed")):
        return

    spec = extract_ceph_cluster_spec(current)
    if not spec:
        errors.append("Ceph Storage node present but spec could not be resolved")
        return
    if not spec.get("networkNad"):
        errors.append("Ceph Storage network is not resolved to a Troshka network")
        return

    body = build_troshka_ceph_cr(
        namespace=ns, project_id=p_id, spec=spec, owner_refs=owner_refs
    )
    try:
        custom_api.get_namespaced_custom_object(
            group=_TROSHKA_DOMAIN,
            version=CRD_VERSION,
            namespace=ns,
            plural="troshkancephs",
            name=TROSHKA_CEPH_CR_NAME,
        )
        custom_api.patch_namespaced_custom_object(
            group=_TROSHKA_DOMAIN,
            version=CRD_VERSION,
            namespace=ns,
            plural="troshkancephs",
            name=TROSHKA_CEPH_CR_NAME,
            body={"spec": spec},
        )
        logger.info("Reconfigure %s: updated TroshkaCeph", p_id[:8])
    except Exception as e:
        if "404" not in str(e) and getattr(e, "status", None) != 404:
            logger.warning(
                "Reconfigure %s: failed to update TroshkaCeph: %s", p_id[:8], e
            )
            errors.append(f"Failed to update Ceph: {e}")
            return
        try:
            custom_api.create_namespaced_custom_object(
                group=_TROSHKA_DOMAIN,
                version=CRD_VERSION,
                namespace=ns,
                plural="troshkancephs",
                body=body,
            )
            logger.info("Reconfigure %s: created TroshkaCeph", p_id[:8])
        except Exception as create_err:
            logger.warning(
                "Reconfigure %s: failed to create TroshkaCeph: %s",
                p_id[:8],
                create_err,
            )
            errors.append(f"Failed to add Ceph: {create_err}")


def patch_kubevirt_gateway_networks(provider, project_id: str, topology: dict) -> None:
    """Refresh gateway Multus attachments + GATEWAY_ADDRS after network changes."""
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    custom_api, _, api_client = _get_k8s_clients(provider)
    ns = _project_ns(provider, project_id)
    nad_refs = _resolve_nad_refs(custom_api, ns)

    gateway_nads: list[str] = []
    gateway_addrs: list[str] = []
    for node in topology.get("nodes", []):
        data = node.get("data") or {}
        if node.get("type") != "networkNode" or data.get("subtype") == "gateway":
            continue
        if not data.get("externalAccess"):
            continue
        cr_name = f"net-{node['id'][:8]}"
        nad = nad_refs.get(cr_name, f"{cr_name}-nad")
        gateway_nads.append(nad)
        gw_ip = (
            data.get("gatewayIp")
            or data.get("gateway")
            or _gateway_ip_for_cidr(data.get("cidr", ""))
        )
        cidr = data.get("cidr", "10.0.0.0/24")
        prefix = cidr.split("/")[1] if "/" in cidr else "24"
        if gw_ip:
            gateway_addrs.append(f"{gw_ip}/{prefix}")

    if not gateway_nads:
        return

    from kubernetes import client as k8s_client

    apps_api = k8s_client.AppsV1Api(api_client)
    dep_name = f"gateway-{ns}"
    try:
        apps_api.patch_namespaced_deployment(
            name=dep_name,
            namespace=ns,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {_NET_ANNOTATION: ",".join(gateway_nads)}
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": "gateway",
                                    "env": [
                                        {
                                            "name": "GATEWAY_ADDRS",
                                            "value": ",".join(gateway_addrs),
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                }
            },
        )
        logger.info(
            "Reconfigure %s: patched gateway networks: %s",
            project_id[:8],
            ",".join(gateway_nads),
        )
    except Exception:
        logger.exception(
            "Reconfigure %s: failed to patch gateway networks (non-fatal)",
            project_id[:8],
        )


def _container_from_node(node: dict) -> dict:
    data = node.get("data") or {}
    return {
        "id": node.get("id", data.get("id", "")),
        "name": data.get("name", data.get("label", "")),
        "image": data.get("image", ""),
        "isPod": data.get("isPod", False),
        "isShowroom": data.get("isShowroom", False),
        "nics": copy.deepcopy(data.get("nics", [])),
        "initContainers": copy.deepcopy(data.get("initContainers", [])),
        "podContainers": copy.deepcopy(data.get("podContainers", [])),
        "mounts": copy.deepcopy(data.get("mounts", [])),
        "infraNetworking": data.get("infraNetworking", False),
        "dnsNameserver": data.get("dnsNameserver", ""),
        "env": data.get("envVars", {}),
        "envVars": data.get("envVars", []),
        "command": data.get("command"),
        "ports": data.get("ports", []),
    }


def _resolve_nic_networks(topology: dict) -> dict[str, str]:
    edges = topology.get("edges", [])
    nodes = {n["id"]: n for n in topology.get("nodes", [])}
    nic_map: dict[str, str] = {}
    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        src_node, tgt_node = nodes.get(src), nodes.get(tgt)
        if not src_node or not tgt_node:
            continue
        if (
            src_node.get("type") == "networkNode"
            and tgt_node.get("type") == "containerNode"
        ):
            handle = edge.get("targetHandle", "")
            nic_id = _extract_nic_id(handle)
            if nic_id:
                nic_map[nic_id] = f"net-{src[:8]}"
        elif (
            tgt_node.get("type") == "networkNode"
            and src_node.get("type") == "containerNode"
        ):
            handle = edge.get("sourceHandle", "")
            nic_id = _extract_nic_id(handle)
            if nic_id:
                nic_map[nic_id] = f"net-{tgt[:8]}"
    return nic_map


def _enrich_container_nics(topology: dict, ctr: dict) -> None:
    nic_map = _resolve_nic_networks(topology)
    net_cidrs = {
        f"net-{n['id'][:8]}": (n.get("data") or {}).get("cidr", "")
        for n in topology.get("nodes", [])
        if n.get("type") == "networkNode"
    }
    for nic in ctr.get("nics", []):
        if not nic.get("networkRef"):
            nic["networkRef"] = nic_map.get(nic.get("id", ""), "")
        ref = nic.get("networkRef", "")
        if ref in net_cidrs:
            nic["cidr"] = net_cidrs[ref]


def _lab_network_nodes(topology: dict) -> list[tuple[str, str]]:
    nets: list[tuple[str, str]] = []
    for node in topology.get("nodes", []):
        data = node.get("data") or {}
        if node.get("type") != "networkNode":
            continue
        if data.get("subtype", "") in ("gateway", "router", "loadbalancer"):
            continue
        if data.get("networkType") == "bmc":
            continue
        cidr = data.get("cidr", "")
        if cidr:
            nets.append((node.get("id", ""), cidr))
    return nets


def _network_dot2(cidr: str) -> str:
    if not cidr:
        return ""
    base = cidr.split("/")[0]
    parts = base.split(".")
    if len(parts) != 4:
        return ""
    parts[3] = "2"
    return ".".join(parts)


def _showroom_ip_for_cidr(cidr: str, used_ips: set[str]) -> str:
    candidate = _network_dot2(cidr)
    if candidate and candidate not in used_ips:
        return candidate
    return ""


def _enrich_showroom_infra_networks(topology: dict, ctr: dict) -> None:
    lab_nets = _lab_network_nodes(topology)
    if not lab_nets:
        return
    if not ctr.get("infraNetworking") and ctr.get("nics"):
        return
    used_ips: set[str] = set()
    for node in topology.get("nodes", []):
        for nic in (node.get("data") or {}).get("nics", []):
            ip = nic.get("ip", "")
            if ip:
                used_ips.add(ip)
    existing_refs = {n.get("networkRef") for n in ctr.get("nics", [])}
    new_nics = list(ctr.get("nics", []))
    for net_id, cidr in lab_nets:
        net_ref = f"net-{net_id[:8]}"
        if net_ref in existing_refs:
            continue
        ip = _showroom_ip_for_cidr(cidr, used_ips)
        if ip:
            used_ips.add(ip)
        new_nics.append(
            {
                "id": f"infra-{net_id[:8]}",
                "networkRef": net_ref,
                "ip": ip,
                "cidr": cidr,
                "model": "virtio",
            }
        )
    ctr["nics"] = new_nics
    dns_net = (ctr.get("dnsNetwork") or "").strip()
    for node in topology.get("nodes", []):
        data = node.get("data") or {}
        if node.get("type") != "networkNode":
            continue
        if dns_net and data.get("name") == dns_net:
            ctr["dnsNameserver"] = _network_dot2(data.get("cidr", ""))
            return
        if data.get("dns"):
            ctr["dnsNameserver"] = _network_dot2(data.get("cidr", ""))
            return
    if lab_nets:
        ctr["dnsNameserver"] = _network_dot2(lab_nets[0][1])


def _container_disk_pvcs(ctr: dict) -> dict[str, str]:
    ctr_id = ctr.get("id", "")
    disk_pvcs: dict[str, str] = {}
    seen: set[str] = set()
    for mount in ctr.get("mounts", []):
        disk_id = mount.get("diskNodeId", "")
        if not disk_id or disk_id in seen:
            continue
        seen.add(disk_id)
        disk_pvcs[disk_id] = f"pod-{ctr_id[:8]}-disk-{disk_id[:8]}"
    for ic in ctr.get("initContainers", []):
        for mount in ic.get("mounts", []):
            disk_id = mount.get("diskNodeId", "")
            if disk_id and disk_id not in seen:
                seen.add(disk_id)
                disk_pvcs[disk_id] = f"pod-{ctr_id[:8]}-disk-{disk_id[:8]}"
    for pc in ctr.get("podContainers", []):
        for mount in pc.get("mounts", []):
            disk_id = mount.get("diskNodeId", "")
            if disk_id and disk_id not in seen:
                seen.add(disk_id)
                disk_pvcs[disk_id] = f"pod-{ctr_id[:8]}-disk-{disk_id[:8]}"
    return disk_pvcs


def _disk_size_gb(topology: dict, disk_id: str) -> int:
    """Size (GiB) of a storage node, matching the operator's default of 20."""
    for node in (topology or {}).get("nodes", []):
        if node.get("id") == disk_id or node.get("data", {}).get("id") == disk_id:
            data = node.get("data", {})
            try:
                return int(data.get("sizeGb") or data.get("size") or 20) or 20
            except (TypeError, ValueError):
                return 20
    return 20


def _resolve_storage_class(core_api, ns: str) -> str:
    """Reuse the namespace's existing PVC storage class (avoids drift with the
    operator/cluster); fall back to the operator's default."""
    try:
        pvcs = core_api.list_namespaced_persistent_volume_claim(namespace=ns)
        for pvc in getattr(pvcs, "items", None) or []:
            sc = getattr(getattr(pvc, "spec", None), "storage_class_name", None)
            if sc:
                return sc
    except Exception:  # noqa: BLE001 - best-effort; fall back to default
        pass
    return _DEFAULT_STORAGE_CLASS


def _ensure_container_pvcs(core_api, ns: str, ctr: dict, topology: dict, owner_ref):
    """Create blank PVCs for a container's disks before its pod is (re)created.

    The reconfigure path builds the pod directly (bypassing the operator, which
    provisions PVCs on full deploy), so without this a re-added container's pod
    stays Pending on a missing PVC. Mirrors the operator's build_blank_pvc."""
    from kubernetes.client.exceptions import ApiException

    disk_pvcs = _container_disk_pvcs(ctr)
    if not disk_pvcs:
        return
    storage_class = _resolve_storage_class(core_api, ns)
    for disk_id, pvc_name in disk_pvcs.items():
        pvc_body = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {"storage": f"{_disk_size_gb(topology, disk_id)}Gi"}
                },
                "storageClassName": storage_class,
            },
        }
        if owner_ref:
            pvc_body["metadata"]["ownerReferences"] = [owner_ref]
        try:
            core_api.create_namespaced_persistent_volume_claim(
                namespace=ns, body=pvc_body
            )
            logger.info("Reconfigure %s: created container PVC %s", ns, pvc_name)
        except ApiException as e:
            if e.status != 409:  # already exists -> fine
                raise


def _env_to_list(env) -> list[dict]:
    if not env:
        return []
    if isinstance(env, list):
        return [
            {
                "name": item.get("key", item.get("name", "")),
                "value": str(item.get("value", "")),
            }
            for item in env
            if item.get("key") or item.get("name")
        ]
    return [{"name": k, "value": str(v)} for k, v in env.items()]


def _split_command(command):
    if not command:
        return {}
    if isinstance(command, list):
        first = command[0] if command else ""
        if first.startswith("-"):
            return {"args": command}
        return {"command": command}
    return {"command": ["/bin/sh", "-c", command]}


def _build_network_annotations(ctr: dict, nad_refs: dict[str, str]) -> list[str]:
    annotations: list[str] = []
    for nic in ctr.get("nics", []):
        net_ref = nic.get("networkRef", "")
        annotations.append(nad_refs.get(net_ref, f"{net_ref}-nad"))
    return annotations


def _build_setup_ip_init(ctr: dict) -> list[dict]:
    inits: list[dict] = []
    for idx, nic in enumerate(ctr.get("nics", [])):
        ip, cidr = nic.get("ip", ""), nic.get("cidr", "")
        if not ip or not cidr or "/" not in cidr:
            continue
        prefix = cidr.split("/", 1)[1]
        dev = f"net{idx + 1}"
        setup_cmd = f"ip addr add {ip}/{prefix} dev {dev} && ip link set {dev} up"
        inits.append(
            {
                "name": f"setup-ip-{idx}",
                "image": _gateway_image(),
                "imagePullPolicy": "Always",
                "command": ["sh", "-c", setup_cmd],
                "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
            }
        )
    return inits


def _create_showroom_pod(
    core_api, ns: str, ctr: dict, nad_refs: dict[str, str], owner_ref: dict
) -> None:
    from kubernetes.client.exceptions import ApiException

    ctr_id = ctr.get("id", "")[:8]
    pod_name = f"pod-{ctr_id}"
    disk_pvcs = _container_disk_pvcs(ctr)
    volumes: list[dict] = []
    volume_mounts: list[dict] = []
    seen_vols: set[str] = set()
    seen_mounts: set[tuple[str, str]] = set()

    def _add_mount(mount: dict) -> None:
        disk_id = mount.get("diskNodeId", "")
        mount_path = mount.get("mountPath", "")
        if not disk_id or not mount_path:
            return
        pvc_name = disk_pvcs.get(disk_id)
        if not pvc_name:
            return
        vol_name = f"disk-{disk_id[:8]}"
        if vol_name not in seen_vols:
            seen_vols.add(vol_name)
            volumes.append(
                {"name": vol_name, "persistentVolumeClaim": {"claimName": pvc_name}}
            )
        key = (vol_name, mount_path)
        if key in seen_mounts:
            return
        seen_mounts.add(key)
        volume_mounts.append({"name": vol_name, "mountPath": mount_path})

    for mount in ctr.get("mounts", []):
        _add_mount(mount)
    for ic in ctr.get("initContainers", []):
        for mount in ic.get("mounts", []):
            _add_mount(mount)
    for pc in ctr.get("podContainers", []):
        for mount in pc.get("mounts", []):
            _add_mount(mount)

    init_containers = _build_setup_ip_init(ctr)
    for i, ic in enumerate(ctr.get("initContainers", [])):
        spec = {
            "name": ic.get("name", f"init-{i}"),
            "image": ic.get("image", ""),
            "env": _env_to_list(ic.get("env") or ic.get("envVars")),
        }
        cmd = _split_command(ic.get("command"))
        if cmd:
            spec.update(cmd)
        if volume_mounts:
            spec["volumeMounts"] = volume_mounts
        init_containers.append(spec)

    containers = []
    for i, pc in enumerate(ctr.get("podContainers", [])):
        c_spec = {
            "name": pc.get("name", f"container-{i}"),
            "image": pc.get("image", ""),
            "env": _env_to_list(pc.get("env") or pc.get("envVars")),
        }
        cmd = _split_command(pc.get("command"))
        if cmd:
            c_spec.update(cmd)
        if pc.get("ports"):
            c_spec["ports"] = [
                {
                    "containerPort": p.get(
                        "container_port", p.get("containerPort", p.get("port", 0))
                    ),
                    "protocol": "TCP",
                }
                for p in pc["ports"]
            ]
        if pc.get("securityContext"):
            c_spec["securityContext"] = pc["securityContext"]
        if volume_mounts:
            c_spec["volumeMounts"] = volume_mounts
        containers.append(c_spec)
    if not containers:
        containers = [{"name": "main", "image": ctr.get("image", "")}]

    net_annotations = _build_network_annotations(ctr, nad_refs)
    pod_body: dict = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": ns,
            "labels": {"app": "troshka-pod", "troshka-pod": ctr_id},
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "initContainers": init_containers,
            "containers": containers,
            "restartPolicy": "Always",
            "securityContext": {"runAsUser": 0, "fsGroup": 0},
            "serviceAccountName": "troshka-network",
            "automountServiceAccountToken": False,
        },
    }
    dns_ns = str(ctr.get("dnsNameserver") or "").strip()
    if dns_ns:
        from app.services.ocp.ops_pod_scaffold import lab_pod_dns_config

        pod_body["spec"]["dnsPolicy"] = "None"
        pod_body["spec"]["dnsConfig"] = lab_pod_dns_config(dns_ns)
    if volumes:
        pod_body["spec"]["volumes"] = volumes
    if net_annotations:
        pod_body["metadata"]["annotations"] = {
            _NET_ANNOTATION: ",".join(net_annotations)
        }

    try:
        core_api.create_namespaced_pod(namespace=ns, body=pod_body)
        logger.info("Reconfigure %s: recreated showroom pod %s", ns, pod_name)
    except ApiException as e:
        if e.status != 409:
            raise


def _wait_pod_deleted(core_api, ns: str, pod_name: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            core_api.read_namespaced_pod(name=pod_name, namespace=ns)
            time.sleep(2)
        except Exception as e:
            if "404" in str(e) or getattr(e, "status", None) == 404:
                return
            time.sleep(2)


def _fetch_troshka_project_cr(custom_api, ns: str, project_id: str) -> dict:
    return custom_api.get_namespaced_custom_object(  # type: ignore[return-value]
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=ns,
        plural="troshkaprojects",
        name=f"project-{project_id[:8]}",
    )


def redeploy_container_kubevirt_bg(
    db,
    host,
    project,
    project_id: str,
    container_id: str,
    topo: dict,
) -> None:
    """Redeploy a KubeVirt pod container (showroom) via delete/recreate."""
    from app.models.provider import Provider
    from app.services.deploy_service import (
        _inject_stored_cluster_kubeconfigs,
        _kubevirt_prebake_showroom,
        _recreate_showroom_app_proxy,
        _sync_deployed_container_node,
    )
    from app.services.deploy_topology import _is_showroom_node
    from app.services.providers import get_provider_driver
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    provider = (
        db.query(Provider).filter_by(id=host.provider_id).first()
        if host.provider_id
        else None
    )
    if not provider:
        raise ValueError("No provider found for host")

    node = next(
        (n for n in topo.get("nodes", []) if n.get("id") == container_id),
        None,
    )
    if not node:
        raise ValueError("Container not found in topology")

    if _is_showroom_node(node):
        _recreate_showroom_app_proxy(db, host, project, project_id, topo)
        driver = get_provider_driver(provider)
        _kubevirt_prebake_showroom(topo, project_id, provider, driver)

    custom_api, _, _ = _get_k8s_clients(provider)
    ns = _project_ns(provider, project_id)
    project_cr = _fetch_troshka_project_cr(custom_api, ns, project_id)
    redeploy_showroom_pod_kubevirt(provider, project_id, topo, node, project_cr)
    _inject_stored_cluster_kubeconfigs(host, project_id, topo)
    _sync_deployed_container_node(project, container_id, topo)


def redeploy_showroom_pod_kubevirt(
    provider, project_id: str, topology: dict, showroom_node: dict, project_cr: dict
) -> None:
    """Delete and recreate the showroom pod (mirrors troshkad redeploy_container_bg)."""
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    custom_api, core_api, _ = _get_k8s_clients(provider)
    ns = _project_ns(provider, project_id)
    ctr = _container_from_node(showroom_node)
    _enrich_container_nics(topology, ctr)
    _enrich_showroom_infra_networks(topology, ctr)
    nad_refs = _resolve_nad_refs(custom_api, ns)
    pod_name = f"pod-{ctr['id'][:8]}"
    try:
        core_api.delete_namespaced_pod(name=pod_name, namespace=ns)
        _wait_pod_deleted(core_api, ns, pod_name)
    except Exception:
        pass
    owner_ref = (_kubevirt_project_owner_refs(project_cr) or [{}])[0]
    _ensure_container_pvcs(core_api, ns, ctr, topology, owner_ref)
    _create_showroom_pod(core_api, ns, ctr, nad_refs, owner_ref)


def reconfigure_showroom_kubevirt(
    s,
    h,
    provider,
    driver,
    p_id: str,
    current: dict,
    deployed: dict,
    vni_map: dict,
    project_cr: dict,
    errors: list[str],
) -> None:
    """Regenerate + redeploy showroom on KubeVirt when its config changed."""
    from app.api.projects import (
        _prepare_showroom_topology,
        _showroom_config_changed,
    )
    from app.services.deploy_service import (
        _inject_stored_cluster_kubeconfigs,
        _kubevirt_prebake_showroom,
        _recreate_showroom_app_proxy,
    )
    from app.services.deploy_topology import build_vms_def_from_topology
    from app.services.showroom_scaffold import (
        _find_showroom_container,
        regenerate_showroom_containers,
    )

    cur = _find_showroom_container(current)
    if not cur:
        return
    vms_def, vm_name_to_id = build_vms_def_from_topology(current)
    regenerate_showroom_containers(cur, vms_def, vm_name_to_id)
    if not _showroom_config_changed(cur, _find_showroom_container(deployed)):
        return
    try:
        _prepare_showroom_topology(h, p_id, current, cur, vni_map, s)
        _kubevirt_prebake_showroom(current, p_id, provider, driver)
        from app.models.project import Project

        proj = s.query(Project).filter_by(id=p_id).first()
        if proj:
            _recreate_showroom_app_proxy(s, h, proj, p_id, current)
        redeploy_showroom_pod_kubevirt(provider, p_id, current, cur, project_cr)
        _inject_stored_cluster_kubeconfigs(h, p_id, current)
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.exception("Reconfigure %s: kubevirt showroom redeploy failed", p_id[:8])
        errors.append(f"Showroom redeploy failed: {e}")
