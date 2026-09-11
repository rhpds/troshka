# Per-Network MTU Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the nested cluster underlay MTU a per-network topology value that round-trips through pattern capture, so jumbo OCP patterns deploy correctly instead of silently wedging OVN.

**Architecture:** The backend is the single MTU resolution point (Approach A). The host reports its default-route interface MTU via the health poll; at deploy the backend resolves each network's MTU (`auto`/explicit → clamp+warn), writes the resolved value into `deployed_topology`, and hands one concrete integer to both providers (troshkad `mtu.size` + bridge/vxlan; KubeVirt NAD `mtu`) and to the OCP install-config. A recert-gate fast-fail turns the next MTU mismatch into a legible error instead of an 87-minute loop.

**Tech Stack:** Python 3.11 (runtime) / 3.13 (CI+tests), FastAPI, SQLAlchemy 2, Alembic, pytest (SQLite), troshkad (host agent), Kopf operator, Next.js/PatternFly frontend.

**Spec:** `docs/superpowers/specs/2026-09-11-per-network-mtu-design.md`

## Global Constraints

- Cognitive complexity ≤ 15 per function (SonarQube S3776) — extract helpers rather than nest.
- Unit tests mock ALL troshkad/network/SSH I/O; no real I/O. Add extra trailing values to any `time.time()` mock (CI runs 3.13; prevents `StopIteration`).
- FK columns use `postgresql.UUID(as_uuid=False)`; new plain columns are fine as `Integer`.
- Alembic migrations run automatically on backend startup — never run `alembic upgrade` manually.
- Run `black` (system `black`, not venv) before each commit. No `Co-Authored-By` lines in commits.
- KubeVirt-native provider must reach feature parity with troshkad — the NAD path is not optional.
- Constants (single source, `network_mtu.py`): `MESH_OVERHEAD = 110`, `OVN_GENEVE_OVERHEAD = 100`, `MTU_FLOOR = 1280`, `DEFAULT_FALLBACK_MTU = 1500`.

---

### Task 1: MTU resolver (pure function)

**Files:**
- Create: `src/backend/app/services/network_mtu.py`
- Test: `src/backend/tests/test_network_mtu.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `resolve_network_mtu(network_data: dict, host_uplink_mtu: int | None, spans_hosts: bool) -> tuple[int, str | None]` and the module-level constants `MESH_OVERHEAD`, `OVN_GENEVE_OVERHEAD`, `MTU_FLOOR`, `DEFAULT_FALLBACK_MTU`.

- [ ] **Step 1: Write the failing tests**

```python
# src/backend/tests/test_network_mtu.py
import pytest
from app.services.network_mtu import (
    resolve_network_mtu, MESH_OVERHEAD, DEFAULT_FALLBACK_MTU, MTU_FLOOR,
)

def test_auto_single_host_uses_full_uplink():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, 8900, spans_hosts=False)
    assert mtu == 8900 and warn is None

def test_missing_field_behaves_like_auto():
    assert resolve_network_mtu({}, 8900, spans_hosts=False) == (8900, None)

def test_auto_multi_host_reserves_mesh_overhead():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, 8900, spans_hosts=True)
    assert mtu == 8900 - MESH_OVERHEAD and warn is None

def test_explicit_within_ceiling_is_honored():
    assert resolve_network_mtu({"mtu": 1500}, 8900, spans_hosts=False) == (1500, None)

def test_explicit_over_ceiling_clamps_and_warns():
    mtu, warn = resolve_network_mtu({"mtu": 9000}, 8900, spans_hosts=False)
    assert mtu == 8900
    assert warn is not None and "reduced" in warn.lower()

def test_unknown_uplink_falls_back_to_1500_with_warning():
    mtu, warn = resolve_network_mtu({"mtu": "auto"}, None, spans_hosts=False)
    assert mtu == DEFAULT_FALLBACK_MTU and warn is not None

