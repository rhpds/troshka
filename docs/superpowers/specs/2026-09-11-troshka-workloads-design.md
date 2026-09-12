# Troshka Workloads Subsystem — Design

**Status:** Draft for review
**Date:** 2026-09-11
**Author:** Patrick Rutledge (with Claude)

## 1. Summary

Add a **workloads** subsystem to Troshka that runs AgnosticD-v2 Ansible workloads
against the VMs and OpenShift clusters of a Troshka project, while remaining
**backwards-compatible** with the existing AgnosticD-v2 / AgnosticV ecosystem.

A workload run takes an **active** Troshka project (however it was created —
template-deployed or pattern-deployed) and provisions software onto it by running
workload roles inside that project's **runner pod**. It is a **pattern-authoring /
software-overlay pipeline**, not a day-2 lifecycle manager:

- **Bake a pattern:** deploy a base project → run workload(s) → snapshot → pattern.
- **Overlay on a base:** deploy a vanilla OCP cluster from a pattern (fast) → layer
  workloads on top.

Runs are **provision-only** and normally happen once.

## 2. Goals / Non-goals

### Goals
- Run AgnosticD-v2 workloads (cluster-scoped `ocp4_workload_*`, tenant
  `ocp4_workload_tenant_*`, and VM-scoped `bastion_*`/`host_*`) against a Troshka
  project.
- Backwards-compatible with the AgnosticV config contract: overlay + `#include`
  merge, inline Ansible Vault secrets, EE selection, `workloads:` lists,
  `requirements_content`, and per-cluster targeting.
- Two units of work: a whole **catalog item** (`agd-v2.<item>.<stage>`) and an
  **ad-hoc single workload role**.
- Two trigger surfaces: interactive **UI** and programmatic **REST API** (for the
  existing AAP/agnosticd pipeline).
- **Never leak** AgnosticV/AgnosticD git credentials or the Vault key — they are
  configured centrally by an admin and stay server-side.
- Bounded disk usage despite very large source repos.

### Non-goals
- Day-2 lifecycle of workloads (no per-workload destroy/remove, no stop/start).
  Cleanup happens when the project is destroyed.
- Re-implementing AgnosticD role logic. Troshka drives the existing AgnosticD-v2
  runner + workload collections unchanged.
- Authoring an AI-friendly AgnosticV overlay doc (was only needed to inform this
  design; not a deliverable).

## 3. Key decisions (locked)

