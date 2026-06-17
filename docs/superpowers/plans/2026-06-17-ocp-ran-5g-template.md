# 5G RAN Lab Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ocp-ran-5g` template that deploys a hub OCP cluster + 3 blank SNO VMs for the 5G RAN RDS Deployments lab, replacing the nested kcli/golden-image approach with Troshka-native VM management.

**Architecture:** New YAML template extending `ocp-cluster`, custom topology generator for the RAN layout (hub + 3 SNOs + bastion + 4 networks), NIC model passthrough for igb SR-IOV emulation, and RAN-specific cloud-init on the bastion for lab services and ACM/ZTP post-install.

**Tech Stack:** Python 3.11 (FastAPI backend), Next.js 15 (frontend), YAML templates, cloud-init, libvirt/QEMU igb NIC model

## Global Constraints

- Python: use `Mapped[type]` + `mapped_column()` for any new models
- UUIDs as strings: `default=lambda: str(uuid.uuid4())`
- NIC model on topology JSONB: `nic.model` field already exists, values `virtio` (default), `e1000e`, `igb`
- Template YAML extends `ocp-cluster` base
- Tests use SQLite — run with `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
- Frontend hot-reloads — no restart needed
- Backend does NOT auto-reload — remind user to restart after Python changes
- Always use `python3` not `python`
- Run `black` before committing
- Hold off on commits until the end

---

### Task 1: Add igb NIC model support

The existing codebase hardcodes `"model": "virtio"` when building the network list for troshkad. The NIC `model` field already exists on topology JSONB and the frontend already has a model dropdown (but missing `igb`). Troshkad already validates models but its allowlist doesn't include `igb` or `e1000e`.

**Files:**
- Modify: `src/backend/app/services/deploy_service.py:229-284` — include NIC model in network entries from `_find_vm_networks`
- Modify: `src/backend/app/services/deploy_service.py:1128-1133` — read model from network entry instead of hardcoding
- Modify: `src/troshkad/troshkad.py:570` — add `igb` and `e1000e` to `_NET_MODELS`
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx:772-776` — add `igb` option to dropdown
- Test: `src/backend/tests/test_template_loader.py` — add test for igb NIC model in generated topology

**Interfaces:**
- Consumes: existing `_find_vm_networks()` function, existing `_create_vm_via_troshkad()` function
- Produces: NIC model passthrough — `_find_vm_networks()` returns `model` key in each entry, `_create_vm_via_troshkad()` uses it

- [ ] **Step 1: Update `_find_vm_networks` to include NIC model**

In `src/backend/app/services/deploy_service.py`, the function `_find_vm_networks` resolves MAC addresses from NIC handles but doesn't include the NIC model. Update both network append sites (BMC at ~line 265 and regular at ~line 278) to include the model from the NIC data:

```python
# In _find_vm_networks, where mac is resolved (~line 247-249):
# After the mac resolution loop, also grab model:
mac = ""
model = "virtio"
if vm_node:
    for nic in vm_node.get("data", {}).get("nics", []):
        if nic["id"] in handle:
            mac = nic.get("mac", "")
            model = nic.get("model", "virtio")
            break
```

Then in both `networks.append()` calls, add `"model": model`:

```python
# BMC network entry (~line 265):
networks.append(
    {
        "bridge": f"br-bmc-{project_id[:8]}",
        "mac": bmc_mac,
        "nic_id": handle,
        "model": model,
    }
)

# Regular network entry (~line 278):
networks.append(
    {
        "bridge": f"br-{vni}",
        "mac": mac,
        "nic_id": handle,
        "model": model,
    }
)
```

- [ ] **Step 2: Update `_create_vm_via_troshkad` to read model from network entry**

In `src/backend/app/services/deploy_service.py` at line 1130, change:

```python
# Old:
entry = {"bridge": net["bridge"], "model": "virtio"}

# New:
entry = {"bridge": net["bridge"], "model": net.get("model", "virtio")}
```

- [ ] **Step 3: Add `igb` and `e1000e` to troshkad's allowed models**

In `src/troshkad/troshkad.py` at line 570, change:

```python
# Old:
_NET_MODELS = {"virtio", "e1000", "rtl8139"}

# New:
_NET_MODELS = {"virtio", "e1000", "e1000e", "igb", "rtl8139"}
```

- [ ] **Step 4: Add `igb` to the frontend NIC model dropdown**

In `src/frontend/src/components/canvas/PropertiesPanel.tsx` at lines 772-776, add the igb option:

```tsx
<option value="virtio">virtio</option>
<option value="igb">igb (SR-IOV)</option>
<option value="e1000e">e1000e</option>
<option value="e1000">e1000</option>
<option value="rtl8139">rtl8139</option>
<option value="vmxnet3">vmxnet3</option>
```

- [ ] **Step 5: Write test for NIC model passthrough**

Add to `src/backend/tests/test_template_loader.py`:

```python
def test_vm_node_nic_model_preserved():
    """NIC model from topology should be preserved (not hardcoded to virtio)."""
    from app.services.template_loader import _vm_node, _id, _mac

    vm, disk, edge = _vm_node("test-vm", 4, 16, 100, 100)
    # Default model should be virtio
    assert vm["data"]["nics"][0]["model"] == "virtio"

    # Manually set model to igb and verify it's preserved
    vm["data"]["nics"][0]["model"] = "igb"
    assert vm["data"]["nics"][0]["model"] == "igb"
```

