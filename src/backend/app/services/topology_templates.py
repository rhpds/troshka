import uuid
import random


def _id():
    return str(uuid.uuid4())


def _mac():
    return "52:54:00:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def _vm_node(name, vcpus, ram, x, y, disk_gb=120, bmc_ip="", cluster_ip=""):
    nic = {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio", "ip": cluster_ip}
    dc = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    dc_cdrom = {"id": f"dp-{_id()}", "name": "cdrom0", "bus": "sata"}
    disk_id = _id()
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 70},
        "data": {
            "label": f"{name}-disk",
            "name": f"{name}-disk",
            "size": disk_gb,
            "format": "qcow2",
            "icon": "🛢",
        },
    }
    vm_data = {
        "label": name,
        "name": name,
        "vcpus": vcpus,
        "ram": ram,
        "os": "rhcos",
        "icon": "🖥",
        "nics": [nic],
        "diskControllers": [dc, dc_cdrom],
        "bmcEnabled": True,
        "firmware": "uefi",
        "secureBoot": False,
        "bootDevices": [disk_id],
        "bootMethod": "disk",
        "powerOnAtDeploy": True,
    }
    if bmc_ip:
        vm_data["bmcIp"] = bmc_ip
    vm_node = {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": x, "y": y},
        "data": vm_data,
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc['id']}-left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return vm_node, disk_node, disk_edge


def _bastion_node(x, y, disk_gb=150, cluster_ip="10.0.0.50"):
    """Bastion/provisioner VM with two NICs (cluster + BMC). Sized to host nested bootstrap VM."""
    nic_cluster = {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio", "ip": cluster_ip}
    nic_bmc = {"id": f"nic-{_id()}", "name": "eth1", "mac": _mac(), "model": "virtio"}
    dc = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    disk_id = _id()
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 70},
        "data": {
            "label": "bastion-disk",
            "name": "bastion-disk",
            "size": disk_gb,
            "format": "qcow2",
            "icon": "🛢",
        },
    }
    vm_node = {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": "bastion",
            "name": "bastion",
            "vcpus": 8,
            "ram": 32,
            "os": "rhel10",
            "icon": "🖥",
            "nics": [nic_cluster, nic_bmc],
            "diskControllers": [dc],
            "bmcEnabled": False,
            "firmware": "bios",
            "secureBoot": False,
            "bootDevices": [disk_id],
            "bootMethod": "disk",
            "powerOnAtDeploy": True,
        },
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc['id']}-left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return vm_node, disk_node, disk_edge, nic_cluster, nic_bmc


OCP_FRONTENDS = [
    {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    {"name": "ingress-https", "bindPort": 443, "mode": "tcp", "backendPort": 443},
    {"name": "ingress-http", "bindPort": 80, "mode": "tcp", "backendPort": 80},
    {"name": "machine-config", "bindPort": 22623, "mode": "tcp", "backendPort": 22623},
]

OCP_DNS_RECORDS = [
    {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
    {"name": "api-int.{guid}.{domain}", "type": "A", "target": "eip"},
    {"name": "*.apps.{guid}.{domain}", "type": "A", "target": "eip"},
]


def _lb_node(x, y, ext_ip_id=""):
    return {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": "ocp-lb",
            "name": "ocp-lb",
            "subtype": "loadbalancer",
            "networkType": "loadbalancer",
            "frontends": OCP_FRONTENDS,
            "lbIp": "10.0.0.2",
            "external": True,
            "extIpId": ext_ip_id,
            "dnsRecords": OCP_DNS_RECORDS,
            "dnsTtl": 30,
        },
    }


def _net_node(name, cidr, x, y):
    return {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": name,
            "name": name,
            "subtype": "network",
            "cidr": cidr,
            "dhcp": True,
        },
    }


def _bmc_node(x, y, bmc_password="password"):
    return {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": "bmc",
            "name": "bmc",
            "subtype": "network",
            "networkType": "bmc",
            "cidr": "192.168.100.0/24",
            "bmcUsername": "admin",
            "bmcPassword": bmc_password,
        },
    }


def _gateway_node(x, y, port_forwards=None):
    return {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": x, "y": y},
        "data": {
            "label": "gateway",
            "name": "gateway",
            "subtype": "gateway",
            "gatewayMode": "nat-portforward",
            "outboundPolicy": "allow-all",
            "portForwards": port_forwards or [],
        },
    }