def test_floor_is_enforced():
    mtu, _ = resolve_network_mtu({"mtu": 800}, 8900, spans_hosts=False)
    assert mtu == MTU_FLOOR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_network_mtu.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.network_mtu`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/backend/app/services/network_mtu.py
"""Single resolution point for a network's underlay MTU (see
docs/superpowers/specs/2026-09-11-per-network-mtu-design.md)."""

MESH_OVERHEAD = 110          # WireGuard (~60) + VXLAN (~50) for multi-host mesh
OVN_GENEVE_OVERHEAD = 100    # OVN-Kubernetes geneve overhead (interface -> overlay)
MTU_FLOOR = 1280
DEFAULT_FALLBACK_MTU = 1500  # host uplink unknown -> safe/portable


def resolve_network_mtu(network_data, host_uplink_mtu, spans_hosts):
    """Resolve a network's underlay MTU. Returns (mtu, warning_or_None)."""
    requested = (network_data or {}).get("mtu")
    if host_uplink_mtu is None:
        return DEFAULT_FALLBACK_MTU, (
            "host uplink MTU unknown; defaulting to "
            f"{DEFAULT_FALLBACK_MTU} (agent may not have reported yet)"
        )

    overhead = MESH_OVERHEAD if spans_hosts else 0
    ceiling = host_uplink_mtu - overhead

    is_auto = requested in (None, "", "auto")
    candidate = ceiling if is_auto else int(requested)

    warning = None
    if candidate > ceiling:
        warning = (
            f"MTU {candidate} reduced to {ceiling}: exceeds host uplink "
            f"{host_uplink_mtu} (overhead {overhead})"
        )
        candidate = ceiling

    if candidate < MTU_FLOOR:
        candidate = MTU_FLOOR
    return candidate, warning
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_network_mtu.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/network_mtu.py src/backend/tests/test_network_mtu.py
git add src/backend/app/services/network_mtu.py src/backend/tests/test_network_mtu.py
git commit -m "feat(mtu): add per-network MTU resolver"
```

---

### Task 2: Host.uplink_mtu column + migration

**Files:**
- Modify: `src/backend/app/models/host.py` (add column near line 40, alongside `agent_version`)
- Create: `src/backend/alembic/versions/<rev>_add_host_uplink_mtu.py`
- Test: `src/backend/tests/test_host_uplink_mtu_model.py`

**Interfaces:**
- Produces: `Host.uplink_mtu: int | None`.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_host_uplink_mtu_model.py
from app.models.host import Host

def test_host_has_uplink_mtu_default_none():
    h = Host()
    assert h.uplink_mtu is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_host_uplink_mtu_model.py -v`
Expected: FAIL with `AttributeError: ... 'uplink_mtu'`.

- [ ] **Step 3: Add the column**

In `src/backend/app/models/host.py`, after the `agent_version` line:

```python
    uplink_mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Create the migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add host uplink_mtu"`
Then fill the generated file's `upgrade`/`downgrade`:

```python
def upgrade():
    op.add_column("hosts", sa.Column("uplink_mtu", sa.Integer(), nullable=True))

def downgrade():
    op.drop_column("hosts", "uplink_mtu")
```

Set `down_revision` to the current head (check `alembic/versions/` for the latest).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_host_uplink_mtu_model.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/models/host.py
git add src/backend/app/models/host.py src/backend/alembic/versions src/backend/tests/test_host_uplink_mtu_model.py
git commit -m "feat(mtu): add Host.uplink_mtu column + migration"
```

---

### Task 3: troshkad reports uplink MTU; backend persists it

**Files:**
- Modify: `src/troshkad/troshkad.py` (`/health` handler; add `_default_route_mtu()` helper)
- Modify: `src/backend/app/services/health_poller.py` (near line 246, where `last_health_at` is set)
- Test: `src/troshkad/tests/test_default_route_mtu.py` (create if dir absent), `src/backend/tests/test_health_poller_uplink_mtu.py`

**Interfaces:**
- Consumes: `Host.uplink_mtu` (Task 2).
- Produces: troshkad `/health` payload key `"uplink_mtu": int | None`; `health_poller` sets `host.uplink_mtu` from it.

- [ ] **Step 1: Write the failing test (troshkad helper)**

```python
# src/troshkad/tests/test_default_route_mtu.py
from unittest.mock import patch
import troshkad

def test_default_route_mtu_parses_ip_output():
    route = "default via 10.0.0.1 dev enp1s0 proto dhcp\n"
    link = "2: enp1s0: <> mtu 8900 qdisc ...\n"
    with patch("troshkad.subprocess.check_output", side_effect=[route, link]):
        assert troshkad._default_route_mtu() == 8900