- [ ] **Step 6: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v
```

Expected: all tests pass including the new one.

---

### Task 2: Create `ocp-ran-5g` template YAML

Define the template that configures the RAN lab topology parameters.

**Files:**
- Create: `src/backend/templates/ocp-ran-5g.yaml`
- Test: `src/backend/tests/test_template_loader.py` — add resolution test

**Interfaces:**
- Consumes: `ocp-cluster.yaml` base template (via `extends`)
- Produces: template YAML loadable by `resolve_template("ocp-ran-5g")`

- [ ] **Step 1: Create the template file**

Create `src/backend/templates/ocp-ran-5g.yaml`:

```yaml
name: ocp-ran-5g
display_name: "5G RAN Lab (ACM + ZTP + GitOps)"
description: "Hub cluster + 3 blank SNO targets for 5G RAN RDS Deployments lab"
category: openshift
install_method: agent
deploy_time: "~45 min (hub) + ~60 min (seed SNO via ACM)"
extends: ocp-cluster
defaults:
  control_count: 1
  control_vcpus: 16
  control_ram_gb: 48
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0
```

Note: `hub_mode`, `sno_count`, `sno_vcpus`, etc. are RAN-specific parameters that won't go through the standard `ocp-cluster` parameter resolution. They'll be handled in the topology generator (Task 3) via the template name check. The YAML keeps things simple by just configuring the hub cluster defaults via the existing parameter system.

- [ ] **Step 2: Write test for template resolution**

Add to `src/backend/tests/test_template_loader.py`:

```python
def test_load_ran_5g_template():
    from app.services.template_loader import load_template

    tmpl = load_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    assert tmpl["name"] == "ocp-ran-5g"
    assert tmpl["extends"] == "ocp-cluster"
    assert tmpl["category"] == "openshift"


def test_resolve_ran_5g_defaults():
    from app.services.template_loader import resolve_template

    resolved = resolve_template("ocp-ran-5g", overrides={}, templates_dir=TEMPLATES_DIR)
    assert resolved["control_count"] == 1
    assert resolved["control_vcpus"] == 16
    assert resolved["control_ram_gb"] == 48
    assert resolved["worker_count"] == 0
    assert resolved["install_method"] == "agent"
```

- [ ] **Step 3: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v
```

Expected: all tests pass.

---

### Task 3: RAN topology generator

The existing `generate_topology_from_template()` creates hub CP nodes + workers + bastion + 2 networks. For the RAN template, we need: hub nodes, 3 blank SNO VMs (BMC-enabled, powered off, extra igb NICs), bastion, and 4 networks (cluster, bmc, sriov, ptp) + gateway. The cleanest approach is to add a RAN-specific generator function that's called when the template name matches.

**Files:**
- Modify: `src/backend/app/services/template_loader.py` — add `_generate_ran_topology()` and call it from `generate_topology_from_template()` when template name is `ocp-ran-5g`
- Test: `src/backend/tests/test_template_loader.py` — add topology generation tests

**Interfaces:**
- Consumes: `resolve_template("ocp-ran-5g")` output from Task 2
- Produces: topology dict with nodes/edges/externalIps matching the spec's VM table and network layout

- [ ] **Step 1: Add helper to create a blank SNO VM node**

Add to `src/backend/app/services/template_loader.py` after the existing `_vm_node` function:

```python
def _sno_node(name, vcpus, ram, x, y, disk_gb=200, cluster_ip="", ptp_mac="", sriov_macs=None):
    """Create a blank SNO VM with igb NICs for SR-IOV/PTP emulation.
    
    No BMC IP — the SNO VMs are discovered by ACM via their cluster-network MAC.
    BMC addressing is configured in the BareMetalHost CRDs, not on the VM itself.
    """
    sriov_macs = sriov_macs or []
    nic_cluster = {"id": f"nic-{_id()}", "name": "eth0", "mac": _mac(), "model": "virtio"}
    if cluster_ip:
        nic_cluster["ip"] = cluster_ip
    nic_ptp = {"id": f"nic-{_id()}", "name": "eth1", "mac": ptp_mac or _mac(), "model": "igb"}
    nic_sriov_0 = {"id": f"nic-{_id()}", "name": "eth2", "mac": sriov_macs[0] if len(sriov_macs) > 0 else _mac(), "model": "igb"}
    nic_sriov_1 = {"id": f"nic-{_id()}", "name": "eth3", "mac": sriov_macs[1] if len(sriov_macs) > 1 else _mac(), "model": "igb"}

    dc0 = {"id": f"dp-{_id()}", "name": "disk0", "bus": "virtio"}
    dc1 = {"id": f"dp-{_id()}", "name": "disk1", "bus": "virtio"}
    disk0_id = _id()
    disk1_id = _id()

    disk0 = {
        "id": disk0_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 70},
        "data": {
            "label": f"{name}-disk0",
            "name": f"{name}-disk0",
            "size": disk_gb,
            "format": "qcow2",
            "icon": "\U0001f6e2",
        },
    }
    disk1 = {
        "id": disk1_id,
        "type": "storageNode",
        "position": {"x": x - 190, "y": y + 170},
        "data": {
            "label": f"{name}-disk1",
            "name": f"{name}-disk1",
            "size": disk_gb,
            "format": "qcow2",
            "icon": "\U0001f6e2",
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
            "os": "blank",
            "icon": "\U0001f4e6",
            "nics": [nic_cluster, nic_ptp, nic_sriov_0, nic_sriov_1],
            "diskControllers": [dc0, dc1],
            "bmcEnabled": True,
            "firmware": "uefi",
            "secureBoot": False,
            "bootDevices": [disk0_id],
            "bootMethod": "disk",
            "powerOnAtDeploy": False,
        },
    }

    disk0_edge = {
        "id": _id(),
        "source": disk0_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc0['id']}-left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }
    disk1_edge = {
        "id": _id(),
        "source": disk1_id,
        "target": vm_node["id"],
        "sourceHandle": "right",
        "targetHandle": f"dp-{dc1['id']}-left",
        "type": "smoothstep",
        "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
        "animated": False,
        "className": "edge-storage-pulse",
    }

    return vm_node, [disk0, disk1], [disk0_edge, disk1_edge]
```

