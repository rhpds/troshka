# OpenShift on Troshka — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a general-purpose load balancer network node (HAProxy) and DNS provider integration to Troshka, enabling pre-built OpenShift clusters to be stamped out as patterns.

**Architecture:** New `loadbalancer` network node type runs HAProxy in the project's network namespace (same lifecycle pattern as dnsmasq/BMC). DNS Provider model stores BIND/Route53 credentials; deploy-from-pattern accepts GUID + domain to create DNS records. Topology templates provide pre-wired OCP starter layouts.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, HAProxy, dnspython, React/TypeScript, PatternFly 6, React Flow, Zustand

**Spec:** `docs/superpowers/specs/2026-06-10-ocp-on-troshka-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `src/backend/app/models/dns_provider.py` | DNSProvider SQLAlchemy model |
| `src/backend/app/schemas/dns_provider.py` | Pydantic request/response schemas for DNS provider |
| `src/backend/app/api/dns_providers.py` | CRUD API routes for DNS providers (admin) |
| `src/backend/app/services/dns_service.py` | DNS record create/delete dispatch (NSUPDATE + Route53) |
| `src/backend/app/services/haproxy_config.py` | HAProxy config generation from topology |
| `src/backend/tests/test_dns_providers.py` | DNS Provider CRUD API tests |
| `src/backend/tests/test_haproxy_config.py` | HAProxy config generation tests |
| `src/backend/tests/test_dns_service.py` | DNS service record creation/deletion tests |
| `src/backend/alembic/versions/XXXX_add_dns_provider_and_project_fields.py` | Migration: dns_providers table + project columns |

### Modified Files
| File | Changes |
|------|---------|
| `src/backend/app/models/__init__.py` | Register DNSProvider model |
| `src/backend/app/models/project.py` | Add `guid`, `domain`, `dns_provider_id` columns |
| `src/backend/app/main.py` | Register dns_providers router |
| `src/backend/app/services/vxlan.py` | Build LB config from topology (alongside gateway/router) |
| `src/backend/app/services/deploy_service.py` | Add LB setup step, DNS record creation, accept GUID/domain on pattern deploy |
| `src/backend/app/api/patterns.py` | Extend deploy-from-pattern endpoint with GUID/domain/dns_provider_id |
| `src/backend/app/schemas/pattern.py` | Add fields to pattern deploy schema |
| `src/backend/app/services/provisioner.py` | Add `haproxy` to cloud-init packages |
| `src/troshkad/troshkad.py` | Add HAProxy setup/teardown handlers |
| `src/frontend/src/components/canvas/Palette.tsx` | Add Load Balancer to networking palette |
| `src/frontend/src/components/canvas/nodes/NetworkNode.tsx` | Render LB node with distinct icon and frontend list |
| `src/frontend/src/components/canvas/PropertiesPanel.tsx` | LB config panel (add/remove frontends, port mappings) |
| `src/frontend/src/components/canvas/Canvas.tsx` | Handle LB node drop, validate LB connections |
| `src/frontend/src/stores/canvasStore.ts` | LB edge styling, connection validation for LB→VM |

---

## Task 1: HAProxy Config Generation Service

**Files:**
- Create: `src/backend/app/services/haproxy_config.py`
- Create: `src/backend/tests/test_haproxy_config.py`

- [ ] **Step 1: Write the failing test for basic config generation**

```python
# src/backend/tests/test_haproxy_config.py
from app.services.haproxy_config import generate_haproxy_config


def test_generate_basic_tcp_config():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
        {"name": "ingress-https", "bindPort": 443, "mode": "tcp", "backendPort": 443},
    ]
    backends = [
        {"name": "cp-0", "ip": "10.0.0.10"},
        {"name": "cp-1", "ip": "10.0.0.11"},
        {"name": "cp-2", "ip": "10.0.0.12"},
    ]
    config = generate_haproxy_config(frontends, backends)

    assert "frontend api" in config
    assert "bind *:6443" in config
    assert "default_backend api-servers" in config
    assert "server cp-0 10.0.0.10:6443 check" in config
    assert "server cp-1 10.0.0.11:6443 check" in config
    assert "server cp-2 10.0.0.12:6443 check" in config
    assert "frontend ingress-https" in config
    assert "bind *:443" in config


def test_generate_config_with_health_check():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    ]
    backends = [
        {"name": "cp-0", "ip": "10.0.0.10"},
    ]
    config = generate_haproxy_config(frontends, backends)
    assert "balance roundrobin" in config
    assert "mode tcp" in config


def test_generate_config_empty_backends():
    frontends = [
        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    ]
    config = generate_haproxy_config(frontends, backends=[])
    assert "frontend api" in config
    assert "backend api-servers" in config


def test_generate_config_global_and_defaults():
    config = generate_haproxy_config(
        [{"name": "x", "bindPort": 80, "mode": "tcp", "backendPort": 80}],
        [{"name": "s1", "ip": "10.0.0.1"}],
    )
    assert "global" in config
    assert "maxconn" in config
    assert "timeout connect" in config
    assert "timeout client" in config
    assert "timeout server" in config
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_haproxy_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.haproxy_config'`

- [ ] **Step 3: Implement haproxy_config.py**

```python
# src/backend/app/services/haproxy_config.py


def _sanitize_name(name: str) -> str:
    return name.replace(" ", "-").lower()


def generate_haproxy_config(frontends: list[dict], backends: list[dict]) -> str:
    lines = [
        "global",
        "    daemon",
        "    maxconn 4096",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 30s",
        "    timeout server 30s",
        "    option tcplog",
        "",
    ]

    for fe in frontends:
        fe_name = _sanitize_name(fe["name"])
        be_name = f"{fe_name}-servers"
        backend_port = fe["backendPort"]

        lines.append(f"frontend {fe_name}")
        lines.append(f"    bind *:{fe['bindPort']}")
        lines.append(f"    default_backend {be_name}")
        lines.append("")

        lines.append(f"backend {be_name}")
        lines.append("    balance roundrobin")
        for be in backends:
            lines.append(f"    server {be['name']} {be['ip']}:{backend_port} check")
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_haproxy_config.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/haproxy_config.py src/backend/tests/test_haproxy_config.py
git commit -m "feat: add HAProxy config generation service"
```

---

## Task 2: LB Config Builder in vxlan.py

