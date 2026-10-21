# Generate the migration candidate

## Verify target contracts before rendering

For every mandatory capability record current implementation evidence, exact
template configuration, and the runtime acceptance check. Read the schema,
importer, and selected backend; exercise a sanitized template through the current
importer where possible. Do not count a future API, new project type, or proposed
expansion service as supported. If no current implementation or composition
supplies the requirement, migration is blocked. Missing evidence is unresolved,
not a promise of future support.

Use the selected Troshka checkout's `schemas/troshka-template.schema.json`,
example templates, import/export implementation, and provider collection. Inspect
the selected AgnosticD config too. Documentation is a starting point; source and
the target schema decide accepted fields.

The inspected integration has two representations:

- Standalone templates use schema keys such as `name`, `networks`, `vms`,
  array-valued `ocp`, `showroom`, and `workloads`.
- The catalog adapter at the Troshka collection's `roles/deploy/tasks/main.yml`
  constructs a template using catalog `template_name` and forwards selected
  fields. It does not necessarily forward every standalone field. Inspect the
  actual adapter and receiving API together; do not copy a schema-valid template
  into an include and assume all its behavior survives.

Check image/library availability and backend-specific networking, nested
virtualization, storage, and access features. A schema-valid image name is not
proof that the target library contains it. Report missing bindings. Do not
generate fake resource IDs, unsupported schema properties, or deploy-ready
claims for unfinished configuration.

## Map dedicated infrastructure

Resolve catalog defaults and sizing expressions before rendering numeric fields.
For example, materialize `max(ceil(num_users / 5), 3)` for the selected release's
user count, and emit actual worker nodes/CPU/RAM in the template. Initial CNV/AWS
scale-out roles should be replaced by that supported initial topology, not left
running against the old provider or assigned to an unimplemented expansion API.
Record the parameter selection; one fixed captured pattern cannot claim to
preserve every size of a parameterized catalog. Use explicitly validated size
variants when necessary, and report unsupported parameter behavior.

Follow source setup scripts and exercise setup/solve/validate automation. Preserve
intentional initial absence (for example, a tlog lesson that removes packages for
the student to install). Do not pre-solve the exercise by inferring packages from
its title. Separate reusable setup from runtime student actions and preserve
package repository/registry access and entitlement requirements.

Reuse an existing compatible Troshka library base image first, including the
existing RHEL base. Apply the CI's packages, configuration, and workloads during
the template build, validate the lab's starting state, then capture the configured
environment as a pattern. A different CNV image alias does not require a new
image. For a customized image such as devtools, trace its software requirements
and reproduce them through setup on the existing base where supported.

Verify the required OS version, architecture, firmware, and disk compatibility
and bind the actual library reference; never emit a fake ID. Import or build a
separate base only when a concrete requirement cannot be met using the existing
base plus setup, and document that requirement. Distinguish the outer VM's base
from images used by nested guests or instructional bootc builds; those may still
be required by the workload or curriculum.

Map the original declared instances and observed workload requirements to VM
names, CPU/RAM, supported OS/images, disks/controllers, NICs/IPs, networks, DNS,
gateway behavior, and boot/start dependencies. Preserve inventory groups and
variables, not just VM counts. Inventory via the inspected collection reads
comma-separated `AnsibleGroup` tags. Verify the template schema and importer
actually preserve those tags and the chosen connection transport.

Match cluster versions, architecture, topology, storage classes, and APIs needed
by workloads. Supply services such as bastion tools, registry, object storage,
git, and DNS only when required. Disconnected registries and egress proxies need
their own readiness/trust checks.

Required GPU/accelerator or other specialized instance-specific hardware is a
current migration blocker. Do not invent passthrough fields, substitute CPU
emulation, or remove the workload silently. Ordinary instance sizing can map to
generic VM CPU/RAM/disk resources when no special capability is required.

For virtualization inside guests, explicitly set the canonical exported flag on
each affected VM:

```yaml
vms:
  hypervisor:
    vcpus: 4
    ram_gb: 16
    nested_virt: true
```

This fragment is not a complete template; derive names and sizing from the CI.
The inspected importer maps `nested_virt` to `nestedVirt`. For OpenShift
Virtualization, set it on each worker that must host nested guests. Verify
host/backend and runtime support separately. Shared tenants require compatible
existing cluster nodes and authorized privileges; never create a cluster as a
workaround for absent tenant capability.
Distinguish virtualization in the outer CNV infrastructure from virtualization
inside the lab's guest VMs. Outer KubeVirt provisioning alone is not evidence
that the guests require `nested_virt: true`. Likewise, remote GPU-backed inference
does not imply the local lab needs GPU passthrough; record the external service
dependency and inspect actual local scheduling/device requirements.

