# Multi-Cluster OCP — Plan 3: Per-Cluster Deploy Config & Export

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OCP config generation multi-cluster: iterate `topology["clusters"]` to produce a correct per-cluster `install-config.yaml` / `agent-config.yaml`, per-cluster DNS records, and non-colliding gateway port-forwards, consuming each cluster's explicit VIPs and per-cluster SNO detection. Plus: normalize OCP install fields on cluster members regardless of origin (canvas- or backend-created), and make template EXPORT round-trip the `ocp:` list + per-VM `cluster:`.

**Architecture:** Backend-only, at **project-creation time** (`customize_topology` and its helpers in `agent_template.py`, invoked from `projects.py`). Deploy consumes cloud-init baked at creation, so no deploy-time changes are needed. The config *builders* become pure per-cluster functions (take a cluster + its member nodes). `customize_topology` loops clusters. The **single-cluster path keeps working exactly as today** (back-compat); generating/executing a *multi-cluster* install on the current single bastion is out of scope — Plan 4's ops pod runs per-cluster installs. Plan 3 makes the per-cluster config **correct and stored**; Plan 4 executes it.

**Tech Stack:** Python 3.11 (CI 3.13), pytest (SQLite). No new deps.

**Spec:** `docs/superpowers/specs/2026-09-01-multi-cluster-ocp-and-bastionless-install-design.md` (§6 Deploy pipeline; §4.3 export; decisions #4, #5, #6). **Predecessors:** Plan 1 (data model) + Plan 2 (canvas), both landed on this branch.

## Global Constraints

- **Control-plane count is 1 or 3 only.** Per-cluster SNO detection: a cluster is SNO when `type=="sno"` (equivalently `controlPlane==1 and workers==0`) → `platform: none: {}`, VIP = the single node IP; compact/standard → `platform: baremetal` with the cluster's explicit `apiVip`/`ingressVip`.
- **Explicit VIPs (Plan 2 decision #5):** consume `cluster.apiVip`/`cluster.ingressVip` from the cluster object. Fall back to the legacy CIDR-offset derivation ONLY when a cluster has no explicit VIP (back-compat for legacy single-cluster templates). SNO VIP = the control-plane node IP.
- **Scope everything by `clusterId`:** node counts, BMC host entries, rendezvous IP, VIPs, DNS, and member selection must filter to the cluster's members (`data.clusterId == cluster["id"]`), NOT the whole topology.
- **Back-compat / non-breaking:** a single-cluster project (or legacy `ocp:` mapping, or a topology with exactly one cluster) must produce byte-identical install-config/agent-config/DNS/port-forwards to today. The full existing suite (incl. `test_agent_template.py`) stays green except where a test is intentionally updated to the per-cluster signature.
- **RHCOS-only** members. Cognitive complexity ≤ 15 per function (extract helpers; `agent_template.py` functions are already near the limit).
- Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest <path> -v`. Run system `black` before commits. `pyright` clean on changed modules. Git via absolute paths / `cd /Users/prutledg/troshka && git ...`. NO Co-Authored-By.
- **PROCESS GUARD for all implementers:** never `git stash`/`stash pop`/`stash apply`; only `git add <paths>` + commit; if `git status` shows unexpected files, STOP and report.

## Codebase map (verified — current state)

- `agent_template.py`: `customize_topology:375` (uses `normalize_ocp_section(...)[0]`), `_resolve_ocp_vips:347` (+`_apply_controller_vips:337`, `_find_sno_node_ip:322`), `_count_ocp_nodes_by_group:967` (whole-topology tally), `_collect_bmc_host_entries:977`, `_build_install_config:1004` (SNO at :1049), `_build_agent_config:1140`/`_build_agent_host_yaml:1086`/`_extract_agent_host:1119`, `_setup_dns_records:460`/`_build_ocp_dns_records:435`, `_find_cluster_cidr:59`, `BastionOCPConfig:30`. Constants `_API_VIP_OFFSET`/`_INGRESS_VIP_OFFSET` near VIP code.
- `template_loader.py`: `_generate_ocp_dns_records:1200` (ALREADY loops clusters; early-returns per-cluster when no `api_vip`), `_generate_ocp_cluster_dns_records:1183`, `_generate_ocp_port_forwards:576` (fixed 6443/443/80 on one EIP), `_create_gateway_node:~654` (calls port-forwards with `[0]`), `_build_vm_data:910` (stamps bmcEnabled/firmware/secureBoot/bootDevices/bootMethod/powerOnAtDeploy/os/diskControllers+cdrom), `_apply_vm_optional_fields:764` (AnsibleGroup tags at :793), `_stamp_cluster_membership:261`, `export_topology_to_template:2078`, `_export_vm:1799`, `_export_vm_role:1721` (workers → `""`).
- `projects.py`: `create_project_from_template:629` (calls `customize_topology` once, :696-714), export synth `:981-986`. `patterns.py` export synth `:577-582`.
- Deploy consumes baked `ciUserData` (`deploy_topology.py:~1434`); `deploy_service.py` does NOT regenerate OCP config. `apply_sno_ocp_vm_flags` (`ocp_topology_flags.py:28`) guards `len(rhcos_vms)==1`.
- Tests: `test_agent_template.py` (single-cluster `_build_install_config`, bastion cloud-init), `test_ocp_clusters.py` (data-model multi-cluster), `test_ocp_topology_flags.py`, `test_deploy_topology.py` (port-forward/EIP), `test_deploy_template.py`.

---

### Task 1: Cluster-scoped member selection + node counting

**Files:** Modify `src/backend/app/services/ocp/agent_template.py`; Test `src/backend/tests/test_agent_template_multicluster.py` (new).

**Interfaces:**
- Produces: `cluster_member_nodes(topology, cluster_id) -> list[dict]` (vmNodes with `data.clusterId == cluster_id`); `_count_ocp_nodes_by_group(topology, group_name, cluster_id=None)` — when `cluster_id` given, count only that cluster's members; when `None`, preserve today's whole-topology behavior (back-compat). Role detection must accept a member whose role comes from `tags.AnsibleGroup` OR `data.clusterRole` (mirror the frontend `memberRole`: controllers/control-plane, workers/worker).

- [ ] **Step 1: Write failing test**

```python
# tests/test_agent_template_multicluster.py
def _vm(name, cid, group):
    return {"type": "vmNode", "data": {"name": name, "clusterId": cid,
            "tags": {"AnsibleGroup": group}, "os": "rhcos"}}

def test_count_scoped_by_cluster():
    from app.services.ocp.agent_template import _count_ocp_nodes_by_group
    topo = {"nodes": [
        _vm("p-cp-0","prod","controllers"), _vm("p-cp-1","prod","controllers"),
        _vm("p-cp-2","prod","controllers"), _vm("p-w-0","prod","workers"),
        _vm("d-cp-0","dev","controllers"),
    ]}
    assert _count_ocp_nodes_by_group(topo, "controllers", cluster_id="prod") == 3
    assert _count_ocp_nodes_by_group(topo, "workers", cluster_id="prod") == 1
    assert _count_ocp_nodes_by_group(topo, "controllers", cluster_id="dev") == 1
    # back-compat: no cluster_id = whole topology
    assert _count_ocp_nodes_by_group(topo, "controllers") == 4

def test_cluster_member_nodes():
    from app.services.ocp.agent_template import cluster_member_nodes
    topo = {"nodes": [_vm("p-cp-0","prod","controllers"), _vm("d-cp-0","dev","controllers")]}
    assert [n["data"]["name"] for n in cluster_member_nodes(topo, "prod")] == ["p-cp-0"]
```

- [ ] **Step 2: Run → fail** (`cd src/backend && ./venv/bin/python3 -m pytest tests/test_agent_template_multicluster.py -v`) — `cluster_member_nodes` missing / `_count_ocp_nodes_by_group` rejects `cluster_id`.

- [ ] **Step 3: Implement** `cluster_member_nodes` + extend `_count_ocp_nodes_by_group` with optional `cluster_id` and AnsibleGroup/clusterRole role detection (extract a `_node_role(node)` helper mirroring the frontend). Preserve the no-`cluster_id` path exactly.

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** (`feat(ocp): cluster-scoped OCP node selection + counting`).

---

### Task 2: Per-cluster VIP resolution

**Files:** Modify `agent_template.py`; Test `test_agent_template_multicluster.py`.

**Interfaces:**
- Produces: `resolve_cluster_vips(cluster, members, topology) -> tuple[str, str]` — returns `(api_vip, ingress_vip)`. Priority: explicit `cluster["apiVip"]`/`cluster["ingressVip"]` if truthy; else SNO (`type=="sno"`/single CP) → the control-plane member's IP for both; else legacy CIDR-offset (`network+2`/`+3`) from the cluster's network. Keep the existing `_resolve_ocp_vips(topology, ocp_cfg)` as a thin back-compat wrapper (single cluster) delegating to the new function.

- [ ] **Step 1: Write failing test**

```python
def test_resolve_vips_explicit():
    from app.services.ocp.agent_template import resolve_cluster_vips
    cluster = {"id": "prod", "type": "standard", "controlPlane": 3,
               "apiVip": "10.0.0.10", "ingressVip": "10.0.0.11"}
    assert resolve_cluster_vips(cluster, [], {"nodes": []}) == ("10.0.0.10", "10.0.0.11")

def test_resolve_vips_sno_uses_node_ip():
    from app.services.ocp.agent_template import resolve_cluster_vips
    cluster = {"id": "dev", "type": "sno", "controlPlane": 1, "apiVip": "", "ingressVip": ""}
    members = [{"type": "vmNode", "data": {"clusterId": "dev",
        "tags": {"AnsibleGroup": "controllers"}, "nics": [{"ip": "10.1.0.20"}]}}]
    assert resolve_cluster_vips(cluster, members, {"nodes": members}) == ("10.1.0.20", "10.1.0.20")
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `resolve_cluster_vips`; refactor `_resolve_ocp_vips` to delegate for the single-cluster case (read the current SNO IP logic in `_find_sno_node_ip`/`_apply_controller_vips` and reuse). Complexity ≤ 15.
- [ ] **Step 4: Run → pass** (+ run `tests/test_agent_template.py` to confirm the single-cluster wrapper is unchanged in behavior).
- [ ] **Step 5: Commit** (`feat(ocp): per-cluster VIP resolution (explicit + SNO + legacy fallback)`).

---

### Task 3: Per-cluster `install-config.yaml`

**Files:** Modify `agent_template.py`; Test `test_agent_template_multicluster.py`.

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: `_build_install_config` refactored to take an explicit cluster context: `_build_install_config(cluster, members, topology, pull_secret, ssh_key, pull_through_registry) -> str`. Uses `cluster["name"]` for `metadata.name`, `cluster["baseDomain"]`, replicas from cluster-scoped counts, `resolve_cluster_vips`, SNO→`platform: none`, else baremetal with the cluster's VIPs + BMC hosts scoped to `members` (`_collect_bmc_host_entries(members)`). `machineNetwork` from the cluster's member network. Keep any existing caller working via a back-compat shim if signature changes (or update the single caller + tests).

- [ ] **Step 1: Write failing tests** — a standard cluster (3cp/2wrk) → `metadata.name: prod`, `controlPlane.replicas: 3`, `compute.replicas: 2`, `platform: baremetal`, `apiVIPs: [10.0.0.10]`; a SNO cluster → `platform:\n  none: {}`, no apiVIPs. Assert on the YAML text (parse with `yaml.safe_load` and assert structured values). Include a two-cluster topology and assert each cluster's config is independent (prod baremetal 3/2, dev none 1/0).

```python
def test_install_config_standard_and_sno():
    import yaml
    from app.services.ocp.agent_template import _build_install_config, cluster_member_nodes
    # build a 2-cluster topology fixture (prod standard, dev sno) with member nodes,
    # nics/macs, and a cluster network; then:
    prod = {"id":"prod","name":"prod","type":"standard","controlPlane":3,"workers":2,
            "baseDomain":"ocp.local","apiVip":"10.0.0.10","ingressVip":"10.0.0.11"}
    ic = yaml.safe_load(_build_install_config(prod, cluster_member_nodes(topo,"prod"), topo,
                        pull_secret="{}", ssh_key="ssh-rsa x", pull_through_registry=None))
    assert ic["metadata"]["name"] == "prod"
    assert ic["controlPlane"]["replicas"] == 3 and ic["compute"][0]["replicas"] == 2
    assert ic["platform"]["baremetal"]["apiVIPs"] == ["10.0.0.10"]
    dev = {"id":"dev","name":"dev","type":"sno","controlPlane":1,"workers":0,
           "baseDomain":"dev.local","apiVip":"","ingressVip":""}
    icd = yaml.safe_load(_build_install_config(dev, cluster_member_nodes(topo,"dev"), topo,
                         pull_secret="{}", ssh_key="ssh-rsa x", pull_through_registry=None))
    assert icd["platform"] == {"none": {}}
```

(Build the `topo` fixture in the test with real member nodes incl. `nics:[{"ip":..,"mac":..}]` and a `networkNode` with a cidr, so replica counts, VIPs, and BMC MACs resolve.)

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the refactor; scope `_collect_bmc_host_entries` to `members`; keep the SNO `platform: none` branch. Update the existing single-cluster caller (`customize_topology`, Task 5) and the direct `test_agent_template.py` callers (Task 8).
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): per-cluster install-config generation`).

---

### Task 4: Per-cluster `agent-config.yaml`

**Files:** Modify `agent_template.py`; Test `test_agent_template_multicluster.py`.

**Interfaces:**
- Produces: `_build_agent_config(cluster, members, topology) -> str` — `metadata.name` = cluster name, per-host NMState from `members` (static IP/MAC/gateway/DNS), `rendezvousIP` = the cluster's first control-plane member IP. Scope everything to `members`.

- [ ] **Step 1: Write failing test** — two clusters produce two agent-configs with distinct `metadata.name` and distinct rendezvousIP (each cluster's own first controller). Assert host lists don't bleed across clusters.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — refactor `_build_agent_config`/`_build_agent_host_yaml`/`_extract_agent_host` to take `members`. Complexity ≤ 15.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): per-cluster agent-config generation`).