| # | Decision |
|---|----------|
| D1 | **Approach A (thin pod):** the backend clones + overlay-merges + vault-decrypts AgnosticV and produces resolved artifacts; the runner pod receives only resolved inputs. Git creds + Vault key **never** enter the pod. |
| D2 | Troshka **owns the merge process** by driving the **canonical `agnosticv` binary** server-side (`--list` / `--merge --output=json`) against the cached agnosticv checkout — it does **not** reimplement AgnosticV merge semantics. Only `!vault` decrypt is Python (so the vault key stays backend-side). The path→catalog-id transform is ported from ci-inspector's `_resolve_binder_crd_name`. Requires the `agnosticv` binary in the backend/worker image. |
| D3 | The EE **always runs in the in-project runner pod**. **Any active project** gets a runner pod (generalize today's OCP-only ops-pod), not just OCP projects. |
| D4 | Unit of work: **both** catalog items (`agd-v2.<item>.<stage>`) and ad-hoc single roles. |
| D5 | Trigger: **both** UI and REST API. |
| D6 | Inventory: **reuse the `troshka.cloud` inventory plugin**; VMs carry an `AnsibleGroup` tag; the backend emits the `*.troshka.yml` inventory and mints a scoped `trk_` key. |
| D7 | Cluster access delivered as **variables**, not a kubeconfig: a `clusters:` dict keyed by name, each `{api_url, api_token}` (cluster-admin SA token). Mirrored to `sandbox_openshift_api_url` / `cluster_admin_agnosticd_sa_token` for tenant workloads. Legacy `KUBECONFIG` file provided only if needed. |
| D8 | Lifecycle: **provision-only**, run once, to bake a pattern or overlay onto a base. |
| D9 | Cloud CLI creds (aws/gcloud/azure) are **admin-central named sets**, injected as scoped 0600 mounts only when needed. **EE images are public** (no registry creds). |
| D10 | **Repo Cache** with a per-repo default-ref config map and a disk budget (bare treeless mirror + per-ref worktrees + LRU eviction). |
| D11 | New **`WorkloadRun`** model; **retain everything** (project_id nullable on project delete) + a **global pruning** job. |

## 4. Architecture

### 4.1 End-to-end flow

```
User / API
  └─ select target: catalog item (agd-v2.<item>.<stage>)  OR  ad-hoc role FQCN
     + choose target project + cluster/VM-group mapping
        │
   ┌────▼─────────────────────────── backend (inside trust boundary) ──────────────┐
   │ Repo Cache        : ensure agnosticv@master, agnosticd-v2@<ref>, workload@<ref> │
   │ AgnosticV Merge   : overlay walk + #include + vault-decrypt → resolved vars      │
   │                     + ee_image + scm_ref + requirements_content                  │
   │ Inventory Emitter : *.troshka.yml + scoped trk_ key                              │
   │ Cluster Access    : clusters{} = {name:{api_url, api_token}}                      │
   │ Secret Store      : (only) the cloud-cred set(s) the EE declares it needs        │
   └────┬────────────────────────────────────────────────────────────────────────────┘
        │  enqueue_job(run_workload, run_id)  (RQ worker)
        ▼
   Runner Pod (privileged, in project network) — receives resolved artifacts as
   0600 read-only mounts; runs `ansible-navigator` with the item's public EE image,
   driving AgnosticD-v2 `openshift-workloads` config (workloads + requirements).
        │
        ▼
   Progress (Redis + WebSocket + DB) → WorkloadRun record → project ready to snapshot
```

### 4.2 Components (each an isolated, testable unit)

**Repo Cache** *(backend, disk-bounded)*
- `agnosticv`: single working copy tracking **`master`**; `git fetch` + reset
  before a run (or on a short TTL) to pick up updates.
- `agnosticd-v2` and workload repos (`core_workloads`, `demo_workloads`,
  `namespaced_workloads`, plus any repo referenced by `requirements_content`):
  one **bare treeless mirror** per repo (`git clone --filter=blob:none --bare`),
  with a **git worktree per `scm_ref`** so all refs share one object store.
- **Default ref** is a config map `{repo → default_ref}` used when an item does not
  pin a ref:
  - `agnosticd-v2` → `main`
  - classic `agnosticd` (legacy) → `development`
  - workload repos → `main`
  (All overridable via config.)
- **Disk safety:** configurable total disk budget; **LRU eviction** of worktrees;
  periodic prune of unreferenced refs; treeless/shallow fetches throughout.
- Workload repos also populate a local collections cache so the EE's
  `ansible-galaxy install` resolves from cache instead of re-fetching each run.

**AgnosticV Merge Engine** *(backend; wraps the canonical `agnosticv` binary)*
- Input: a catalog-item id (`agd-v2.<item>.<stage>`) or an ad-hoc spec.
- **Does not reimplement the merge.** It shells out to the `agnosticv` binary
  against the cached agnosticv checkout:
  - `agnosticv --list --dir <root> --git=false --output=json` → the authoritative
    list of stage-file paths (the tool handles the non-uniform tree).
  - `agnosticv --git=false --merge <path> --output=json` → the fully merged vars
    (overlay walk + `#include` + `.agnosticv.yaml` related_files).
- **Catalog-id ↔ path** mapping ports ci-inspector's `_resolve_binder_crd_name`
  (path → dotted id: `replace('_','-').lower()`, dot-join, last segment = stage);
  a reverse index maps the user-facing dotted id to a `--list` path.
- **Vault decrypt is the only Python-side merge logic**: after merge, string values
  beginning with `$ANSIBLE_VAULT` are decrypted with the admin Vault key via a
  self-contained `cryptography`-based VaultAES256 implementation (no `ansible-core`
  dependency).
- Output: `{ extra_vars, ee_image, scm_ref, requirements_content }` (the latter
  three read from `__meta__.deployer.*` / `requirements_content`).
- **Dependency:** the `agnosticv` Go binary must ship in the backend/worker image.
- Tests: pure unit tests for the path→id transform and vault decrypt (hermetic +
  one real `ansible-vault` vector); subprocess wrappers unit-tested with mocked
  `subprocess.run`; an optional real-binary integration test guarded by
  `shutil.which("agnosticv")`.

**Central Secret Store** *(admin-only)*
- Fernet-encrypted (reuse `app/core/encryption.py`) rows for: AgnosticV git creds,
  AgnosticD-v2 / classic-agnosticd git creds, the Vault key, and named cloud-cred
  sets (aws/gcloud/azure).
- Admin-scoped API + UI. Never project-scoped; never transmitted to a pod except
  the specific cloud-cred set an EE declares it needs.

**Inventory Emitter + VM tagging** *(backend + canvas)*
- Adds an `AnsibleGroup` tag capability to VM nodes (canvas UI writes
  `vmNode.data.tags.AnsibleGroup`, comma-separated).
- Emits the `*.troshka.yml` inventory that activates the `troshka.cloud` inventory
  plugin, pointing it at the project (`api_url`, scoped `api_key`, `project_id`,
  `connection_mode`).
- Contract (from the plugin): exactly one VM tagged `bastions` with an external IP
  in SSH mode; every VM needs a name and a first-NIC IP; groups are the tag values
  verbatim.

**Cluster Access Resolver** *(backend)*
- For each target cluster in the project, produces `{api_url, api_token}` where
  `api_token` is a cluster-admin **SA token** (minted/read via the stored admin
  kubeconfig harvested at install / recert).
- Injects a `clusters:` dict keyed by cluster name into the resolved extra-vars;
  the AgnosticD `openshift_workload_deployer` translates `workloads[].clusters`
  into per-role `K8S_AUTH_HOST`/`K8S_AUTH_API_KEY`/`K8S_AUTH_VERIFY_SSL=false`.
- Also surfaces `sandbox_openshift_api_url` + `cluster_admin_agnosticd_sa_token`
  for tenant workloads, and (only when required) a legacy kubeconfig file +
  `KUBECONFIG`.
- Exposed via a new ops-pod-scoped endpoint `GET /projects/{id}/cluster-access`.
- **Optional** convenience: a `troshka.cloud` lookup/module that auto-populates the
  `clusters:` dict from the project (keeps AgnosticV configs from hard-coding
  endpoints). Not required for the core path.

**Workload Runner** *(runner pod)*
- Generalizes the existing ops-pod scaffolding so any active project can get a
  privileged runner pod on the project network.
- Receives resolved artifacts as 0600 read-only mounts (extra-vars, inventory,
  scoped key, cluster access, needed cloud-cred set). Never receives git creds or
  the Vault key.
- Runs `ansible-navigator` with the item's public EE image (nested podman in the
  privileged pod), driving AgnosticD-v2's `openshift-workloads` config
  (`config: openshift-workloads`, `workloads:` list, `requirements_content`).
