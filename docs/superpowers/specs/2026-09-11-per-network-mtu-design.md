# Per-Network MTU (portable OVN MTU for OCP patterns)

**Date:** 2026-09-11
**Status:** Design — approved in brainstorming, pending spec review
**Author:** Patrick Rutledge (with Claude)

## 1. Background / incident

Three OCP pattern deploys in dedicated CI (sandbox `sandbox-hz5vg-troshka`,
cluster ocpv06) hung in recert for 87+ minutes. The console never came up and
the showroom cluster terminal had no kubeconfig.

### Root cause (confirmed on the live cluster)

`ovnkube-controller` fatally aborts on every nested node:

```
F ovnkube.go:148] failed to run ovnkube: failed to start default node network controller:
  MTU (1500) of network interface br-ex is too small for specified overlay MTU (8800)
```

Cascade, top to bottom:

1. Host VM uplink `enp1s0` = **8900** (inherited from ocpv06's jumbo OVN pod
   network via KubeVirt masquerade binding).
2. troshkad creates the nested cluster bridge **`br-1001` at MTU 1500** — a bare
   `ip link add … type bridge`. **No MTU is set anywhere in the codebase**
   (zero `mtu` references in troshkad, zero git history), so the host's 8900 is
   never propagated down.
3. Nested OCP nodes' `br-ex` come up at 1500.
4. The captured gold pattern's OVN is **baked at MTU 8800** (it was captured in a
   jumbo environment; OVN computes overlay = interface − ~100 once, at install,
   and that value is frozen into the pattern's etcd).
5. `1500 < 8800` → ovnkube-controller crashloops on all nodes → `network`
   operator **Degraded** → OVN CNI can't wire pod IPs → apiserver/oauth/console
   stuck `ContainerCreating` for 43h → `route.openshift.io` down.
6. The recert gate (`ops_pod_install.py:479`) requires every operator
   non-degraded + `oc get route console`, so it **never passes** → ops pod loops
   → cluster creds never harvested → `_store_ops_pod_creds` never runs → showroom
   terminal kubeconfig never injected (the empty terminal is downstream of this,
   not a separate bug).

Ruled out during investigation: expired/invalid ocp4 token (zero
`ImagePullBackOff`/`401` cluster-wide; the failure is pre-image-pull). The
`oc debug` DNS timeout to `10.0.0.2:53` is a **separate** follow-up (lab dnsmasq
not forwarding upstream — see §11), not part of this incident.

### The core problem

OVN's overlay MTU is chosen once at install from the primary interface MTU and
frozen into the pattern. A pattern is therefore **pinned to the underlay MTU it
was born on.** Troshka never sets the nested underlay MTU, so a pattern captured
on a jumbo host cannot run where the nested bridge defaults to 1500. MTU must
**travel with the pattern** rather than being re-derived from whatever host it
lands on.

## 2. Goals

- Make the nested cluster underlay MTU a first-class, **per-network** value that
  is stored in the topology and round-trips through pattern capture/deploy.
- Fix the incident: existing jumbo gold patterns deploy on jumbo hosts with no
  recapture.
- New networks default to a sensible value with no user action, overridable.
- Never silently wedge OVN on an MTU mismatch: clamp with a warning, and make the
  recert gate fail fast and legibly when the underlay genuinely can't satisfy the
  pattern.
- Identical behavior across the troshkad (nested libvirt) and KubeVirt-native
  providers — parity is a hard requirement.

## 3. Non-goals

- Live MTU migration of an already-running cluster (OCP's disruptive
  `network.operator … migration mtu` procedure). Out of scope.