---

### Task 5: `customize_topology` loops clusters + per-cluster DNS

**Files:** Modify `agent_template.py` (`customize_topology`, `_setup_dns_records`); possibly `projects.py`; Test `test_agent_template_multicluster.py`.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: `customize_topology` reads `topology["clusters"]` (falling back to `normalize_ocp_section(resolved["ocp"])` mapped to cluster objects when `clusters` absent — legacy). For EACH cluster it resolves VIPs, builds install-config + agent-config, and writes per-cluster DNS records (`api.<name>.<domain>`, `api-int`, `*.apps`) to the cluster's network node — all clusters' records coexist. **Single-cluster back-compat:** when there's exactly one cluster, the bastion cloud-init is baked exactly as today (same bastion, same files). **Multi-cluster:** store each cluster's generated `install-config`/`agent-config` on the cluster object (e.g. `cluster["_generatedInstallConfig"]`, `cluster["_generatedAgentConfig"]`) or a per-cluster structure for Plan 4's pod to consume; do NOT attempt to bake N clusters into one bastion (leave a clear TODO/marker for Plan 4). DNS + port-forwards (Task 6) are applied for all clusters regardless.

- [ ] **Step 1: Write failing test** — a two-cluster topology through `customize_topology` yields DNS records for BOTH clusters on their networks, and per-cluster generated configs stored on each cluster object; a one-cluster topology still bakes the bastion cloud-init (assert `ciUserData` present on the bastion, unchanged shape).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the loop + `_setup_dns_records` per-cluster; guard the bastion-bake to the single-cluster path; store multi-cluster artifacts. Keep `customize_topology` under complexity 15 by delegating per-cluster work to a `_customize_one_cluster(...)` helper.
- [ ] **Step 4: Run → pass** (+ `tests/test_agent_template.py` single-cluster still green).
- [ ] **Step 5: Commit** (`feat(ocp): customize_topology iterates clusters + per-cluster DNS`).

