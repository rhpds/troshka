# Multi-Cluster OCP — Plan 1: Data Model, Schema & Back-Compat

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an OCP cluster a first-class, persisted object in the template schema and topology — parsing both the legacy singular `ocp:` mapping and the new `ocp:` list, deriving per-cluster node counts/types, materializing RHCOS VMs from counts, tagging every VM with its `clusterId`, migrating legacy persisted topology lazily, and remapping all cluster references when topology is cloned.

**Architecture:** Pure backend change in `src/backend`. The template's `ocp:` section is normalized to a list of cluster dicts; topology generation emits `topology["clusters"]` (camelCase objects) and stamps `vmNode.data.clusterId` + `parentNode`. Count-only clusters (no enumerated VMs) materialize RHCOS control-plane/worker nodes so the agnosticd REST path works without enumerating VMs. Legacy topology without `clusters[]` is migrated on load. `_remap_topology` grows cluster-aware remapping.

**Tech Stack:** Python 3.11 (CI/dev run 3.13), FastAPI, SQLAlchemy 2, pytest (SQLite, dev-mode auth). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-01-multi-cluster-ocp-and-bastionless-install-design.md` (§4 Data model & schema; §3 decisions #2, #3, #4, #5, #9, #11)

## Global Constraints

- **RHCOS-only** OCP nodes; materialized cluster VMs use `os: "rhcos"`.
- **Control-plane count is 1 or 3 only.** `type: sno` → 1; `type: compact|standard` → 3. Never 2 or 4+.
- **Cognitive complexity ≤ 15 per function** (SonarQube S3776). Extract helpers rather than nesting.
- **UUIDs as strings**; follow existing `mapped_column`/JSONB conventions. No schema/DB migration needed — `clusters[]` lives inside the existing `topology` JSONB column.
- **Back-compat is non-breaking**: legacy `ocp:` *mapping* and new `ocp:` *list* both parse; shape-detection (mapping vs list), no `schemaVersion` field.
- **Tests**: SQLite; add extra trailing values to any `time.time()` mocks to avoid `StopIteration` under 3.13. Model new tests on `src/backend/tests/test_template_loader.py` (function-local imports, `TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")`).
- Run tests with `cd src/backend && ./venv/bin/python3 -m pytest <path> -v`.
- Run `black` (system `black`, not venv) before each commit.

---

### Task 1: Normalize the `ocp:` section (legacy mapping ↔ new list)

**Files:**
- Modify: `src/backend/app/services/template_loader.py` (add `normalize_ocp_section`; call it from `_copy_template_content_sections:30`)
- Test: `src/backend/tests/test_ocp_clusters.py` (new)

**Interfaces:**
- Produces: `normalize_ocp_section(ocp: dict | list | None) -> list[dict]` — returns a list of cluster dicts with snake_case keys. A dict input is wrapped to a one-element list; each entry is given a `name` (from `name` or legacy `cluster_name`, default `"ocp"`). `api_vip`/`ingress_vip`/`base_domain`/`type`/`workers`/`ocp_version` and sizing keys are preserved verbatim when present. `None`/falsy → `[]`.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_ocp_clusters.py
def test_normalize_legacy_mapping_wraps_to_list():
    from app.services.template_loader import normalize_ocp_section

    legacy = {
        "cluster_name": "ocp",
        "base_domain": "ocp.local",
        "api_vip": "10.0.0.10",
        "ingress_vip": "10.0.0.11",
    }
    out = normalize_ocp_section(legacy)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["name"] == "ocp"
    assert out[0]["base_domain"] == "ocp.local"
    assert out[0]["api_vip"] == "10.0.0.10"
    assert out[0]["ingress_vip"] == "10.0.0.11"


def test_normalize_new_list_passthrough_and_name_default():
    from app.services.template_loader import normalize_ocp_section

    out = normalize_ocp_section(
        [
            {"name": "prod", "type": "standard", "workers": 2},
            {"type": "sno", "base_domain": "dev.local"},
        ]
    )
    assert [c["name"] for c in out] == ["prod", "ocp"]  # 2nd defaults name
    assert out[0]["type"] == "standard" and out[0]["workers"] == 2