Assign one executor per phase. When AAP runs `software_workloads`, do not also
populate Troshka auto-run `workloads` with the same roles. When Troshka owns a
phase, verify the provider adapter forwards its configuration and that its runner
supports the selected framework generation and execution environment.

Produce a phase table with source ref, role, executor, inputs/outputs, readiness
ordering, replay behavior, and cleanup owner. Separate reusable software/content
from credentials, routing, registrations, and other deployment-specific state.

## Remove hosting GUID and certificate dependencies

Trace each GUID consumer. Internal names can become stable within the dedicated
environment; allocator/order IDs can remain in the orchestration layer. Replace
GUID-derived passwords with fresh runtime secrets delivered through a verified
mechanism. Trace original resource ID consumers before removing them; map an
equivalent local output or adapt the workload.

Use a local CA and distribute trust to relevant OS clients, containers, JVMs,
browsers, and workload EEs as applicable. Alternatively use supported ingress
TLS for browser entry. Check SANs/SNI and renewal/lifetime requirements. Do not
replace external issuance with `validate_certs: false`, `curl -k`, or blanket
proxy verification bypass. If the platform cannot supply the required trust
path, record a capability blocker, not a successful conversion.

Keep real cloud/certificate dependencies that are intrinsic to the lesson
explicit. Explain any curriculum change to the author; do not silently remove
the learning objective.

## Generate tenant configuration instead of a cluster

Preserve the source cluster allocator and supplied connection contract. Generate
a new workload/tenant CI with namespace/tenant identity, RBAC/quota, workload
refs, removal behavior, and Showroom. Do not force `env_type: troshka` if that
configuration provisions owned infrastructure and cannot consume the allocation.

Names stable inside one namespace may repeat across tenants; namespaces,
cluster-scoped objects, routes, and external registrations need uniqueness at
their actual scope. Preserve a verified allocator identity rather than a hardcoded
GUID substitute. Prefer tenant-scoped runtime credentials; explicitly bound any
privileged setup and do not hand cluster-admin access to Showroom/student sessions.

Detect incompatible cluster-scoped changes before calling a workload portable.
An operator/CRD may be a preinstalled pool prerequisite, a separately managed
cluster service, or a dedicated-cluster requirement. Record the choice and owner.
Never silently allocate a cluster to make a tenant assessment pass.

If external-cluster attachment or standalone tenant Showroom hosting is absent
in Troshka, deliver the assessment/native tenant candidate and state that
Troshka integration is blocked. A hybrid can use an independently owned access
environment only when supported and acceptable; it must not capture cluster
credentials or turn into a per-tenant cluster build.

## New catalog layout and release artifacts

Follow repository conventions and assign a new CI identity/asset UUID. Reuse
shared includes for portable configuration; avoid copying provider-specific
includes or secrets wholesale. Preserve owner metadata and the original CI.

The inspected AgnosticV Troshka guidance stores topology outside CI directories:

```text
troshka/templates/MY-LAB-infra_template.yaml
troshka/MY-LAB/.agnosticv.yaml
troshka/MY-LAB/common.yaml
troshka/MY-LAB/dev.yaml
```

`common.yaml` includes `/troshka/templates/MY-LAB-infra_template.yaml` and
`.agnosticv.yaml` registers that absolute repository path in `related_files`.
Confirm the target CLI resolves it correctly. Putting a template beside common
files can create recursive catalog discovery. A tenant-only CI follows the
repository's tenant conventions rather than adding a fictitious infra template.

For dedicated environments, dev explicitly selects `troshka_deploy_mode:
template`. Test/prod/event eventually select `pattern` or `pattern_workloads`
with a verified `troshka_pattern_id`. Do not emit publishable stages with a fake
ID before capture. Provide a stage delta/report and materialize the stages only
when the required pattern exists. A pattern name alone is insufficient evidence
of immutable promotion.

For tenant-only environments, all stages consume existing allocations and pin
workload/content revisions; no cluster capture occurs. Preserve catalog stage
semantics, pool configuration, and per-allocation access values. A retained native
tenant flow is a partial Troshka migration, not completed Troshka attachment.

## Static checks

Parse generated YAML and validate standalone templates against the actual target
JSON schema. Check template references (VM/network/tab/cluster targets) beyond
JSON types. Canonically merge each materialized CI stage with schema validation
enabled, storing merged results outside Git. Run the target repository's existing
YAML/Ansible checks. If Jinja prevents static semantic evaluation, record the
specific remaining runtime check rather than claiming it passed.
