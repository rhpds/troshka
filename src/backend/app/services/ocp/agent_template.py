"""
OCP Agent-Based Installer template customization.

No bootstrap VM needed — the CP nodes boot from an agent ISO and
self-assemble into a cluster. No nested virtualization, no libvirt on bastion.

Flow:
1. Bastion creates agent ISO: openshift-install agent create image
2. Bastion serves ISO via HTTP
3. CP nodes boot from ISO via Redfish virtual media (sushy-emulator)
4. Nodes discover each other, form cluster
5. openshift-install agent wait-for install-complete
"""

import ipaddress
import re
import shlex
import uuid
from dataclasses import dataclass

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_YAML_BLOCK_SCALAR = "  - |\n"
_MKDIR_OCP_INSTALL = "    mkdir -p /home/cloud-user/ocp-install/openshift\n"
_API_VIP_OFFSET = 2
_INGRESS_VIP_OFFSET = 3


@dataclass
class BastionOCPConfig:
    """OCP cluster configuration for bastion cloud-init setup."""

    cluster_name: str
    base_domain: str
    ocp_version: str
    template_id: str
    auto_install_ocp: bool
    api_vip: str
    ingress_vip: str
    bastion_bmc_ip: str
    pull_through_registry: dict | None = None


def _find_bastion_ip(topology):
    """Find the bastion's cluster network IP."""
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data", {})
        if data.get("name") == "bastion" or "bastions" in data.get("tags", {}).get(
            "AnsibleGroup", ""
        ):
            nics = data.get("nics", [])
            if nics and nics[0].get("ip"):
                return nics[0]["ip"]
    return None


def _find_cluster_cidr(topology):
    """Find the cluster network CIDR from topology."""
    default = "10.0.0.0/24"
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        if data.get("subtype") == "network" and data.get("networkType") != "bmc":
            cidr = data.get("cidr", default)
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return default
            return cidr
    return default


def _find_ocp_mount_device(node_id, edges, nodes_by_id):
    """Find the block device path for a storage node connected to an RHCOS VM."""
    for edge in edges:
        if edge.get("source") == node_id:
            vm_id = edge.get("target")
        elif edge.get("target") == node_id:
            vm_id = edge.get("source")
        else:
            continue
        vm_node = nodes_by_id.get(vm_id)
        if not vm_node or vm_node.get("type") != "vmNode":
            continue
        if vm_node.get("data", {}).get("os") != "rhcos":
            continue
        handle = edge.get("targetHandle") or edge.get("sourceHandle", "")
        dcs = vm_node.get("data", {}).get("diskControllers", [])
        disk_index = next(
            (i for i, dc in enumerate(dcs) if dc["id"] in handle),
            -1,
        )
        if disk_index >= 0:
            return f"/dev/vd{chr(ord('a') + disk_index)}"
        break
    return None


def _collect_ocp_mounts(topology):
    """Find storage nodes with ocpMount and return list of (disk_index, mount_path).

    disk_index is the 0-based position of the disk on its parent VM (vda=0, vdb=1, etc.).
    """
    mounts = []
    edges = topology.get("edges", [])
    nodes_by_id = {n["id"]: n for n in topology.get("nodes", [])}

    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        mount_path = node.get("data", {}).get("ocpMount")
        if not mount_path:
            continue
        dev = _find_ocp_mount_device(node["id"], edges, nodes_by_id)
        if dev:
            mounts.append({"device": dev, "mount": mount_path})
    return mounts


def _generate_ocp_mount_script(topology):
    """Generate shell script lines to create MachineConfig extra manifests for ocp_mount disks.

    Follows the Red Hat Solution 4952011 pattern:
    - systemd-mkfs service to format the disk
    - systemd mount unit with prjquota
    - restorecon service for SELinux contexts
    """
    import base64 as _b64

    import yaml as _yaml  # type: ignore[import-untyped]

    mounts = _collect_ocp_mounts(topology)
    if not mounts:
        return ""

    lines = "    # Create extra manifests for disk mounts\n"
    lines += _MKDIR_OCP_INSTALL
    for m in mounts:
        dev = m["device"]
        mount_path = m["mount"]
        mount_unit = mount_path.strip("/").replace("/", "-")
        dev_unit = dev.replace("/dev/", "dev-").replace("/", "-")
        mc = {
            "apiVersion": "machineconfiguration.openshift.io/v1",
            "kind": "MachineConfig",
            "metadata": {
                "labels": {"machineconfiguration.openshift.io/role": "master"},
                "name": f"98-{mount_unit}",
            },
            "spec": {
                "config": {
                    "ignition": {"version": "3.2.0"},
                    "systemd": {
                        "units": [
                            {
                                "name": f"systemd-mkfs@{dev_unit}.service",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Make File System on {dev}\n"
                                    f"DefaultDependencies=no\n"
                                    f"BindsTo={dev_unit}.device\n"
                                    f"After={dev_unit}.device var.mount\n"
                                    f"Before=systemd-fsck@{dev_unit}.service\n"
                                    f"\n"
                                    f"[Service]\n"
                                    f"Type=oneshot\n"
                                    f"RemainAfterExit=yes\n"
                                    f'ExecStart=-/bin/bash -c "/bin/rm -rf {mount_path}/*"\n'
                                    f"ExecStart=/usr/lib/systemd/systemd-makefs xfs {dev}\n"
                                    f"TimeoutSec=0\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy={mount_unit}.mount\n"
                                ),
                            },
                            {
                                "name": f"{mount_unit}.mount",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Mount {dev} to {mount_path}\n"
                                    f"Before=local-fs.target\n"
                                    f"Requires=systemd-mkfs@{dev_unit}.service\n"
                                    f"After=systemd-mkfs@{dev_unit}.service\n"
                                    f"\n"
                                    f"[Mount]\n"
                                    f"What={dev}\n"
                                    f"Where={mount_path}\n"
                                    f"Type=xfs\n"
                                    f"Options=defaults,prjquota\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy=local-fs.target\n"
                                ),
                            },
                            {
                                "name": f"restorecon-{mount_unit}.service",
                                "enabled": True,
                                "contents": (
                                    f"[Unit]\n"
                                    f"Description=Restore recursive SELinux security contexts\n"
                                    f"DefaultDependencies=no\n"
                                    f"After={mount_unit}.mount\n"
                                    f"Before=crio.service\n"
                                    f"\n"
                                    f"[Service]\n"
                                    f"Type=oneshot\n"
                                    f"RemainAfterExit=yes\n"
                                    f"ExecStart=/sbin/restorecon -R {mount_path}/\n"
                                    f"TimeoutSec=0\n"
                                    f"\n"
                                    f"[Install]\n"
                                    f"WantedBy=multi-user.target graphical.target\n"
                                ),
                            },
                        ]
                    },
                }
            },
        }
        mc_yaml = _yaml.dump(mc, default_flow_style=False)
        mc_b64 = _b64.b64encode(mc_yaml.encode()).decode()
        fname = f"98-{mount_unit}.yaml"
        lines += f"    echo '{mc_b64}' | base64 -d > /home/cloud-user/ocp-install/openshift/{fname}\n"
        lines += f"    echo 'Created MachineConfig for {mount_path} on {dev}'\n"
    return lines