---

### Task 6: Non-colliding per-cluster port-forwards

**Files:** Modify `template_loader.py` (`_generate_ocp_port_forwards`, `_create_gateway_node`); Test `test_deploy_topology.py` or `test_ocp_clusters.py`.

**Interfaces:**
- Produces: `_generate_ocp_port_forwards(eip_id, vms_def, clusters)` (plural) — emits, per cluster, port-forwards to that cluster's api/ingress VIPs with **distinct external ports** so multiple clusters coexist on one EIP: cluster index `i` → `api` external `6443 + i` → `<apiVip>:6443`, `ingress-https` external `443 + i*... ` (choose a non-overlapping scheme, e.g. `8443 + i`→443, `8080 + i`→80; document it), keeping the FIRST cluster on the canonical `6443/443/80` for back-compat. The bastion SSH `2222→22` stays. Update `_create_gateway_node` to pass all clusters.

- [ ] **Step 1: Write failing test** — one cluster → canonical `6443→apiVip:6443`, `443`, `80` (unchanged from today). Two clusters → cluster0 canonical, cluster1 on distinct external ports mapping to cluster1's VIPs; no external-port collision; each maps to the correct cluster's VIP.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the per-cluster scheme; back-compat for one cluster is byte-identical.
- [ ] **Step 4: Run → pass** (+ existing port-forward tests in `test_deploy_topology.py`).
- [ ] **Step 5: Commit** (`feat(ocp): non-colliding per-cluster gateway port-forwards`).