- Ad-hoc single-role runs synthesize a minimal `workloads: [<role>]` +
  `requirements_content` for that role's collection, then run the same path.

**Job + Progress** *(reuse existing patterns)*
- `enqueue_job(run_workload, run_id)` → RQ worker → monitor thread →
  `_update_deploy_progress` (Redis + WebSocket + `WorkloadRun` JSONB) →
  restart-recovery via a `resume_workload_monitors()` mirror of
  `resume_ops_pod_monitors()`.

**API + UI**
- API: list catalog items; launch a run (catalog or ad-hoc) with target/cluster
  mapping; get run status/logs; list run history.
- UI: catalog/ad-hoc selection, target + cluster mapping, VM `AnsibleGroup`
  tagging on the canvas, live progress/logs, run-history list.

## 5. Trust boundary (credential isolation)

| Stays backend-side (never enters the pod) | Enters the pod (0600 mounts, never argv) |
|---|---|
| AgnosticV + AgnosticD git creds | Resolved, vault-decrypted extra-vars for **this item only** |
| Vault key | `*.troshka.yml` inventory + scoped `trk_` key |
| Full secret store | Per-cluster `{api_url, api_token}` for target clusters |
| Repo Cache + Merge Engine | Only the cloud-cred set(s) the selected EE declares it needs |