def _generate_dns_manifests(topology, base_domain):
    """Generate extra manifests for disconnected registry DNS resolution.

    Two manifests:
    1. MachineConfig appending registry hostname to /etc/hosts (works before CoreDNS starts)
    2. DNS operator forwarder for the lab domain (works after CoreDNS takes over)
    """
    import base64 as _b64

    import yaml as _yaml

    cluster_cidr = _find_cluster_cidr(topology)
    net = ipaddress.ip_network(cluster_cidr, strict=False)
    gateway_ip = str(net.network_address + 1)

    dns_records = []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        for rec in node.get("data", {}).get("dnsRecords", []):
            if rec.get("ip") and rec.get("name") and not rec["name"].startswith("."):
                dns_records.append(rec)

    if not dns_records:
        return ""

    lines = "    # Create extra manifests for disconnected DNS resolution\n"
    lines += _MKDIR_OCP_INSTALL

    hosts_lines = "\\n".join(f"{rec['ip']} {rec['name']}" for rec in dns_records)
    mc = {
        "apiVersion": "machineconfiguration.openshift.io/v1",
        "kind": "MachineConfig",
        "metadata": {
            "labels": {"machineconfiguration.openshift.io/role": "master"},
            "name": "99-lab-etc-hosts",
        },
        "spec": {
            "config": {
                "ignition": {"version": "3.2.0"},
                "systemd": {
                    "units": [
                        {
                            "name": "lab-etc-hosts.service",
                            "enabled": True,
                            "contents": (
                                "[Unit]\n"
                                "Description=Add lab DNS entries to /etc/hosts\n"
                                "Before=crio.service kubelet.service\n"
                                "After=network-online.target\n"
                                "[Service]\n"
                                "Type=oneshot\n"
                                "RemainAfterExit=yes\n"
                                f"ExecStart=/bin/bash -c 'grep -q lab-etc-hosts /etc/hosts || echo -e \"{hosts_lines}  # lab-etc-hosts\" >> /etc/hosts'\n"
                                "[Install]\n"
                                "WantedBy=multi-user.target\n"
                            ),
                        }
                    ]
                },
            }
        },
    }
    mc_yaml = _yaml.dump(mc, default_flow_style=False)
    mc_b64 = _b64.b64encode(mc_yaml.encode()).decode()
    lines += f"    echo '{mc_b64}' | base64 -d > /home/cloud-user/ocp-install/openshift/99-lab-etc-hosts.yaml\n"

    dns_fwd = {
        "apiVersion": "operator.openshift.io/v1",
        "kind": "DNS",
        "metadata": {"name": "default"},
        "spec": {
            "servers": [
                {
                    "name": "lab-forward",
                    "zones": [base_domain],
                    "forwardPlugin": {"upstreams": [gateway_ip]},
                }
            ]
        },
    }
    dns_b64 = _b64.b64encode(
        _yaml.dump(dns_fwd, default_flow_style=False).encode()
    ).decode()
    lines += f"    echo '{dns_b64}' | base64 -d > /home/cloud-user/ocp-install/openshift/99-dns-forwarder.yaml\n"
    lines += f"    echo 'Created DNS manifests for {base_domain} (hosts + forwarder)'\n"
    return lines


def _cluster_is_sno(cluster, members):
    """True when the cluster is single-node (explicit type or 1 CP / 0 workers)."""
    if cluster.get("type") == "sno":
        return True
    cp = cluster.get("controlPlane")
    workers = cluster.get("workers")
    if cp is not None and workers is not None:
        return cp == 1 and workers == 0
    return False


def _cluster_control_plane_ip(members):
    """Return the first control-plane member's primary NIC IP (or None)."""
    for node in members:
        if node.get("type") != "vmNode" or _node_role(node) != "control-plane":
            continue
        nics = node.get("data", {}).get("nics", [])
        ip = nics[0].get("ip") if nics else None
        if ip:
            return ip
    return None


def _cidr_for_members(members, topology):
    """Cluster network CIDR scoped to the members' network, else topology default.

    Prefers a cluster networkNode whose CIDR contains one of the members' NIC
    IPs (so multi-cluster topologies resolve the right subnet); falls back to
    :func:`_find_cluster_cidr` when no member IP maps to a defined network.
    """
    member_ips = [
        nic.get("ip")
        for m in members
        if m.get("type") == "vmNode"
        for nic in m.get("data", {}).get("nics", [])
        if nic.get("ip")
    ]
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        if data.get("subtype") != "network" or data.get("networkType") == "bmc":
            continue
        cidr = data.get("cidr")
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if any(ipaddress.ip_address(ip) in net for ip in member_ips):
            return cidr
    return _find_cluster_cidr(topology)


def _derive_cluster_vips(cluster, members, topology):
    """Derive VIPs when not explicitly set: SNO -> node IP, else CIDR offsets."""
    if _cluster_is_sno(cluster, members):
        cp_ip = _cluster_control_plane_ip(members)
        if cp_ip:
            return cp_ip, cp_ip
    net = ipaddress.ip_network(_cidr_for_members(members, topology), strict=False)
    return (
        str(net.network_address + _API_VIP_OFFSET),
        str(net.network_address + _INGRESS_VIP_OFFSET),
    )


def resolve_cluster_vips(cluster, members, topology):
    """Resolve (api_vip, ingress_vip) for a single OCP cluster.

    Priority: explicit ``cluster["apiVip"]``/``["ingressVip"]`` when both are
    truthy; otherwise SNO clusters use the control-plane member's IP for both
    VIPs, and multi-node clusters fall back to the cluster network's
    ``network+2`` / ``network+3`` offsets. Partial explicit values are kept and
    only the missing side is derived.
    """
    api_vip = cluster.get("apiVip") or ""
    ingress_vip = cluster.get("ingressVip") or ""
    if api_vip and ingress_vip:
        return api_vip, ingress_vip
    d_api, d_ingress = _derive_cluster_vips(cluster, members, topology)
    return api_vip or d_api, ingress_vip or d_ingress


def _legacy_cluster_from_config(topology, template_id, config):
    """Build a single cluster-shaped dict for the legacy (no ``clusters``) path.

    Mirrors the pre-multicluster behavior: cluster name/domain come from the
    API/template ``config``, VIPs from the normalized ``ocp`` section, and
    replica counts from the whole topology (with the ``ocp-sno`` fallback of one
    control-plane when no controllers are tagged). Omits ``id`` so callers scope
    to the whole topology's VM nodes.
    """
    from app.services.template_loader import normalize_ocp_section

    resolved = config.get("resolved", {})
    ocp_clusters = normalize_ocp_section(resolved.get("ocp"))
    ocp_cfg = ocp_clusters[0] if ocp_clusters else {}
    cp_count = _count_ocp_nodes_by_group(topology, "controllers")
    if cp_count > 0:
        num_masters = cp_count
    elif template_id == "ocp-sno":
        num_masters = 1
    else:
        num_masters = 3
    num_workers = _count_ocp_nodes_by_group(topology, "workers")
    return {
        "name": config.get("cluster_name", "ocp"),
        "baseDomain": config.get("base_domain", "ocp.local"),
        "apiVip": ocp_cfg.get("api_vip", ""),
        "ingressVip": ocp_cfg.get("ingress_vip", ""),
        "controlPlane": num_masters,
        "workers": num_workers,
        "type": "sno" if (num_masters == 1 and num_workers == 0) else None,
        "pullThroughRegistry": resolved.get("pull_through_registry"),
    }


def _cluster_members_for(topology, cluster):
    """Return a cluster's member VM nodes.

    Uses ``data.clusterId`` scoping when the cluster carries an ``id``
    (multi-cluster); otherwise falls back to every VM node in the topology
    (legacy single-cluster).
    """
    cluster_id = cluster.get("id")
    if cluster_id:
        return cluster_member_nodes(topology, cluster_id)
    return [n for n in topology.get("nodes", []) if n.get("type") == "vmNode"]


def _customize_one_cluster(topology, cluster, config, include_extras):
    """Resolve VIPs, build+store configs, and write DNS for a single cluster.

    Stores the generated ``install-config``/``agent-config`` on the cluster
    object (``_generatedInstallConfig``/``_generatedAgentConfig``) for Plan 4's
    ops pod, and writes ``api``/``api-int``/``*.apps`` records to the cluster's
    own network node. ``include_extras`` gates the resolved lab ``dns_records``
    so they are added once (not duplicated across every cluster's network).
    Returns the resolved ``(api_vip, ingress_vip)``.
    """
    resolved = config.get("resolved", {})
    members = _cluster_members_for(topology, cluster)
    api_vip, ingress_vip = resolve_cluster_vips(cluster, members, topology)
    ptr = cluster.get("pullThroughRegistry") or resolved.get("pull_through_registry")
    cluster["_generatedInstallConfig"] = _build_install_config(
        cluster,
        members,
        topology,
        config.get("pull_secret_json", ""),
        config.get("ssh_pub_key", ""),
        pull_through_registry=ptr,
    )
    cluster["_generatedAgentConfig"] = _build_agent_config(cluster, members, topology)
    _setup_dns_records(
        topology,
        cluster.get("name", "ocp"),
        cluster.get("baseDomain", "ocp.local"),
        api_vip,
        ingress_vip,
        resolved if include_extras else {},
        members=members,
    )
    return api_vip, ingress_vip


def _bake_single_cluster_bastion(topology, config, template_id, api_vip, ingress_vip):
    """Bake the bastion cloud-init for the single-cluster case (unchanged bake).

    Delegates to :func:`_setup_bastion_cloud_init` with the exact same inputs as
    the pre-multicluster path, so single-cluster deploys stay byte-for-byte
    identical.
    """
    resolved = config.get("resolved", {})
    _setup_bastion_cloud_init(
        topology,
        config.get("common_password", ""),
        config.get("ssh_pub_key", ""),
        config.get("ssh_key_ids", []),
        config.get("ssh_keys", []),
        config.get("bastion_iso"),
        config.get("pull_secret_json", ""),
        BastionOCPConfig(
            cluster_name=config.get("cluster_name", "ocp"),
            base_domain=config.get("base_domain", "ocp.local"),
            ocp_version=config.get("ocp_version", "4.20"),
            template_id=template_id,
            auto_install_ocp=config.get("auto_install_ocp", True),
            api_vip=api_vip,
            ingress_vip=ingress_vip,
            bastion_bmc_ip=config.get("bastion_bmc_ip", "192.168.100.50"),
            pull_through_registry=resolved.get("pull_through_registry"),
        ),
    )