- Recovering the three currently-wedged clusters in place (owner chose "leave
  them, decide later").
- The DNS forward-upstream fix (tracked separately, §11).

## 4. Decisions (settled in brainstorming)

| # | Decision |
|---|----------|
| D1 | MTU is a **per-network attribute** stored in topology (`node.data.mtu`). |
| D2 | Legacy patterns with **no MTU field** → **host-uplink fallback** (no recapture required). |
| D3 | New network default = **`"auto"`** → resolves at deploy to `uplink − overhead`; overridable per network. |
| D4 | **Drop** any `TROSHKA_NESTED_MTU` env override — the per-network field + `auto` supersede it. |
| D5 | Manual MTU the host can't carry → **clamp to `uplink − overhead` + warn**; never hard-fail on the number. |
| D6 | **Approach A** — the backend is the single resolution point; the host reports its uplink MTU. |
| D7 | Overhead is **placement-aware**: single-host ≈ 0, multi-host mesh ≈ 110 (WireGuard + VXLAN). |

## 5. Data model

### Topology (JSONB, no migration)

Each network node gains `data.mtu`:

- `"auto"` (default) — resolve to `uplink − overhead` at deploy.
- integer — an explicit request (still subject to clamp).
- absent — treated identically to `"auto"` (covers legacy patterns, D2).

Because it lives in `topology`, it round-trips through pattern capture for free.

### Host model (Alembic migration, auto-runs on startup)

```python
uplink_mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Populated by troshkad reporting the MTU of the host's **default-route
interface**, piggybacked on the existing health poll response. Nullable so old
agents / not-yet-reported hosts are handled (see §9 edge case).

## 6. The resolver (backend, single function — both providers call it)

```
resolve_network_mtu(network_data, host_uplink_mtu, spans_hosts) -> (mtu: int, warning: str | None)
```

1. `requested = network_data.get("mtu")`; `"auto"`/`None`/absent → treat as auto.
2. `overhead = MESH_OVERHEAD if spans_hosts else 0`  (`MESH_OVERHEAD ≈ 110`).
3. `ceiling = host_uplink_mtu - overhead`.
4. auto → `candidate = ceiling`; explicit → `candidate = requested`.
5. If `candidate > ceiling` → `mtu = ceiling`, warning =
   `"MTU {requested} reduced to {ceiling}: exceeds host uplink {uplink} (overhead {overhead})"`.
6. Else `mtu = candidate`, warning = `None`.
7. Floor at OVN minimum (e.g. 1280); if `host_uplink_mtu is None` → return
   `(1500, warning)` (§9).
8. The resolved concrete `mtu` is written into `deployed_topology` so capture
   stores a real number and the deploy is deterministic.

`spans_hosts` comes from the backend's known host placement for the project's
mesh at deploy time.

### Why placement-aware overhead

- **Single-host** (dedicated CI): overhead ≈ 0 → auto = full uplink (8900).
  Legacy 8800 patterns satisfy `8900 ≥ 8800` → **incident fixed**.
- **Multi-host mesh** (WireGuard ~60 + VXLAN ~50): overhead ≈ 110 → auto = 8790.
  A legacy 8800 pattern here **clamps + warns** — correct, because an 8800
  overlay physically cannot ride a WG+VXLAN tunnel over an 8900 uplink without
  fragmenting. The warning replaces today's silent OVN wedge.

## 7. Provider application (all fed the single resolved integer)

### troshkad (nested libvirt)

- **VM NIC** — append `,mtu.size={mtu}` to the `--network` arg in
  `_handle_vm_create` (`src/troshkad/troshkad.py:1274`). Backend includes `mtu`
  in each net dict of the VM-create params.
- **Bridge** — `ip link set br-{vni} mtu {mtu}` in the `networks/full-setup`
  path (after `_ensure_host_dummy_bridge` creates the bridge).
- **Multi-host VXLAN** — set `vxlan-{vni}` MTU to the resolved value in
  `_handle_mesh_join_network` so encapsulated frames fit the uplink.

### KubeVirt-native (operator) — parity

- `build_nad` (`src/operator/helpers/k8s.py:75`) sets `config["mtu"] = {mtu}`.
- `mtu` rides on the Network CR the backend creates/updates; the VMI inherits it
  from the NAD. Same resolved integer as troshkad.

### install-config (fresh installs only)

- `agent_template.py` sets `networking.clusterNetworkMTU = nic_mtu - 100`
  (OVN geneve overhead) for the machine network the OCP nodes attach to.
- Recert/pattern deploys skip this (OVN already baked). It governs only
  brand-new installs and makes the eventual capture deterministic instead of
  relying on OCP auto-detect.

## 8. Capture round-trip

- On capture, freeze the **resolved concrete** MTU into the pattern's
  network-node `data.mtu` (read from `deployed_topology`), never `"auto"`. A
  pattern therefore always carries the exact number it was built with →
  "default to whatever's in the pattern."
- `"auto"`, `null`, and field-absent all take the same resolver path — one
  behavior to reason about and test.

## 9. Frontend, warnings, fast-fail, edge case

### Frontend

- Network-node config panel gains an **MTU field**: default **"Auto"**, or a
  custom integer validated to ~1280–9000. Helper text: "Auto = host uplink;
  jumbo when the host supports it." PatternFly, in the existing node inspector.
  (Showing the *resolved* value post-deploy is a later nicety, not v1.)

### Warning surfacing

- Resolver warnings are emitted into the **deploy progress log** (the stream
  `ClusterInstallLogModal` already renders) and `logger.warning`, so a clamp is
  visible rather than silent.

### Recert-gate fast-fail (the 87-minute-loop fix)

- In the `ops_pod_install.py` recert gate, when the `network` operator is
  Degraded with a message matching the OVN **"MTU … too small for specified
  overlay MTU"** pattern, break immediately with a legible breadcrumb, e.g.
  `[key] recert failed: cluster OVN needs MTU >= 8900 but node br-ex is 1500`,
  instead of looping 160×15s. Addresses [[status-not-cosmetic]].

### Edge case

- `host.uplink_mtu is None` (old agent not yet reporting) → resolver returns
  `1500 + warn` (safe/portable) rather than guessing jumbo.

## 10. Testing (unit; mock all troshkad/network I/O; extra `time.time()` values for 3.13)

- `resolve_network_mtu`: explicit / auto-single-host / auto-multi-host /
  clamp+warn / legacy-missing→fallback / uplink-unknown→1500 / floor.
- troshkad `--network` arg includes `mtu.size=N`; bridge + vxlan MTU set.
- `build_nad` includes `config["mtu"]`.
- install-config `clusterNetworkMTU == nic_mtu - 100`.
- Capture freezes resolved MTU into pattern topology.
- Recert gate parses the MTU-degraded message → fast-fail breadcrumb.
- Health report populates `Host.uplink_mtu`.

## 11. Follow-ups (out of scope here)

- **DNS forward-upstream**: the nested lab dnsmasq (`10.0.0.2:53`) does not
  forward upstream, so in-cluster resolution of external names
  (`registry.redhat.io`) times out. Ties to the existing "DNS forward-upstream
  checkbox no-op" note. Only surfaced here via `oc debug` image pulls; separate
  work item.
- Recovery of the three currently-wedged clusters (redeploy after this fix is the
  clean path; they are effectively unrecoverable in place).

## 12. Affected files (implementation map)

- `src/backend/app/models/host.py` — `uplink_mtu` column.
- `src/backend/alembic/versions/*` — migration for the column.
- `src/backend/app/services/...` — `resolve_network_mtu` + deploy wiring
  (VM-create params, network params, `deployed_topology` write, warning emit).
- `src/backend/app/services/ocp/agent_template.py` — `clusterNetworkMTU`.
- `src/backend/app/services/ocp/ops_pod_install.py` — recert gate MTU fast-fail.
- `src/troshkad/troshkad.py` — NIC `mtu.size`, bridge + vxlan MTU, health report
  of default-route iface MTU.
- `src/operator/helpers/k8s.py` — NAD `mtu`; Network CR carries `mtu`.
- Frontend network-node inspector — MTU field.
- Pattern capture path — freeze resolved MTU into topology.
- Tests across the above.
