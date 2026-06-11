import uuid
import random


def _id():
    return str(uuid.uuid4())


def _mac():
    return "52:54:00:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def _nic():
    return {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio"}


def _disk_controller():
    return {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}


def _vm_node(name, vcpus, ram, x, y):
    return {
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
            "nics": [_nic()],
            "diskControllers": [_disk_controller()],
            "bmcEnabled": True,
            "firmware": "uefi",
            "secureBoot": False,
            "bootDevices": [],
            "powerOnAtDeploy": True,
        },
    }


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


def _wire(src_node, tgt_node):
    """Create an edge from a network/LB node to a VM node using the VM's first NIC."""
    nic_id = tgt_node["data"]["nics"][0]["id"]
    return {
        "id": _id(),
        "source": src_node["id"],
        "target": tgt_node["id"],
        "sourceHandle": f"{src_node['id']}-bottom",
        "targetHandle": f"{nic_id}-top",
        "type": "smoothstep",
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

    if template_id == "ocp-sno":
        net = _net_node("cluster", "10.0.0.0/24", 300, 0)
        bmc = _bmc_node(500, 0)
        lb = _lb_node(100, 0)
        sno = _vm_node("sno-0", 8, 32, 200, 200)
        bootstrap = _vm_node("bootstrap", 4, 16, 450, 200)
        nodes = [net, bmc, lb, sno, bootstrap]
        for vm in [sno, bootstrap]:
            edges.append(_wire(net, vm))
            edges.append(_wire(lb, vm))

    elif template_id == "ocp-compact":
        net = _net_node("cluster", "10.0.0.0/24", 350, 0)
        bmc = _bmc_node(600, 0)
        lb = _lb_node(100, 0)
        cps = [_vm_node(f"cp-{i}", 8, 16, 150 + i * 220, 200) for i in range(3)]
        bootstrap = _vm_node("bootstrap", 4, 16, 150 + 3 * 220, 200)
        nodes = [net, bmc, lb] + cps + [bootstrap]
        for vm in cps + [bootstrap]:
            edges.append(_wire(net, vm))
            edges.append(_wire(lb, vm))

    elif template_id == "ocp-standard":
        net = _net_node("cluster", "10.0.0.0/24", 400, 0)
        bmc = _bmc_node(650, 0)
        lb = _lb_node(100, 0)
        cps = [_vm_node(f"cp-{i}", 4, 16, 150 + i * 220, 200) for i in range(3)]
        workers = [_vm_node(f"worker-{i}", 4, 16, 150 + i * 220, 450) for i in range(2)]
        bootstrap = _vm_node("bootstrap", 4, 16, 150 + 3 * 220, 200)
        nodes = [net, bmc, lb] + cps + workers + [bootstrap]
        for vm in cps + workers + [bootstrap]:
            edges.append(_wire(net, vm))
        for vm in cps + [bootstrap]:
            edges.append(_wire(lb, vm))

    else:
        raise ValueError(f"Unknown template: {template_id}")

    return {"nodes": nodes, "edges": edges}


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]