The runner pod is a pure executor that only ever sees its own project's resolved
inputs. The scoped `trk_` key follows the existing least-privilege ops-pod pattern
(`topology:read`, `vm:exec`, plus a new `cluster:access` scope for the new endpoint).

## 6. Data model changes

- **`WorkloadRun`** (new table):
  - `id`, `project_id` (nullable FK — set null on project delete), `owner_id`
  - `kind` (`catalog_item` | `ad_hoc`)
  - `catalog_item` (e.g. `agd-v2.vllm-playground-aws.prod`) or `role_fqcn`
  - `scm_ref` / `tag`, `ee_image`
  - `target_map` (JSONB: cluster/VM-group selection)
  - `status`, `started_at`, `ended_at`, `log_ref`
  - `resulting_pattern_id` (nullable)
- **VM node topology:** `AnsibleGroup` under `vmNode.data.tags` (no new table).
- **Secret store:** encrypted config rows (admin-scoped) for central creds + named
  cloud-cred sets.
- **Pruning:** a global retention job removes `WorkloadRun` rows past a configurable
  TTL (and/or orphaned rows with null `project_id`), keeping the table bounded.

## 7. Backwards-compatibility mapping

Troshka reproduces the AgnosticD contract exactly:
- `config: openshift-workloads`
- `workloads:` — list of FQCN roles (or `{name, clusters, data_prefix?}` mappings)
- `requirements_content.collections` — installed dynamically in the EE (from the
  local cache)
- EE image from `__meta__.deployer.execution_environment.image` (public registry)
- `clusters:` dict keyed by name → `{api_url, api_token}`; the deployer maps
  `workloads[].clusters` → `K8S_AUTH_*` env per role
- Tenant workloads: `sandbox_openshift_api_url`, `cluster_admin_agnosticd_sa_token`

## 8. Error handling & edge cases

- **VM-only projects:** runner pod must provision for any active project, not just
  OCP (D3).
- **EE cloud-cred needs:** a Troshka-side mapping (EE image → required cloud-cred
  set) selects which admin cloud-cred set to inject; default is none.
- **Ad-hoc requirements synthesis:** infer the collection for a bare role and build
  a minimal `requirements_content`.
- **Missing/incorrect VM tags:** validate that a VM-targeted run has the required
  `AnsibleGroup`/bastion contract before launching; fail fast with a clear message.
- **Repo cache pressure:** LRU eviction + disk budget; a run blocks briefly if a
  ref must be materialized.
- **Cancellation, dead-pod detection, restart recovery:** reuse the deploy monitor
  patterns.

## 9. Testing strategy

- **AgnosticV Merge Engine:** pure unit tests over fixture overlays covering
  maps-merge, lists-replace, `#include` resolution, `!vault` decrypt, stage
  overrides, and catalog-id→path mapping. Highest-value tests.
- **Cluster Access Resolver + Inventory Emitter:** unit tests with mocked project
  state; assert the exact vars/inventory shape AgnosticD expects.
- **Repo Cache:** unit tests for default-ref resolution, worktree reuse, and LRU
  eviction under a disk budget (mocked git).
- **Runner / pod interactions:** mocked — no real troshkad/SSH/network I/O in unit
  tests (per project convention); pytest timeouts as guardrails.

## 10. Open refinements (non-blocking)

- Exact `cluster:access` endpoint payload and SA-token minting details.
- Whether to ship the optional `troshka.cloud` cluster-auto-discovery helper in v1
  or defer.
- EE → cloud-cred-set mapping storage/UX.
- Confirm remote branch names for the default-ref map at implementation time.