def customize_topology(topology: dict, template_id: str, config: dict) -> dict:
    """Apply OCP Agent-Based configuration to a base topology.

    Iterates ``topology["clusters"]`` (falling back to a one-element legacy
    cluster when absent), resolving per-cluster VIPs, install-config,
    agent-config, and DNS records. Single-cluster topologies still bake the
    bastion cloud-init exactly as before; multi-cluster topologies leave the
    per-cluster generated configs on each cluster object for Plan 4's ops pod.
    """
    if not config.get("common_password"):
        raise ValueError(
            "No password provided. Set common_password in the API request "
            "or in the template YAML."
        )

    # Make every cluster member (backend- OR canvas-created) deploy-ready before
    # counts, BMC host entries, and rendezvous selection read their fields.
    from app.services.template_loader import normalize_cluster_member_fields

    normalize_cluster_member_fields(topology)

    clusters = topology.get("clusters") or [
        _legacy_cluster_from_config(topology, template_id, config)
    ]

    last_vips = ("", "")
    for i, cluster in enumerate(clusters):
        last_vips = _customize_one_cluster(
            topology, cluster, config, include_extras=(i == 0)
        )

    _attach_bastion_image(topology, config.get("bastion_image"))
    _attach_bastion_iso(topology, config.get("bastion_iso"))

    if len(clusters) == 1:
        api_vip, ingress_vip = last_vips
        _bake_single_cluster_bastion(
            topology, config, template_id, api_vip, ingress_vip
        )
    # else: Plan 4: ops pod consumes per-cluster _generated* configs
    # (no single-bastion bake for multi-cluster; DNS + port-forwards still apply
    # to every cluster above).

    return topology


def _build_ocp_dns_records(
    cluster_name,
    base_domain,
    api_vip,
    ingress_vip,
    topology,
    resolved,
):
    """Build the full list of OCP DNS records including extras from resolved config."""
    records = [
        {"name": f"api.{cluster_name}.{base_domain}", "ip": api_vip},
        {"name": f"api-int.{cluster_name}.{base_domain}", "ip": api_vip},
        {"name": f".apps.{cluster_name}.{base_domain}", "ip": ingress_vip},
    ]
    bastion_ip = _find_bastion_ip(topology)
    for extra in resolved.get("dns_records", []):
        target = extra.get("target", "")
        ip = extra.get("ip", "")
        if target == "bastion" and bastion_ip:
            ip = bastion_ip
        if ip:
            records.append({"name": extra["name"], "ip": ip})
    return records


def _cluster_network_node(topology, members):
    """Return the lab network node a cluster's DNS records belong on.

    Prefers the cluster networkNode whose CIDR contains one of ``members``' NIC
    IPs (so multi-cluster topologies write each cluster's records to its own
    network); falls back to the first eligible network node — matching the
    pre-multicluster single-cluster behavior when ``members`` is empty.
    """
    member_ips = [
        nic.get("ip")
        for m in members
        if m.get("type") == "vmNode"
        for nic in m.get("data", {}).get("nics", [])
        if nic.get("ip")
    ]
    first = None
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.get("data", {})
        if data.get("subtype") != "network" or data.get("networkType") == "bmc":
            continue
        if first is None:
            first = node
        cidr = data.get("cidr")
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if any(ipaddress.ip_address(ip) in net for ip in member_ips):
            return node
    return first


def _setup_dns_records(
    topology,
    cluster_name,
    base_domain,
    api_vip,
    ingress_vip,
    resolved=None,
    members=None,
):
    """Write a cluster's api/api-int/apps DNS records to its network node.

    ``members`` scopes which network node receives the records (see
    :func:`_cluster_network_node`); when ``None``/empty the first eligible
    network node is used (single-cluster back-compat). All clusters' records
    coexist because each writes to its own network node.
    """
    resolved = resolved or {}
    node = _cluster_network_node(topology, members or [])
    if not node:
        return
    node["data"]["dns"] = True
    node["data"]["dnsDomain"] = base_domain
    new_records = _build_ocp_dns_records(
        cluster_name,
        base_domain,
        api_vip,
        ingress_vip,
        topology,
        resolved,
    )
    node["data"]["dnsRecords"] = _merge_dns_records(
        node["data"].get("dnsRecords"), new_records
    )


def _merge_dns_records(existing, new_records):
    """Merge ``new_records`` into ``existing``, deduped by record ``name``.

    Records for different cluster names all survive when several clusters share
    one network node; same-name records are last-writer-wins. Order is stable:
    existing records keep their position, new names append in order.
    """
    merged = list(existing or [])
    by_name = {r.get("name"): i for i, r in enumerate(merged) if r.get("name")}
    for rec in new_records:
        name = rec.get("name")
        if name in by_name:
            merged[by_name[name]] = rec
        else:
            by_name[name] = len(merged)
            merged.append(rec)
    return merged


def _attach_bastion_image(topology, bastion_image):
    """Reuse from IPI template."""
    if not bastion_image:
        return
    for node in topology.get("nodes", []):
        if node.get("type") == "storageNode" and (
            node.get("data", {}).get("name") in ("bastion-disk", "bastion-disk0")
        ):
            node["data"]["source"] = "library"
            node["data"]["libraryItemId"] = bastion_image["id"]
            node["data"]["libraryItemName"] = bastion_image["name"]
            node["data"]["libraryItemSize"] = bastion_image["size_gb"]
            node["data"]["size"] = max(
                bastion_image["size_gb"], node["data"].get("size", 0)
            )
            break


