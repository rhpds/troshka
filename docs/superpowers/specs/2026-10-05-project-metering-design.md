# Troshka project metering (native)

**Date:** 2026-10-05  
**Status:** Implemented

In-app usage → dollars so a user can see **live spend**, set a **budget** (warn / auto-stop), and read a **final bill** after the project is gone. Independent of reporting-api ingest. Metering never POSTs usage or cost to reporting.

Related (do not merge): [`docs/superpowers/plans/2026-10-03-reporting-api-ingest.md`](../plans/2026-10-03-reporting-api-ingest.md) — pointer-only org chargeback.

## Goals

- Cloud-agnostic: same meter on libvirt, AWS/GCP/Azure, KubeVirt, OCP Virt.
- Per-cloud **type defaults** plus optional **per-host** rate overrides.
- CPU and RAM accrue only while the VM is **running**. Disk, Elastic IPs, and Ceph accrue whenever they exist (including while the project is stopped).
- Hybrid accumulate: periodic reconcile is source of truth; power/resource events refresh immediately.
- Budget: ~80% UI warn, 100% **auto-stop** (existing stop path; project kept). Persistent resources can keep accruing after stop; do not auto-stop twice.
- Final invoice frozen on destroy; history in a root **Metering** nav item.
- No worker id, no reporting-api payloads from this subsystem.

## Non-goals (v1)

- Sending meter data to demo-reporting / chargeback.
- Scraping the K8s project namespace (that is reporting’s collectors).
- Start/stop as billable *events* (only open/close intervals).
- Per-hypervisor invoice split for multi-host (one project invoice; line items may still name hosts).
- Multi-currency, tax, Salesforce.
- Auto-delete at a second dollar cap.
- Backfill of historical projects.

## Concepts

```
topology / VM power / disks / EIPs / Ceph
        │
        ├─ event hooks (start/stop/attach/release)  →  open/close intervals now
        └─ poll worker (1–5 min)                    →  reconcile vs topology
                         │
                         ▼
              metering_intervals (qty × unit_rate × duration)
                         │
                         ├─ GET spend (live)  → project banner + Metering page
                         ├─ budget 80% / 100% → warn banner / auto-stop
                         └─ destroy           → close all → project_invoices row
```

Spend at time T = Σ closed intervals + Σ open intervals through T.

Changing a rate does **not** rewrite closed rows. New/reopened intervals copy the rate in effect at open.

## Rate card

**Lookup:** host override → nested factor → instance catalog (apportioned) → admin type default → kubevirt unit rates.

KubeVirt shared-cluster rates (CPU/RAM/disk) are the fallback for libvirt, ocpvirt, and unknown SKUs. ocpvirt host VMs use those same unit rates (a `64c-256g` parent is just kubevirt CPU+RAM × size; nested guests pay their fraction).

**Nested factor** (`metering.nested_factor`, default `0.5`): multiplies CPU/RAM unit rates for every provider **except** `kubevirt`, and except hosts with `billing_mode=dedicated`. Reflects overcommit on nested virt. Disk / EIP / Ceph are not discounted.

**Billing mode** (per host, default `shared`):

| Mode | Who | Accrues |
| --- | --- | --- |
| `shared` | Quickstart / multi-tenant pools | Running guest CPU/RAM (+ nested factor); project stop freezes all intervals |
| `dedicated` | Single-tenant CI | Full host CPU+RAM hourly whether VMs are up or not; no nested factor; no per-guest double bill |

Public-cloud hosts (`ec2` / `gcp` / `azure`) use Linux on-demand **retail hourly** from `src/backend/app/data/instance_hourly_rates.json` (us-east-1 / us-central1 / eastus, dated in-file). That hourly is split into per-vCPU and per-GiB using kubevirt CPU:RAM weights, then a shared project pays only for **its running VMs** as `(vm vCPUs / host vCPUs)` + `(vm RAM / host RAM)` × nested factor. Idle capacity and other tenants are not billed to this project. Disk / EIP / Ceph stay on kubevirt unit rates (not in the instance list price). Metal SKUs are listed separately from nested sizes.

| Key | Unit | Accrues |
| --- | --- | --- |
| `vcpu_hour` | $ / vCPU-hour | VM running |
| `ram_gib_hour` | $ / GiB RAM-hour | VM running |
| `disk_gib_hour` | $ / GiB disk-hour | disk exists |
| `eip_hour` | $ / EIP-hour | EIP associated |
| `ceph_gib_hour` | $ / GiB Ceph-hour | Ceph volume/share exists |

Admin UI: type defaults on **Metering rates**; per-host overrides on Hosts. Users never edit rates.

## Data model

### `metering_intervals`

Open or closed usage rows.

- `id`, `project_id` (FK, cascade delete **after** invoice freeze)
- `kind`: `vcpu` | `ram` | `disk` | `eip` | `ceph`
- `resource_id`: VM/disk/EIP/Ceph node or row id (string)
- `qty` (numeric): vCPUs, GiB, or 1 for EIP
- `unit_rate` (numeric): $/unit-hour copied at open
- `host_id`, `provider_type` (denormalized for invoices)
- `started_at`, `ended_at` (null = open)
- Unique-ish: at most one **open** row per `(project_id, kind, resource_id)`

### `projects` budget fields

- `budget_usd` (nullable) — null = no cap
- `budget_warned` (bool) — like `auto_stop_warned`
- Optional `budget_stopped` — true after 100% auto-stop so we do not fire stop again while disk/EIP/Ceph keep climbing

