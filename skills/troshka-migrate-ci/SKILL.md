---
name: troshka-migrate-ci
description: Assess and migrate AgnosticV catalog items from AgnosticD or AgnosticD-v2 to Troshka, including workload compatibility, isolated templates and patterns, shared-cluster tenants, and Showroom conversion.
---

# Migrate a catalog item to Troshka

Start from an AgnosticV CI path, stage, and revision. Trace the actual framework
and workloads, assess feasibility, and generate a reviewable candidate. Use
existing Troshka and AgnosticD interfaces; report missing capabilities rather
than inventing template keys, APIs, or automatic compatibility.

Ask only for inputs that cannot be inferred from the request or available source:
AgnosticV root/ref, CI path/stage, Troshka root/version, framework and collection
roots/refs, and an output directory. Record supplied versus inferred values.
Use repository-relative paths in deliverables, not developer-specific paths.
Discover relevant AgnosticV repositories, not just the primary checkout. Resolve
each catalog ID in the correct repository and record its path and revision.

## Select the requested mode

| Mode | Output and boundary |
| --- | --- |
| `assess` | Source-backed feasibility report; no provisioning or external writes |
| `generate` | Assessment plus template or tenant configuration, new CI candidate, Showroom, and adaptation diffs |
| `build` | Explicit development build using supported deployment tooling |
| `capture` | Explicit pattern capture of owned infrastructure after validation |
| `validate-pattern` | Explicit fresh-project restore and acceptance checks |
| `prepare-promotion` | Reviewable test/prod/optional event configuration using validated artifacts |

A feasibility question defaults to `assess`. A request to generate artifacts
permits local candidate files, not catalog publication, orders, capture, or
resource deletion. Honor action scope already authorized in the session; do not
ask again merely because a stage changed. Check current runtime capabilities
before any live action. Stop dependent work on missing authorization or missing
capability, and continue useful assessment/generation work.

## Classify ownership first

- **Dedicated:** CI owns its VMs/cluster. Dev builds a template; a validated
  capture becomes the pattern used by later stages.
- **Shared tenant:** CI consumes an existing or pool-supplied cluster. Preserve
  the allocation and tenant workload flow. Do not build a cluster for each
  tenant or capture/stop/destroy the shared cluster. Its release artifacts are
  pinned workload/content revisions and allocation configuration, not a cluster
  disk pattern.
- **Hybrid:** Record ownership per component. Only independently owned
  infrastructure follows template-to-pattern promotion.

Tenant support in AgnosticD is not proof of attachment support in Troshka. A
workload runner targeting clusters in a project's topology is not automatically
an external-cluster tenant API. Report a platform gap and retain the native tenant
flow when no supported Troshka binding is available.

## Workflow and references

1. Read [discovery](references/discovery.md). Resolve the canonical AgnosticV
   merge, effective framework generation, requirements, role source, Showroom,
   lifecycle, and allocation ownership. Begin the
   [migration report](assets/migration-report.md).
2. For `generate`, read [conversion](references/conversion.md). Map actual
   infrastructure and workload contracts; generate a dedicated template or
   tenant configuration as appropriate. Keep unresolved bindings visible.
3. For either ownership model, read [Showroom](references/showroom.md). In
   `assess`, inspect Showroom requirements and record the proposed changes. In
   `generate`, convert existing content or generate a minimal entry point.
   Later modes use those artifacts within the authorized action scope.
   Showroom is required for a completed migration.
4. For live stages, resumption, or promotion, read
   [lifecycle](references/lifecycle.md). Use the
   [state example](assets/migration-state.example.json) as a structure for an
   external state file, never as proof of live state or authorization.

## Migration invariants

- Assess current Troshka capabilities. Identify each required feature's
  implemented schema/API/runtime path and bake supported settings into the
  template. An absent required capability blocks migration today; a proposed
  feature is not a migration path. Missing evidence remains explicitly unresolved.
- Trace component conditions, parameter forwarding, and dynamic lab definitions.
  Wrapper YAML may contain no VMs because a child loads them from Showroom source.

- Inspect workload source, including pre-software, post-software, removal roles,
  and dynamically included tasks. Missing source is `unresolved`.
- Distinguish AgnosticD and AgnosticD-v2. Legacy execution requires evidence or
  explicit adaptations, not an unexplained switch to the v2 runner.
- Required GPUs or other specialized instance-specific hardware capabilities
  are unsupported migration targets right now. Mark migration blocked for Troshka
  when workloads require them; do not imply passthrough exists or substitute
  ordinary VMs. Instance types used only for CPU/RAM sizing can still be mapped.
- When workloads require nested virtualization, set `nested_virt: true` on every
  affected VM in the generated template. Verify host/backend support separately.
  For shared tenants, assess the existing cluster rather than building one.
- Remove GUID-format dependencies and external certificate issuance used only
  for hosting. Stable internal names are appropriate inside isolated boundaries.
  Shared tenants still require unique namespace/resource ownership and ingress
  routing. Never replace every GUID occurrence with a constant.
- Preserve TLS verification and distribute local trust. Report unsupported trust
  paths. Do not claim isolation makes verification unnecessary.
- Keep curriculum dependencies explicit: cloud IAM, global cluster settings, or
  public certificate lessons may need retained resources or author decisions.
- Assign each workload phase and each Showroom instance one executor. Do not
  duplicate AAP execution with Troshka template auto-workloads.
- Keep operational secrets, decrypted merges, credentials, access tokens,
  provision data, and state/logs outside Git. Generated content must not contain
  development-project links or credentials.
- Preserve the original CI. Use a new CI identity and the target repository's
  stage/include conventions; do not publish any stage implicitly.

## Evidence and completion

Use dependency statuses `reusable`, `infra_mapping`, `workload_adaptation`,
`external_required`, and `unresolved`, with source path/revision/line evidence.
Report overall feasibility as `candidate`, `partial`, or `blocked`. A candidate
is not a verified migration. A blocker in a required dependency prevents full
feasibility, regardless of how many other roles are portable.

Record checks as `passed`, `failed`, `unverified`, or `not_applicable`, and retain
the command or observation supporting each claim. Missing access is unknown,
not evidence that a resource is absent. Source-only review, static validation,
offline exercises, and live deployment are different evidence categories.

Before delivery, inspect generated diffs, run available schema/canonical merge
checks without printing secrets, and report what remains blocked. Dedicated
migration acceptance requires a fresh pattern deployment. Tenant acceptance
requires two independent allocations and cleanup isolation, not cluster capture.
Exercise capability probes through the current topology importer as well as its
schema to verify counts, NICs, disks, and nested-virt flags survive. A partial
capability probe is not a deployable migration template.

For skill maintenance, use the raw-source requests and rubric in
[behavioral exercises](evals/cases.md). Evaluate observable decisions and generated
artifacts, not whether outputs repeat these instructions.
