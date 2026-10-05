# Troshka → Reporting API ingest

> **Source:** [troshka billing](https://docs.google.com/document/d/1N96aMyLoYi6bLt3uGa6o03w7KrCV1y5vvsdveI71PtA/edit) (Google Doc, 2 Oct 2026). Decisions still open for discussion.
>
> **For agentic workers:** track work with the checkboxes below. Do not implement collectors or send worker id.

**Goal:** Troshka posts a **pointer** (who / where / when) on project deploy and destroy. Reporting-api and other tools inspect the **project namespace** after the fact for usage and cost, then chargeback to the SSO user. Native in-app metering (live spend, budgets, invoices) is a separate subsystem and does not send payloads to reporting-api.

**Repos:** Troshka (`redhat-gpte/troshka`) + reporting-api (`rhpds/demo-reporting`).

---

## Billing model

Troshka does **not** send a bill of materials. Phase 1 is the only Troshka work.

| Phase 1 — Troshka (live) | Phase 2 — reporting (after the fact) |
| --- | --- |
| On deploy / destroy, POST: `uuid` + SSO email + project namespace + host cluster + timestamps. No VM list, no vCPU/RAM, no catalog, no worker id. | Other tools inspect `cluster` + `namespace` (metrics, kube inventory, storage) for **usage and cost**. Chargeback: `users.email` → Workday `employee_number` / cost center / department. |

```
OpenShift SSO  →  oauth-proxy (X-Forwarded-Email)  →  Troshka  →  POST /provision/troshka
                                                                    ↓
                                                         users.email → Workday
                                                                    ↓
                                              provisions row (pointer) + retired_at window
                                                                    ↓
                                         cost tools scrape cluster + namespace → chargeback
```

---

## Decisions (open for discussion)

| Topic | Decision |
| --- | --- |
| Shape | New Troshka-only HTTP API. Do not POST AnarchySubjects or fake ResourceClaims. |
| Storage | New **columns** on `provisions`. `service_type = troshka`. |
| Identity | SSO email only. Chargeback key. No worker id on the wire. |
| Employee / worker id | Reporting looks up users by email. Workday owns `employee_number`. |
| UUID | `projects.id`. Same value on create and retire. |
| Namespace | Project K8s ns (`{project_prefix}{project_id[:8]}`, e.g. `troshka-a1b2c3d4`). Locator for collectors. Not Troshka control-plane ns. |
| Cluster | Deploy host `hosts.instance_id`. Which cluster collectors inspect. |
| When | Create on **deploy** (host assigned). Retire on **destroy**. Not empty-canvas create. |
| Retire | Same Troshka endpoint with `event=deleted`, or set-retired-by-uuid. Prefer one Troshka endpoint. |
| Scope | **KubeVirt shared prod only.** Not ocpvirt running troshkad in a VM (dedicated Troshka CI) — reporting already covers that via Babylon. |

---

## Payload

`POST /api/provision/v1/troshka` (name flexible):

```json
{
  "uuid": "<projects.id>",
  "email": "user@redhat.com",
  "namespace": "troshka-a1b2c3d4",
  "cluster": "<hosts.instance_id>",
  "event": "created | deleted",
  "provisioned_at": "2026-10-03T13:00:00Z",
  "retired_at": "2026-10-04T18:00:00Z"
}
```

| Field | Required | Source | Notes |
| --- | --- | --- | --- |
| `uuid` | yes | `projects.id` | PK in `provisions` |
| `email` | created | SSO `X-Forwarded-Email` (owner) | Never send `X-Forwarded-User` as email; skip if missing |
| `namespace` | yes | `_project_ns(provider, project_id)` | Collectors scrape this ns |
| `cluster` | created | `Host.instance_id` via `project.host_id` | Which cluster |
| `event` | yes | deploy vs destroy | `created` / `deleted` |
| `provisioned_at` | created | deploy start or first host assign | |
| `retired_at` | deleted | destroy time | Window for collectors |

**Do not send:** worker_id, inventory, Anarchy/ResourceClaim/Tower/catalog/Salesforce, `X-Forwarded-User` as identity.

**Emit:** deploy / first host assigned (`created`); destroy (`deleted`).

**Do not emit:** canvas draft create; stop / start / reconfigure.

Skip when `auth_source` is not SSO or email is missing / `local-dev@troshka`. Idempotent upsert on `uuid`. Destroy of unknown uuid: log, do not fail destroy.

---

## Out of scope (v1)

- Worker id from SSO / Identity.extra / `--pass-access-token`
- Inventory / metrics / SKU from Troshka (collectors)
- Start/stop as separate events
- One row per hypervisor (v1 = one row per project, primary host)
- Backfill of historical projects

---

## Open questions

- [ ] Exact URL prefix and auth (Babylon operator token vs Troshka-specific key)
- [ ] `provisioned_at`: deploy-start vs deploy-success?
- [ ] Failed deploys that allocated a host: open a row and close on destroy/cleanup?
- [ ] Column names: new `cluster` vs reuse `sandbox_name` / `cloud_region`

---

## Todo — reporting-api (`rhpds/demo-reporting`)

- [ ] Add `POST /api/provision/v1/troshka` (do **not** use `ProvisionCreateSchema` / Anarchy `FIELD_MAPPING`)
- [ ] Accept payload above; `event=deleted` on the same path (avoid Anarchy `last_state` enums)
- [ ] Migration: nullable `namespace`, `cluster` on `provisions` (or agreed reuse of existing columns)
- [ ] Write `service_type=troshka`, `uuid`, `user_id` from email, `provisioned_at` / `retired_at`
- [ ] Make Anarchy-required ORM fields nullable for `service_type=troshka` (no dummy governors)
- [ ] Lookup `users` by email; stub without `employee_number` if missing; **never UPDATE** `employee_number`
- [ ] Do **not** add Troshka `worker_id`; do **not** fake `provision_request` / ResourceClaim
- [ ] Filter Babylon catalog/governor charts to `service_type=babylon`
- [ ] Tests for create, idempotent create, delete/retire, unknown uuid, missing email
- [ ] Document collector contract: scrape `cluster` + `namespace` in `[provisioned_at, retired_at]`

## Todo — Troshka

- [ ] Config: reporting base URL + API token (`TROSHKA_REPORTING__*` / Helm); **off** in local/dev
- [ ] Enable only for **KubeVirt shared prod** (SSO)
- [ ] Fire-and-forget client (RQ or daemon thread); deploy/destroy must not block; retry 5xx; destroy still succeeds if reporting is down
- [ ] Client module (function-based) + deploy hook (host assigned) + destroy hook
- [ ] Owner email from project owner (SSO upsert); skip if not SSO / no email
- [ ] `cluster` = `project.host_id` → `Host.instance_id`
- [ ] `namespace` = `_project_ns(provider, project_id)` — not `POD_NAMESPACE`
- [ ] Unit tests with mocked HTTP

## Todo — rollout

- [ ] Reporting-api: endpoint + migration + tests
- [ ] Troshka: client + hooks behind flag + tests
- [ ] Stage: non-prod Troshka → reporting stage; one create + destroy; confirm row + Workday join
- [ ] Prod: enable flag on shared Troshka (KubeVirt) only