Default budget: none in v1 (user sets on the project). Admin org-wide default can wait.

### `project_invoices`

Survives project delete (no ON DELETE CASCADE from `projects`).

- `id`, `project_id` (UUID string, no cascade), `project_name`, `owner_id`
- `total_usd`, `line_items` JSONB (per kind: qty-hours, rate, subtotal, hosts)
- `period_start`, `period_end` (`finalized_at`)
- `currency`: `USD`

Destroy order: reconcile + close all intervals → insert invoice → contribute month slices to draft monthly statements → then existing destroy (intervals go away with the project).

Users see invoices they own; admins see all.

### `monthly_statements` (hybrid rollup)

Per-owner UTC calendar month. Compute overlap cost from intervals, then persist an immutable row.

- `owner_id`, `period_start` / `period_end` (month bounds), unique per owner+month
- `line_items.by_project[]`: project name, subtotal, `by_kind[]`
- `status`: `draft` while month open / after mid-month destroy contributions; `final` after month closes
- Metering poll finalizes the previous month; admin can `POST /metering/statements/finalize?year=&month=`
- Destroy still creates a per-project invoice; monthly statements are the accumulation view

## Accumulate (hybrid)

**Events (immediate):** VM start/stop, deploy/destroy, auto-stop, disk add/remove, EIP allocate/release, Ceph attach/detach. Close matching open rows; open new ones with current rate.

**Poll:** RQ/timer alongside `project_timer` (every few minutes). For each non-draft project with a host, diff topology/DB vs open rows. Missing running VM → close vcpu/ram; extra running VM → open; same for disk/EIP/Ceph. Never rely on events alone.

**CPU/RAM qty** from VM topology (`vcpu` / `memory` GiB). **Disk** from disk size GiB. **EIP** qty 1 per associated address. **Ceph** from attached capacity GiB.

Multi-host: interval `host_id` is the VM’s assigned host; rates from that host.

Draft / no host: no intervals.

## Budget enforcement

Reuse `project_timer` / auto-stop stop path — do not invent a second stopper.

- After each reconcile (and after event-driven spend update): `spend >= 0.8 * budget` and not `budget_warned` → set flag, WS/banner warn.
- `spend >= budget` and project `active` and not already `budget_stopped` → same path as auto-stop (state stopping, `auto_stopped` or `budget_stopped`, spawn stop).
- After stop on **shared** hosts, all intervals close (meter freezes). On **dedicated** hosts, full-host intervals stay open. Spend can exceed 100%; UI shows overage. No second stop. No auto-delete from budget in v1.

Warn copy: spend vs budget, not the 5-minute auto-stop timer copy.

## API

All existing auth. Owner (and admin) only.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/metering/rates` | Type defaults (admin write; users may read for display) |
| PUT | `/api/v1/metering/rates` | Admin replace type defaults |
| GET | `/api/v1/projects/{id}/metering` | Live spend: total, by kind, budget, warned/stopped |
| PATCH | `/api/v1/projects/{id}` | `budget_usd` (existing project update) |
| GET | `/api/v1/metering/invoices` | Current user’s invoices (admin: `?all=true`) |
| GET | `/api/v1/metering/invoices/{id}` | One invoice + line items |

Host override: extend existing host GET/PATCH, not a new resource.

Live `GET .../metering` may close-through-now in memory without writing, or close-reopen open rows; poll is still the persist.

## UI

**Root nav** (with Getting Started / Projects / …): **Metering** → `/metering`.

- Table of **invoices** (past destroyed projects): name, dates, total, link to detail.
- Optional second tab **Active**: live spend + budget for current projects (same data as project banner). Empty state if none.

**Invoice detail:** line items by kind, rates, period, project name (even if deleted).

**Project page (live):** toolbar/banner — spend so far, budget if set, warn at 80%, “stopped: over budget” after 100% stop. Budget field on project settings (alongside auto-stop minutes).

**Admin:** rate table by provider type; host form “metering overrides”.

Do not put metering under Admin only — users must see their own bills.

## Config

```yaml
metering:
  enabled: true          # false: no poll, no intervals, APIs 404
  poll_seconds: 120
  warn_ratio: 0.8
  rates:                 # type defaults; DB/admin overrides win if we store rates in DB
    aws: { vcpu_hour: 0, ram_gib_hour: 0, ... }
```

v1: persist admin-edited rates in DB (`metering_rate_defaults` keyed by type) so Helm restarts do not wipe UI edits. Config YAML is seed/fallback.

## Testing

- Interval open/close on start/stop; disk accrues while stopped.
- Poll repairs a missed stop (closes vcpu/ram).
- Rate change: closed rows unchanged; new interval uses new rate.
- Host override vs type default.
- Budget 80% warn once; 100% triggers stop once; spend can exceed after stop.
- Destroy writes invoice then deletes project; GET invoice still works.
- Metering APIs do not call reporting-api.

## Rollout

1. Schema + service + poll + tests (no UI).
2. Project live spend + budget PATCH + auto-stop hook.
3. `/metering` nav + invoices.
4. Admin rates + host overrides.
5. Enable poll in prod when rates are non-zero.

## Open (v1 defaults)

- Exact PatternFly icon for Metering nav (DollarSign or equivalent).
- Whether Active tab on `/metering` ships in the same PR as invoices (yes if cheap).
- Ceph qty: provisioned GiB vs used GiB — **provisioned** in v1.