def test_normalize_none_returns_empty():
    from app.services.template_loader import normalize_ocp_section

    assert normalize_ocp_section(None) == []
    assert normalize_ocp_section({}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_ocp_section'`.

- [ ] **Step 3: Write minimal implementation**

```python
# in src/backend/app/services/template_loader.py (module level)
def normalize_ocp_section(ocp) -> list[dict]:
    """Normalize the template ``ocp:`` section to a list of cluster dicts.

    Accepts the legacy singular mapping (wrapped into a one-element list) or
    the new list form. Each cluster is guaranteed a ``name`` (from ``name`` or
    legacy ``cluster_name``, default ``"ocp"``).
    """
    if not ocp:
        return []
    entries = [ocp] if isinstance(ocp, dict) else list(ocp)
    clusters = []
    for entry in entries:
        c = dict(entry)
        c["name"] = c.get("name") or c.get("cluster_name") or "ocp"
        c.pop("cluster_name", None)
        clusters.append(c)
    return clusters
```

Then, in `_copy_template_content_sections`, normalize `ocp` when copied. Replace the generic copy for the `"ocp"` section:

```python
    for section in _TEMPLATE_CONTENT_SECTIONS:
        if tmpl.get(section):
            if section == "ocp":
                resolved["ocp"] = normalize_ocp_section(tmpl["ocp"])
            else:
                resolved[section] = tmpl[section]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git add src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): normalize ocp section to cluster list (legacy + new schema)"
```

---

### Task 2: Build camelCase cluster objects with type/count derivation

**Files:**
- Modify: `src/backend/app/services/template_loader.py` (add `build_topology_clusters`)
- Test: `src/backend/tests/test_ocp_clusters.py`

**Interfaces:**
- Consumes: `normalize_ocp_section` output (list of snake_case cluster dicts).
- Produces: `build_topology_clusters(ocp_list: list[dict], vms_def: dict | None) -> list[dict]` — returns a list of camelCase cluster objects for `topology["clusters"]`. Each object: `id`, `name`, `type`, `controlPlane`, `workers`, `controlPlaneCpu`, `controlPlaneMemory`, `controlPlaneDisk`, `workerCpu`, `workerMemory`, `workerDisk`, `baseDomain`, `apiVip`, `ingressVip`, `ocpVersion`, `pullThroughRegistry`.
  - `type` derivation when absent: count that cluster's VMs (`vms_def` entries whose `cluster` == cluster name, or all when a single cluster) by `role`; `control-plane<=1 and workers==0` → `sno`; `workers==0` → `compact`; else `standard`.
  - `controlPlane` from type (`sno`→1, else→3). `workers` from the `workers:` field, else the counted worker VMs, else 0.
  - Sizing defaults when absent: CP `8`/`16384`/`120`, worker `4`/`8192`/`100`.
  - `id` = a slug of `name` (lowercased, non-alnum → `-`).

- [ ] **Step 1: Write the failing test**

```python
def test_build_clusters_type_from_explicit_field():
    from app.services.template_loader import build_topology_clusters, normalize_ocp_section

    ocp = normalize_ocp_section(
        [{"name": "prod", "type": "standard", "workers": 2,
          "base_domain": "ocp.local", "api_vip": "10.0.0.10",
          "ingress_vip": "10.0.0.11"}]
    )
    clusters = build_topology_clusters(ocp, vms_def=None)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["id"] == "prod" and c["name"] == "prod"
    assert c["type"] == "standard"
    assert c["controlPlane"] == 3 and c["workers"] == 2
    assert c["apiVip"] == "10.0.0.10" and c["ingressVip"] == "10.0.0.11"
    assert c["controlPlaneCpu"] == 8 and c["workerMemory"] == 8192


def test_build_clusters_infer_type_from_vm_roles():
    from app.services.template_loader import build_topology_clusters, normalize_ocp_section

    # legacy mapping, no type — 3 CP + 2 workers => standard
    ocp = normalize_ocp_section({"cluster_name": "ocp", "base_domain": "ocp.local"})
    vms = {
        "cp-0": {"role": "control-plane"}, "cp-1": {"role": "control-plane"},
        "cp-2": {"role": "control-plane"},
        "worker-0": {"role": "worker"}, "worker-1": {"role": "worker"},
        "bastion": {"role": "bastion"},
    }
    clusters = build_topology_clusters(ocp, vms_def=vms)
    assert clusters[0]["type"] == "standard"
    assert clusters[0]["controlPlane"] == 3 and clusters[0]["workers"] == 2


def test_build_clusters_infer_sno():
    from app.services.template_loader import build_topology_clusters, normalize_ocp_section

    ocp = normalize_ocp_section({"cluster_name": "ocp"})
    vms = {"cp-0": {"role": "control-plane"}}
    clusters = build_topology_clusters(ocp, vms_def=vms)
    assert clusters[0]["type"] == "sno" and clusters[0]["controlPlane"] == 1
    assert clusters[0]["workers"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k build_clusters -v`
Expected: FAIL — `ImportError: cannot import name 'build_topology_clusters'`.

- [ ] **Step 3: Write minimal implementation**

```python
import re

_CP_SIZE_DEFAULTS = {"cpu": 8, "memory": 16384, "disk": 120}
_WORKER_SIZE_DEFAULTS = {"cpu": 4, "memory": 8192, "disk": 100}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "ocp").lower()).strip("-") or "ocp"


def _count_cluster_roles(cluster_name, vms_def, single_cluster):
    cp = wk = 0
    for cfg in (vms_def or {}).values():
        if not single_cluster and cfg.get("cluster") != cluster_name:
            continue
        role = cfg.get("role")
        if role == "control-plane":
            cp += 1
        elif role == "worker":
            wk += 1
    return cp, wk


def _infer_type(cp, wk):
    if cp <= 1 and wk == 0:
        return "sno"
    if wk == 0:
        return "compact"
    return "standard"


def build_topology_clusters(ocp_list: list[dict], vms_def: dict | None) -> list[dict]:
    """Build camelCase cluster objects for ``topology['clusters']``."""
    single = len(ocp_list) == 1
    out = []
    for entry in ocp_list:
        name = entry["name"]
        cp_count, wk_count = _count_cluster_roles(name, vms_def, single)
        ctype = entry.get("type") or _infer_type(cp_count, wk_count)
        control_plane = 1 if ctype == "sno" else 3
        workers = entry.get("workers")
        if workers is None:
            workers = wk_count
        out.append(
            {
                "id": _slug(name),
                "name": name,
                "type": ctype,
                "controlPlane": control_plane,
                "workers": int(workers),
                "controlPlaneCpu": entry.get("control_plane_cpu", _CP_SIZE_DEFAULTS["cpu"]),
                "controlPlaneMemory": entry.get("control_plane_memory", _CP_SIZE_DEFAULTS["memory"]),
                "controlPlaneDisk": entry.get("control_plane_disk", _CP_SIZE_DEFAULTS["disk"]),
                "workerCpu": entry.get("worker_cpu", _WORKER_SIZE_DEFAULTS["cpu"]),
                "workerMemory": entry.get("worker_memory", _WORKER_SIZE_DEFAULTS["memory"]),
                "workerDisk": entry.get("worker_disk", _WORKER_SIZE_DEFAULTS["disk"]),
                "baseDomain": entry.get("base_domain", "ocp.local"),
                "apiVip": entry.get("api_vip"),
                "ingressVip": entry.get("ingress_vip"),
                "ocpVersion": entry.get("ocp_version", "4.20"),
                "pullThroughRegistry": entry.get("pull_through_registry"),
            }
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k build_clusters -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git add src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): derive cluster type/counts into topology cluster objects"
```

---

### Task 3: Materialize RHCOS VMs from counts for count-only clusters

**Files:**
- Modify: `src/backend/app/services/template_loader.py` (add `materialize_cluster_vms`)
- Test: `src/backend/tests/test_ocp_clusters.py`

**Interfaces:**
- Consumes: `build_topology_clusters` output.
- Produces: `materialize_cluster_vms(clusters: list[dict], vms_def: dict) -> dict` — returns a NEW `vms_def` where any cluster lacking enough enumerated control-plane/worker VMs gets generated ones (names `<clusterId>-cp-<n>`, `<clusterId>-worker-<n>`), each with `os: "rhcos"`, the right `role`, `cluster: <name>`, and `cpu`/`memory`/`disk` seeded from the cluster's per-role sizing. Enumerated VMs are preserved; generation only tops up the shortfall. This is what makes the agnosticd REST path work when a template gives `type + workers` but no VM list.

- [ ] **Step 1: Write the failing test**

```python
def test_materialize_generates_missing_cp_and_workers():
    from app.services.template_loader import (
        build_topology_clusters, materialize_cluster_vms, normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 2}])
    clusters = build_topology_clusters(ocp, vms_def={})
    vms = materialize_cluster_vms(clusters, vms_def={})
    cps = [n for n, c in vms.items() if c.get("role") == "control-plane"]
    wks = [n for n, c in vms.items() if c.get("role") == "worker"]
    assert len(cps) == 3 and len(wks) == 2
    sample = vms[cps[0]]
    assert sample["os"] == "rhcos" and sample["cluster"] == "prod"
    assert sample["cpu"] == 8 and sample["memory"] == 16384 and sample["disk"] == 120


def test_materialize_preserves_enumerated_vms():
    from app.services.template_loader import (
        build_topology_clusters, materialize_cluster_vms, normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "prod", "type": "standard", "workers": 2}])
    vms_in = {"cp-0": {"role": "control-plane", "cluster": "prod", "cpu": 16}}
    clusters = build_topology_clusters(ocp, vms_def=vms_in)
    vms = materialize_cluster_vms(clusters, vms_def=vms_in)
    # keeps the custom cp-0 (cpu 16), tops up to 3 CP total
    assert vms["cp-0"]["cpu"] == 16
    assert len([c for c in vms.values() if c.get("role") == "control-plane"]) == 3


def test_materialize_sno_single_node():
    from app.services.template_loader import (
        build_topology_clusters, materialize_cluster_vms, normalize_ocp_section,
    )

    ocp = normalize_ocp_section([{"name": "dev", "type": "sno"}])
    clusters = build_topology_clusters(ocp, vms_def={})
    vms = materialize_cluster_vms(clusters, vms_def={})
    assert len([c for c in vms.values() if c.get("role") == "control-plane"]) == 1
    assert len([c for c in vms.values() if c.get("role") == "worker"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k materialize -v`
Expected: FAIL — `ImportError: cannot import name 'materialize_cluster_vms'`.

- [ ] **Step 3: Write minimal implementation**

```python
def _existing_role_names(vms_def, cluster_name, role, single):
    return [
        n for n, c in vms_def.items()
        if c.get("role") == role and (single or c.get("cluster") == cluster_name)
    ]


def _make_node(cluster, role, cpu, memory, disk):
    return {
        "role": role,
        "os": "rhcos",
        "cluster": cluster["name"],
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
    }


def _topup(vms, cluster, role, want, cpu, memory, disk, single):
    have = _existing_role_names(vms, cluster["name"], role, single)
    prefix = "cp" if role == "control-plane" else "worker"
    for i in range(len(have), want):
        vms[f"{cluster['id']}-{prefix}-{i}"] = _make_node(cluster, role, cpu, memory, disk)


def materialize_cluster_vms(clusters: list[dict], vms_def: dict) -> dict:
    """Top up each cluster's control-plane/worker VMs to match its counts."""
    vms = dict(vms_def or {})
    single = len(clusters) == 1
    for cluster in clusters:
        _topup(vms, cluster, "control-plane", cluster["controlPlane"],
               cluster["controlPlaneCpu"], cluster["controlPlaneMemory"],
               cluster["controlPlaneDisk"], single)
        _topup(vms, cluster, "worker", cluster["workers"],
               cluster["workerCpu"], cluster["workerMemory"],
               cluster["workerDisk"], single)
    return vms
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k materialize -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git add src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): materialize RHCOS cluster VMs from counts"
```

---

### Task 4: Emit `topology['clusters']` and stamp `clusterId` + `parentNode`

**Files:**
- Modify: `src/backend/app/services/template_loader.py` — `_generate_topology_from_vms:1043` (emit clusters; materialize before VM loop) and `_build_vm_data:644` / `_apply_vm_optional_fields:498` (stamp `clusterId`)
- Test: `src/backend/tests/test_ocp_clusters.py`

**Interfaces:**
- Consumes: `build_topology_clusters`, `materialize_cluster_vms`.
- Produces: generated topology dict now contains `topology["clusters"]` (list from Task 2) and every RHCOS OCP `vmNode` has `data.clusterId` set to its cluster id and `parentNode` set to that cluster's node id. The cluster node id is `f"cluster-{cluster['id']}"`; it is added to `topology["clusters"][i]["nodeId"]`. (Frontend Plan 2 renders the boundary from these.)

- [ ] **Step 1: Write the failing test**

```python
def test_generated_topology_has_clusters_and_member_refs():
    from app.services.template_loader import resolve_inline_template, generate_topology

    tmpl = {
        "name": "t", "install_method": "agent", "category": "openshift",
        "networks": {"cluster": {"cidr": "10.0.0.0/24"}},
        "ocp": [{"name": "prod", "type": "standard", "workers": 2,
                 "api_vip": "10.0.0.10", "ingress_vip": "10.0.0.11"}],
    }
    resolved = resolve_inline_template(tmpl)
    topo = generate_topology(resolved)
    assert "clusters" in topo and len(topo["clusters"]) == 1
    prod = topo["clusters"][0]
    assert prod["id"] == "prod" and prod["nodeId"] == "cluster-prod"
    members = [n for n in topo["nodes"]
               if n.get("data", {}).get("clusterId") == "prod"]
    assert len(members) == 5  # 3 cp + 2 workers
    assert all(n.get("parentNode") == "cluster-prod" for n in members)
```

Note: use whatever the real public entry point for generation is. Confirm the exact name (`generate_topology` vs the internal `_generate_topology_from_vms`) by reading `template_loader.py` around line 1043 and the call site in `src/backend/app/api/projects.py:655`; call the same function the API calls.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k generated_topology -v`
Expected: FAIL — no `clusters` key / `clusterId` unset.

- [ ] **Step 3: Write minimal implementation**

In the generator, before iterating VMs, normalize + build + materialize:

```python
    ocp_list = normalize_ocp_section(resolved.get("ocp"))
    clusters = build_topology_clusters(ocp_list, resolved.get("vms"))
    if clusters:
        resolved["vms"] = materialize_cluster_vms(clusters, resolved.get("vms") or {})
        for c in clusters:
            c["nodeId"] = f"cluster-{c['id']}"
```

Map each VM name → cluster id (single-cluster → all OCP VMs join it; multi → by `vms[name]['cluster']`). In `_build_vm_data`/`_apply_vm_optional_fields`, when a VM belongs to a cluster, set:

```python
    vm_data["clusterId"] = cluster_id
    node["parentNode"] = f"cluster-{cluster_id}"
```

Finally attach `topology["clusters"] = clusters`. Keep each helper under complexity 15 — extract a `_cluster_id_for_vm(name, vms_def, clusters)` helper if the mapping logic pushes the generator over the limit.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git add src/backend/app/services/template_loader.py src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): emit topology.clusters and stamp clusterId/parentNode on VMs"
```

---

### Task 5: Lazy migration of legacy persisted topology

**Files:**
- Create: `src/backend/app/services/ocp/cluster_migration.py`
- Modify: call site where topology is loaded for read/edit — `src/backend/app/api/projects.py` `get_project:1006` (apply migration to the returned topology if `category == openshift` and `clusters` absent)
- Test: `src/backend/tests/test_ocp_clusters.py`

**Interfaces:**
- Produces: `migrate_topology_clusters(topology: dict) -> dict` — if `topology` has OCP-ish RHCOS nodes tagged `controllers`/`workers` but no `clusters` key, synthesize a one-element `clusters[]` (id `ocp`, type inferred from node counts, VIPs from existing cluster DNS records if present else `None`) and stamp `clusterId="ocp"` + `parentNode="cluster-ocp"` on those nodes. Idempotent: returns `topology` unchanged if `clusters` already present or no OCP nodes exist.

- [ ] **Step 1: Write the failing test**

```python
def test_migrate_legacy_topology_synthesizes_cluster():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    legacy = {
        "nodes": [
            {"id": "n1", "type": "vmNode",
             "data": {"os": "rhcos", "name": "cp-0",
                      "tags": {"AnsibleGroup": "controllers"}}},
            {"id": "n2", "type": "vmNode",
             "data": {"os": "rhcos", "name": "worker-0",
                      "tags": {"AnsibleGroup": "workers"}}},
        ],
        "edges": [],
    }
    out = migrate_topology_clusters(legacy)
    assert len(out["clusters"]) == 1
    assert out["clusters"][0]["id"] == "ocp"
    assert out["clusters"][0]["type"] == "standard"  # 1 cp + 1 worker => standard
    assert all(
        n["data"]["clusterId"] == "ocp" and n["parentNode"] == "cluster-ocp"
        for n in out["nodes"] if n["data"].get("os") == "rhcos"
    )


def test_migrate_is_idempotent_when_clusters_present():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    topo = {"nodes": [], "edges": [], "clusters": [{"id": "prod"}]}
    assert migrate_topology_clusters(topo) is topo


def test_migrate_noop_without_ocp_nodes():
    from app.services.ocp.cluster_migration import migrate_topology_clusters

    topo = {"nodes": [{"id": "n1", "data": {"os": "rhel"}}], "edges": []}
    out = migrate_topology_clusters(topo)
    assert "clusters" not in out or out["clusters"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k migrate -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# src/backend/app/services/ocp/cluster_migration.py
"""Lazy migration of legacy single-cluster OCP topology to clusters[]."""


def _is_ocp_node(node):
    return node.get("data", {}).get("os") == "rhcos"


def _group_of(node):
    return node.get("data", {}).get("tags", {}).get("AnsibleGroup", "")


def migrate_topology_clusters(topology: dict) -> dict:
    if not isinstance(topology, dict):
        return topology
    if topology.get("clusters"):
        return topology
    ocp_nodes = [n for n in topology.get("nodes", []) if _is_ocp_node(n)]
    if not ocp_nodes:
        return topology
    cp = sum(1 for n in ocp_nodes if "controllers" in _group_of(n))
    wk = sum(1 for n in ocp_nodes if "workers" in _group_of(n))
    ctype = "sno" if cp <= 1 and wk == 0 else ("compact" if wk == 0 else "standard")
    for n in ocp_nodes:
        n.setdefault("data", {})["clusterId"] = "ocp"
        n["parentNode"] = "cluster-ocp"
    topology["clusters"] = [{
        "id": "ocp", "name": "ocp", "nodeId": "cluster-ocp", "type": ctype,
        "controlPlane": 1 if ctype == "sno" else 3, "workers": wk,
        "baseDomain": "ocp.local", "apiVip": None, "ingressVip": None,
        "ocpVersion": "4.20",
    }]
    return topology
```

Wire into `get_project` (only for openshift-category projects; apply to the topology returned to the client, not persisted here — persistence happens on next save).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k migrate -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/ocp/cluster_migration.py src/backend/tests/test_ocp_clusters.py src/backend/app/api/projects.py && git add src/backend/app/services/ocp/cluster_migration.py src/backend/tests/test_ocp_clusters.py src/backend/app/api/projects.py && git commit -m "feat(ocp): lazily migrate legacy topology to clusters[]"
```

---

### Task 6: Remap cluster references when cloning topology

**Files:**
- Modify: `src/backend/app/api/patterns.py` — `_remap_topology:161` (add cluster remapping) plus a new helper `_remap_clusters`
- Test: `src/backend/tests/test_ocp_clusters.py`

**Interfaces:**
- Consumes: `_remap_topology(topology)` (existing).
- Produces: after remap, `topology["clusters"][i]["id"]` and `["nodeId"]` are regenerated, every `vmNode.data.clusterId` and `parentNode` points at the remapped cluster/node ids, and edges/node ids remain internally consistent. New cluster ids are unique slugs (`f"{old_id}-{short_uuid}"` acceptable) so two clones in one project don't collide.

- [ ] **Step 1: Write the failing test**

```python
def test_remap_topology_remaps_cluster_refs():
    from app.api.patterns import _remap_topology

    topo = {
        "clusters": [{"id": "prod", "nodeId": "cluster-prod", "name": "prod"}],
        "nodes": [
            {"id": "cluster-prod", "type": "clusterNode", "data": {"name": "prod"}},
            {"id": "n1", "type": "vmNode", "parentNode": "cluster-prod",
             "data": {"os": "rhcos", "clusterId": "prod",
                      "nics": [{"id": "nic1", "mac": "52:54:00:aa:bb:cc"}]}},
        ],
        "edges": [],
    }
    out = _remap_topology(topo)
    new_cluster = out["clusters"][0]
    assert new_cluster["id"] != "prod"
    member = next(n for n in out["nodes"] if n["type"] == "vmNode")
    assert member["data"]["clusterId"] == new_cluster["id"]
    assert member["parentNode"] == new_cluster["nodeId"]
    # the cluster node itself got a new id matching nodeId
    cluster_node = next(n for n in out["nodes"] if n["type"] == "clusterNode")
    assert cluster_node["id"] == new_cluster["nodeId"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k remap -v`
Expected: FAIL — cluster id unchanged / member refs stale.

- [ ] **Step 3: Write minimal implementation**

Because `_remap_node_ids` already regenerates every node id into `id_map` (including the `clusterNode`), reuse that map. Add a helper and call it inside `_remap_topology` before `return topo`:

```python
def _remap_clusters(topo, id_map):
    """Remap clusters[] ids/nodeIds and member clusterId/parentNode refs."""
    old_node_to_cluster = {}  # old cluster nodeId -> new nodeId
    for cluster in topo.get("clusters", []):
        old_node_id = cluster.get("nodeId")
        new_node_id = id_map.get(old_node_id, old_node_id)
        old_node_to_cluster[cluster.get("id")] = {
            "id": new_node_id.replace("cluster-", "", 1) if new_node_id else cluster.get("id"),
            "nodeId": new_node_id,
        }
        cluster["nodeId"] = new_node_id
        cluster["id"] = old_node_to_cluster[cluster.get("id")]["id"] if False else \
            (new_node_id.replace("cluster-", "", 1) if new_node_id else cluster["id"])
    # simpler: derive cluster id from its remapped node id
    id_by_old = {}
    for cluster in topo.get("clusters", []):
        id_by_old  # (see refined form below)
```

Refine to the clean form (implement this, not the sketch above):

```python
def _remap_clusters(topo, id_map):
    old_to_new = {}  # old cluster id -> new cluster id
    for cluster in topo.get("clusters", []):
        old_cluster_id = cluster.get("id")
        old_node_id = cluster.get("nodeId")
        new_node_id = id_map.get(old_node_id, old_node_id)
        new_cluster_id = (
            new_node_id[len("cluster-"):]
            if new_node_id and new_node_id.startswith("cluster-")
            else new_node_id or old_cluster_id
        )
        cluster["id"] = new_cluster_id
        cluster["nodeId"] = new_node_id
        old_to_new[old_cluster_id] = new_cluster_id
    for node in topo.get("nodes", []):
        data = node.get("data", {})
        if data.get("clusterId") in old_to_new:
            data["clusterId"] = old_to_new[data["clusterId"]]
        # parentNode already remapped via id_map by _remap_node_ids/_remap_edges?
        if node.get("parentNode") in id_map:
            node["parentNode"] = id_map[node["parentNode"]]
```

Then in `_remap_topology`, after `_remap_node_ids(...)` populates `id_map`, add: `_remap_clusters(topo, id_map)`. Confirm `_remap_node_ids` assigns new ids for **all** node types (including `clusterNode`); if it filters by type, widen it to cover `clusterNode`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k remap -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/api/patterns.py src/backend/tests/test_ocp_clusters.py && git add src/backend/app/api/patterns.py src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): remap cluster ids and member refs on topology clone"
```

---

### Task 7: Rewrite shipped OCP templates to the list schema

**Files:**
- Modify: `src/backend/templates/ocp-sno.yaml`, `ocp-compact.yaml`, `ocp-standard.yaml`
- Modify: `example_templates/ocp-sno.yaml`, `ocp-compact.yaml`, `ocp-standard.yaml` (keep in sync)
- Test: `src/backend/tests/test_ocp_clusters.py` and existing `src/backend/tests/test_template_loader.py` (must still pass)

**Interfaces:**
- Consumes: everything above (the list schema must round-trip through resolve + generate).
- Produces: shipped templates use the new `ocp:` list form; existing template tests still pass (they assert on `vms`, not on `ocp` shape).

- [ ] **Step 1: Write the failing test**

```python
def test_shipped_templates_use_ocp_list_and_generate_clusters():
    import os
    from app.services.template_loader import (
        load_template, resolve_inline_template, generate_topology,
    )
    tdir = os.path.join(os.path.dirname(__file__), "..", "templates")
    for name, expect_type in [("ocp-sno", "sno"),
                              ("ocp-compact", "compact"),
                              ("ocp-standard", "standard")]:
        raw = load_template(name, templates_dir=tdir)
        assert isinstance(raw["ocp"], list), f"{name} ocp not a list"
        topo = generate_topology(resolve_inline_template(raw))
        assert topo["clusters"][0]["type"] == expect_type
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocp_clusters.py -k shipped -v`
Expected: FAIL — `raw["ocp"]` is still a mapping.

- [ ] **Step 3: Rewrite the templates**

In each template, replace the mapping:

```yaml
ocp:
  cluster_name: ocp
  base_domain: ocp.local
  api_vip: 10.0.0.10
  ingress_vip: 10.0.0.11
```

with the list form (SNO example; adjust `type` per template — `sno`/`compact`/`standard`):

```yaml
ocp:
  - name: ocp
    type: sno
    base_domain: ocp.local
    api_vip: 10.0.0.10
    ingress_vip: 10.0.0.11
```

Leave the `vms:` sections as-is (they stay enumerated; the single cluster claims them). Remove the `bastion` VM from these templates only if a later plan (bastion removal) says so — **not in this plan**; keep the bastion for now so nothing else breaks.

- [ ] **Step 4: Run the full template + cluster suites**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py tests/test_ocp_clusters.py -v`
Expected: PASS (all — legacy template tests unaffected, new list tests green).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/templates/ocp-sno.yaml src/backend/templates/ocp-compact.yaml src/backend/templates/ocp-standard.yaml example_templates/ocp-sno.yaml example_templates/ocp-compact.yaml example_templates/ocp-standard.yaml src/backend/tests/test_ocp_clusters.py && git commit -m "feat(ocp): rewrite shipped OCP templates to ocp list schema"
```

---

### Task 8: Full regression + agnosticd REST-path sanity

**Files:**
- Test only — run the broader suite to catch consumers of the `ocp` mapping shape.

- [ ] **Step 1: Run the OCP-adjacent suites**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py tests/test_ocp_clusters.py tests/test_agent_template.py tests/test_deploy_template.py tests/test_deploy_topology.py tests/test_ocp_topology_flags.py tests/test_api_projects.py -v`
Expected: PASS. If `test_agent_template.py` fails, it is reading `resolved["ocp"]` as a mapping — that is **expected** and is fixed in Plan 3 (per-cluster deploy). Record which tests fail so Plan 3 targets them; do NOT patch `agent_template.py` here.

- [ ] **Step 2: Run pyright**

Run: `pyright src/backend/app/services/template_loader.py src/backend/app/services/ocp/cluster_migration.py src/backend/app/api/patterns.py`
Expected: no new errors.

- [ ] **Step 3: Commit (if any test-only fixes were needed)**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "test(ocp): regression pass for multi-cluster data model"
```

---

## Self-Review

**Spec coverage (§4 + decisions):**
- §4.1 topology `clusters[]` + camelCase fields → Tasks 2, 4. ✓
- §4.1 `vmNode.data.clusterId` + `parentNode` nesting → Task 4. ✓
- §4.1 per-role cpu/mem/disk seeding → Tasks 2, 3. ✓
- §4.2 remapping (`clusters[].id`, `clusterId`, `parentNode`) → Task 6. ✓
- §4.3 `ocp:` list + legacy mapping back-compat → Tasks 1, 7. ✓
- §4.3 count-only clusters (agnosticd path) materialize VMs → Task 3. ✓
- §4.3 lazy topology migration → Task 5. ✓
- Decision #4 type→CP lock (1/3) → Task 2. ✓
- Decision #5 explicit VIPs carried per cluster → Tasks 2, 4 (fields carried; UI entry is Plan 2; VIP *consumption* is Plan 3). ✓
- **Deferred by design to later plans:** canvas/clusterNode rendering & drag membership (Plan 2), per-cluster install-config/DNS/port-forwards (Plan 3), ops pod (Plan 4), console pod (Plan 5), inventory plugin clusterId (Plan 6). Called out in Task 8 so Plan 3 inherits the failing `agent_template` tests.

**Placeholder scan:** No TBD/TODO; every code step has real code. Task 4 and Task 8 ask the implementer to confirm one real name (`generate_topology` public entry) and one real behavior (`_remap_node_ids` covers `clusterNode`) against the live source rather than guessing — these are verification steps, not placeholders.

**Type consistency:** `normalize_ocp_section` (snake_case dicts) → `build_topology_clusters` (camelCase, adds `id`) → `materialize_cluster_vms` (snake_case vms) → generator emits `topology["clusters"]` with `nodeId` → `migrate_topology_clusters` and `_remap_clusters` operate on the same camelCase `id`/`nodeId`/`clusterId`/`parentNode` names throughout. Consistent.

---

## Roadmap (subsequent plans — written when this lands)

- **Plan 2 — Canvas & frontend:** `clusterNode` type, drag-in/out membership, count→materialize (mirrors Task 3 in TS), cluster config panel with VIP/type/sizing, role dropdown.
- **Plan 3 — Per-cluster deploy:** `agent_template.py` per-cluster loop; per-cluster install-config/agent-config, DNS records, port-forwards; VIP consumption + SNO `platform: none`. Fixes the tests Task 8 flags.
- **Plan 4 — Ops pod:** persistent per-project pod (podman + KubeVirt parity), EE image, ISO build + Redfish + wait-for; scoped `ApiKey`.
- **Plan 5 — Console pod + showroom terminal:** non-root oc-only pod, merged kubeconfig contexts, ttyd tab.
- **Plan 6 — Inventory & day-2:** `troshka.cloud` `inventory/troshka.py` per-cluster groups; day-2/monitoring loops.