- [ ] **Step 2: Add the RAN topology generator**

Add to `src/backend/app/services/template_loader.py` before `generate_topology_from_template`:

```python
def _generate_ran_topology(resolved, bmc_password="password", external_access=False):
    """Generate topology for the 5G RAN lab template.
    
    Layout: hub cluster (1 or 3 CP nodes) + bastion + 3 blank SNO VMs,
    connected via 4 networks (cluster, bmc, sriov, ptp) + gateway.
    """
    nodes = []
    edges = []
    external_ips = []

    VM_SPACING = 400
    GW_Y = 0
    NET_ROW_Y = 150
    HUB_ROW_Y = 350
    SNO_ROW_Y = HUB_ROW_Y + 450

    control_count = resolved.get("control_count", 1)
    bastion_cfg = resolved.get("bastion", {})
    # Override bastion sizing for RAN lab
    bastion_cfg = {**bastion_cfg, "vcpus": 4, "ram_gb": 8, "disk_gb": 100}

    vm_x_start = 150
    bast_x = vm_x_start + (control_count + 1) * VM_SPACING
    net_x = vm_x_start + int((control_count / 2) * VM_SPACING)

    # Port forwards
    ocp_port_forwards = []
    if external_access:
        eip_id = _id()
        external_ips = [{"id": eip_id, "label": "RAN-Lab"}]
        ocp_port_forwards = [
            {"extIpId": eip_id, "extPort": "22", "intIp": "192.168.125.50", "intPort": "22", "proto": "tcp"},
            {"extIpId": eip_id, "extPort": "443", "intIp": "192.168.125.11", "intPort": "443", "proto": "tcp"},
            {"extIpId": eip_id, "extPort": "6443", "intIp": "192.168.125.10", "intPort": "6443", "proto": "tcp"},
            {"extIpId": eip_id, "extPort": "80", "intIp": "192.168.125.50", "intPort": "80", "proto": "tcp"},
        ]

    # ── Networks ──
    cluster_net = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x, "y": NET_ROW_Y},
        "data": {
            "name": "cluster-network",
            "label": "cluster-network",
            "subtype": "network",
            "cidr": "192.168.125.0/24",
            "dhcp": True,
            "icon": "\U0001f310",
        },
    }
    bmc_net = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": bast_x, "y": NET_ROW_Y},
        "data": {
            "name": "bmc",
            "label": "bmc",
            "subtype": "network",
            "cidr": "192.168.50.0/24",
            "dhcp": False,
            "networkType": "bmc",
            "bmcUsername": "admin",
            "bmcPassword": bmc_password,
            "icon": "\U0001f310",
        },
    }
    sriov_net = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x + VM_SPACING, "y": NET_ROW_Y},
        "data": {
            "name": "sriov-network",
            "label": "sriov-network",
            "subtype": "network",
            "cidr": "192.168.100.0/24",
            "dhcp": False,
            "icon": "\U0001f310",
        },
    }
    ptp_net = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x + 2 * VM_SPACING, "y": NET_ROW_Y},
        "data": {
            "name": "ptp-network",
            "label": "ptp-network",
            "subtype": "network",
            "cidr": "192.168.200.0/24",
            "dhcp": False,
            "icon": "\U0001f310",
        },
    }
    gw = {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": net_x, "y": GW_Y},
        "data": {
            "name": "gateway",
            "label": "gateway",
            "subtype": "gateway",
            "gatewayMode": "nat-portforward" if external_access else "nat",
            "portForwards": ocp_port_forwards,
            "outboundPolicy": "restrict",
            "outboundPorts": "53,80,443,123,icmp",
            "icon": "\U0001f310",
        },
    }

    nodes.extend([cluster_net, bmc_net, sriov_net, ptp_net, gw])
    edges.append(_gw_net_edge(gw["id"], cluster_net["id"]))

    # ── Bastion ──
    bast_vm, bast_disk, bast_disk_edge = _bastion_node(
        bast_x, HUB_ROW_Y, bastion_cfg, cluster_ip="192.168.125.50"
    )
    nodes.extend([bast_vm, bast_disk])
    edges.extend([
        bast_disk_edge,
        _net_edge(cluster_net["id"], bast_vm, 0),
        _net_edge(bmc_net["id"], bast_vm, 1, "bottom"),
    ])

    # ── Hub CP nodes ──
    for i in range(control_count):
        # Hub nodes get: cluster NIC (virtio) + BMC NIC (virtio) + PTP NIC (igb)
        vm, disk, disk_edge = _vm_node(
            f"hub-cp-{i}",
            resolved.get("control_vcpus", 16),
            resolved.get("control_ram_gb", 48) if i == 0 else 26,
            vm_x_start + i * VM_SPACING,
            HUB_ROW_Y,
            disk_gb=resolved.get("control_disk_gb", 120),
            bmc_ip=f"192.168.50.{10 + i}",
            cluster_ip=f"192.168.125.{10 + i}",
            tags={"AnsibleGroup": "controllers"},
        )
        # Add BMC NIC
        bmc_nic = {"id": f"nic-{_id()}", "name": "eth1", "mac": _bmc_mac(), "model": "virtio"}
        vm["data"]["nics"].append(bmc_nic)
        # Add PTP NIC (igb)
        ptp_nic = {"id": f"nic-{_id()}", "name": "eth2", "mac": _mac(), "model": "igb"}
        vm["data"]["nics"].append(ptp_nic)
        # Add second disk for LVMS
        dc_extra = {"id": f"dp-{_id()}", "name": "disk1", "bus": "virtio"}
        vm["data"]["diskControllers"].append(dc_extra)
        extra_disk_id = _id()
        extra_disk = {
            "id": extra_disk_id,
            "type": "storageNode",
            "position": {"x": vm["position"]["x"] - 190, "y": vm["position"]["y"] + 170},
            "data": {
                "label": f"hub-cp-{i}-disk1",
                "name": f"hub-cp-{i}-disk1",
                "size": 120,
                "format": "qcow2",
                "icon": "\U0001f6e2",
            },
        }
        extra_disk_edge = {
            "id": _id(),
            "source": extra_disk_id,
            "target": vm["id"],
            "sourceHandle": "right",
            "targetHandle": f"dp-{dc_extra['id']}-left",
            "type": "smoothstep",
            "style": {"stroke": "rgba(251,191,36,0.6)", "strokeWidth": 2, "strokeDasharray": "4 4"},
            "animated": False,
            "className": "edge-storage-pulse",
        }

        nodes.extend([vm, disk, extra_disk])
        edges.extend([
            disk_edge,
            extra_disk_edge,
            _net_edge(cluster_net["id"], vm, 0),
            _net_edge(bmc_net["id"], vm, 1, "bottom"),
            _net_edge(ptp_net["id"], vm, 2, "bottom"),
        ])

    # ── SNO VMs ──
    sno_names = ["sno-seed", "sno-abi", "sno-ibi"]
    for i, sno_name in enumerate(sno_names):
        vm, disks, disk_edges = _sno_node(
            sno_name,
            vcpus=12,
            ram=24,
            x=vm_x_start + i * VM_SPACING,
            y=SNO_ROW_Y,
            disk_gb=200,
            cluster_ip=f"192.168.125.{20 + i}",
        )
        nodes.append(vm)
        nodes.extend(disks)
        edges.extend(disk_edges)
        edges.append(_net_edge(cluster_net["id"], vm, 0))
        edges.append(_net_edge(ptp_net["id"], vm, 1, "bottom"))
        edges.append(_net_edge(sriov_net["id"], vm, 2, "bottom"))
        # Second SR-IOV NIC connects to same sriov network
        edges.append(_net_edge(sriov_net["id"], vm, 3, "bottom"))

    return {"nodes": nodes, "edges": edges, "externalIps": external_ips}
```

