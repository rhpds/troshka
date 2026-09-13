# DNS Upstream Forwarding — Functional, Honest, Consistent

**Status:** Draft — implementation DEFERRED (2026-09-13)
**Date:** 2026-09-13
**Author:** Patrick Rutledge (with Claude)

> **REVISION 2026-09-13:** The "bare `53` is TCP-only" firewall theory is
> **WRONG** — `troshkad.py:_add_outbound_port_rule` already opens **both tcp and
> udp** for a bare port, and the KubeVirt operator forwards too. So there is **no
> firewall fix** here (dropped from scope). The troshkad workload-runner's
> `Could not resolve host` was a **separate** bug — a transit IP collision (runner
> reused the ops pod's fixed `.4`), tracked/fixed independently, NOT this spec.
> This spec is now scoped to making `dnsUpstream` **functional + honest +
> consistent** only (§4.1–4.3, §4.5). Implementation is **deferred**.

## 1. Summary

Make a network's **"Forward to upstream (internet)"** DNS option (`dnsUpstream`)
actually work: it must **gate** upstream DNS forwarding, **display honestly**, and
behave **consistently across both providers** (troshkad + KubeVirt). Today the
checkbox is effectively a no-op (dnsmasq forwards regardless), it renders
unchecked even when forwarding is active, KubeVirt always forwards, and — the
functional bug that motivated this — the gateway's DNS egress rule is generated
for a **bare `53`** which does not open **UDP 53**, so upstream resolution can
fail (e.g. a workload runner failing `Could not resolve host: github.com`).

## 2. Background (root cause, validated 2026-09-13)

Discovered while live-validating the workloads runner on troshkad. The runner
git-clones agnosticd-v2 + `core_workloads` from github.com, so it needs upstream
DNS. It failed to resolve github.com. Findings:

- The project's cluster dnsmasq **does** forward (`server=8.8.8.8`, `1.1.1.1`) and
  serves internal names (`address=/api.ocp.local/…`). Forwarding config is present.
- The gateway restricts outbound to `outboundPorts = 53,80/tcp,443/tcp,123/udp`
  (bare `53`). **This is fine:** the daemon opens bare ports as **both** tcp+udp,
  so UDP DNS egress is permitted. (The workload-runner's resolve failure was a
  separate transit IP collision, not this.)
- KubeVirt's operator dnsmasq (`src/operator/helpers/dnsmasq.py`
  `_add_dns_forwarder_config`) **always** adds `server=` forwarders, ignoring the
  checkbox — which is why the KubeVirt workload E2E succeeded and masked the issue.
- The frontend checkbox (`PropertiesPanel.tsx` ~4192) binds to
  `data.dnsUpstream ?? false`; the field is absent on existing networks, so it
  renders **unchecked** even though forwarding is effectively on.

## 3. Goals / Non-goals

### Goals
- `dnsUpstream` **functionally gates** upstream DNS forwarding on **both**
  providers (ON = forward; OFF = internal-only).
- The checkbox **displays the real state** (default ON for new networks) and shows
  a **warning** when turned OFF (workloads/agnosticd may break).
- **Backwards-compatible migration:** existing networks with no `dnsUpstream` field
  are treated as **ON**, preserving today's always-forward behavior (no silent
  breakage of deployed/served projects).

### Non-goals
- No checkbox **inversion** (keep "Forward to upstream (internet)" semantics).
- No change to how internal names (`api.ocp.local`, `*.apps`) are served.
- Not adding per-record or split-horizon upstream policy — a single boolean.

## 4. Design

### 4.1 Field semantics + migration
- Topology network node field `data.dnsUpstream: bool`.
- **New networks default `true`** (templates + canvas default).
- **Migration/read semantics:** absence ⇒ **treated as `true`** everywhere it is
  read (backend + operator + frontend display), so existing projects keep
  forwarding. Only an explicit `false` disables it.

### 4.2 Gating — troshkad
- The troshkad dnsmasq config currently always emits upstream `server=` entries.
  Gate them on the effective `dnsUpstream` (absent⇒true): emit `server=` +
  `no-resolv` only when ON; when OFF, no upstream servers (internal-only), leaving
  the `address=/…` internal records intact.
- The troshkad daemon path is `troshkad.py:_build_dnsmasq_config_lines` (~4188,
  the `no-resolv`/`server=` lines) — gate those on the effective `dnsUpstream`.
  (No outbound-port change needed — bare `53` already allows udp.)

### 4.3 Gating — KubeVirt operator
- `src/operator/helpers/dnsmasq.py` `_add_dns_forwarder_config` becomes
  **conditional** on the network's effective `dnsUpstream` (absent⇒true). When OFF,
  no `server=` forwarders are written. This removes the "always forwards" bug and
  makes KubeVirt consistent with troshkad.

### 4.4 Firewall — REMOVED (no-op)
`troshkad.py:_add_outbound_port_rule` already emits **both tcp and udp** rules for
a bare port (`for proto in ("tcp","udp")`), so bare `53` already permits UDP DNS.
No firewall change is needed. (Original theory disproven — see the revision note.)

### 4.5 UI
- Checkbox reads the effective value (absent⇒checked); default checked for new
  networks.
- When **unchecked**, render a warning near the DNS section: e.g. "Internal-only
  DNS — workloads/agnosticd that fetch from the internet (git, galaxy, images) may
  fail." Non-blocking (a proxy/pull-through setup can legitimately run internal-only).

## 5. Components touched
- **Frontend:** `src/frontend/src/components/canvas/PropertiesPanel.tsx` (default +
  warning), `src/frontend/src/lib/gatewayValidation.ts` (effective-value helper if
  needed).
- **Backend:** `src/backend/app/services/deploy_topology.py` (effective-`dnsUpstream`
  read), `src/troshkad/troshkad.py:_build_dnsmasq_config_lines` (gate `server=`),
  templates/canvas default.
- **Operator:** `src/operator/helpers/dnsmasq.py` (`_add_dns_forwarder_config`
  gated on the flag).

## 6. Error handling & edge cases
- Absent `dnsUpstream` ⇒ ON (migration safety) — asserted by tests on all readers.
- `dnsUpstream` ON but gateway `outboundPolicy=restrict` without 53 ⇒ existing
  validation already flags "gateway outbound must allow DNS (53)"; keep it.
- OFF ⇒ internal names still resolve; only upstream stops.

## 7. Testing
- Backend unit: effective-`dnsUpstream` (absent⇒true; explicit false⇒false);
  daemon dnsmasq `server=`/`no-resolv` present iff ON.
- Operator unit: `_add_dns_forwarder_config` emits `server=` iff ON.
- Frontend: `tsc` clean; checkbox default checked; warning renders when off.

## 8. Open items
- None blocking (daemon path identified). Implementation deferred per 2026-09-13.