def test_default_route_mtu_returns_none_on_error():
    with patch("troshkad.subprocess.check_output", side_effect=Exception("boom")):
        assert troshkad._default_route_mtu() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/troshkad && python3 -m pytest tests/test_default_route_mtu.py -v`
Expected: FAIL (`_default_route_mtu` undefined).

- [ ] **Step 3: Implement helper + wire into `/health`**

```python
# src/troshkad/troshkad.py
def _default_route_mtu():
    """MTU of the host's default-route interface, or None on any failure."""
    try:
        route = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True, timeout=5
        )
        iface = route.split("dev", 1)[1].split()[0]
        link = subprocess.check_output(
            ["ip", "link", "show", "dev", iface], text=True, timeout=5
        )
        return int(link.split("mtu", 1)[1].split()[0])
    except Exception:
        return None
```

In the `/health` handler, add `"uplink_mtu": _default_route_mtu()` to the returned dict (alongside `"version"`).

- [ ] **Step 4: Run to verify pass**

Run: `cd src/troshkad && python3 -m pytest tests/test_default_route_mtu.py -v`
Expected: PASS.

- [ ] **Step 5: Write + run the backend persistence test**

```python
# src/backend/tests/test_health_poller_uplink_mtu.py
from app.services import health_poller

def test_persist_uplink_mtu_sets_host_field():
    class H:  # minimal stand-in
        uplink_mtu = None
    host = H()
    health_poller._apply_uplink_mtu(host, {"version": "x", "uplink_mtu": 8900})
    assert host.uplink_mtu == 8900

def test_persist_uplink_mtu_ignores_missing():
    class H:
        uplink_mtu = 1500
    host = H()
    health_poller._apply_uplink_mtu(host, {"version": "x"})
    assert host.uplink_mtu == 1500
```

Add the helper in `health_poller.py` and call it where the health dict is handled (near line 246):

```python
def _apply_uplink_mtu(host, health):
    mtu = (health or {}).get("uplink_mtu")
    if isinstance(mtu, int) and mtu > 0:
        host.uplink_mtu = mtu
```

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_health_poller_uplink_mtu.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/health_poller.py src/backend/tests/test_health_poller_uplink_mtu.py
git add src/troshkad/troshkad.py src/troshkad/tests/test_default_route_mtu.py src/backend/app/services/health_poller.py src/backend/tests/test_health_poller_uplink_mtu.py
git commit -m "feat(mtu): report host default-route MTU via health poll"
```

---

### Task 4: Deploy wiring — resolve per network, write deployed_topology, inject mtu into net dicts

**Files:**
- Modify: `src/backend/app/services/deploy_topology.py:909` (net-dict builder)
- Modify: the deploy entry that builds VM-create params + writes `deployed_topology` (in `deploy_service.py`; locate the call that assembles per-VM `networks` from `deploy_topology`)
- Test: `src/backend/tests/test_deploy_mtu_wiring.py`

**Interfaces:**
- Consumes: `resolve_network_mtu` (Task 1), `Host.uplink_mtu` (Task 2).
- Produces: each VM net dict gains `"mtu": int`; `deployed_topology` network nodes gain resolved `data.mtu`; warnings appended to the deploy progress log.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_deploy_mtu_wiring.py
from app.services.deploy_topology import resolve_topology_mtus