---

### Task 7: Member OCP-field normalization

**Files:** Modify `template_loader.py` (add `normalize_cluster_member_fields`) + call from generation and/or `customize_topology`; Test `test_ocp_clusters.py`.

**Interfaces:**
- Produces: `normalize_cluster_member_fields(topology) -> topology` — for every vmNode with `data.clusterId` (a cluster member), ensure the OCP install fields exist with role-correct defaults if missing: `os:"rhcos"`, `firmware:"uefi"`, `bmcEnabled` (True for control-plane, True for worker too since baremetal agent install needs BMC per node — confirm against `_build_vm_data` default and match), `bootDevices`/`bootMethod`/`powerOnAtDeploy`/`secureBoot`, `diskControllers` incl a cdrom controller, and `tags.AnsibleGroup`/`clusterRole` (default worker when both absent — the dragged-in/legacy case). Idempotent; never overwrites a value the user/generator already set. Apply it in `customize_topology` (so canvas-created members become deploy-ready) and after count materialization.

- [ ] **Step 1: Write failing tests** — a canvas-style member (only `os:rhcos`+`clusterId`+`clusterRole`) gets `firmware:"uefi"`, `bmcEnabled` set, `bootDevices`/`diskControllers` present; a member with `clusterId` but no AnsibleGroup/clusterRole is defaulted to worker (`AnsibleGroup` contains `workers`, `clusterRole:"worker"`); idempotent (second call no-ops); an already-configured member is untouched.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `normalize_cluster_member_fields` reusing the exact defaults from `_build_vm_data`/`_apply_vm_optional_fields` (extract shared default helpers if needed to avoid drift). Wire the call into `customize_topology` (before building install/agent config, so counts/BMC entries see normalized members).
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): normalize OCP install fields on cluster members`).

---

### Task 8: Export round-trip (`ocp:` list + per-VM `cluster:` + worker role)

**Files:** Modify `template_loader.py` (`export_topology_to_template`, `_export_vm`, `_export_vm_role`), `projects.py` (`:981`), `patterns.py` (`:577`); Test `test_ocp_clusters.py`.

**Interfaces:**
- Produces: `export_topology_to_template` emits `ocp:` as a LIST built from `topology["clusters"]` (name, type, base_domain, api_vip, ingress_vip, workers, per-role sizing, ocp_version, pull_through_registry — snake_case template keys). `_export_vm` emits `cluster: <cluster name>` for member VMs. `_export_vm_role` returns `worker` for the workers group (not `""`). The API export endpoints stop synthesizing from `ocpMeta` and use the topology-emitted `ocp:` (or the export function now owns it). A full **round-trip test** (generate topology from a 2-cluster template → export → resolve → generate) preserves both clusters, their types/VIPs, and per-VM membership.

- [ ] **Step 1: Write failing round-trip test** — from a 2-cluster template, `generate_topology_from_template` → `export_topology_to_template` produces `ocp:` list of 2 with correct names/types/VIPs and vms carrying `cluster:`; re-resolving + regenerating yields 2 clusters with the same membership counts. Also assert a worker VM exports `role: worker`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** export of `ocp:` list + `cluster:` + worker-role fix; update the two API endpoints to use it (remove the `ocpMeta` single-mapping synth, or keep as a fallback for legacy topology without `clusters`).
- [ ] **Step 4: Run → pass** (+ existing export tests in `test_api_projects.py`/`test_patterns.py` still green — update any asserting the old single `ocp` mapping shape).
- [ ] **Step 5: Commit** (`feat(ocp): export ocp cluster list + per-VM cluster membership`).

---

### Task 9: Update single-cluster generation tests + regression

**Files:** `test_agent_template.py` (update to new per-cluster signatures where changed), run adjacent suites.

- [ ] **Step 1:** Update `test_agent_template.py` callers of `_build_install_config`/`_build_agent_config` to the new signatures (wrap the single cluster as a cluster object + members), keeping their single-cluster assertions. Confirm the legacy-mapping bastion cloud-init test still passes (back-compat path).
- [ ] **Step 2:** Run the OCP-adjacent suites: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_agent_template.py tests/test_agent_template_multicluster.py tests/test_ocp_clusters.py tests/test_template_loader.py tests/test_deploy_topology.py tests/test_deploy_template.py tests/test_ocp_topology_flags.py tests/test_api_projects.py tests/test_patterns.py -v` → all green.
- [ ] **Step 3:** Full suite `./venv/bin/python3 -m pytest -q` → green (note known pre-existing test-isolation flakes; two clean runs).
- [ ] **Step 4:** `pyright src/backend/app/services/ocp/agent_template.py src/backend/app/services/template_loader.py src/backend/app/api/projects.py src/backend/app/api/patterns.py` → no new errors.
- [ ] **Step 5: Commit** (`test(ocp): per-cluster deploy regression pass`).