**Files:**
- Modify: `src/backend/app/services/vxlan.py` (add LB config extraction alongside gateway config at ~line 208)

The `build_host_network_config()` function already extracts gateway and router configs from topology. The LB config follows the same pattern: find the `loadbalancer` networkNode, extract its frontends, find connected VM IPs from edges.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_haproxy_config.py (append to existing file)
from app.services.vxlan import build_host_network_config


def test_build_lb_config_from_topology():
    topology = {
        "nodes": [
            {
                "id": "net-1",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "name": "cluster",
                    "cidr": "10.0.0.0/24",
                    "dhcp": True,
                },
            },
            {
                "id": "lb-1",
                "type": "networkNode",
                "data": {
                    "subtype": "loadbalancer",
                    "networkType": "loadbalancer",
                    "name": "ocp-lb",
                    "frontends": [
                        {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
                        {"name": "ingress", "bindPort": 443, "mode": "tcp", "backendPort": 443},
                    ],
                },
            },
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "name": "cp-0",
                    "nics": [{"id": "nic-1", "ip": "10.0.0.10"}],
                },
            },
            {
                "id": "vm-2",
                "type": "vmNode",
                "data": {
                    "name": "cp-1",
                    "nics": [{"id": "nic-2", "ip": "10.0.0.11"}],
                },
            },
        ],
        "edges": [
            {"source": "net-1", "target": "vm-1", "sourceHandle": "net-1-bottom", "targetHandle": "nic-1-top"},
            {"source": "net-1", "target": "vm-2", "sourceHandle": "net-1-bottom", "targetHandle": "nic-2-top"},
            {"source": "lb-1", "target": "vm-1", "sourceHandle": "lb-1-bottom", "targetHandle": "nic-1-top"},
            {"source": "lb-1", "target": "vm-2", "sourceHandle": "lb-1-bottom", "targetHandle": "nic-2-top"},
        ],
    }
    vni_map = {"net-1": 100}

    result = build_host_network_config(topology, vni_map, peer_ips=[])
    assert result.get("loadbalancer") is not None
    lb = result["loadbalancer"]
    assert len(lb["frontends"]) == 2
    assert lb["frontends"][0]["name"] == "api"
    assert len(lb["backends"]) == 2
    backend_ips = {b["ip"] for b in lb["backends"]}
    assert "10.0.0.10" in backend_ips
    assert "10.0.0.11" in backend_ips
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_haproxy_config.py::test_build_lb_config_from_topology -v`
Expected: FAIL — the function returns no `loadbalancer` key

- [ ] **Step 3: Add LB config extraction to vxlan.py**

In `src/backend/app/services/vxlan.py`, after the router config block (~line 257) and before the return statement (line 259), add LB config extraction. This follows the same pattern as the gateway block (line 208-236): find the node, extract data, find connected VMs via edges, resolve their IPs.

```python
    # Build load balancer config if present
    lb_config = None
    for node in nodes:
        if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "loadbalancer":
            data = node.get("data", {})
            node_id = node["id"]

            # Find connected VM IPs via edges
            connected_vm_ids = set()
            for edge in edges:
                other_id = edge["target"] if edge["source"] == node_id else edge["source"] if edge["target"] == node_id else None
                if other_id:
                    other_node = next((n for n in nodes if n["id"] == other_id), None)
                    if other_node and other_node.get("type") == "vmNode":
                        connected_vm_ids.add(other_id)

            backends = []
            for vm_id in connected_vm_ids:
                vm_node = next((n for n in nodes if n["id"] == vm_id), None)
                if not vm_node:
                    continue
                vm_data = vm_node.get("data", {})
                vm_name = vm_data.get("name", vm_id[:8])
                for nic in vm_data.get("nics", []):
                    ip = nic.get("ip")
                    if ip:
                        backends.append({"name": vm_name, "ip": ip})
                        break

            lb_config = {
                "name": data.get("name"),
                "frontends": data.get("frontends", []),
                "backends": backends,
                "dns_records": data.get("dnsRecords", []),
                "dns_ttl": data.get("dnsTtl", 30),
            }
            break
```

Then update the return statement to include it:

```python
    return {
        "networks": networks,
        "gateway": gateway_config,
        "routers": router_configs,
        "loadbalancer": lb_config,
        "vni_map": vni_map,
    }
```

Also ensure the LB node is excluded from VNI allocation (same as BMC). In `allocate_vnis_for_project()`, the filter at ~line 57 already filters by `subtype == "network"` — since LB uses `subtype: "loadbalancer"`, it's already excluded. Verify this is the case.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_haproxy_config.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/vxlan.py src/backend/tests/test_haproxy_config.py
git commit -m "feat: extract LB config from topology in vxlan.py"
```

---

## Task 3: Troshkad HAProxy Handlers

**Files:**
- Modify: `src/troshkad/troshkad.py` — add HAProxy setup/teardown handlers

The HAProxy handlers follow the exact pattern of dnsmasq (lines 1677-1751): generate config file, kill old process, start new process in namespace.

- [ ] **Step 1: Add HAProxy setup handler**

In `src/troshkad/troshkad.py`, add a new handler function `_handle_lb_setup` near the existing `_handle_dnsmasq_setup` function. Register it in the command dispatch table.

The handler receives:
```python
{
    "ns": "troshka-{pid}",
    "project_id": "abc123...",
    "frontends": [...],
    "backends": [{"name": "cp-0", "ip": "10.0.0.10"}, ...]
}
```

Implementation:

```python
def _handle_lb_setup(job, params):
    ns = params["ns"]
    pid = params["project_id"][:8]
    frontends = params.get("frontends", [])
    backends = params.get("backends", [])

    haproxy_conf = f"/etc/haproxy/troshka-{pid}.cfg"
    haproxy_pid = f"/run/troshka-haproxy-{pid}.pid"

    # Generate config
    lines = [
        "global",
        "    daemon",
        "    maxconn 4096",
        f"    pidfile {haproxy_pid}",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 30s",
        "    timeout server 30s",
        "    option tcplog",
        "",
    ]
    for fe in frontends:
        fe_name = fe["name"].replace(" ", "-").lower()
        be_name = f"{fe_name}-servers"
        lines.append(f"frontend {fe_name}")
        lines.append(f"    bind *:{fe['bindPort']}")
        lines.append(f"    default_backend {be_name}")
        lines.append("")
        lines.append(f"backend {be_name}")
        lines.append("    balance roundrobin")
        for be in backends:
            lines.append(f"    server {be['name']} {be['ip']}:{fe['backendPort']} check")
        lines.append("")

    config_content = "\n".join(lines)
    _log(job, f"Writing HAProxy config to {haproxy_conf}")
    with open(haproxy_conf, "w") as f:
        f.write(config_content)

    # Kill old HAProxy for this project
    try:
        if os.path.exists(haproxy_pid):
            with open(haproxy_pid) as f:
                old_pid = f.read().strip()
            if old_pid:
                _run_cmd(job, ["kill", "-9", old_pid], timeout=5, check=False)
    except Exception:
        pass

    # Start HAProxy in namespace
    _run_cmd(job, ["ip", "netns", "exec", ns, "haproxy", "-f", haproxy_conf, "-D", "-p", haproxy_pid], timeout=10)
    _log(job, f"HAProxy started in namespace {ns}")
```

- [ ] **Step 2: Add HAProxy teardown handler**

```python
def _handle_lb_teardown(job, params):
    pid = params["project_id"][:8]
    ns = params.get("ns", f"troshka-{pid}")

    haproxy_conf = f"/etc/haproxy/troshka-{pid}.cfg"
    haproxy_pid = f"/run/troshka-haproxy-{pid}.pid"

    # Kill HAProxy
    try:
        if os.path.exists(haproxy_pid):
            with open(haproxy_pid) as f:
                old_pid = f.read().strip()
            if old_pid:
                _run_cmd(job, ["kill", "-9", old_pid], timeout=5, check=False)
    except Exception:
        pass

    # Remove files
    for f in [haproxy_conf, haproxy_pid]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    _log(job, f"HAProxy teardown complete for project {pid}")
```

- [ ] **Step 3: Register handlers in the command dispatch table**

Find the command dispatch dictionary (search for `_COMMAND_HANDLERS` or the `if path ==` chain) and add:

```python
"/lb/setup": _handle_lb_setup,
"/lb/teardown": _handle_lb_teardown,
```

- [ ] **Step 4: Add HAProxy teardown to full-teardown**

In the existing `_handle_full_teardown` function (around line 1916), add a call to `_handle_lb_teardown` before the namespace deletion, similar to how dnsmasq is cleaned up:

```python
    # Teardown HAProxy if running
    _handle_lb_teardown(job, {"project_id": params["project_id"], "ns": ns})
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: add HAProxy setup/teardown handlers to troshkad"
```

---