- [ ] **Step 3: Wire the RAN generator into `generate_topology_from_template`**

Modify `generate_topology_from_template()` in `src/backend/app/services/template_loader.py` to dispatch to the RAN generator when the template name matches. Add at the top of the function:

```python
def generate_topology_from_template(
    resolved: dict,
    bmc_password: str = "password",
    external_access: bool = False,
) -> dict:
    # Dispatch to specialized generators
    if resolved.get("name") == "ocp-ran-5g":
        return _generate_ran_topology(resolved, bmc_password, external_access)

    # ... existing code unchanged ...
```

- [ ] **Step 4: Write tests for the RAN topology**

Add to `src/backend/tests/test_template_loader.py`:

```python
def test_generate_ran_topology_node_counts():
    from app.services.template_loader import generate_topology_from_template, resolve_template

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)
    nodes = topo["nodes"]

    vm_nodes = [n for n in nodes if n["type"] == "vmNode"]
    net_nodes = [n for n in nodes if n["type"] == "networkNode"]
    storage_nodes = [n for n in nodes if n["type"] == "storageNode"]

    # SNO hub (1 CP) + bastion + 3 SNOs = 5 VMs
    assert len(vm_nodes) == 5
    # cluster + bmc + sriov + ptp + gateway = 5 networks
    assert len(net_nodes) == 5

    vm_names = [n["data"]["name"] for n in vm_nodes]
    assert "hub-cp-0" in vm_names
    assert "bastion" in vm_names
    assert "sno-seed" in vm_names
    assert "sno-abi" in vm_names
    assert "sno-ibi" in vm_names


def test_generate_ran_topology_sno_vms_are_blank():
    from app.services.template_loader import generate_topology_from_template, resolve_template

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    sno_vms = [n for n in topo["nodes"] if n["type"] == "vmNode" and n["data"]["name"].startswith("sno-")]
    for vm in sno_vms:
        assert vm["data"]["os"] == "blank"
        assert vm["data"]["powerOnAtDeploy"] is False
        assert vm["data"]["bmcEnabled"] is True
        assert vm["data"]["firmware"] == "uefi"


def test_generate_ran_topology_sno_igb_nics():
    from app.services.template_loader import generate_topology_from_template, resolve_template

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    sno_vms = [n for n in topo["nodes"] if n["type"] == "vmNode" and n["data"]["name"].startswith("sno-")]
    for vm in sno_vms:
        nics = vm["data"]["nics"]
        # 4 NICs: cluster (virtio), ptp (igb), sriov x2 (igb)
        assert len(nics) == 4
        assert nics[0]["model"] == "virtio"  # cluster
        assert nics[1]["model"] == "igb"     # ptp
        assert nics[2]["model"] == "igb"     # sriov 0
        assert nics[3]["model"] == "igb"     # sriov 1


def test_generate_ran_topology_hub_has_bmc_and_ptp_nics():
    from app.services.template_loader import generate_topology_from_template, resolve_template

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    hub = next(n for n in topo["nodes"] if n["type"] == "vmNode" and n["data"]["name"] == "hub-cp-0")
    nics = hub["data"]["nics"]
    # 3 NICs: cluster (virtio), bmc (virtio), ptp (igb)
    assert len(nics) == 3
    assert nics[0]["model"] == "virtio"
    assert nics[1]["model"] == "virtio"
    assert nics[2]["model"] == "igb"


def test_generate_ran_topology_network_cidrs():
    from app.services.template_loader import generate_topology_from_template, resolve_template

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)

    nets = {n["data"]["name"]: n["data"].get("cidr", "") for n in topo["nodes"] if n["type"] == "networkNode"}
    assert nets["cluster-network"] == "192.168.125.0/24"
    assert nets["bmc"] == "192.168.50.0/24"
    assert nets["sriov-network"] == "192.168.100.0/24"
    assert nets["ptp-network"] == "192.168.200.0/24"
```