def _attach_bastion_iso(topology, bastion_iso):
    """Reuse from IPI template."""
    if not bastion_iso:
        return
    bastion_vm = None
    for node in topology.get("nodes", []):
        if (
            node.get("type") == "vmNode"
            and node.get("data", {}).get("name") == "bastion"
        ):
            bastion_vm = node
            break
    if not bastion_vm:
        return

    iso_node_id = str(uuid.uuid4())
    bast_x = bastion_vm["position"]["x"]
    bast_y = bastion_vm["position"]["y"]
    dc2 = {"id": f"dp-{str(uuid.uuid4())}", "name": "cdrom0", "bus": "sata"}
    bastion_vm["data"]["diskControllers"].append(dc2)

    iso_node = {
        "id": iso_node_id,
        "type": "storageNode",
        "position": {"x": bast_x - 190, "y": bast_y + 170},
        "data": {
            "label": "rhel-dvd",
            "name": "rhel-dvd",
            "size": (
                bastion_iso["size_bytes"] // (1024**3)
                if bastion_iso.get("size_bytes")
                else 10
            ),
            "format": "iso",
            "icon": "\U0001f4bf",
            "source": "library",
            "libraryItemId": bastion_iso["id"],
            "libraryItemName": bastion_iso["name"],
        },
    }
    iso_edge = {
        "id": str(uuid.uuid4()),
        "source": iso_node_id,
        "target": bastion_vm["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc2['id']}-left",
        "type": "smoothstep",
        "style": {
            "stroke": "rgba(251,191,36,0.6)",
            "strokeWidth": 2,
            "strokeDasharray": "4 4",
        },
        "animated": False,
        "className": "edge-storage-pulse",
    }
    topology["nodes"].append(iso_node)
    topology["edges"].append(iso_edge)


def _setup_bastion_no_auto_install(
    node, password, ssh_key_ids, ssh_keys, pull_through_registry
):
    """Configure bastion cloud-init when auto_install_ocp is false."""
    node["data"]["cloudInit"] = True
    cloud_user_pw = node["data"].get("ciCloudUserPassword") or password
    if cloud_user_pw:
        node["data"]["ciCloudUserPassword"] = cloud_user_pw
    if ssh_key_ids:
        node["data"]["ciSshKeyIds"] = ssh_key_ids
    if ssh_keys:
        node["data"]["ciSshKeys"] = ssh_keys

    if pull_through_registry and pull_through_registry.get("enabled"):
        if "ciUserData" not in node["data"]:
            node["data"]["ciUserData"] = "runcmd:\n"
        node["data"]["ciUserData"] += _generate_ptr_registries_conf(
            pull_through_registry
        )


def _generate_ptr_registries_conf(pull_through_registry, guard=""):
    """Generate pull-through registry config script for containers/registries.conf.d."""
    ptr_url = pull_through_registry["url"]
    result = (
        _YAML_BLOCK_SCALAR
        + guard
        + "    mkdir -p /etc/containers/registries.conf.d\n"
        + "    cat > /etc/containers/registries.conf.d/rhdp-cache.conf << 'EOF'\n"
    )
    for source, org in pull_through_registry.get("orgs", {}).items():
        result += (
            f"    [[registry]]\n"
            f'      prefix = "{source}"\n'
            f'      location = "{ptr_url}/{org}"\n'
            f"\n"
        )
    result += "    EOF\n"
    return result


def _collect_bmc_ips_and_password(topology, password):
    """Collect BMC IPs for hub cluster VMs and read BMC password from BMC network."""
    bmc_ips = []
    for tnode in topology.get("nodes", []):
        td = tnode.get("data", {})
        group = td.get("tags", {}).get("AnsibleGroup", "")
        if (
            tnode.get("type") == "vmNode"
            and td.get("bmcEnabled")
            and td.get("bmcIp")
            and group in ("controllers", "workers")
        ):
            ip = str(ipaddress.IPv4Address(td["bmcIp"]))
            bmc_ips.append(ip)

    bmc_pw = password
    for tnode in topology.get("nodes", []):
        td = tnode.get("data", {})
        if td.get("networkType") == "bmc" and td.get("bmcPassword"):
            bmc_pw = td["bmcPassword"]
            break
    return " ".join(bmc_ips), bmc_pw


def _write_ocp_config_files(node, guard, install_config, agent_config):
    """Write install-config.yaml and agent-config.yaml to bastion cloud-init."""
    if install_config:
        indented_ic = "\n".join("    " + line for line in install_config.split("\n"))
        node["data"]["ciUserData"] += (
            _YAML_BLOCK_SCALAR
            + guard
            + "    mkdir -p /home/cloud-user/ocp-install\n"
            + "    cat > /home/cloud-user/ocp-install/install-config.yaml << 'ICEOF'\n"
            f"{indented_ic}\n"
            "    ICEOF\n"
            "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
        )

    if agent_config:
        indented_ac = "\n".join("    " + line for line in agent_config.split("\n"))
        node["data"]["ciUserData"] += (
            _YAML_BLOCK_SCALAR
            + guard
            + "    cat > /home/cloud-user/ocp-install/agent-config.yaml << 'ACEOF'\n"
            f"{indented_ac}\n"
            "    ACEOF\n"
            "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install\n"
        )


def _write_itms_manifest(node, guard, pull_through_registry):
    """Write ImageTagMirrorSet extra manifest for pull-through registry."""
    if not pull_through_registry or not pull_through_registry.get("enabled"):
        return
    ptr_url = pull_through_registry["url"]
    itms_yaml = (
        "apiVersion: config.openshift.io/v1\n"
        "kind: ImageTagMirrorSet\n"
        "metadata:\n"
        "  name: pull-through-registry-tags\n"
        "spec:\n"
        "  imageTagMirrors:\n"
    )
    for source, org in pull_through_registry.get("orgs", {}).items():
        itms_yaml += (
            f"    - source: {source}\n"
            f"      mirrors:\n"
            f"        - {ptr_url}/{org}\n"
        )
    indented_itms = "\n".join("    " + line for line in itms_yaml.split("\n"))
    node["data"]["ciUserData"] += (
        _YAML_BLOCK_SCALAR
        + guard
        + _MKDIR_OCP_INSTALL
        + "    cat > /home/cloud-user/ocp-install/openshift/itms-pull-through.yaml << 'ITMSEOF'\n"
        f"{indented_itms}\n"
        "    ITMSEOF\n"
        "    chown -R cloud-user:cloud-user /home/cloud-user/ocp-install/openshift\n"
    )


def _setup_bastion_auto_install(
    node,
    topology,
    password,
    ssh_pub_key,
    ssh_key_ids,
    ssh_keys,
    bastion_iso,
    pull_secret_json,
    ocp_config: BastionOCPConfig,
):
    node["data"]["cloudInit"] = True
    node["data"]["ciPackages"] = [
        "git",
        "ansible-core",
        "python3-pip",
        "bind-utils",
        "nmstate",
        "@Server with GUI",
        "firefox",
        "ptyxis",
        "gnome-shell-extension-dash-to-dock",
        "google-noto-sans-fonts",
        "google-noto-sans-mono-fonts",
        "dejavu-sans-fonts",
        "desktop-backgrounds-gnome",
    ]
    cloud_user_pw = node["data"].get("ciCloudUserPassword") or password
    if cloud_user_pw:
        node["data"]["ciCloudUserPassword"] = cloud_user_pw
    if ssh_key_ids:
        node["data"]["ciSshKeyIds"] = ssh_key_ids
    if ssh_keys:
        node["data"]["ciSshKeys"] = ssh_keys

    if bastion_iso:
        node["data"]["ciUserData"] = (
            "mounts:\n"
            '  - [/dev/sr0, /mnt/rhel-dvd, iso9660, "ro,nofail", "0", "0"]\n'
            "yum_repos:\n"
            "  rhel-dvd-baseos:\n"
            "    name: RHEL DVD BaseOS\n"
            "    baseurl: file:///mnt/rhel-dvd/BaseOS\n"
            "    enabled: true\n"
            "    gpgcheck: false\n"
            "  rhel-dvd-appstream:\n"
            "    name: RHEL DVD AppStream\n"
            "    baseurl: file:///mnt/rhel-dvd/AppStream\n"
            "    enabled: true\n"
            "    gpgcheck: false\n"
            "runcmd:\n"
            "  - nmcli con up cluster-nic 2>/dev/null || true\n"
        )

    if "ciUserData" not in node["data"]:
        node["data"]["ciUserData"] = "runcmd:\n"

    _guard = "    [ -f /home/cloud-user/ocp-install/auth/kubeconfig ] && exit 0\n"

    if pull_secret_json:
        node["data"]["ciUserData"] += (
            _YAML_BLOCK_SCALAR
            + _guard
            + "    cat > /home/cloud-user/pull-secret.json << 'PULLSECRETEOF'\n"
            f"    {pull_secret_json}\n"
            "    PULLSECRETEOF\n"
            "    chown cloud-user:cloud-user /home/cloud-user/pull-secret.json\n"
            "    chmod 600 /home/cloud-user/pull-secret.json\n"
        )

    if ocp_config.pull_through_registry and ocp_config.pull_through_registry.get(
        "enabled"
    ):
        node["data"]["ciUserData"] += _generate_ptr_registries_conf(
            ocp_config.pull_through_registry,
            _guard,
        )

    install_config = _build_install_config_legacy(
        topology,
        ocp_config.template_id,
        ocp_config.cluster_name,
        ocp_config.base_domain,
        ocp_config.api_vip,
        ocp_config.ingress_vip,
        password,
        pull_secret_json,
        ssh_pub_key,
        pull_through_registry=ocp_config.pull_through_registry,
    )
    agent_config = _build_agent_config_legacy(
        topology,
        ocp_config.cluster_name,
        ocp_config.base_domain,
        ocp_config.api_vip,
        ocp_config.ingress_vip,
    )

    bmc_ips_str, bmc_pw = _collect_bmc_ips_and_password(topology, password)

    node["data"]["ciUserData"] += _build_install_script(
        ocp_config.ocp_version,
        ocp_config.auto_install_ocp,
        bmc_pw,
        bmc_ips_str,
        ocp_config.cluster_name,
        ocp_config.base_domain,
        topology=topology,
    )

    _write_ocp_config_files(node, _guard, install_config, agent_config)
    _write_itms_manifest(node, _guard, ocp_config.pull_through_registry)

    node["data"][
        "ciUserData"
    ] += "  - sudo -u cloud-user nohup /home/cloud-user/install-ocp.sh > /home/cloud-user/install.log 2>&1 &\n"

    node["data"]["ciUserData"] += (
        _YAML_BLOCK_SCALAR
        + _guard
        + "    cat > /root/setup-desktop.sh << 'DESKTOPEOF'\n"
        + "    #!/bin/bash\n"
        "    set -x\n"
        "    dnf remove -y gnome-initial-setup gnome-software gnome-tour subscription-manager-cockpit 2>/dev/null\n"
        "    sed -i 's|^ExecStart=.*gsd-subman|#ExecStart=/usr/libexec/gsd-subman|' /lib/systemd/user/org.gnome.SettingsDaemon.Subscription.service 2>/dev/null\n"
        "    systemctl disable --now rhsmcertd 2>/dev/null\n"
        "    systemctl mask rhsmcertd 2>/dev/null\n"
        "    mkdir -p /etc/skel/.config\n"
        "    echo yes > /etc/skel/.config/gnome-initial-setup-done\n"
        "    for u in root cloud-user; do\n"
        "      d=$(eval echo ~$u)\n"
        "      mkdir -p $d/.config\n"
        "      echo yes > $d/.config/gnome-initial-setup-done\n"
        "      chown -R $u:$u $d/.config\n"
        "    done\n"
        "    if rpm -q ptyxis >/dev/null 2>&1; then\n"
        "      TERM_APP=org.gnome.Ptyxis.desktop\n"
        "    else\n"
        "      TERM_APP=org.gnome.Terminal.desktop\n"
        "    fi\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/favorite-apps \"['$TERM_APP', 'firefox.desktop']\"\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/overlay-scrolling false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/screensaver/lock-enabled false\n"
        '    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/session/idle-delay "uint32 0"\n'
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type \"'nothing'\"\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/settings-daemon/plugins/power/idle-dim false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/color-scheme \"'prefer-dark'\"\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/interface/gtk-theme \"'Adwaita-dark'\"\n"
        "    sudo -u cloud-user dbus-run-session gnome-extensions enable dash-to-dock@micxgx.gmail.com 2>/dev/null\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/dock-fixed false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/autohide true\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/intellihide true\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/show-trash false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/shell/extensions/dash-to-dock/show-mounts false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/mutter/dynamic-workspaces false\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/wm/preferences/num-workspaces 1\n"
        "    sudo -u cloud-user dbus-run-session dconf write /org/gnome/desktop/wm/preferences/button-layout \"'appmenu:minimize,maximize,close'\"\n"
        "    # Ptyxis terminal: Hurtado palette (dark theme with readable Ansible output)\n"
        "    if rpm -q ptyxis >/dev/null 2>&1; then\n"
        '      PROFILE_UUID=$(sudo -u cloud-user dbus-run-session dconf read /org/gnome/Ptyxis/default-profile-uuid 2>/dev/null | tr -d "\'")\n'
        '      if [ -z "$PROFILE_UUID" ]; then\n'
        "        PROFILE_UUID=$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 32)\n"
        "        sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/default-profile-uuid \"'$PROFILE_UUID'\"\n"
        "        sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/profile-uuids \"['$PROFILE_UUID']\"\n"
        "      fi\n"
        "      sudo -u cloud-user dbus-run-session dconf write /org/gnome/Ptyxis/Profiles/$PROFILE_UUID/palette \"'Hurtado'\"\n"
        "    fi\n"
        "    MONITORS_XML='<monitors version=\"2\"><configuration><logicalmonitor><x>0</x><y>0</y><scale>1</scale><primary>yes</primary><monitor><monitorspec><connector>Virtual-1</connector><vendor>unknown</vendor><product>unknown</product><serial>unknown</serial></monitorspec><mode><width>1920</width><height>1080</height><rate>60</rate></mode></monitor></logicalmonitor></configuration></monitors>'\n"
        "    for u in root cloud-user; do\n"
        "      d=$(eval echo ~$u)\n"
        "      mkdir -p $d/.config\n"
        '      echo "$MONITORS_XML" > $d/.config/monitors.xml\n'
        "      chown -R $u:$u $d/.config\n"
        "    done\n"
        "    mkdir -p /var/lib/gdm/.config\n"
        '    echo "$MONITORS_XML" > /var/lib/gdm/.config/monitors.xml\n'
        "    chown -R gdm:gdm /var/lib/gdm/.config\n"
        "    sed -i '/^\\[daemon\\]/a AutomaticLoginEnable=True' /etc/gdm/custom.conf\n"
        "    sed -i '/^AutomaticLoginEnable/a AutomaticLogin=cloud-user' /etc/gdm/custom.conf\n"
        "    grep -q KUBECONFIG /home/cloud-user/.bashrc || echo 'export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig' >> /home/cloud-user/.bashrc\n"
        "    systemctl set-default graphical.target\n"
        "    systemctl isolate graphical.target\n"
        "    DESKTOPEOF\n"
        "    chmod 755 /root/setup-desktop.sh\n"
        "    [ -f /var/log/desktop-install.log ] || nohup /root/setup-desktop.sh > /var/log/desktop-install.log 2>&1 &\n"
    )

    console_url = f"https://console-openshift-console.apps.{ocp_config.cluster_name}.{ocp_config.base_domain}"
    node["data"]["ciUserData"] += (
        _YAML_BLOCK_SCALAR
        + _guard
        + "    mkdir -p /etc/firefox/policies\n"
        + "    cat > /etc/firefox/policies/policies.json << 'FPEOF'\n"
        "    {\n"
        '      "policies": {\n'
        f'        "Homepage": {{"URL": "{console_url}", "Locked": true, "StartPage": "homepage"}},\n'
        '        "OverrideFirstRunPage": "",\n'
        '        "OverridePostUpdatePage": "",\n'
        '        "UserMessaging": {"WhatsNew": false, "ExtensionRecommendations": false, "FeatureRecommendations": false, "UrlbarInterventions": false, "SkipOnboarding": true, "MoreFromMozilla": false},\n'
        '        "DisableTelemetry": true,\n'
        '        "Certificates": {"ImportEnterpriseRoots": true},\n'
        '        "NoDefaultBookmarks": true,\n'
        '        "DontCheckDefaultBrowser": true,\n'
        '        "DisableAppUpdate": true\n'
        "      }\n"
        "    }\n"
        "    FPEOF\n"
    )
    node["data"]["ciUserData"] += (
        _YAML_BLOCK_SCALAR
        + _guard
        + "    FIREFOX_DIR=$(find /usr/lib64/firefox /usr/lib/firefox -maxdepth 0 2>/dev/null | head -1)\n"
        + '    if [ -n "$FIREFOX_DIR" ]; then\n'
        "      mkdir -p $FIREFOX_DIR/defaults/pref\n"
        "      cat > $FIREFOX_DIR/defaults/pref/autoconfig.js << 'ACEOF'\n"
        '    pref("browser.sessionstore.resume_from_crash", false);\n'
        '    pref("browser.shell.checkDefaultBrowser", false);\n'
        '    pref("browser.startup.homepage_override.mstone", "ignore");\n'
        '    pref("browser.disableResetPrompt", true);\n'
        '    pref("browser.slowStartup.notificationDisabled", true);\n'
        '    pref("browser.laterrun.enabled", false);\n'
        "    ACEOF\n"
        "    fi\n"
    )

    bmc_ip = str(ipaddress.IPv4Address(ocp_config.bastion_bmc_ip))
    nics = node["data"].get("nics", [])
    cluster_mac = nics[0]["mac"] if len(nics) > 0 else ""
    bmc_mac = nics[1]["mac"] if len(nics) > 1 else ""
    node["data"]["ciNetworkConfig"] = (
        "version: 2\n"
        "ethernets:\n"
        "  cluster-nic:\n"
        f"    match:\n"
        f'      macaddress: "{cluster_mac}"\n'
        "    dhcp4: true\n"
        "  bmc-nic:\n"
        f"    match:\n"
        f'      macaddress: "{bmc_mac}"\n'
        "    addresses:\n"
        f"      - {bmc_ip}/24\n"
    )


def _setup_bastion_cloud_init(
    topology,
    password,
    ssh_pub_key,
    ssh_key_ids,
    ssh_keys,
    bastion_iso,
    pull_secret_json,
    ocp_config: BastionOCPConfig,
):
    for node in topology.get("nodes", []):
        if (
            node.get("type") != "vmNode"
            or node.get("data", {}).get("name") != "bastion"
        ):
            continue

        if not ocp_config.auto_install_ocp:
            _setup_bastion_no_auto_install(
                node,
                password,
                ssh_key_ids,
                ssh_keys,
                ocp_config.pull_through_registry,
            )
            break

        _setup_bastion_auto_install(
            node,
            topology,
            password,
            ssh_pub_key,
            ssh_key_ids,
            ssh_keys,
            bastion_iso,
            pull_secret_json,
            ocp_config,
        )
        break


def _node_role(node):
    """Resolve a VM node's OCP cluster role ("control-plane"/"worker"/None).

    Mirrors the frontend ``memberRole``: an explicit ``data.clusterRole`` wins;
    otherwise fall back to ``data.tags.AnsibleGroup`` ("controllers" ->
    control-plane, "workers" -> worker). Returns ``None`` when neither applies
    (e.g. an AnsibleGroup of "bastions,showroom" is not a cluster member).
    """
    data = node.get("data", {})
    role = data.get("clusterRole")
    if role in ("control-plane", "worker"):
        return role
    group = data.get("tags", {}).get("AnsibleGroup")
    if isinstance(group, str):
        if "controllers" in group:
            return "control-plane"
        if "workers" in group:
            return "worker"
    return None


def cluster_member_nodes(topology, cluster_id):
    """Return the VM nodes belonging to ``cluster_id`` (``data.clusterId`` match)."""
    return [
        n
        for n in topology.get("nodes", [])
        if n.get("type") == "vmNode"
        and n.get("data", {}).get("clusterId") == cluster_id
    ]


_GROUP_TO_ROLE = {"controllers": "control-plane", "workers": "worker"}


def _count_ocp_nodes_by_group(topology, group_name, cluster_id=None):
    """Count VM nodes matching a role group ("controllers"/"workers").

    Role is resolved via :func:`_node_role` (clusterRole or AnsibleGroup). When
    ``cluster_id`` is given, only that cluster's members (``data.clusterId``)
    are counted; when ``None``, the whole topology is counted (back-compat with
    single-cluster callers).
    """
    target_role = _GROUP_TO_ROLE.get(group_name)
    if cluster_id is not None:
        nodes = cluster_member_nodes(topology, cluster_id)
    else:
        nodes = [n for n in topology.get("nodes", []) if n.get("type") == "vmNode"]
    return sum(1 for n in nodes if _node_role(n) == target_role)


def _collect_bmc_host_entries(members):
    """Collect baremetal host entries for install-config.yaml.

    ``members`` is the list of VM nodes belonging to a single cluster (see
    :func:`cluster_member_nodes`), so hosts never leak across clusters.
    """
    entries = []
    for node in members:
        if node.get("type") != "vmNode":
            continue
        td = node.get("data", {})
        if not td.get("bmcEnabled") or not td.get("bmcIp"):
            continue
        group = td.get("tags", {}).get("AnsibleGroup", "")
        if group not in ("controllers", "workers"):
            continue
        vm_name = td.get("name", "")
        boot_mac = td.get("nics", [{}])[0].get("mac", "")
        if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
            continue
        role = "master" if group == "controllers" else "worker"
        entries.extend(
            [
                f"      - name: {vm_name}",
                f"        role: {role}",
                f"        bootMACAddress: {boot_mac}",
            ]
        )
    return entries


def _cluster_replicas(cluster, topology):
    """Resolve (control_plane, worker) replica counts for a cluster.

    Explicit ``cluster["controlPlane"]``/``["workers"]`` win; otherwise the
    counts are derived from the cluster's member nodes (``data.clusterId``)
    via :func:`_count_ocp_nodes_by_group`.
    """
    cluster_id = cluster.get("id")
    cp = cluster.get("controlPlane")
    if cp is None:
        cp = _count_ocp_nodes_by_group(topology, "controllers", cluster_id=cluster_id)
    workers = cluster.get("workers")
    if workers is None:
        workers = _count_ocp_nodes_by_group(topology, "workers", cluster_id=cluster_id)
    return cp, workers


def _append_pull_through_digest_sources(ic_lines, pull_through_registry):
    """Append imageDigestSources entries for a pull-through registry (if enabled)."""
    if not pull_through_registry or not pull_through_registry.get("enabled"):
        return
    ptr_url = pull_through_registry["url"]
    ic_lines.append("imageDigestSources:")
    for source, org in pull_through_registry.get("orgs", {}).items():
        ic_lines.extend(
            [
                "- mirrors:",
                f"  - {ptr_url}/{org}",
                f"  source: {source}",
            ]
        )


def _build_install_config(
    cluster,
    members,
    topology,
    pull_secret,
    ssh_key,
    pull_through_registry=None,
):
    """Build a single cluster's ``install-config.yaml``.

    ``cluster`` is a cluster-shaped dict (``id``/``name``/``baseDomain`` and
    optional explicit ``controlPlane``/``workers``/``apiVip``/``ingressVip``),
    ``members`` are its VM nodes (see :func:`cluster_member_nodes`), and
    ``topology`` is the full topology used to resolve the cluster's network.
    SNO clusters emit ``platform: none``; everything else emits baremetal with
    the cluster's VIPs and BMC hosts scoped to ``members``.
    """
    cluster_name = cluster.get("name", "ocp")
    base_domain = cluster.get("baseDomain", "ocp.local")
    num_masters, num_workers = _cluster_replicas(cluster, topology)
    api_vip, ingress_vip = resolve_cluster_vips(cluster, members, topology)

    ic_lines = [
        "apiVersion: v1",
        f"baseDomain: {base_domain}",
        "metadata:",
        f"  name: {cluster_name}",
        "compute:",
        "  - name: worker",
        f"    replicas: {num_workers}",
        "    architecture: amd64",
        "controlPlane:",
        "  name: master",
        f"  replicas: {num_masters}",
        "  architecture: amd64",
        "networking:",
        "  networkType: OVNKubernetes",
        "  clusterNetwork:",
        "    - cidr: 10.128.0.0/14",
        "      hostPrefix: 23",
        "  serviceNetwork:",
        "    - 172.30.0.0/16",
        "  machineNetwork:",
        f"    - cidr: {_cidr_for_members(members, topology)}",
    ]
    if _cluster_is_sno(cluster, members):
        ic_lines.extend(["platform:", "  none: {}"])
    else:
        ic_lines.extend(
            [
                "platform:",
                "  baremetal:",
                "    apiVIPs:",
                f"      - {api_vip}",
                "    ingressVIPs:",
                f"      - {ingress_vip}",
                "    hosts:",
            ]
        )
        ic_lines.extend(_collect_bmc_host_entries(members))

    if pull_secret:
        ic_lines.append(f"pullSecret: '{pull_secret}'")
    if ssh_key:
        ic_lines.append(f"sshKey: '{ssh_key}'")

    _append_pull_through_digest_sources(ic_lines, pull_through_registry)

    return "\n".join(ic_lines)


def _build_install_config_legacy(
    topology,
    template_id,
    cluster_name,
    base_domain,
    api_vip,
    ingress_vip,
    _password,
    pull_secret_json,
    ssh_pub_key,
    pull_through_registry=None,
):
    """Single-cluster back-compat wrapper for :func:`_build_install_config`.

    Reconstructs a cluster-shaped dict — replica counts from the whole topology
    plus the legacy ``ocp-sno`` fallback (num_masters=1 when no controllers are
    present) — and whole-topology members, so single-cluster callers get
    byte-identical output to the pre-multicluster implementation.
    """
    cp_count = _count_ocp_nodes_by_group(topology, "controllers")
    if cp_count > 0:
        num_masters = cp_count
    elif template_id == "ocp-sno":
        num_masters = 1
    else:
        num_masters = 3
    cluster = {
        "name": cluster_name,
        "baseDomain": base_domain,
        "apiVip": api_vip,
        "ingressVip": ingress_vip,
        "controlPlane": num_masters,
        "workers": _count_ocp_nodes_by_group(topology, "workers"),
    }
    members = [n for n in topology.get("nodes", []) if n.get("type") == "vmNode"]
    return _build_install_config(
        cluster,
        members,
        topology,
        pull_secret_json,
        ssh_pub_key,
        pull_through_registry=pull_through_registry,
    )


def _build_agent_host_yaml(vm_name, role, boot_mac, cluster_ip, prefix_len, gateway_ip):
    """Build a single host entry for agent-config.yaml."""
    return (
        f"    - hostname: {vm_name}\n"
        f"      role: {role}\n"
        f"      interfaces:\n"
        f"        - name: cluster-nic\n"
        f"          macAddress: {boot_mac}\n"
        f"      networkConfig:\n"
        f"        interfaces:\n"
        f"          - name: cluster-nic\n"
        f"            type: ethernet\n"
        f"            state: up\n"
        f"            identifier: mac-address\n"
        f"            mac-address: {boot_mac}\n"
        f"            ipv4:\n"
        f"              enabled: true\n"
        f"              address:\n"
        f"                - ip: {cluster_ip}\n"
        f"                  prefix-length: {prefix_len}\n"
        f"              dhcp: false\n"
        f"        dns-resolver:\n"
        f"          config:\n"
        f"            server:\n"
        f"              - {gateway_ip}\n"
        f"        routes:\n"
        f"          config:\n"
        f"            - destination: 0.0.0.0/0\n"
        f"              next-hop-address: {gateway_ip}\n"
        f"              next-hop-interface: cluster-nic\n"
    )


def _extract_agent_host(node, gateway_ip, prefix_len):
    if node.get("type") != "vmNode":
        return None
    td = node.get("data", {})
    if not td.get("bmcEnabled") or not td.get("bmcIp"):
        return None
    group = td.get("tags", {}).get("AnsibleGroup", "")
    if group not in ("controllers", "workers"):
        return None
    vm_name = td.get("name", "")
    cluster_ip = td.get("nics", [{}])[0].get("ip", "")
    boot_mac = td.get("nics", [{}])[0].get("mac", "")
    if not _NAME_RE.match(vm_name) or not _MAC_RE.match(boot_mac):
        return None
    role = "master" if group == "controllers" else "worker"
    host_yaml = _build_agent_host_yaml(
        vm_name, role, boot_mac, cluster_ip, prefix_len, gateway_ip
    )
    return host_yaml, cluster_ip


def _build_agent_config(cluster, members, topology):
    """Build a single cluster's ``agent-config.yaml`` (scoped to ``members``).

    ``cluster`` is a cluster-shaped dict (``name`` used for ``metadata.name``),
    ``members`` are its VM nodes (see :func:`cluster_member_nodes`), and
    ``topology`` is the full topology used only to resolve the cluster's
    network. Per-host NMState entries (static IP/MAC/gateway/DNS) come from
    ``members`` alone, so hosts never leak across clusters. ``rendezvousIP`` is
    the cluster's first control-plane member IP (falling back to ``network+10``
    when no control-plane IP is present).
    """
    cluster_name = cluster.get("name", "ocp")
    net = ipaddress.ip_network(_cidr_for_members(members, topology), strict=False)
    gateway_ip = str(net.network_address + 1)
    prefix_len = net.prefixlen

    hosts_yaml = ""
    for node in members:
        result = _extract_agent_host(node, gateway_ip, prefix_len)
        if result:
            hosts_yaml += result[0]

    rendezvous_ip = _cluster_control_plane_ip(members) or str(net.network_address + 10)

    ac_lines = [
        "apiVersion: v1beta1",
        "kind: AgentConfig",
        "metadata:",
        f"  name: {cluster_name}",
        f"rendezvousIP: {rendezvous_ip}",
        "additionalNTPSources:",
        "  - clock.redhat.com",
        "  - pool.ntp.org",
        "hosts:",
    ]
    ac_lines.append(hosts_yaml.rstrip())

    return "\n".join(ac_lines)


def _build_agent_config_legacy(
    topology,
    cluster_name,
    base_domain,
    _api_vip_override="",
    _ingress_vip_override="",
):
    """Single-cluster back-compat wrapper for :func:`_build_agent_config`.

    Reconstructs a cluster-shaped dict and treats the whole topology's VM nodes
    as the cluster members, so single-cluster callers get output equivalent to
    the pre-multicluster implementation. The VIP override args are accepted for
    call-site compatibility but unused (they never affected agent-config
    output).
    """
    cluster = {"name": cluster_name, "baseDomain": base_domain}
    members = [n for n in topology.get("nodes", []) if n.get("type") == "vmNode"]
    return _build_agent_config(cluster, members, topology)


def _build_bastion_autologin_steps(cluster_name: str, base_domain: str) -> str:
    """Shell steps to stash kubeadmin creds in Firefox via Selenium (not NSS ctypes)."""
    import base64

    from app.services.ocp_autologin import GECKODRIVER_URL, OCP_AUTOLOGIN_SCRIPT

    script_b64 = base64.b64encode(OCP_AUTOLOGIN_SCRIPT.encode()).decode()
    console_url = f"https://console-openshift-console.apps.{cluster_name}.{base_domain}"
    return (
        "    # Save kubeadmin password into Firefox via headless Selenium\n"
        "    if ! find /home/cloud-user/.mozilla/firefox -maxdepth 2 "
        "-name cert9.db 2>/dev/null | grep -q .; then\n"
        "      firefox --headless --no-remote >/dev/null 2>&1 &\n"
        "      FXPID=$!\n"
        "      sleep 5\n"
        "      kill $FXPID 2>/dev/null; wait $FXPID 2>/dev/null || true\n"
        "      sleep 2\n"
        "    fi\n"
        "    if [ ! -x /usr/local/bin/geckodriver ]; then\n"
        f"      curl -sfL {GECKODRIVER_URL} | sudo tar xz -C /usr/local/bin/ || true\n"
        "      sudo chmod +x /usr/local/bin/geckodriver 2>/dev/null || true\n"
        "    fi\n"
        "    python3 -c 'import selenium' 2>/dev/null "
        "|| pip3 install --user selenium 2>/dev/null || true\n"
        f"    echo '{script_b64}' | base64 -d > /home/cloud-user/ocp-autologin.py\n"
        "    chown cloud-user:cloud-user /home/cloud-user/ocp-autologin.py\n"
        "    chmod 755 /home/cloud-user/ocp-autologin.py\n"
        "    pkill -x firefox 2>/dev/null || true\n"
        "    sleep 2\n"
        "    export GECKODRIVER_PATH=/usr/local/bin/geckodriver\n"
        f"    python3 /home/cloud-user/ocp-autologin.py {shlex.quote(console_url)} 2>&1 || true\n"
    )


# Shared source of truth for the OCP client mirror + agent-install command
# strings. Both the bastion cloud-init installer (:func:`_build_install_script`)
# and the ops-pod install runner (`ops_pod_install`) build their steps from these
# helpers, so the Redfish/serve/wait-for/create-image behavior stays identical.
_MIRROR_CLIENTS_BASE = (
    "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp"
)


def _installer_tarball_url(tarball: str) -> str:
    """Mirror URL for an OCP client tarball at ``stable-$OCP_VERSION`` (shell var)."""
    return f"{_MIRROR_CLIENTS_BASE}/stable-$OCP_VERSION/{tarball}"


def _agent_create_image_cmd(indent: str, oi_bin: str, log_path: str) -> str:
    """`openshift-install agent create image` with timestamped, tee'd output."""
    return (
        f"{indent}{oi_bin} agent create image --dir . --log-level debug 2>&1 | "
        'awk \'{print strftime("[%H:%M:%S]") " " $0; fflush()}\' | '
        f"tee {log_path}\n"
    )


def _serve_iso_cmd(indent: str, workdir: str, port: int) -> str:
    """Serve the agent ISO over HTTP and compute ``ISO_URL`` for Redfish."""
    i = indent
    return (
        f"{i}# Open firewall for ISO serving (script runs as cloud-user)\n"
        f"{i}sudo firewall-cmd --add-port={port}/tcp --permanent 2>/dev/null && "
        "sudo firewall-cmd --reload 2>/dev/null || true\n"
        f"{i}cd {workdir}\n"
        f"{i}nohup python3 -m http.server {port} > /tmp/http-server.log 2>&1 &\n"
        f"{i}HTTP_PID=$!\n"
        f'{i}echo "HTTP server PID: $HTTP_PID"\n'
        f"{i}\n"
        f"{i}# Boot each CP node via Redfish virtual media\n"
        f"{i}BASTION_IP=$(hostname -I | awk '{{print $1}}')\n"
        f'{i}ISO_URL="http://${{BASTION_IP}}:{port}/agent.x86_64.iso"\n'
        f'{i}echo "ISO URL: $ISO_URL"\n'
    )


def _redfish_insert_media_cmd(indent: str, bmc_ips_str: str) -> str:
    """Redfish loop: InsertMedia + ComputerSystem.Reset (ForceRestart) per BMC."""
    b = indent
    b2 = indent + "  "
    b4 = indent + "    "
    return (
        f"{b}for BMC_IP in {bmc_ips_str}; do\n"
        f'{b2}echo "Mounting ISO on BMC $BMC_IP..."\n'
        f"{b2}# Get system UUID from sushy\n"
        f"{b2}SYS_ID=$(curl -s -u admin:$BMC_PASS http://${{BMC_IP}}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\")\n"
        f'{b2}echo "  System: $SYS_ID"\n'
        f"{b2}# Insert virtual media (Systems path, HTTP, with auth)\n"
        f'{b2}curl -s -u admin:$BMC_PASS -X POST "http://${{BMC_IP}}:8000/redfish/v1/Systems/${{SYS_ID}}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia" \\\n'
        f"{b4}-H 'Content-Type: application/json' \\\n"
        f'{b4}-d "{{\\"Image\\": \\"${{ISO_URL}}\\", \\"Inserted\\": true, \\"WriteProtected\\": true}}" || true\n'
        f"{b2}# Reboot — UEFI boot order is hd,cdrom so empty disk falls through to ISO\n"
        f"{b2}# After agent writes CoreOS to disk, next reboot boots from disk first\n"
        f'{b2}curl -s -u admin:$BMC_PASS -X POST "http://${{BMC_IP}}:8000/redfish/v1/Systems/${{SYS_ID}}/Actions/ComputerSystem.Reset" \\\n'
        f"{b4}-H 'Content-Type: application/json' \\\n"
        f'{b4}-d \'{{"ResetType": "ForceRestart"}}\' || true\n'
        f'{b2}echo "Booted $BMC_IP from ISO"\n'
        f"{b}done\n"
    )


def _redfish_eject_media_cmd(indent: str, bmc_ips_str: str) -> str:
    """Redfish loop: EjectMedia per BMC (called after install completes)."""
    b = indent
    b2 = indent + "  "
    return (
        f"{b}for BMC_IP in {bmc_ips_str}; do\n"
        f"{b2}SYS_ID=$(curl -s -u admin:$BMC_PASS http://${{BMC_IP}}:8000/redfish/v1/Systems | python3 -c \"import json,sys; print(json.load(sys.stdin)['Members'][0]['@odata.id'].split('/')[-1])\" 2>/dev/null)\n"
        f"{b2}curl -s -u admin:$BMC_PASS -X POST \"http://${{BMC_IP}}:8000/redfish/v1/Systems/${{SYS_ID}}/VirtualMedia/Cd/Actions/VirtualMedia.EjectMedia\" -H 'Content-Type: application/json' -d '{{}}' >/dev/null 2>&1\n"
        f"{b}done\n"
    )


def _wait_for_complete_cmd(indent: str, oi_bin: str, install_dir: str) -> str:
    """`openshift-install agent wait-for install-complete` with timestamped output."""
    return (
        f"{indent}{oi_bin} agent wait-for install-complete --dir {install_dir} "
        "--log-level debug 2>&1 | "
        'awk \'{print strftime("[%H:%M:%S]") " " $0; fflush()}\'\n'
    )


def _build_install_script(
    ocp_version,
    auto_install,
    bmc_password="",
    bmc_ips_str="",
    cluster_name="ocp",
    base_domain="ocp.local",
    topology=None,
):
    return (
        _YAML_BLOCK_SCALAR
        + "    cat > /home/cloud-user/install-ocp.sh << 'SCRIPTEOF'\n"
        "    #!/bin/bash\n"
        "    set -e\n"
        "    cd /home/cloud-user\n"
        "    \n"
        "    # Skip if cluster is already installed (pattern deploy)\n"
        "    if [ -f /home/cloud-user/ocp-install/auth/kubeconfig ]; then\n"
        "      echo 'Cluster already installed, skipping.'\n"
        "      exit 0\n"
        "    fi\n"
        "    \n"
        "    # Wait for network. Test DNS reachability to 8.8.8.8 (a TCP/UDP:53\n"
        "    # resolve) rather than ICMP ping, since gateway egress policy may\n"
        "    # block icmp while still allowing DNS/HTTPS the install needs.\n"
        "    echo 'Waiting for network...'\n"
        "    net_ok() { timeout 2 bash -c '</dev/tcp/8.8.8.8/53' &>/dev/null; }\n"
        "    for i in $(seq 1 15); do\n"
        "      net_ok && break\n"
        "      sleep 2\n"
        "    done\n"
        "    if ! net_ok; then\n"
        "      echo 'ERROR: No network connectivity after 30 seconds'\n"
        "      exit 1\n"
        "    fi\n"
        "    echo 'Network OK'\n"
        "    \n"
        f"    OCP_VERSION={ocp_version}\n"
        "    \n"
        "    # Download openshift-install and oc if not present\n"
        "    if [ ! -f openshift-install ]; then\n"
        '      echo "Downloading openshift-install $OCP_VERSION..."\n'
        f"      curl -L -o /tmp/openshift-install.tar.gz {_installer_tarball_url('openshift-install-linux.tar.gz')}\n"
        "      tar xzf /tmp/openshift-install.tar.gz && rm -f /tmp/openshift-install.tar.gz\n"
        '      echo "Downloading oc client..."\n'
        f"      curl -L -o /tmp/openshift-client.tar.gz {_installer_tarball_url('openshift-client-linux.tar.gz')}\n"
        "      tar xzf /tmp/openshift-client.tar.gz && rm -f /tmp/openshift-client.tar.gz\n"
        "      sudo mv oc kubectl /usr/bin/\n"
        '      echo "Downloaded openshift-install and oc"\n'
        "    fi\n"
        "    \n"
        "    echo ''\n"
        "    echo '================================================'\n"
        "    echo 'OCP Agent-Based Installer Ready'\n"
        "    echo '================================================'\n"
        "    echo ''\n"
        "    echo 'install-config.yaml:  ~/ocp-install/install-config.yaml'\n"
        "    echo 'agent-config.yaml:    ~/ocp-install/agent-config.yaml'\n"
        "    echo 'Pull secret:          ~/pull-secret.json'\n"
        "    echo 'openshift-install:    ~/openshift-install'\n"
        "    echo ''\n"
        "    echo 'To create agent ISO and install:'\n"
        "    echo '  cd ~/ocp-install'\n"
        "    echo '  ~/openshift-install agent create image --dir .'\n"
        "    echo '  # Serve ISO and boot nodes via BMC'\n"
        "    echo '  ~/openshift-install agent wait-for install-complete --dir . --log-level debug'\n"
        "    echo ''\n"
        "    echo ''\n"
        + (
            "    # Auto-run agent-based installer\n"
            "    INSTALL_START=$(date +%s)\n"
            '    echo "Install started at $(date)"\n'
            f"    BMC_PASS='{bmc_password}'\n"
            "    echo 'Creating agent ISO...'\n"
            "    cd /home/cloud-user/ocp-install\n"
            "    cp install-config.yaml install-config.yaml.bak\n"
            "    cp agent-config.yaml agent-config.yaml.bak\n"
            "    [ -d openshift ] && cp -a openshift openshift.bak\n"
            + _generate_ocp_mount_script(topology or {})
            + _generate_dns_manifests(topology or {}, base_domain)
            + _agent_create_image_cmd(
                "    ",
                "/home/cloud-user/openshift-install",
                "/home/cloud-user/create-image.log",
            )
            + "    \n"
            "    echo 'Agent ISO created. Serving via HTTP and booting nodes...'\n"
            + _serve_iso_cmd("    ", "/home/cloud-user/ocp-install", 8080)
            + "    \n"
            + _redfish_insert_media_cmd("    ", bmc_ips_str)
            + "    \n"
            "    \n"
            "    echo 'Waiting for cluster installation to complete...'\n"
            + _wait_for_complete_cmd(
                "    ",
                "/home/cloud-user/openshift-install",
                "/home/cloud-user/ocp-install",
            )
            + "    OCP_EXIT=${PIPESTATUS[0]}\n"
            "    INSTALL_END=$(date +%s)\n"
            "    ELAPSED=$(( INSTALL_END - INSTALL_START ))\n"
            "    echo ''\n"
            "    echo '================================================'\n"
            "    if [ $OCP_EXIT -ne 0 ]; then\n"
            '    echo "Install FAILED at $(date) (exit code $OCP_EXIT)"\n'
            '    echo "Total time: $(( ELAPSED / 60 )) min $(( ELAPSED % 60 )) sec"\n'
            "    echo '================================================'\n"
            "    kill $HTTP_PID 2>/dev/null\n"
            "    exit 1\n"
            "    fi\n"
            '    echo "Install completed at $(date)"\n'
            '    echo "Total time: $(( ELAPSED / 60 )) min $(( ELAPSED % 60 )) sec"\n'
            "    echo '================================================'\n"
            "    # Eject agent ISO via Redfish virtual media\n"
            "    echo 'Ejecting agent ISO from nodes...'\n"
            + _redfish_eject_media_cmd("    ", bmc_ips_str)
            + "    # Write static MOTD with cluster credentials\n"
            "    KUBEADMIN_PW=$(cat /home/cloud-user/ocp-install/auth/kubeadmin-password)\n"
            f"    printf '\\nOpenShift Console: https://console-openshift-console.apps.{cluster_name}.{base_domain}\\nUsername:          kubeadmin\\nPassword:          %s\\n\\n' \"$KUBEADMIN_PW\" | sudo tee /etc/motd >/dev/null\n"
            "    # Trust the OCP CA so Firefox doesn't show cert warnings\n"
            "    export KUBECONFIG=/home/cloud-user/ocp-install/auth/kubeconfig\n"
            "    oc get secret -n openshift-ingress router-certs-default -o jsonpath='{.data.tls\\.crt}' 2>/dev/null | base64 -d | sudo tee /etc/pki/ca-trust/source/anchors/ocp-ingress.pem >/dev/null && sudo update-ca-trust\n"
            + _build_bastion_autologin_steps(cluster_name, base_domain)
            + "    # Cleanup: remove cached ISO, temp files, and pull secret from disk\n"
            + "    rm -f /home/cloud-user/pull-secret.json\n"
            + "    rm -rf /home/cloud-user/.cache/agent/ /tmp/http-server.log /tmp/cookies /tmp/*.zip /var/tmp/dnf-*\n"
            + "    dnf clean all 2>/dev/null\n"
            + "    # Kill the HTTP server used to serve the agent ISO\n"
            + "    kill $HTTP_PID 2>/dev/null\n"
            if auto_install
            else ""
        )
        + "    SCRIPTEOF\n"
        "    chown cloud-user:cloud-user /home/cloud-user/install-ocp.sh\n"
        "    chmod 755 /home/cloud-user/install-ocp.sh\n"
    )