---

## Self-Review

**Spec coverage (§6 + §4.3):**
- Per-cluster install-config (name/domain/replicas/platform/VIPs/BMC hosts) → Tasks 1,2,3. ✓
- Per-cluster agent-config + rendezvous → Task 4. ✓
- customize_topology loops clusters; per-cluster DNS → Task 5. ✓
- Non-colliding per-cluster port-forwards → Task 6. ✓
- SNO `platform: none` per cluster; explicit VIPs → Tasks 2,3. ✓
- Member OCP-field normalization (canvas-created deploy-ready; default role for AnsibleGroup-less members — the pulled-forward Plan-2 item) → Task 7. ✓
- Export round-trip (`ocp:` list + per-VM `cluster:` + worker role) → Task 8. ✓
- Back-compat single-cluster byte-identical → Tasks 2,3,5,6,9 (explicit). ✓
- **Deferred to Plan 4:** executing a MULTI-cluster install (the current single bastion runs one; the ops pod runs per-cluster). Plan 3 stores per-cluster configs; Plan 4 consumes them. Also: `apply_sno_ocp_vm_flags` multi-cluster monitoring/bastion flags (single-cluster-shaped) — revisit in Plan 4/5 if per-cluster monitoring is needed.

**Placeholder scan:** test fixtures for `_build_install_config`/`_build_agent_config` require real member nodes (nics/macs) + a network node — the tasks say to build those in the fixture; that's construction guidance, not a TBD. Port-forward external-port scheme (Task 6) is specified with a concrete example; the implementer picks the exact non-overlapping numbers and documents them.

**Type/interface consistency:** cluster objects use the Plan-1 camelCase shape (`apiVip`/`ingressVip`/`controlPlane`/`baseDomain`); template export uses snake_case (`api_vip`/`base_domain`) per the YAML schema — Task 8 maps between them, consistent with `normalize_ocp_section`/`build_topology_clusters`. `cluster_member_nodes`/`_count_ocp_nodes_by_group(cluster_id=)`/`resolve_cluster_vips`/`_build_install_config(cluster, members, ...)`/`_build_agent_config(cluster, members, ...)` names are used consistently across Tasks 1-9.

## Roadmap (subsequent plans)
- **Plan 4** — ops pod (bastionless install): consume Plan 3's per-cluster configs, run agent ISO build + Redfish + wait-for per cluster in parallel from the netns-attached pod; scoped API key; remove the bastion.
- **Plan 5** — console pod + showroom terminal. **Plan 6** — inventory plugin per-cluster groups + day-2/monitoring.