## Task 4: Backend Deploy Integration for LB

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` — add LB setup step after network setup
- Modify: `src/backend/app/services/troshkad_client.py` — add LB client methods (if needed, or just use existing `troshkad_request`)

- [ ] **Step 1: Add LB setup call in deploy flow**

In `src/backend/app/services/deploy_service.py`, in the `deploy_project_async()` function, after the network setup step (~line 927) and before EIP allocation (~line 929), add the LB setup step:

```python
        # Setup Load Balancer (HAProxy) if present
        net_config = build_host_network_config(topology, vni_map, peer_ips)
        lb_config = net_config.get("loadbalancer")
        if lb_config:
            _deploy_progress[project_id] = {"step": "Setting up load balancer", "detail": "Starting HAProxy"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            first_vni = list(vni_map.values())[0] if vni_map else None
            ns = f"troshka-{project_id[:8]}"
            lb_params = {
                "ns": ns,
                "project_id": project_id,
                "frontends": lb_config["frontends"],
                "backends": lb_config["backends"],
            }
            result = troshkad_request(host, "POST", "/commands/lb/setup", body=lb_params)
            if not result or result.get("error"):
                project.state = "error"
                project.deploy_error = f"LB setup failed: {result}"
                s.commit()
                return
```

- [ ] **Step 2: Add LB port forwards to gateway config**

When the LB is present, its frontend ports need to be forwarded through the EIP, just like gateway port forwards. In the EIP/port-forward configuration step, add the LB ports.

Find the section where port forwards are configured for the gateway (~lines 929-983). After EIP assignment, if there's an LB config, add its frontend ports to the port forward list that gets passed to the nftables rules:

```python
        # Add LB frontend ports to EIP port forwards
        if lb_config:
            for fe in lb_config["frontends"]:
                # Add port forward: EIP:bindPort → transit_ns_ip:bindPort
                # (HAProxy listens on 0.0.0.0 inside the namespace)
                gateway_port_forwards.append({
                    "extPort": fe["bindPort"],
                    "intIp": transit_ns_ip,
                    "intPort": fe["bindPort"],
                })
```

The exact integration point depends on how gateway port forwards are currently structured. Read the deploy flow carefully and add LB ports alongside existing port forward logic.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py
git commit -m "feat: integrate LB setup into deploy flow with EIP port forwarding"
```

---

## Task 5: Add haproxy to host cloud-init packages

**Files:**
- Modify: `src/backend/app/services/provisioner.py` — add `haproxy` to the cloud-init package list

- [ ] **Step 1: Add haproxy package**

In `src/backend/app/services/provisioner.py`, find the cloud-init package list (around line 155-165 where `qemu-kvm`, `libvirt`, etc. are listed). Add `haproxy` to the list:

```python
    "haproxy",
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/provisioner.py
git commit -m "feat: add haproxy to host cloud-init package list"
```

---

## Task 6: DNS Provider Model + Migration

**Files:**
- Create: `src/backend/app/models/dns_provider.py`
- Modify: `src/backend/app/models/__init__.py`
- Modify: `src/backend/app/models/project.py`
- Create: `src/backend/alembic/versions/XXXX_add_dns_provider_and_project_fields.py`
- Create: `src/backend/tests/test_dns_providers.py`

- [ ] **Step 1: Write the failing model test**

```python
# src/backend/tests/test_dns_providers.py
import os
os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy.dialects import sqlite
sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from tests.conftest import TestSession


def test_create_dns_provider():
    from app.models.dns_provider import DnsProvider

    db = TestSession()
    provider = DnsProvider(
        name="Test BIND",
        type="nsupdate",
        config={
            "server": "10.0.0.53",
            "port": 53,
            "key_name": "update-key",
            "key_secret": "secret123",
            "key_algorithm": "hmac-sha256",
            "default_zone": "example.com",
        },
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    assert provider.id is not None
    assert len(provider.id) == 36
    assert provider.name == "Test BIND"
    assert provider.type == "nsupdate"
    assert provider.config["server"] == "10.0.0.53"
    assert provider.config["default_zone"] == "example.com"
    db.delete(provider)
    db.commit()
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_providers.py::test_create_dns_provider -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.dns_provider'`

- [ ] **Step 3: Create DNS Provider model**

```python
# src/backend/app/models/dns_provider.py
import datetime
import uuid

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DnsProvider(Base):
    __tablename__ = "dns_providers"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Register in models/__init__.py**

Add to `src/backend/app/models/__init__.py`:

```python
from app.models.dns_provider import DnsProvider
```

And add `"DnsProvider"` to the `__all__` list.

- [ ] **Step 5: Add project fields**

Add to `src/backend/app/models/project.py`, after the existing columns (~line 31):

```python
    guid: Mapped[str | None] = mapped_column(String(50), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dns_provider_id: Mapped[str | None] = mapped_column(ForeignKey("dns_providers.id"), nullable=True)
```

- [ ] **Step 6: Run model test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_providers.py -v`
Expected: PASS

- [ ] **Step 7: Create Alembic migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add dns provider and project guid domain fields"
```

Edit the generated migration file:

```python
def upgrade() -> None:
    op.create_table(
        "dns_providers",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("projects", sa.Column("guid", sa.String(50), nullable=True))
    op.add_column("projects", sa.Column("domain", sa.String(255), nullable=True))
    op.add_column("projects", sa.Column("dns_provider_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("dns_providers.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "dns_provider_id")
    op.drop_column("projects", "domain")
    op.drop_column("projects", "guid")
    op.drop_table("dns_providers")
```

Set `down_revision` to `'fa4247629ec7'` (current head).

- [ ] **Step 8: Run migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/dns_provider.py src/backend/app/models/__init__.py src/backend/app/models/project.py src/backend/alembic/versions/ src/backend/tests/test_dns_providers.py
git commit -m "feat: add DnsProvider model and project guid/domain fields"
```

---

## Task 7: DNS Provider Schemas + API

**Files:**
- Create: `src/backend/app/schemas/dns_provider.py`
- Create: `src/backend/app/api/dns_providers.py`
- Modify: `src/backend/app/main.py`
- Append: `src/backend/tests/test_dns_providers.py`

- [ ] **Step 1: Write failing API tests**

```python
# src/backend/tests/test_dns_providers.py (append to existing file)
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db

_db = TestSession()
_admin = _db.query(User).filter_by(role="admin").first()
if not _admin:
    _admin = User(email="dns-admin@example.com", display_name="Admin", role="admin",
                  auth_source="local", password_hash=hash_password("pass"))
    _db.add(_admin)
    _db.commit()
    _db.refresh(_admin)
ADMIN_TOKEN = create_jwt(user_id=_admin.id, email=_admin.email, role=_admin.role)
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
_db.close()

client = TestClient(app)


def test_create_dns_provider_api():
    resp = client.post("/api/v1/dns-providers", json={
        "name": "Test BIND API",
        "type": "nsupdate",
        "config": {
            "server": "10.0.0.53",
            "port": 53,
            "key_name": "update-key",
            "key_secret": "secret",
            "key_algorithm": "hmac-sha256",
            "default_zone": "example.com",
        },
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test BIND API"
    assert data["type"] == "nsupdate"


def test_list_dns_providers_api():
    resp = client.get("/api/v1/dns-providers", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_delete_dns_provider_api():
    create_resp = client.post("/api/v1/dns-providers", json={
        "name": "To Delete",
        "type": "nsupdate",
        "config": {"server": "1.2.3.4"},
    }, headers=ADMIN_HEADERS)
    pid = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/dns-providers/{pid}", headers=ADMIN_HEADERS)
    assert resp.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_providers.py::test_create_dns_provider_api -v`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 3: Create schemas**

```python
# src/backend/app/schemas/dns_provider.py
import datetime

from pydantic import BaseModel


class DnsProviderCreate(BaseModel):
    name: str
    type: str
    config: dict


class DnsProviderUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None


class DnsProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Create API routes**

```python
# src/backend/app/api/dns_providers.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.dns_provider import DnsProvider
from app.models.user import User
from app.schemas.dns_provider import DnsProviderCreate, DnsProviderResponse, DnsProviderUpdate

router = APIRouter(prefix="/dns-providers", tags=["dns-providers"])


@router.get("/", response_model=list[DnsProviderResponse])
def list_dns_providers(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    return db.query(DnsProvider).order_by(DnsProvider.name).all()


@router.get("/{provider_id}", response_model=DnsProviderResponse)
def get_dns_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    return provider


@router.post("/", response_model=DnsProviderResponse, status_code=201)
def create_dns_provider(body: DnsProviderCreate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    existing = db.query(DnsProvider).filter_by(name=body.name).first()
    if existing:
        raise HTTPException(409, "DNS provider with this name already exists")
    provider = DnsProvider(name=body.name, type=body.type, config=body.config)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch("/{provider_id}", response_model=DnsProviderResponse)
def update_dns_provider(provider_id: str, body: DnsProviderUpdate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    if body.name is not None:
        provider.name = body.name
    if body.config is not None:
        provider.config = body.config
    db.commit()
    db.refresh(provider)
    return provider


@router.delete("/{provider_id}", status_code=204)
def delete_dns_provider(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    provider = db.query(DnsProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(404, "DNS provider not found")
    db.delete(provider)
    db.commit()
```

- [ ] **Step 5: Register router in main.py**

In `src/backend/app/main.py`, add after the storage_pool imports (~line 114):

```python
from app.api import dns_providers as dns_provider_routes  # noqa: E402
```

And add after the storage_pool router (~line 128):

```python
app.include_router(dns_provider_routes.router, prefix="/api/v1")
```

- [ ] **Step 6: Run API tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_providers.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/schemas/dns_provider.py src/backend/app/api/dns_providers.py src/backend/app/main.py src/backend/tests/test_dns_providers.py
git commit -m "feat: add DNS Provider CRUD API"
```

---

## Task 8: DNS Service (NSUPDATE + Route53)

**Files:**
- Create: `src/backend/app/services/dns_service.py`
- Create: `src/backend/tests/test_dns_service.py`

- [ ] **Step 1: Write failing tests**

```python
# src/backend/tests/test_dns_service.py
import os
os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from unittest.mock import patch, MagicMock
from app.services.dns_service import resolve_dns_records, create_dns_records, delete_dns_records


def test_resolve_dns_records_replaces_tokens():
    templates = [
        {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
        {"name": "*.apps.{guid}.{domain}", "type": "A", "target": "eip"},
    ]
    records = resolve_dns_records(templates, guid="abc123", domain="lab.example.com", eip="1.2.3.4")
    assert records[0]["name"] == "api.abc123.lab.example.com"
    assert records[0]["value"] == "1.2.3.4"
    assert records[1]["name"] == "*.apps.abc123.lab.example.com"
    assert records[1]["value"] == "1.2.3.4"


def test_resolve_dns_records_handles_missing_tokens():
    templates = [
        {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
    ]
    records = resolve_dns_records(templates, guid="abc", domain="ex.com", eip=None)
    assert records[0]["value"] is None


@patch("app.services.dns_service._nsupdate_create")
def test_create_dns_records_nsupdate(mock_nsupdate):
    provider_config = {
        "server": "10.0.0.53",
        "port": 53,
        "key_name": "update-key",
        "key_secret": "secret",
        "key_algorithm": "hmac-sha256",
        "default_zone": "example.com",
    }
    records = [
        {"name": "api.abc.example.com", "type": "A", "value": "1.2.3.4"},
    ]
    create_dns_records("nsupdate", provider_config, records, ttl=30)
    mock_nsupdate.assert_called_once()


@patch("app.services.dns_service._nsupdate_delete")
def test_delete_dns_records_nsupdate(mock_nsupdate):
    provider_config = {
        "server": "10.0.0.53",
        "port": 53,
        "key_name": "update-key",
        "key_secret": "secret",
        "key_algorithm": "hmac-sha256",
        "default_zone": "example.com",
    }
    records = [
        {"name": "api.abc.example.com", "type": "A", "value": "1.2.3.4"},
    ]
    delete_dns_records("nsupdate", provider_config, records)
    mock_nsupdate.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement DNS service**

```python
# src/backend/app/services/dns_service.py
import logging
import subprocess

logger = logging.getLogger(__name__)


def resolve_dns_records(
    templates: list[dict],
    guid: str,
    domain: str,
    eip: str | None,
) -> list[dict]:
    records = []
    for tmpl in templates:
        name = tmpl["name"].replace("{guid}", guid).replace("{domain}", domain)
        value = eip if tmpl.get("target") == "eip" else tmpl.get("target")
        records.append({
            "name": name,
            "type": tmpl.get("type", "A"),
            "value": value,
        })
    return records


def create_dns_records(
    provider_type: str,
    provider_config: dict,
    records: list[dict],
    ttl: int = 30,
) -> list[str]:
    errors = []
    if provider_type == "nsupdate":
        _nsupdate_create(provider_config, records, ttl, errors)
    elif provider_type == "route53":
        _route53_create(provider_config, records, ttl, errors)
    else:
        errors.append(f"Unknown DNS provider type: {provider_type}")
    return errors


def delete_dns_records(
    provider_type: str,
    provider_config: dict,
    records: list[dict],
) -> list[str]:
    errors = []
    if provider_type == "nsupdate":
        _nsupdate_delete(provider_config, records, errors)
    elif provider_type == "route53":
        _route53_delete(provider_config, records, errors)
    else:
        errors.append(f"Unknown DNS provider type: {provider_type}")
    return errors


def _nsupdate_create(config: dict, records: list[dict], ttl: int, errors: list[str]):
    server = config["server"]
    port = config.get("port", 53)
    key_name = config["key_name"]
    key_secret = config["key_secret"]
    algorithm = config.get("key_algorithm", "hmac-sha256")
    zone = config.get("default_zone", "")

    commands = [f"server {server} {port}", f"zone {zone}"]
    for rec in records:
        if not rec.get("value"):
            continue
        commands.append(f"update add {rec['name']}. {ttl} {rec['type']} {rec['value']}")
    commands.append("send")
    commands.append("quit")

    nsupdate_input = "\n".join(commands) + "\n"

    try:
        result = subprocess.run(
            ["nsupdate", f"-y", f"{algorithm}:{key_name}:{key_secret}"],
            input=nsupdate_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = f"nsupdate failed: {result.stderr.strip()}"
            logger.error(err)
            errors.append(err)
        else:
            logger.info(f"DNS records created: {[r['name'] for r in records]}")
    except Exception as e:
        err = f"nsupdate error: {e}"
        logger.error(err)
        errors.append(err)


def _nsupdate_delete(config: dict, records: list[dict], errors: list[str]):
    server = config["server"]
    port = config.get("port", 53)
    key_name = config["key_name"]
    key_secret = config["key_secret"]
    algorithm = config.get("key_algorithm", "hmac-sha256")
    zone = config.get("default_zone", "")

    commands = [f"server {server} {port}", f"zone {zone}"]
    for rec in records:
        commands.append(f"update delete {rec['name']}. {rec['type']}")
    commands.append("send")
    commands.append("quit")

    nsupdate_input = "\n".join(commands) + "\n"

    try:
        result = subprocess.run(
            ["nsupdate", f"-y", f"{algorithm}:{key_name}:{key_secret}"],
            input=nsupdate_input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = f"nsupdate delete failed: {result.stderr.strip()}"
            logger.error(err)
            errors.append(err)
        else:
            logger.info(f"DNS records deleted: {[r['name'] for r in records]}")
    except Exception as e:
        err = f"nsupdate delete error: {e}"
        logger.error(err)
        errors.append(err)


def _route53_create(config: dict, records: list[dict], ttl: int, errors: list[str]):
    try:
        import boto3
        client = boto3.client(
            "route53",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
        )
        changes = []
        for rec in records:
            if not rec.get("value"):
                continue
            changes.append({
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": rec["name"],
                    "Type": rec["type"],
                    "TTL": ttl,
                    "ResourceRecords": [{"Value": rec["value"]}],
                },
            })
        if changes:
            client.change_resource_record_sets(
                HostedZoneId=config["hosted_zone_id"],
                ChangeBatch={"Changes": changes},
            )
            logger.info(f"Route53 records created: {[r['name'] for r in records]}")
    except Exception as e:
        err = f"Route53 error: {e}"
        logger.error(err)
        errors.append(err)


def _route53_delete(config: dict, records: list[dict], errors: list[str]):
    try:
        import boto3
        client = boto3.client(
            "route53",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
        )
        changes = []
        for rec in records:
            if not rec.get("value"):
                continue
            changes.append({
                "Action": "DELETE",
                "ResourceRecordSet": {
                    "Name": rec["name"],
                    "Type": rec["type"],
                    "TTL": 30,
                    "ResourceRecords": [{"Value": rec["value"]}],
                },
            })
        if changes:
            client.change_resource_record_sets(
                HostedZoneId=config["hosted_zone_id"],
                ChangeBatch={"Changes": changes},
            )
            logger.info(f"Route53 records deleted: {[r['name'] for r in records]}")
    except Exception as e:
        err = f"Route53 delete error: {e}"
        logger.error(err)
        errors.append(err)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_dns_service.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/dns_service.py src/backend/tests/test_dns_service.py
git commit -m "feat: add DNS service with NSUPDATE and Route53 support"
```

---

## Task 9: Deploy-from-Pattern API Extension

**Files:**
- Modify: `src/backend/app/schemas/pattern.py` — add GUID, domain, dns_provider_id to deploy schema
- Modify: `src/backend/app/api/patterns.py` — accept new fields in deploy endpoint
- Modify: `src/backend/app/services/deploy_service.py` — create DNS records on deploy, delete on teardown

- [ ] **Step 1: Add fields to pattern deploy schema**

Find the deploy schema in `src/backend/app/schemas/pattern.py` (the request body for the deploy endpoint) and add:

```python
    guid: str | None = None
    domain: str | None = None
    dns_provider_id: str | None = None
```

- [ ] **Step 2: Pass new fields through the deploy endpoint**

In `src/backend/app/api/patterns.py`, find the deploy endpoint. When creating the project from the pattern, set the new fields:

```python
    if body.guid:
        project.guid = body.guid
    if body.domain:
        project.domain = body.domain
    if body.dns_provider_id:
        project.dns_provider_id = body.dns_provider_id
```

- [ ] **Step 3: Add DNS record creation to deploy flow**

In `src/backend/app/services/deploy_service.py`, after EIP is assigned and port forwards are configured, add DNS record creation:

```python
        # Create DNS records if DNS provider configured
        if project.dns_provider_id and project.guid and project.domain:
            from app.models.dns_provider import DnsProvider
            from app.services.dns_service import resolve_dns_records, create_dns_records

            dns_provider = s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
            if dns_provider and lb_config:
                _deploy_progress[project_id] = {"step": "Creating DNS records", "detail": f"*.{project.guid}.{project.domain}"}
                notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})

                eip_address = None  # resolve from project's assigned EIP
                for eip in external_ips:
                    if eip.get("_public_ip"):
                        eip_address = eip["_public_ip"]
                        break

                dns_templates = lb_config.get("dns_records", [])
                records = resolve_dns_records(dns_templates, guid=project.guid, domain=project.domain, eip=eip_address)
                errors = create_dns_records(dns_provider.type, dns_provider.config, records, ttl=lb_config.get("dns_ttl", 30))

                # Store created records for teardown
                project.deployed_topology = project.deployed_topology or {}
                project.deployed_topology["_dns_records"] = [r for r in records if r.get("value")]
                s.commit()

                if errors:
                    logger.warning(f"DNS record creation had errors: {errors}")
```

- [ ] **Step 4: Add DNS record deletion to teardown flow**

In the teardown function, before EIP release, add:

```python
        # Delete DNS records if they were created
        if project.dns_provider_id:
            from app.models.dns_provider import DnsProvider
            from app.services.dns_service import delete_dns_records

            dns_provider = s.query(DnsProvider).filter_by(id=project.dns_provider_id).first()
            deployed_topo = project.deployed_topology or {}
            dns_records = deployed_topo.get("_dns_records", [])
            if dns_provider and dns_records:
                delete_dns_records(dns_provider.type, dns_provider.config, dns_records)
```

- [ ] **Step 5: Also extend bulk-deploy endpoint**

Find the bulk-deploy endpoint in `src/backend/app/api/patterns.py`. Add GUID generation support to bulk deploy — when `guid` is not provided, auto-generate one per project (e.g., using the first 8 characters of the project ID):

```python
    # In bulk deploy loop, for each project:
    project_guid = body.guid_template.replace("{n}", str(i+1)) if body.guid_template else project.id[:8]
    project.guid = project_guid
    project.domain = body.domain
    project.dns_provider_id = body.dns_provider_id
```

Add `guid_template`, `domain`, and `dns_provider_id` to the bulk deploy schema as well.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/schemas/pattern.py src/backend/app/api/patterns.py src/backend/app/services/deploy_service.py
git commit -m "feat: extend deploy-from-pattern with GUID, domain, DNS provider"
```

---

## Task 10: Frontend — LB Node on Canvas

**Files:**
- Modify: `src/frontend/src/components/canvas/Palette.tsx` — add LB to networking section
- Modify: `src/frontend/src/components/canvas/Canvas.tsx` — handle LB drop, LB connection validation
- Modify: `src/frontend/src/components/canvas/nodes/NetworkNode.tsx` — render LB variant
- Modify: `src/frontend/src/stores/canvasStore.ts` — LB edge styling, connection validation
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` — LB config panel

- [ ] **Step 1: Add LB to palette**

In `src/frontend/src/components/canvas/Palette.tsx`, add to the Networking section (after gateway, ~line 55):

```typescript
      {
        type: "loadbalancer",
        label: "Load Balancer",
        desc: "HAProxy L4",
        icon: "⚖",
        iconClass: "palette-icon-lb",
      },
```

- [ ] **Step 2: Handle LB node drop on canvas**

In `src/frontend/src/components/canvas/Canvas.tsx`, in the `onDrop` handler (~line 162-315), add a case for the `"loadbalancer"` type that creates a networkNode with the LB data:

```typescript
    } else if (itemType === "loadbalancer") {
      newNode = {
        id: generateNodeId(),
        type: "networkNode",
        position,
        data: {
          subtype: "loadbalancer",
          networkType: "loadbalancer",
          name: `lb-${String(lbCount).padStart(2, "0")}`,
          frontends: [
            { name: "https", bindPort: 443, mode: "tcp", backendPort: 443 },
            { name: "http", bindPort: 80, mode: "tcp", backendPort: 80 },
          ],
          dnsRecords: [],
          dnsTtl: 30,
        },
      };
    }
```

- [ ] **Step 3: Render LB variant in NetworkNode**

In `src/frontend/src/components/canvas/nodes/NetworkNode.tsx`, add the LB rendering case. Follow the existing pattern for gateway/BMC — different icon, color, and info display:

- Color: `rgba(59, 130, 246, ...)` (blue)
- Icon: "⚖" emoji or a custom SVG
- Display: list frontend ports (e.g., "443, 80, 6443")
- Handles: top and bottom (for VM connections), same as network nodes

- [ ] **Step 4: Add LB edge styling in canvasStore**

In `src/frontend/src/stores/canvasStore.ts`, in the `onConnect` handler (~line 231), add handling for LB↔VM connections:

```typescript
    // LB connections — blue dashed
    if (sourceNode.data.networkType === "loadbalancer" || targetNode.data.networkType === "loadbalancer") {
      newEdge.style = { stroke: "rgba(59,130,246,0.5)", strokeWidth: 2, strokeDasharray: "6 4" };
      newEdge.animated = true;
    }
```

- [ ] **Step 5: Add LB config panel in PropertiesPanel**

In `src/frontend/src/components/canvas/PropertiesPanel.tsx`, add a section for LB nodes that shows:
- Name field
- Frontend list (add/remove buttons):
  - Name, bind port, backend port, mode (tcp only for now)
- DNS record templates (add/remove):
  - Record name template (e.g., `api.{guid}.{domain}`)
  - Type (A)
- DNS TTL

Follow the existing pattern of other sections in PropertiesPanel — PatternFly form groups with text inputs and number inputs.

- [ ] **Step 6: Test in browser**

Start the frontend dev server, open a project, drag an LB node from the palette, connect VMs to it, verify:
- LB renders with blue styling and "⚖" icon
- Frontend ports show on the node
- PropertiesPanel shows LB config when selected
- Adding/removing frontends works
- Connecting VMs creates blue dashed edges

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/Palette.tsx src/frontend/src/components/canvas/Canvas.tsx src/frontend/src/components/canvas/nodes/NetworkNode.tsx src/frontend/src/stores/canvasStore.ts src/frontend/src/components/canvas/PropertiesPanel.tsx
git commit -m "feat: add Load Balancer network node to canvas"
```

---

## Task 11: Frontend — Pattern Deploy with GUID/Domain/DNS Provider

**Files:**
- Modify: `src/frontend/src/app/library/patterns/page.tsx` — add GUID, domain, DNS provider fields to deploy dialogs

- [ ] **Step 1: Extend DeployNameModal**

In the deploy dialog (`src/frontend/src/app/library/patterns/page.tsx`), add fields:
- GUID (text input, optional — auto-generates from project ID if blank)
- Domain (text input, optional)
- DNS Provider (dropdown, fetched from `/api/v1/dns-providers`)

The POST body to `/api/v1/patterns/{id}/deploy` becomes:
```json
{
  "name": "...",
  "guid": "abc123",
  "domain": "sandbox123.opentlc.com",
  "dns_provider_id": "uuid"
}
```

- [ ] **Step 2: Extend BulkDeployModal**

Add the same fields to the bulk deploy dialog. GUID becomes a template with `{n}` placeholder (e.g., `lab-{n}`).

- [ ] **Step 3: Test in browser**

Deploy a pattern with GUID and domain filled in. Verify:
- Fields appear in the deploy dialog
- DNS provider dropdown lists configured providers
- Deploy call includes the new fields
- Project shows GUID/domain in the project page

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/library/patterns/page.tsx
git commit -m "feat: add GUID, domain, DNS provider to pattern deploy dialogs"
```

---

## Task 12: Frontend — DNS Provider Admin Page

**Files:**
- Create: `src/frontend/src/app/admin/dns-providers/page.tsx`

- [ ] **Step 1: Create admin page**

Follow the existing pattern of the storage pools admin page. Create a PatternFly-based admin page at `/admin/dns-providers` that provides:
- List of DNS providers (table with name, type, created date)
- Create button → modal with name, type dropdown (nsupdate/route53), config fields
- Delete button with confirmation

The config fields change based on type:
- **nsupdate**: server, port, key name, key secret, key algorithm, default zone
- **route53**: access key ID, secret access key, hosted zone ID

- [ ] **Step 2: Add nav link**

Add "DNS Providers" to the admin sidebar/navigation (find where "Storage Pools" is listed and add alongside it).

- [ ] **Step 3: Test in browser**

Navigate to admin → DNS Providers. Create a provider, verify it appears in the list, delete it.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/dns-providers/
git commit -m "feat: add DNS Providers admin page"
```

---

## Task 13: Topology Templates (OCP Starters)

**Files:**
- Create: `src/backend/app/services/topology_templates.py`
- Modify: `src/backend/app/api/projects.py` — add template-based project creation endpoint

- [ ] **Step 1: Create topology template definitions**

```python
# src/backend/app/services/topology_templates.py
import uuid


def _id():
    return str(uuid.uuid4())


def _mac():
    import random
    return "52:54:00:{:02x}:{:02x}:{:02x}".format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def _vm_node(name, vcpus, ram, disk_gb, x, y, bmc=True, pxe=False):
    nic_id = f"nic-{_id()}"
    dc_id = f"dp-{_id()}"
    return {
        "id": _id(),
        "type": "vmNode",
        "position": {"x": x, "y": y},
        "data": {
            "name": name,
            "vcpus": vcpus,
            "ram": ram,
            "os": "rhcos",
            "icon": "🖥",
            "nics": [{"id": nic_id, "name": "eth0", "mac": _mac(), "model": "virtio"}],
            "diskControllers": [{"id": dc_id, "bus": "virtio"}],
            "bmcEnabled": bmc,
            "firmware": "uefi",
            "secureBoot": False,
            "bootDevices": [],
            "powerOnAtDeploy": True,
        },
    }


def _lb_node(frontends, x, y):
    return {
        "id": _id(),
        "type": "networkNode",
        "position": {"x": x, "y": y},
        "data": {
            "subtype": "loadbalancer",
            "networkType": "loadbalancer",
            "name": "ocp-lb",
            "frontends": frontends,
            "dnsRecords": [
                {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
                {"name": "api-int.{guid}.{domain}", "type": "A", "target": "eip"},
                {"name": "*.apps.{guid}.{domain}", "type": "A", "target": "eip"},
            ],
            "dnsTtl": 30,
        },
    }


OCP_FRONTENDS = [
    {"name": "api", "bindPort": 6443, "mode": "tcp", "backendPort": 6443},
    {"name": "ingress-https", "bindPort": 443, "mode": "tcp", "backendPort": 443},
    {"name": "ingress-http", "bindPort": 80, "mode": "tcp", "backendPort": 80},
    {"name": "machine-config", "bindPort": 22623, "mode": "tcp", "backendPort": 22623},
]


TEMPLATES = {
    "ocp-sno": {
        "name": "OpenShift SNO",
        "description": "Single Node OpenShift (8 vCPU, 32 GB RAM)",
        "category": "openshift",
    },
    "ocp-compact": {
        "name": "OpenShift Compact 3-Node",
        "description": "3 combined control plane + worker nodes (8 vCPU, 16 GB each)",
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
        # Network
        net = {"id": _id(), "type": "networkNode", "position": {"x": 300, "y": 0},
               "data": {"subtype": "network", "name": "cluster", "cidr": "10.0.0.0/24", "dhcp": True}}
        # BMC network (auto-created by syncBmcNetwork in frontend, but explicit in template)
        bmc_net = {"id": _id(), "type": "networkNode", "position": {"x": 500, "y": 0},
                   "data": {"subtype": "network", "networkType": "bmc", "name": "bmc",
                            "cidr": "192.168.100.0/24"}}
        # LB
        lb = _lb_node(OCP_FRONTENDS, 100, 0)
        # VMs
        cp = _vm_node("sno-0", 8, 32, 120, 200, 200)
        bootstrap = _vm_node("bootstrap", 4, 16, 120, 400, 200)
        nodes = [net, bmc_net, lb, cp, bootstrap]
        # Wire VMs to network and LB
        for vm in [cp, bootstrap]:
            nic_id = vm["data"]["nics"][0]["id"]
            edges.append({"id": _id(), "source": net["id"], "target": vm["id"],
                         "sourceHandle": f"{net['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})
            edges.append({"id": _id(), "source": lb["id"], "target": vm["id"],
                         "sourceHandle": f"{lb['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})

    elif template_id == "ocp-compact":
        net = {"id": _id(), "type": "networkNode", "position": {"x": 350, "y": 0},
               "data": {"subtype": "network", "name": "cluster", "cidr": "10.0.0.0/24", "dhcp": True}}
        lb = _lb_node(OCP_FRONTENDS, 100, 0)
        cps = [_vm_node(f"cp-{i}", 8, 16, 120, 150 + i * 220, 200) for i in range(3)]
        bootstrap = _vm_node("bootstrap", 4, 16, 120, 150 + 3 * 220, 200)
        nodes = [net, lb] + cps + [bootstrap]
        for vm in cps + [bootstrap]:
            nic_id = vm["data"]["nics"][0]["id"]
            edges.append({"id": _id(), "source": net["id"], "target": vm["id"],
                         "sourceHandle": f"{net['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})
            edges.append({"id": _id(), "source": lb["id"], "target": vm["id"],
                         "sourceHandle": f"{lb['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})

    elif template_id == "ocp-standard":
        net = {"id": _id(), "type": "networkNode", "position": {"x": 400, "y": 0},
               "data": {"subtype": "network", "name": "cluster", "cidr": "10.0.0.0/24", "dhcp": True}}
        lb = _lb_node(OCP_FRONTENDS, 100, 0)
        cps = [_vm_node(f"cp-{i}", 4, 16, 120, 150 + i * 220, 200) for i in range(3)]
        workers = [_vm_node(f"worker-{i}", 4, 16, 120, 150 + i * 220, 450) for i in range(2)]
        bootstrap = _vm_node("bootstrap", 4, 16, 120, 150 + 3 * 220, 200)
        nodes = [net, lb] + cps + workers + [bootstrap]
        for vm in cps + workers + [bootstrap]:
            nic_id = vm["data"]["nics"][0]["id"]
            edges.append({"id": _id(), "source": net["id"], "target": vm["id"],
                         "sourceHandle": f"{net['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})
        # Only CP + bootstrap connected to LB (not workers for API, but workers for ingress)
        for vm in cps + [bootstrap]:
            nic_id = vm["data"]["nics"][0]["id"]
            edges.append({"id": _id(), "source": lb["id"], "target": vm["id"],
                         "sourceHandle": f"{lb['id']}-bottom", "targetHandle": f"{nic_id}-top",
                         "type": "smoothstep", "animated": True})

    return {"nodes": nodes, "edges": edges}


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]
```

- [ ] **Step 2: Add API endpoint for templates**

In `src/backend/app/api/projects.py`, add endpoints:

```python
@router.get("/templates", response_model=list[dict])
def list_topology_templates(user: User = Depends(get_current_user)):
    from app.services.topology_templates import list_templates
    return list_templates()


@router.post("/from-template", status_code=201)
def create_project_from_template(
    body: dict,  # {template_id, name}
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.topology_templates import generate_topology, TEMPLATES
    template_id = body.get("template_id")
    if template_id not in TEMPLATES:
        raise HTTPException(404, "Template not found")
    topology = generate_topology(template_id)
    project = Project(
        name=body.get("name", TEMPLATES[template_id]["name"]),
        owner_id=user.id,
        topology=topology,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return {"id": project.id, "name": project.name}
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/topology_templates.py src/backend/app/api/projects.py
git commit -m "feat: add OCP topology templates (SNO, compact, standard)"
```

---

## Task 14: Run Full Test Suite

- [ ] **Step 1: Run all backend tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Expected: All tests pass, including new tests for haproxy_config, dns_providers, dns_service.

- [ ] **Step 2: Fix any failures**

If existing tests break due to the new `loadbalancer` key in `build_host_network_config()` return value, update those tests to handle the new key.

- [ ] **Step 3: Run alembic migration check**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic check
```

Expected: No pending migrations.

- [ ] **Step 4: Commit any test fixes**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "fix: update tests for LB and DNS provider additions"
```