- [ ] **Step 5: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v
```

Expected: all tests pass.

---

### Task 4: RAN-specific OCP customization

The existing `agent_template.py` customizes DNS records, bastion cloud-init, install-config, and agent-config for the hub cluster. For the RAN template, we need different defaults: domain `5g-deployment.lab`, cluster name `hub`, and the bastion cloud-init must set up lab services (Gitea, MinIO, registry, dnsmasq, Showroom) instead of the standard OCP desktop experience.

This task handles the DNS and install-config changes. The bastion cloud-init for lab services is a large separate piece (Task 5).

**Files:**
- Modify: `src/backend/app/services/ocp/agent_template.py` — handle RAN template defaults in `customize_topology()`
- Modify: `src/backend/app/api/projects.py` — pass RAN-specific defaults when template is `ocp-ran-5g`
- Test: `src/backend/tests/test_template_loader.py` — verify RAN customization

**Interfaces:**
- Consumes: `customize_topology(topology, "ocp-ran-5g", config)` from Task 3's topology
- Produces: customized topology with RAN-specific DNS records, install-config, agent-config

- [ ] **Step 1: Set RAN defaults in the projects API**

In `src/backend/app/api/projects.py`, after `resolved = resolve_template(template_id)` (~line 204), add defaults for the RAN template:

```python
    try:
        resolved = resolve_template(template_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Template '{template_id}' not found"
        )

    # RAN template defaults
    if template_id == "ocp-ran-5g":
        body.setdefault("cluster_name", "hub")
        body.setdefault("base_domain", "5g-deployment.lab")
        body.setdefault("bastion_bmc_ip", "192.168.50.50")
```

- [ ] **Step 2: Handle SNO hub mode in agent_template.py**

The RAN hub in SNO mode has `control_count: 1`, which is the same as the `ocp-sno` template path. The existing code already handles SNO vs multi-node in `_build_install_config`. No changes needed for install-config — the existing logic works because the template resolves to `control_count: 1`.

However, `customize_topology` uses `template_id` to decide SNO vs multi-node. For the RAN template, we need to check the actual control count instead. In `src/backend/app/services/ocp/agent_template.py`, update the SNO detection in `customize_topology()`:

```python
def customize_topology(topology: dict, template_id: str, config: dict) -> dict:
    # ... existing variable extraction ...

    api_vip = "10.0.0.2"
    ingress_vip = "10.0.0.3"

    # Use template-specific VIPs
    if template_id == "ocp-ran-5g":
        api_vip = "192.168.125.10"
        ingress_vip = "192.168.125.11"

    # Detect SNO by counting CP nodes instead of only checking template_id
    cp_count = sum(1 for n in topology.get("nodes", [])
                   if n.get("type") == "vmNode"
                   and (n.get("data", {}).get("name", "").startswith("cp-")
                        or n.get("data", {}).get("name", "").startswith("hub-cp-")
                        or "sno" in n.get("data", {}).get("name", "")))
    is_sno = cp_count == 1 or template_id == "ocp-sno"

    if is_sno:
        sno_ip = _find_sno_node_ip(topology)
        if sno_ip:
            dns_api = sno_ip
            dns_ingress = sno_ip
        else:
            dns_api = api_vip
            dns_ingress = ingress_vip
    else:
        dns_api = api_vip
        dns_ingress = ingress_vip
