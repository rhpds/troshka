# AgnosticV catalog migration skill

Date: 2026-10-21
Status: Initial skill package implemented and checked offline on 2026-10-21; tenant requirements incorporated; live migration validation remains per CI

## Outcome

Provide a resumable AI skill that a catalog developer can point at an existing
AgnosticV catalog item (CI). The skill assesses migration feasibility, generates
a Troshka template and converted or new Showroom, and prepares a new AgnosticV
CI. Development builds use the template; test, production, and optional event
stages deploy a validated pattern captured from the resulting project.

The migration replaces infrastructure provisioning while preserving applicable
workload code and instructional content. The resulting environment should be
self-contained within Troshka wherever the lesson permits. Assessment covers
both AgnosticD and AgnosticD-v2 and source providers including CNV, AWS, Azure,
GCP, and IBM Cloud. This is not a promise of automatic conversion for every CI.
Legacy execution compatibility must be established from source and validation;
the skill must not silently treat legacy workloads as AgnosticD-v2 workloads.

## Tenant-only catalog items

Classify ownership before proposing infrastructure: dedicated environment,
tenant on an existing shared cluster, or hybrid. A tenant-only CI must reuse a
provisioned/pool-supplied cluster rather than deploy a cluster for every order.
Trace the actual cluster allocation, connection outputs, workload execution,
namespace/RBAC/quota isolation, and release contract. Preserve that allocation
mechanism where possible. The existing AgnosticD-v2 openshift-workloads and
namespace configurations are evidence to inspect, not proof of Troshka support.

Shared tenants need unique namespace/resource ownership even when GUID-format
dependencies are removed. Use the allocator's tenant identity; stable internal
names are safe only within tenant boundaries. Audit cluster-scoped resources,
operators, CRDs, privileges, ingress hosts, and cleanup before asserting shared
cluster compatibility. Some workloads require dedicated clusters and should be
reported as such, rather than silently creating one.

Tenant migration produces a workload/tenant configuration and converted or new
Showroom. Do not fabricate an infrastructure template or capture the shared
cluster as a tenant pattern. Dev/test/prod/event pin workload/content revisions
and allocation settings. Template-to-pattern promotion applies only to owned
infrastructure, such as an optional separate access environment in a hybrid CI.

If a Troshka project cannot attach an existing shared-cluster tenant or host its
access environment using supported interfaces, report a platform capability gap.
Retain the native tenant flow as a viable outcome; do not invent an attachment
API or claim that a dedicated-cluster workaround satisfies tenant migration.
Tenant destroy removes owned resources and releases its allocation only; it
must not destroy, stop, capture, or modify another tenant or the shared cluster.
Validate two concurrent tenants and destruction isolation without a cluster
capture/restore cycle. Live tenant creation and cleanup remain explicit actions.

## Existing foundations

Required GPUs and other specialized instance-specific hardware capabilities
currently block Troshka migration. Ordinary instance sizing may map to generic
VM resources. Workloads needing nested virtualization require `nested_virt: true`
on every affected template VM, including workers that run nested guests, plus
separate host/backend/runtime verification. Shared tenants must obtain the
capability from their existing cluster rather than a per-tenant cluster build.

- `docs/dev/deploy-pipeline.md` documents the AgnosticD-v2 Troshka provider,
  template inclusion, and `template`, `pattern`, and `pattern_workloads` modes.
- `src/backend/app/services/workloads/agnosticv.py` uses the canonical AgnosticV
  merge and resolves workload variables, requirements, EE image, and SCM ref.
- `src/backend/app/services/workloads/inventory.py` provides inventory generation
  and validation of VM names, addresses, and Ansible groups.
- `src/backend/app/services/workloads/pod_launch.py` provides a workload runner
  with inventory and cluster access. Its current implementation targets v2.

These are implementation starting points, not evidence of end-to-end migration
compatibility. Verify current schemas, APIs, and integration repositories when
implementing the skill; do not invent unsupported template fields or endpoints.

## Skill workflow

### 1. Discover and resolve

Accept the AgnosticV repository, CI path, stage, and source revision. Resolve
includes and overlays through the canonical AgnosticV tooling. Identify the
effective AgnosticD generation, configuration, SCM revision, execution
environment, collection requirements, workload roles, Showroom sources, and
provision/start/stop/destroy actions. Follow relevant workload source code,
including pre-software and post-provision tasks.

Record source paths and resolved revisions. Flag inaccessible sources and
dynamic dependencies as unknown rather than inferring compatibility. Never
persist decrypted secrets in reports or generated repository artifacts.

### 2. Assess feasibility

Produce a dependency matrix with source evidence, proposed replacement, and
validation criteria. Classify dependencies as reusable unchanged, infrastructure
mapping required, workload adaptation required, external resource required, or
unresolved. Distinguish infrastructure hosting the lesson from infrastructure
that is itself the subject of the lesson.

Assess inventory names/groups/variables, OS and privileges, SSH and execution
transport, disks, networking, DNS, TLS trust, cluster access, bastion services,
registries, credentials, user outputs, and lifecycle behavior. Report unsupported
capabilities explicitly; an assessment may conclude that migration is partial
or blocked.

### 3. Generate a reviewable candidate