def _topo():
    return {"nodes": [
        {"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}},
        {"id": "net2", "type": "networkNode", "data": {"mtu": 9000}},
    ]}

def test_resolves_and_writes_back_concrete_mtu():
    topo = _topo()
    warnings = resolve_topology_mtus(topo, host_uplink_mtu=8900, spans_hosts=False)
    by_id = {n["id"]: n for n in topo["nodes"]}
    assert by_id["net1"]["data"]["mtu"] == 8900      # auto -> uplink
    assert by_id["net2"]["data"]["mtu"] == 8900      # 9000 clamped
    assert any("reduced" in w.lower() for w in warnings)

def test_per_network_mtu_map():
    from app.services.deploy_topology import network_mtu_map
    m = network_mtu_map(_topo(), host_uplink_mtu=8900, spans_hosts=False)
    assert m == {"net1": 8900, "net2": 8900}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_mtu_wiring.py -v`
Expected: FAIL (`resolve_topology_mtus` undefined).

- [ ] **Step 3: Implement resolver-over-topology + net-dict injection**

In `deploy_topology.py`:

```python
from app.services.network_mtu import resolve_network_mtu

def network_mtu_map(topology, host_uplink_mtu, spans_hosts):
    """{network_node_id: resolved_mtu} for every network node."""
    out = {}
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        mtu, _ = resolve_network_mtu(node.get("data", {}), host_uplink_mtu, spans_hosts)
        out[node["id"]] = mtu
    return out

def resolve_topology_mtus(topology, host_uplink_mtu, spans_hosts):
    """Write resolved concrete MTU back into each network node; return warnings."""
    warnings = []
    for node in topology.get("nodes", []):
        if node.get("type") != "networkNode":
            continue
        data = node.setdefault("data", {})
        mtu, warn = resolve_network_mtu(data, host_uplink_mtu, spans_hosts)
        data["mtu"] = mtu
        if warn:
            warnings.append(f"{data.get('name', node['id'])}: {warn}")
    return warnings
```

Extend the net-dict builder at line 909 to carry the resolved MTU. Give
`_build_vm_network_entry` (the function returning line 909's dict) access to the
`mtu_map` and add `"mtu": mtu_map.get(network_node_id)` to the returned dict:

```python
    return {"bridge": f"br-{vni}", "mac": mac, "nic_id": handle,
            "model": model, "mtu": mtu_map.get(network_node_id)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_mtu_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Wire into the deploy entrypoint**

In `deploy_service.py`, at the point the topology is prepared for deploy (before building per-VM params and before persisting `deployed_topology`): fetch `host.uplink_mtu`, compute `spans_hosts` (True when the project's networks are placed on >1 host — reuse existing mesh/placement logic), then:

```python
mtu_warnings = resolve_topology_mtus(topology, host.uplink_mtu, spans_hosts)
for w in mtu_warnings:
    _log(w)            # same progress-log channel install steps already use
    logger.warning("MTU: %s", w)
mtu_map = network_mtu_map(topology, host.uplink_mtu, spans_hosts)
# pass mtu_map through to the net-dict builder
```

Add an assertion-style test that the deploy path passes `mtu` into VM params by
unit-testing the builder call with a stub `mtu_map` (no real deploy).

- [ ] **Step 6: Run full backend suite + commit**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_mtu_wiring.py tests/test_network_mtu.py -v`

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/deploy_topology.py src/backend/app/services/deploy_service.py src/backend/tests/test_deploy_mtu_wiring.py
git add src/backend/app/services/deploy_topology.py src/backend/app/services/deploy_service.py src/backend/tests/test_deploy_mtu_wiring.py
git commit -m "feat(mtu): resolve per-network MTU at deploy and inject into net dicts"
```

---

### Task 5: troshkad applies MTU (NIC + bridge + vxlan)

**Files:**
- Modify: `src/troshkad/troshkad.py` — `_handle_vm_create` (line 1274), bridge creation in `networks/full-setup` path, `_handle_mesh_join_network`
- Test: `src/troshkad/tests/test_vm_network_mtu.py`

**Interfaces:**
- Consumes: net dict `"mtu"` (Task 4) in VM-create params; network params `"mtu"` in full-setup.
- Produces: `--network …,mtu.size=N`; `ip link set br-{vni} mtu N`; vxlan iface MTU N.

- [ ] **Step 1: Write the failing test (NIC arg)**

```python
# src/troshkad/tests/test_vm_network_mtu.py
import troshkad

def test_network_arg_includes_mtu_size():
    net = {"bridge": "br-1001", "model": "virtio", "mtu": 8850}
    arg = troshkad._network_arg(net)
    assert arg == "bridge=br-1001,model=virtio,mtu.size=8850"

def test_network_arg_omits_mtu_when_absent():
    net = {"bridge": "br-1001", "model": "virtio"}
    assert "mtu.size" not in troshkad._network_arg(net)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/troshkad && python3 -m pytest tests/test_vm_network_mtu.py -v`
Expected: FAIL (`_network_arg` undefined).

- [ ] **Step 3: Extract + extend the NIC arg builder**

Refactor lines 1270-1277 of `_handle_vm_create` to use a helper (keeps
`_handle_vm_create` under the complexity cap):

```python
def _network_arg(net):
    bridge = _validate_bridge_name(net.get("bridge", "br-troshka-00000000"))
    model = _validate_net_model(net.get("model", "virtio"))
    arg = f"bridge={bridge},model={model}"
    mac = net.get("mac", "")
    if mac:
        arg += f",mac={_validate_mac(mac)}"
    mtu = net.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        arg += f",mtu.size={int(mtu)}"
    return arg
```

And in the loop: `cmd.extend(["--network", _network_arg(net)])`.

- [ ] **Step 4: Run to verify pass**

Run: `cd src/troshkad && python3 -m pytest tests/test_vm_network_mtu.py -v`
Expected: PASS.

- [ ] **Step 5: Set bridge + vxlan MTU**

In the `networks/full-setup` path, after the bridge is created/ensured, when the
network params carry an `mtu`:

```python
    mtu = net.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        _run_cmd(job, ["ip", "link", "set", bridge, "mtu", str(mtu)], check=False)
```

In `_handle_mesh_join_network`, after creating `vxlan-{vni}`, set the same MTU on
the vxlan interface (so encapsulated frames fit the uplink).

Add a test asserting the full-setup command list includes an
`ip link set br-<vni> mtu <n>` invocation (mock `_run_cmd`, capture calls).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py src/troshkad/tests/test_vm_network_mtu.py
git commit -m "feat(mtu): troshkad sets NIC/bridge/vxlan MTU from resolved value"
```

Note for reviewer: after merge, the agent must be updated on hosts
(`./scripts/update-agent.sh`) for this to take effect.

---

### Task 6: KubeVirt-native NAD MTU (parity)

**Files:**
- Modify: `src/operator/helpers/k8s.py:build_nad` (line 75)
- Modify: backend Network-CR creation to include `spec.mtu` (resolved value)
- Test: `src/operator/tests/test_operator_handlers.py` (extend NAD tests) or new `test_nad_mtu.py`

**Interfaces:**
- Consumes: resolved network MTU (Task 4) carried on the Network CR `spec.mtu`.
- Produces: NAD `config["mtu"] = N`.

- [ ] **Step 1: Write the failing test**

```python
# src/operator/tests/test_nad_mtu.py
import json
from helpers.k8s import build_nad

def test_nad_includes_mtu_when_set():
    cr = {"metadata": {"name": "net1", "namespace": "troshka-x"},
          "spec": {"mtu": 8850}}
    cfg = json.loads(build_nad(cr)["spec"]["config"])
    assert cfg["mtu"] == 8850

def test_nad_omits_mtu_when_absent():
    cr = {"metadata": {"name": "net1", "namespace": "troshka-x"}, "spec": {}}
    cfg = json.loads(build_nad(cr)["spec"]["config"])
    assert "mtu" not in cfg
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/operator && python3 -m pytest tests/test_nad_mtu.py -v`
Expected: FAIL (`mtu` not in config).

- [ ] **Step 3: Implement**

In `build_nad`, after building `config`:

```python
    mtu = _spec.get("mtu")
    if isinstance(mtu, int) and mtu > 0:
        config["mtu"] = mtu
```

In the backend code that creates/updates the Network CR, set `spec["mtu"]` to the
network's resolved MTU (from `network_mtu_map`).

- [ ] **Step 4: Run to verify pass**

Run: `cd src/operator && python3 -m pytest tests/test_nad_mtu.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/operator/helpers/k8s.py src/operator/tests/test_nad_mtu.py
git add src/operator/helpers/k8s.py src/operator/tests/test_nad_mtu.py src/backend/app
git commit -m "feat(mtu): KubeVirt NAD carries resolved MTU (parity)"
```

---

### Task 7: install-config clusterNetworkMTU (fresh installs)

**Files:**
- Modify: `src/backend/app/services/ocp/agent_template.py` (install-config networking block)
- Test: `src/backend/tests/test_install_config_mtu.py`

**Interfaces:**
- Consumes: resolved NIC MTU for the cluster's machine network (Task 4), `OVN_GENEVE_OVERHEAD` (Task 1).
- Produces: `networking.clusterNetworkMTU = nic_mtu - OVN_GENEVE_OVERHEAD` in the generated install-config.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_install_config_mtu.py
from app.services.ocp.agent_template import _cluster_network_mtu

def test_cluster_network_mtu_subtracts_geneve_overhead():
    assert _cluster_network_mtu(8850) == 8750

def test_cluster_network_mtu_none_when_unset():
    assert _cluster_network_mtu(None) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_install_config_mtu.py -v`
Expected: FAIL (`_cluster_network_mtu` undefined).

- [ ] **Step 3: Implement + inject into install-config**

```python
# agent_template.py
from app.services.network_mtu import OVN_GENEVE_OVERHEAD

def _cluster_network_mtu(nic_mtu):
    if not isinstance(nic_mtu, int) or nic_mtu <= 0:
        return None
    return nic_mtu - OVN_GENEVE_OVERHEAD
```

Where the install-config `networking:` block is assembled, when
`_cluster_network_mtu(nic_mtu)` is not None, emit `clusterNetworkMTU: <value>`.
`nic_mtu` = the resolved MTU of the network the cluster's machines attach to.

- [ ] **Step 4: Run to verify pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_install_config_mtu.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/ocp/agent_template.py src/backend/tests/test_install_config_mtu.py
git add src/backend/app/services/ocp/agent_template.py src/backend/tests/test_install_config_mtu.py
git commit -m "feat(mtu): set clusterNetworkMTU in OCP install-config"
```

---

### Task 8: Capture freezes resolved MTU into pattern topology

**Files:**
- Modify: the pattern-save/capture path (search `filter_by(type="personal")` / pattern save in `src/backend/app/services/`)
- Test: `src/backend/tests/test_pattern_capture_mtu.py`

**Interfaces:**
- Consumes: `deployed_topology` network nodes with resolved `data.mtu` (Task 4).
- Produces: captured pattern topology network nodes carry the concrete `data.mtu`.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_pattern_capture_mtu.py
from app.services.pattern_capture import carry_resolved_mtu  # adjust import to real module

def test_capture_prefers_resolved_deployed_mtu():
    topo = {"nodes": [{"id": "net1", "type": "networkNode", "data": {"mtu": "auto"}}]}
    deployed = {"nodes": [{"id": "net1", "type": "networkNode", "data": {"mtu": 8850}}]}
    carry_resolved_mtu(topo, deployed)
    assert topo["nodes"][0]["data"]["mtu"] == 8850
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_capture_mtu.py -v`
Expected: FAIL (`carry_resolved_mtu` undefined).

- [ ] **Step 3: Implement + call from capture**

```python
def carry_resolved_mtu(pattern_topology, deployed_topology):
    """Freeze each network node's concrete resolved MTU into the pattern."""
    resolved = {
        n["id"]: (n.get("data") or {}).get("mtu")
        for n in (deployed_topology or {}).get("nodes", [])
        if n.get("type") == "networkNode"
    }
    for node in pattern_topology.get("nodes", []):
        if node.get("type") == "networkNode" and resolved.get(node["id"]):
            node.setdefault("data", {})["mtu"] = resolved[node["id"]]
```

Call it during pattern capture, passing the project's `deployed_topology`.

- [ ] **Step 4: Run to verify pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_capture_mtu.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services && git add src/backend/app/services src/backend/tests/test_pattern_capture_mtu.py
git commit -m "feat(mtu): freeze resolved MTU into captured pattern topology"
```

---

### Task 9: Recert-gate MTU fast-fail

**Files:**
- Modify: `src/backend/app/services/ocp/ops_pod_install.py` (recert gate loop, ~lines 450-486)
- Modify: `src/frontend/src/components/canvas/ClusterInstallLogModal.tsx` (recognize the new failure breadcrumb, optional)
- Test: `src/backend/tests/test_ops_pod.py` (extend)

**Interfaces:**
- Consumes: the recert gate's `oc get co` output.
- Produces: a fast-fail breadcrumb `[key] recert failed: <MTU message>` and `exit 1` when the `network` operator is Degraded with the OVN MTU-too-small message.

- [ ] **Step 1: Write the failing test**

```python
# in src/backend/tests/test_ops_pod.py
def test_recert_gate_fast_fails_on_ovn_mtu_mismatch():
    from app.services.ocp.ops_pod_install import _recert_cluster_block
    script = _recert_cluster_block("ocp-x", "/workdir", "multinode")
    # the generated gate script must grep the network CO degraded message for
    # the MTU signature and break with a 'recert failed' breadcrumb
    assert "too small for specified overlay MTU" in script
    assert "recert failed" in script
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ops_pod.py::test_recert_gate_fast_fails_on_ovn_mtu_mismatch -v`
Expected: FAIL.

- [ ] **Step 3: Add the fast-fail to the gate script**

Inside the gate loop (after computing `bad`/`nr`), add a check that reads the
`network` operator's degraded message and, if it contains the OVN MTU signature,
emits a legible breadcrumb and exits non-zero immediately:

```bash
  netmsg=$(echo "$out" | awk '$1=="network"{ $1=$2=$3=$4=$5=""; print }')
  case "$netmsg" in
    *"too small for specified overlay MTU"*)
      echo "[<key>] recert failed: cluster OVN MTU exceeds node interface MTU -"\
"$netmsg"; exit 1;;
  esac
```

(Build this line with the same f-string `cluster_key` interpolation the
surrounding script uses. Keep the added helper logic small — extract if the gate
function exceeds the complexity cap.)

- [ ] **Step 4: Run to verify pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ops_pod.py -k recert -v`
Expected: PASS (new test + existing recert tests still green).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/ocp/ops_pod_install.py
git add src/backend/app/services/ocp/ops_pod_install.py src/backend/tests/test_ops_pod.py src/frontend/src/components/canvas/ClusterInstallLogModal.tsx
git commit -m "feat(mtu): recert gate fast-fails on OVN MTU mismatch"
```

---

### Task 10: Frontend — network node MTU field

**Files:**
- Modify: the network-node inspector/config component (search `networkType` in `src/frontend/src/components/canvas/`)
- Test: existing frontend test pattern if present; otherwise manual verification note

**Interfaces:**
- Consumes/Produces: reads/writes `node.data.mtu` on the network node (`"auto"` or integer), auto-saved via the canvas 1s debounce.

- [ ] **Step 1: Add the field**

In the network node config panel, add an MTU control:
- A select "Auto" (stores `"auto"`) vs "Custom"; when Custom, a number input.
- Validate integer within 1280–9000; show inline error otherwise.
- Helper text: "Auto = host uplink; jumbo when the host supports it."
- On change, update `node.data.mtu` in the Zustand `useCanvasStore` (same path other node fields use), which triggers the debounced `_saveTopologyToApi`.

- [ ] **Step 2: Verify in the running app**

Run the app (`./dev-services.sh start`), open a project, select a network node,
set MTU to Custom 1500, confirm it persists after reload (topology round-trip).
Set back to Auto, confirm `data.mtu === "auto"`.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas
git commit -m "feat(mtu): per-network MTU field in canvas node inspector"
```

---

## Self-Review

**Spec coverage:**
- §5 data model → Tasks 1 (constants), 2 (Host column), topology field used across 4/8/10. ✓
- §6 resolver → Task 1. ✓
- §7 provider application → Task 5 (troshkad), Task 6 (KubeVirt), Task 7 (install-config). ✓
- §8 capture round-trip → Task 8. ✓
- §9 frontend → Task 10; warnings → Task 4 (emit); fast-fail → Task 9; uplink-unknown edge → Task 1 test. ✓
- §5 Host.uplink_mtu reporting → Task 3. ✓
- §10 testing → each task is TDD. ✓

**Type consistency:** `resolve_network_mtu(network_data, host_uplink_mtu, spans_hosts) -> (int, str|None)` used identically in Tasks 1, 4, and referenced by 6/7 via `network_mtu_map`/constants. `network_mtu_map` and `resolve_topology_mtus` defined in Task 4 and consumed in 4/6. `_cluster_network_mtu` (Task 7), `carry_resolved_mtu` (Task 8), `_network_arg` (Task 5), `_default_route_mtu`/`_apply_uplink_mtu` (Task 3) each defined before use. ✓

**Placeholder scan:** No TBD/TODO; each code step has real code. Two integration points are described by search-anchor rather than a fixed line number (the `deploy_service.py` deploy entrypoint in Task 4 Step 5, and the Network-CR creation in Task 6 Step 3) because their exact call sites must be located in-repo; both specify exactly what to add and the surrounding function. ✓

## Notes for execution

- After merge, run `./scripts/update-agent.sh` so hosts pick up the troshkad changes; restart the RQ worker after deploy_service edits ([[restart-worker-for-deploy-code]]).
- These are unit-test-only changes plus one frontend manual check; no live-cluster deploy is required to land the code. Recovering the three wedged clusters is a separate, later decision.