```

Also update `_find_sno_node_ip` to recognize hub-cp-0 as a potential SNO node:

```python
def _find_sno_node_ip(topology):
    cp_nodes = []
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        name = node.get("data", {}).get("name", "")
        nics = node.get("data", {}).get("nics", [])
        ip = nics[0].get("ip") if nics else None
        if "sno" in name and ip:
            return ip
        if (name.startswith("cp-") or name.startswith("hub-cp-")) and ip:
            cp_nodes.append(ip)
    if len(cp_nodes) == 1:
        return cp_nodes[0]
    return None
```

- [ ] **Step 3: Update `_build_install_config` to handle RAN template**

The existing function uses `template_id in ("ocp-compact", "ocp-sno")` to determine worker/master counts. For RAN, we need to check the actual topology. Replace the hardcoded checks with CP count detection:

In `_build_install_config`, change:

```python
    num_workers = 0 if template_id in ("ocp-compact", "ocp-sno") else 2
    num_masters = 1 if template_id == "ocp-sno" else 3
```

To:

```python
    # Count actual CP and worker nodes from topology
    cp_nodes_count = sum(1 for n in topology.get("nodes", [])
                         if n.get("type") == "vmNode"
                         and n.get("data", {}).get("tags", {}).get("AnsibleGroup") == "controllers")
    worker_nodes_count = sum(1 for n in topology.get("nodes", [])
                             if n.get("type") == "vmNode"
                             and n.get("data", {}).get("tags", {}).get("AnsibleGroup") == "workers")
    num_masters = cp_nodes_count if cp_nodes_count > 0 else (1 if template_id == "ocp-sno" else 3)
    num_workers = worker_nodes_count
```

And update the SNO platform check:

```python
    if num_masters == 1 and num_workers == 0:
        ic_lines.extend([
            "platform:",
            "  none: {}",
        ])
    else:
        ic_lines.extend([
            "platform:",
            "  baremetal:",
            "    apiVIPs:",
            f"      - {api_vip}",
            "    ingressVIPs:",
            f"      - {ingress_vip}",
            "    hosts:",
        ])
```

- [ ] **Step 4: Write test for RAN customization**

Add to `src/backend/tests/test_template_loader.py`:

```python
def test_ran_topology_dns_records():
    from app.services.template_loader import generate_topology_from_template, resolve_template
    from app.services.ocp.agent_template import customize_topology

    resolved = resolve_template("ocp-ran-5g", templates_dir=TEMPLATES_DIR)
    topo = generate_topology_from_template(resolved)
    customize_topology(topo, "ocp-ran-5g", {
        "cluster_name": "hub",
        "base_domain": "5g-deployment.lab",
        "bastion_bmc_ip": "192.168.50.50",
    })

    cluster_net = next(n for n in topo["nodes"]
                       if n["type"] == "networkNode" and n["data"]["name"] == "cluster-network")
    dns = cluster_net["data"].get("dnsRecords", [])
    dns_names = [r["name"] for r in dns]
    assert "api.hub.5g-deployment.lab" in dns_names
    assert "api-int.hub.5g-deployment.lab" in dns_names
    assert ".apps.hub.5g-deployment.lab" in dns_names
```

- [ ] **Step 5: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v
```

Expected: all tests pass.

---

### Task 5: Bastion cloud-init for RAN lab services

The bastion needs cloud-init that installs and configures: container registry, Gitea, MinIO, dnsmasq, and Showroom. This is the most complex piece — it's a large `runcmd` block. The Showroom setup should be modular (reusable for future labs).

**Files:**
- Create: `src/backend/app/services/ocp/ran_bastion.py` — generates the bastion cloud-init user-data for the RAN lab
- Modify: `src/backend/app/services/ocp/agent_template.py` — call RAN bastion setup when template is `ocp-ran-5g`

**Interfaces:**
- Consumes: topology dict, config dict (bastion_password, pull_secret, student_name, lab_version)
- Produces: cloud-init user-data string appended to bastion node's `ciUserData`

- [ ] **Step 1: Create `ran_bastion.py` with the lab services cloud-init generator**

Create `src/backend/app/services/ocp/ran_bastion.py`:

```python
"""Cloud-init generator for the 5G RAN lab bastion.

Produces runcmd blocks that set up:
- Container registry (port 8443)
- Gitea git server (port 3000)
- MinIO S3 storage (port 9002)
- dnsmasq DNS/DHCP
- Showroom lab guide UI (port 80/443 via Traefik)
"""

LAB_REPO = "https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git"
LAB_VERSION = "lab-4.20"
REGISTRY_HOST = "infra.5g-deployment.lab:8443"


def generate_bastion_cloud_init(
    bastion_password: str,
    student_name: str = "lab-user",
    lab_version: str = LAB_VERSION,
    bastion_hostname: str = "",
) -> str:
    """Return cloud-init runcmd blocks for RAN lab bastion services."""
    lines = []
    lines.append(_registry_block())
    lines.append(_gitea_block(lab_version))
    lines.append(_minio_block())
    lines.append(_dnsmasq_block())
    lines.append(_showroom_block(student_name, bastion_password, lab_version, bastion_hostname))
    return "\n".join(lines)


def _registry_block():
    return (
        "  - |\n"
        "    # Container registry setup\n"
        "    mkdir -p /opt/registry/{auth,certs,data,conf}\n"
        "    # Generate self-signed cert\n"
        "    openssl req -newkey rsa:4096 -nodes -sha256 -keyout /opt/registry/certs/registry-key.pem "
        "-x509 -days 365 -out /opt/registry/certs/registry-cert.pem "
        "-subj '/CN=infra.5g-deployment.lab' -addext 'subjectAltName=DNS:infra.5g-deployment.lab'\n"
        "    cp /opt/registry/certs/registry-cert.pem /etc/pki/ca-trust/source/anchors/\n"
        "    update-ca-trust\n"
        "    dnf install -y httpd-tools\n"
        "    htpasswd -bBc /opt/registry/auth/htpasswd admin 'r3dh4t1!'\n"
        "    cat > /opt/registry/conf/config.yml << 'REGEOF'\n"
        "    version: 0.1\n"
        "    log:\n"
        "      fields:\n"
        "        service: registry\n"
        "    storage:\n"
        "      filesystem:\n"
        "        rootdirectory: /var/lib/registry\n"
        "    http:\n"
        "      addr: :8443\n"
        "      tls:\n"
        "        certificate: /certs/registry-cert.pem\n"
        "        key: /certs/registry-key.pem\n"
        "    auth:\n"
        "      htpasswd:\n"
        "        realm: Registry\n"
        "        path: /auth/htpasswd\n"
        "    REGEOF\n"
        "    podman run -d --name registry --restart=always "
        "-p 8443:8443 "
        "-v /opt/registry/data:/var/lib/registry:z "
        "-v /opt/registry/auth:/auth:z "
        "-v /opt/registry/certs:/certs:z "
        "-v /opt/registry/conf/config.yml:/etc/docker/registry/config.yml:z "
        "docker.io/library/registry:2\n"
    )


def _gitea_block(lab_version):
    return (
        "  - |\n"
        "    # Gitea git server setup\n"
        "    mkdir -p /opt/gitea\n"
        "    chown 1000:1000 /opt/gitea\n"
        "    podman run -d --name gitea --restart=always "
        "-p 3000:3000 -p 2222:22 "
        "-v /opt/gitea:/data:z "
        "docker.io/gitea/gitea:1.21\n"
        "    sleep 10\n"
        "    # Create admin user and mirror lab repo\n"
        "    podman exec --user 1000 gitea /bin/sh -c "
        "'gitea admin user create --username student --password student "
        "--email student@5g-deployment.lab --must-change-password=false --admin' || true\n"
        "    sleep 5\n"
        f"    curl -s -u student:student -X POST http://localhost:3000/api/v1/repos/migrate "
        "-H 'Content-Type: application/json' "
        f"-d '{{\"service\":\"2\",\"clone_addr\":\"https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git\",\"uid\":1,\"repo_name\":\"5g-ran-deployments-on-ocp-lab\"}}' || true\n"
    )


def _minio_block():
    return (
        "  - |\n"
        "    # MinIO S3 storage setup\n"
        "    mkdir -p /opt/minio/s3-volume\n"
        "    podman run -d --name minio --restart=always "
        "-p 9002:9000 -p 9001:9001 "
        "-v /opt/minio/s3-volume:/data:z "
        "-e MINIO_ROOT_USER=admin "
        "-e MINIO_ROOT_PASSWORD=admin1234 "
        "quay.io/minio/minio:latest server /data --console-address ':9001'\n"
        "    sleep 5\n"
        "    # Install mc client and create buckets\n"
        "    curl -sL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/bin/mc\n"
        "    chmod 755 /usr/bin/mc\n"
        "    mc alias set minio http://localhost:9002 admin admin1234 || true\n"
        "    mc mb minio/sno-abi minio/sno-ibi minio/logs minio/multiclusterobservability 2>/dev/null || true\n"
    )


def _dnsmasq_block():
    return (
        "  - |\n"
        "    # dnsmasq DNS setup for lab domain\n"
        "    dnf install -y dnsmasq\n"
        "    mkdir -p /opt/dnsmasq/include.d\n"
        "    BASTION_IP=$(ip -4 addr show eth0 | grep -oP '(?<=inet )\\S+' | cut -d/ -f1)\n"
        "    cat > /opt/dnsmasq/dnsmasq.conf << 'DNSEOF'\n"
        "    strict-order\n"
        "    bind-dynamic\n"
        "    bogus-priv\n"
        "    dhcp-authoritative\n"
        "    conf-dir=/opt/dnsmasq/include.d\n"
        "    DNSEOF\n"
        "    # Hub cluster DNS entries\n"
        "    cat > /opt/dnsmasq/include.d/hub.ipv4 << HUBEOF\n"
        "    address=/api.hub.5g-deployment.lab/192.168.125.10\n"
        "    address=/api-int.hub.5g-deployment.lab/192.168.125.10\n"
        "    address=/.apps.hub.5g-deployment.lab/192.168.125.11\n"
        "    address=/infra.5g-deployment.lab/${BASTION_IP}\n"
        "    HUBEOF\n"
        "    # Start dnsmasq\n"
        "    dnsmasq -C /opt/dnsmasq/dnsmasq.conf --no-daemon &\n"
    )


def _showroom_block(student_name, password, lab_version, bastion_hostname):
    """Modular Showroom setup — reusable for future lab templates."""
    return (
        "  - |\n"
        "    # Showroom lab guide UI setup\n"
        "    mkdir -p /opt/showroom/lab-content\n"
        f"    git clone --single-branch -b {lab_version} "
        f"https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab.git "
        "/opt/showroom/lab-repo\n"
        "    # Build lab docs with Antora\n"
        "    podman run --rm -v /opt/showroom/lab-repo:/antora:z "
        "quay.io/rhsysdeseng/showroom:antora-v3.0.0 site.yml\n"
        "    cp -r /opt/showroom/lab-repo/gh-pages/* /opt/showroom/lab-content/\n"
        "    # Serve lab content via Apache\n"
        "    podman run -d --name showroom-apache --restart=always "
        "-p 8888:8080 "
        "-v /opt/showroom/lab-content:/var/www/html:z "
        "quay.io/fedora/httpd-24-micro:2.4\n"
        "    # Wetty web terminal\n"
        "    podman run -d --name showroom-wetty --restart=always "
        "--network host "
        f"-e SSHHOST=127.0.0.1 -e SSHPORT=22 -e SSHUSER={student_name} "
        f"-e SSHPASS={password} "
        "-e BASE=/terminal "
        "quay.io/rhsysdeseng/showroom:wetty\n"
    )
```