Generate a Troshka infrastructure template, required workload adaptations,
Showroom content/configuration changes, and a new AgnosticV CI. Preserve shared
workload references through includes where practical rather than maintaining
duplicated copies. Preserve the original CI during validation.

Match the catalog repository's schema and naming conventions. Register template
files as related files where AgnosticV requires it. Identify a single executor
for every workload phase so AAP and Troshka do not both execute the same chain.

### 4. Build and validate development

Use the new dev stage in template mode to build the project and execute its
build-time workloads. Check infrastructure and application readiness, expected
user outputs, Showroom access, and applicable lifecycle operations. Preserve
failed-stage evidence and resume only after checking current project state.

### 5. Capture and test a pattern

Capture the validated project using the supported pattern mechanism. Record an
immutable pattern identity/version using available platform semantics; if those
cannot guarantee stable promotion, report that limitation before promotion.

Deploy into a separate isolated project with fresh deployment credentials and
identity. Validate independence from the source project and its GUID, URLs,
certificate issuer, DNS zone, and credentials. Check concurrent isolation and
that destroying the test project does not affect the source project.

### 6. Prepare promotion

Have test consume the captured pattern. Promote the same validated artifact to
prod and optional event configuration, allowing explicit stage-specific settings.
Do not rebuild separately for each stage. Use pattern_workloads only where
post-restore setup is required; otherwise use pattern mode.

## Isolation and identity contract

Remove GUID-based naming used solely for uniqueness inside the environment.
Use stable internal hostnames, service names, and DNS names scoped to the isolated
project. Remove dependencies on source cloud resource IDs and provisioning facts
where equivalent local values can satisfy the workload contract.

Do not perform blind GUID substitution. Trace every use: an internal name,
credential seed, external registration, or application identifier may need a
different replacement. Catalog order identifiers may remain outside the guest
environment for orchestration and ownership; internal workloads must not depend
on their former naming convention.

Replace external certificate issuance dependencies with an internal CA/trust
arrangement or supported Troshka ingress TLS. Distribute trust to all applicable
clients and preserve TLS verification. Browser-facing entry points must use
the supported access mechanism for the selected Troshka backend. Isolation is
not a substitute for certificate validation.

External cloud APIs, public DNS, identity services, or certificate workflows that
are intrinsic to the lesson require an explicit curriculum decision. Report
retained dependencies; do not represent such migrations as fully self-contained.

## Build-time versus deployment-time state

Classify each workload and its outputs as baked into the pattern or initialized
after restore. Software installation and reusable lesson content may be captured.
Deployment credentials, external routing, user access information, and external
registrations require fresh handling. Assess certificate and private-key reuse
explicitly; regenerate identity material where isolation or trust requires it.

Document ordering and retry semantics for post-restore work. A retry must not
duplicate external registrations or repeat destructive initialization. Validate
any retained external resource cleanup separately from project destruction.

## Showroom is a required output

When a Showroom exists, inspect its deployment configuration, content sources,
variables, links, embedded consoles, and instructional assumptions. Preserve
lesson ordering and substantive content. Adapt cloud hostnames, GUID-derived
URLs, terminal access, application links, cluster access, and certificate
assumptions to the migrated environment. Flag semantic lesson changes for the
author rather than silently rewriting the curriculum.

When no Showroom exists, generate a minimal environment overview, available
terminal/console/application links, credentials guidance, and readiness checks
from verified template and workload outputs. Do not invent exercises.

Reusable Showroom content may be captured in the pattern. Populate access values
from the actual deployed project after restore using supported Troshka mechanisms.
Do not bake development-project links or credentials into content. Deliver
credentials through the appropriate authenticated runtime mechanism, not Git.

## Artifacts and resumability

Keep a reviewable migration report with source revisions, dependency decisions,
generated artifact paths, workload phase ownership, validation results, and
remaining blockers. Store operational project/pattern/run references and logs
outside Git in a resumable state record; exclude secrets and decrypted variables.
Reconcile live state before resuming actions. Source changes invalidate affected
assessment and validation results.

Assessment and artifact generation form the initial skill implementation scope.
Build, capture, and promotion are explicit resumable actions whose authorization
is tracked separately; respect authorization already provided in the session.
Never interpret a request for feasibility as authorization to provision resources
or publish a production catalog change.

## Acceptance criteria

- Every compatibility finding has source evidence or is marked unresolved.
- Generated artifacts pass the target repositories' schema/static checks.
- The dev CI builds the expected project and executes each workload phase once.
- Internal workloads work without former GUID and external issuance dependencies,
  except for explicitly documented curriculum requirements.
- Existing Showroom content is converted, or a minimal Showroom is generated.
- A fresh pattern deployment exposes working access links for its own project;
  no links or credentials point back to the development project.
- Relevant TLS clients validate their endpoints without blanket verification bypass.
- Provision, restore, retry, start/stop where supported, and destroy have recorded
  outcomes; a successful build alone does not establish migration completion.
- Test/prod/event reference the intended validated pattern artifact.

Validate the skill against representative OpenShift, Linux VM, legacy AgnosticD,
and cloud-dependent examples, including existing-Showroom and no-Showroom cases.
Expected outcomes include correct partial or blocked assessments, not just
successful conversions. No production deployment is required to test the skill.