def _net_edge(src_node, tgt_vm, style_type="network", nic_index=0):
    """Edge from network/LB node to VM's NIC.

    VM handles are rendered as nic-{nic.id}-top and dp-{dc.id}-left
    where nic.id is already 'nic-{uuid}', so the full handle is 'nic-nic-{uuid}-top'.
    """
    nic_id = tgt_vm["data"]["nics"][nic_index]["id"]
    styles = {
        "network": {"stroke": "rgba(34,211,238,0.5)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        "lb": {"stroke": "rgba(59,130,246,0.5)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        "gateway": {"stroke": "rgba(74,222,128,0.5)", "strokeWidth": 2, "strokeDasharray": "8 4"},
    }
    return {
        "id": _id(),
        "source": src_node["id"],
        "target": tgt_vm["id"],
        "sourceHandle": "bottom",
        "targetHandle": f"nic-{nic_id}-top",
        "type": "smoothstep",
        "style": styles.get(style_type, styles["network"]),
        "animated": True,
    }


def _lb_vm_edge(lb_node, tgt_vm, nic_index=0):
    """Edge from LB top handle to VM bottom NIC handle."""
    nic_id = tgt_vm["data"]["nics"][nic_index]["id"]
    return {
        "id": _id(),
        "source": lb_node["id"],
        "target": tgt_vm["id"],
        "sourceHandle": "top",
        "targetHandle": f"nic-{nic_id}-bottom",
        "type": "smoothstep",
        "style": {"stroke": "rgba(59,130,246,0.5)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        "animated": True,
    }


def _gw_net_edge(gw_node, net_node):
    """Edge from gateway to network."""
    return {
        "id": _id(),
        "source": gw_node["id"],
        "target": net_node["id"],
        "sourceHandle": "left",
        "targetHandle": "left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(74,222,128,0.5)", "strokeWidth": 2, "strokeDasharray": "8 4"},
        "animated": True,
    }


TEMPLATES = {
    "ocp-sno": {
        "name": "OpenShift SNO (Agent Installer)",
        "description": "Single Node OpenShift — 8 vCPU, 32 GB RAM",
        "category": "openshift",
        "install_method": "agent",
    },
    "ocp-compact": {
        "name": "OpenShift Compact 3-Node (Agent Installer)",
        "description": "3 combined control plane + worker nodes — 8 vCPU, 16 GB each",
        "category": "openshift",
        "install_method": "agent",
    },
    "ocp-standard": {
        "name": "OpenShift Standard 3+2 (Agent Installer)",
        "description": "3 control plane + 2 worker nodes",
        "category": "openshift",
        "install_method": "agent",
    },
}


def generate_topology(template_id: str, bmc_password: str = "password") -> dict:
    nodes = []
    edges = []
    eip_id = _id()
    external_ips = [{"id": eip_id, "label": "OCP"}]

    ssh_port_forward = {"extIpId": eip_id, "extPort": "22", "intIp": "10.0.0.50", "intPort": "22", "proto": "tcp"}
    # IPI manages its own VIPs via keepalived — just port-forward from gateway
    api_vip = "10.0.0.2"
    ingress_vip = "10.0.0.3"
    ocp_port_forwards = [
        ssh_port_forward,
        {"extIpId": eip_id, "extPort": "6443", "intIp": api_vip, "intPort": "6443", "proto": "tcp"},
        {"extIpId": eip_id, "extPort": "443", "intIp": ingress_vip, "intPort": "443", "proto": "tcp"},
        {"extIpId": eip_id, "extPort": "80", "intIp": ingress_vip, "intPort": "80", "proto": "tcp"},
    ]

    # Layout constants
    VM_SPACING = 400        # horizontal gap between VM columns (room for disk to the left)
    GW_Y = 0                # gateway row (top)
    NET_ROW_Y = 150         # network/BMC row
    VM_ROW_Y = 350          # VM row
    LB_ROW_Y = VM_ROW_Y + 300  # LB row (below VMs)
    WORKER_ROW_Y = VM_ROW_Y + 370  # worker row (standard layout only)

    # Bastion position: right side, same row as VMs
    BASTION_X_OFFSET = 4  # columns from vm_x_start (after bootstrap)

    if template_id == "ocp-sno":
        vm_x_start = 150
        net_x = vm_x_start + VM_SPACING - 20
        net = _net_node("cluster-network", "10.0.0.0/24", net_x, NET_ROW_Y)
        bmc = _bmc_node(vm_x_start + VM_SPACING, NET_ROW_Y, bmc_password)
        gw = _gateway_node(net_x, GW_Y, port_forwards=ocp_port_forwards)
        sno_vm, sno_disk, sno_disk_edge = _vm_node("sno-0", 8, 32, vm_x_start, VM_ROW_Y, bmc_ip="192.168.100.10", cluster_ip="10.0.0.10")
        bast_vm, bast_disk, bast_disk_edge, _, _ = _bastion_node(
            vm_x_start + VM_SPACING, VM_ROW_Y)
        nodes = [net, bmc, gw, sno_vm, sno_disk, bast_vm, bast_disk]
        edges = [sno_disk_edge, bast_disk_edge, _gw_net_edge(gw, net)]
        edges.append(_net_edge(net, sno_vm, "network"))
        edges.append(_net_edge(net, bast_vm, "network", nic_index=0))
        edges.append(_net_edge(bmc, bast_vm, "network", nic_index=1))

    elif template_id == "ocp-compact":
        vm_x_start = 150
        bast_x = vm_x_start + 3 * VM_SPACING
        net_x = vm_x_start + int(1.5 * VM_SPACING) - 120
        net = _net_node("cluster-network", "10.0.0.0/24", net_x, NET_ROW_Y)
        bmc = _bmc_node(bast_x, NET_ROW_Y, bmc_password)
        gw = _gateway_node(net_x, GW_Y, port_forwards=ocp_port_forwards)
        vm_data = []
        for i in range(3):
            vm, disk, disk_edge = _vm_node(f"cp-{i}", 8, 16, vm_x_start + i * VM_SPACING, VM_ROW_Y, bmc_ip=f"192.168.100.{10 + i}", cluster_ip=f"10.0.0.{10 + i}")
            vm_data.append((vm, disk, disk_edge))
        bast_vm, bast_disk, bast_disk_edge, _, _ = _bastion_node(bast_x, VM_ROW_Y)
        nodes = [net, bmc, gw, bast_vm, bast_disk]
        for vm, disk, disk_edge in vm_data:
            nodes.extend([vm, disk])
            edges.append(disk_edge)
        edges.append(bast_disk_edge)
        edges.append(_gw_net_edge(gw, net))
        for vm, _, _ in vm_data:
            edges.append(_net_edge(net, vm, "network"))
        edges.append(_net_edge(net, bast_vm, "network", nic_index=0))
        edges.append(_net_edge(bmc, bast_vm, "network", nic_index=1))

    elif template_id == "ocp-standard":
        vm_x_start = 150
        bast_x = vm_x_start + 3 * VM_SPACING
        net_x = vm_x_start + int(1.5 * VM_SPACING) - 120
        net = _net_node("cluster-network", "10.0.0.0/24", net_x, NET_ROW_Y)
        bmc = _bmc_node(bast_x, NET_ROW_Y, bmc_password)
        gw = _gateway_node(net_x, GW_Y, port_forwards=ocp_port_forwards)
        cp_data = []
        for i in range(3):
            vm, disk, disk_edge = _vm_node(f"cp-{i}", 4, 16, vm_x_start + i * VM_SPACING, VM_ROW_Y, bmc_ip=f"192.168.100.{10 + i}", cluster_ip=f"10.0.0.{10 + i}")
            cp_data.append((vm, disk, disk_edge))
        w_data = []
        for i in range(2):
            vm, disk, disk_edge = _vm_node(f"worker-{i}", 4, 16, vm_x_start + i * VM_SPACING, WORKER_ROW_Y, bmc_ip=f"192.168.100.{20 + i}", cluster_ip=f"10.0.0.{20 + i}")
            w_data.append((vm, disk, disk_edge))
        bast_vm, bast_disk, bast_disk_edge, _, _ = _bastion_node(bast_x, VM_ROW_Y)
        nodes = [net, bmc, gw, bast_vm, bast_disk]
        for vm, disk, disk_edge in cp_data + w_data:
            nodes.extend([vm, disk])
            edges.append(disk_edge)
        edges.append(bast_disk_edge)
        edges.append(_gw_net_edge(gw, net))
        for vm, _, _ in cp_data + w_data:
            edges.append(_net_edge(net, vm, "network"))
        edges.append(_net_edge(net, bast_vm, "network", nic_index=0))
        edges.append(_net_edge(bmc, bast_vm, "network", nic_index=1))

    else:
        raise ValueError(f"Unknown template: {template_id}")

    return {"nodes": nodes, "edges": edges, "externalIps": external_ips}


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]