- [ ] **Step 2: Wire RAN bastion into agent_template.py**

In `src/backend/app/services/ocp/agent_template.py`, at the end of `customize_topology()` (before `return topology`), add:

```python
    # RAN lab bastion — replace standard OCP bastion setup
    if template_id == "ocp-ran-5g":
        from app.services.ocp.ran_bastion import generate_bastion_cloud_init

        for node in topology.get("nodes", []):
            if node.get("type") == "vmNode" and node.get("data", {}).get("name") == "bastion":
                ran_ci = generate_bastion_cloud_init(
                    bastion_password=bastion_password,
                    student_name="lab-user",
                )
                # Append RAN-specific blocks after standard cloud-init
                node["data"]["ciUserData"] = node["data"].get("ciUserData", "") + ran_ci
                break

    return topology
```

- [ ] **Step 3: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py -v
```

Expected: all existing tests still pass.

---

### Task 6: Frontend — add RAN template to new-project dialog

The frontend needs to show the `ocp-ran-5g` template in the template picker. The existing template list comes from the API (`GET /templates`), which reads from the YAML files. Since we created the YAML in Task 2, the template will auto-appear. However, we should verify and ensure the `hub_mode` parameter is exposed if needed.

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx:772` (already done in Task 1)
- Verify: template appears in `GET /templates` API response

**Interfaces:**
- Consumes: `GET /api/templates` endpoint (already returns all YAML templates with `extends`)
- Produces: RAN template visible in UI template picker

- [ ] **Step 1: Verify template appears in API**

Start the backend and verify:

```bash
curl -s http://localhost:8200/api/templates | python3 -m json.tool
```

Expected: `ocp-ran-5g` appears in the list with display name "5G RAN Lab (ACM + ZTP + GitOps)".

- [ ] **Step 2: Test creating a project from the RAN template via API**

```bash
curl -s -X POST http://localhost:8200/api/projects/from-template \
  -H 'Content-Type: application/json' \
  -d '{
    "template_id": "ocp-ran-5g",
    "name": "RAN Lab Test",
    "bastion_password": "redhat123"
  }' | python3 -m json.tool
```

Expected: project created successfully, returns `{"id": "...", "name": "RAN Lab Test"}`.

- [ ] **Step 3: Verify topology in the UI**

Open http://localhost:3100, navigate to the new project. Verify:
- 5 VMs visible: hub-cp-0, bastion, sno-seed, sno-abi, sno-ibi
- 5 network nodes: cluster-network, bmc, sriov-network, ptp-network, gateway
- SNO VMs show igb NICs in the properties panel
- SNO VMs show as powered off (powerOnAtDeploy: false)
- Hub CP has 3 NICs (cluster, bmc, ptp)

---

### Task 7: Run full test suite and commit

**Files:**
- All files modified in Tasks 1-6

- [ ] **Step 1: Run black on all modified Python files**

```bash
black src/backend/app/services/deploy_service.py src/backend/app/services/template_loader.py src/backend/app/services/ocp/agent_template.py src/backend/app/services/ocp/ran_bastion.py src/backend/app/api/projects.py src/backend/tests/test_template_loader.py
```

- [ ] **Step 2: Run full test suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Run frontend type check**

```bash
cd src/frontend && npx tsc --noEmit
```

Expected: no type errors.

- [ ] **Step 4: Commit all changes**

```bash
cd /Users/prutledg/troshka
git add src/backend/templates/ocp-ran-5g.yaml \
        src/backend/app/services/deploy_service.py \
        src/backend/app/services/template_loader.py \
        src/backend/app/services/ocp/agent_template.py \
        src/backend/app/services/ocp/ran_bastion.py \
        src/backend/app/api/projects.py \
        src/backend/tests/test_template_loader.py \
        src/troshkad/troshkad.py \
        src/frontend/src/components/canvas/PropertiesPanel.tsx \
        docs/superpowers/specs/2026-06-17-ocp-ran-5g-template-design.md
git commit -m "feat: add 5G RAN lab template with igb NIC model support

New ocp-ran-5g template generates hub cluster + 3 blank SNO VMs for the
5G RAN RDS Deployments lab. Supports toggling between SNO and compact
hub modes. Adds igb NIC model for SR-IOV/PTP emulation without hardware.
Bastion cloud-init sets up lab services (Gitea, MinIO, registry, dnsmasq,
Showroom)."
```
