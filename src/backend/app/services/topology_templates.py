import uuid
import random


def _id():
    return str(uuid.uuid4())


def _mac():
    return "52:54:00:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def _vm_node(name, vcpus, ram, x, y, disk_gb=120):
    nic = {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio"}
    dc = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    disk_id = _id()
    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": x - 30, "y": y + 250},
        "data": {
            "label": f"{name}-disk",
            "name": f"{name}-disk",
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
            "label": name,
            "name": name,
            "vcpus": vcpus,
            "ram": ram,
            "os": "rhcos",
            "icon": "🖥",
            "nics": [nic],
            "diskControllers": [dc],
            "bmcEnabled": True,
            "firmware": "uefi",
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
        "sourceHandle": f"{disk_id}-right",
        "targetHandle": f"{dc['id']}-left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }
    return vm_node, disk_node, disk_edge


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


def _lb_node(x, y):
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


def _bmc_node(x, y):
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
        },
    }


def _gateway_node(x, y):
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
            "portForwards": [],
        },
    }


def _net_edge(src_node, tgt_vm, style_type="network"):
    """Edge from network/LB node to VM's first NIC."""
    nic_id = tgt_vm["data"]["nics"][0]["id"]
    styles = {
        "network": {"stroke": "rgba(34,211,238,0.5)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        "lb": {"stroke": "rgba(59,130,246,0.5)", "strokeWidth": 2, "strokeDasharray": "6 4"},
        "gateway": {"stroke": "rgba(74,222,128,0.5)", "strokeWidth": 2, "strokeDasharray": "8 4"},
    }
    return {
        "id": _id(),
        "source": src_node["id"],
        "target": tgt_vm["id"],
        "sourceHandle": f"{src_node['id']}-bottom",
        "targetHandle": f"{nic_id}-top",
        "type": "smoothstep",
        "style": styles.get(style_type, styles["network"]),
        "animated": True,
    }


def _gw_net_edge(gw_node, net_node):
    """Edge from gateway to network (orange handle to orange handle)."""
    return {
        "id": _id(),
        "source": gw_node["id"],
        "target": net_node["id"],
        "sourceHandle": f"{gw_node['id']}-left",
        "targetHandle": f"{net_node['id']}-right",
        "type": "smoothstep",
        "style": {"stroke": "rgba(74,222,128,0.5)", "strokeWidth": 2, "strokeDasharray": "8 4"},
        "animated": True,
    }


TEMPLATES = {
    "ocp-sno": {
        "name": "OpenShift SNO",
        "description": "Single Node OpenShift — 8 vCPU, 32 GB RAM",
        "category": "openshift",
    },
    "ocp-compact": {
        "name": "OpenShift Compact 3-Node",
        "description": "3 combined control plane + worker nodes — 8 vCPU, 16 GB each",
        "category": "openshift",
    },
    "ocp-standard": {
        "name": "OpenShift Standard 3+2",
        "description": "3 control plane + 2 worker nodes",
        "category": "openshift",
    },
}


def generate_topology(template_id: str) -> dict:
    nodes = []
    edges = []
    eip_id = _id()
    external_ips = [{"id": eip_id, "label": "OCP"}]

    if template_id == "ocp-sno":
        net = _net_node("cluster", "10.0.0.0/24", 350, 50)
        bmc = _bmc_node(550, 50)
        lb = _lb_node(100, 50)
        gw = _gateway_node(550, 170)
        sno_vm, sno_disk, sno_disk_edge = _vm_node("sno-0", 8, 32, 200, 250)
        bs_vm, bs_disk, bs_disk_edge = _vm_node("bootstrap", 4, 16, 500, 250)
        nodes = [lb, net, bmc, gw, sno_vm, sno_disk, bs_vm, bs_disk]
        edges = [sno_disk_edge, bs_disk_edge, _gw_net_edge(gw, net)]
        for vm in [sno_vm, bs_vm]:
            edges.append(_net_edge(net, vm, "network"))
            edges.append(_net_edge(lb, vm, "lb"))

    elif template_id == "ocp-compact":
        net = _net_node("cluster", "10.0.0.0/24", 400, 50)
        bmc = _bmc_node(650, 50)
        lb = _lb_node(100, 50)
        gw = _gateway_node(850, 170)
        vm_data = []
        for i in range(3):
            vm, disk, disk_edge = _vm_node(f"cp-{i}", 8, 16, 150 + i * 230, 250)
            vm_data.append((vm, disk, disk_edge))
        bs_vm, bs_disk, bs_disk_edge = _vm_node("bootstrap", 4, 16, 150 + 3 * 230, 250)
        vm_data.append((bs_vm, bs_disk, bs_disk_edge))
        nodes = [lb, net, bmc, gw]
        for vm, disk, disk_edge in vm_data:
            nodes.extend([vm, disk])
            edges.append(disk_edge)
        edges.append(_gw_net_edge(gw, net))
        for vm, _, _ in vm_data:
            edges.append(_net_edge(net, vm, "network"))
            edges.append(_net_edge(lb, vm, "lb"))

    elif template_id == "ocp-standard":
        net = _net_node("cluster", "10.0.0.0/24", 450, 50)
        bmc = _bmc_node(700, 50)
        lb = _lb_node(100, 50)
        gw = _gateway_node(900, 170)
        cp_data = []
        for i in range(3):
            vm, disk, disk_edge = _vm_node(f"cp-{i}", 4, 16, 150 + i * 230, 250)
            cp_data.append((vm, disk, disk_edge))
        w_data = []
        for i in range(2):
            vm, disk, disk_edge = _vm_node(f"worker-{i}", 4, 16, 150 + i * 230, 550)
            w_data.append((vm, disk, disk_edge))
        bs_vm, bs_disk, bs_disk_edge = _vm_node("bootstrap", 4, 16, 150 + 3 * 230, 250)
        nodes = [lb, net, bmc, gw]
        for vm, disk, disk_edge in cp_data + w_data + [(bs_vm, bs_disk, bs_disk_edge)]:
            nodes.extend([vm, disk])
            edges.append(disk_edge)
        edges.append(_gw_net_edge(gw, net))
        for vm, _, _ in cp_data + w_data + [(bs_vm, bs_disk, bs_disk_edge)]:
            edges.append(_net_edge(net, vm, "network"))
        for vm, _, _ in cp_data + [(bs_vm, bs_disk, bs_disk_edge)]:
            edges.append(_net_edge(lb, vm, "lb"))

    else:
        raise ValueError(f"Unknown template: {template_id}")

    return {"nodes": nodes, "edges": edges, "externalIps": external_ips}


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]
