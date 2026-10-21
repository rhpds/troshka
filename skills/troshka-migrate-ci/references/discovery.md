# Discovery and feasibility

## Resolve the actual catalog configuration

Discover available catalog checkouts when the repository is not given. BU,
partner, and platform catalogs may live outside the primary repository. Use a
bounded location search supplied by the developer (for example `~/*agnosticv*`),
inspect repository identity/default branch/dirty state, and resolve the ID in
each relevant canonical index. Do not silently substitute an older similarly
named item from another repository. Case and underscore/hyphen normalization
can differ between IDs and paths. Refresh checkouts only within requested scope,
preserving local changes/branches and using non-destructive fast-forward updates.

Record the AgnosticV repository URL without credentials, checked revision, dirty
state, CI path and stage. If the requested ref differs from the available source,
use a separate source checkout/workspace when authorized; never reset the user's
checkout. Do not run workload code while assessing it.

Use the canonical AgnosticV executable; never recreate include or overlay merging.
Confirm the installed CLI's flags with `agnosticv --help` (some builds exit 2 for
help). The currently inspected CLI supports this form from the AgnosticV root:

```bash
agnosticv --git=false --merge path/to/ci/dev.yaml --output=json
```

Merged output may contain secrets. Capture it in restricted temporary storage
outside Git, inspect only what is necessary, and redact before reporting. Do not
paste it into chat or logs. Use the existing secret/vault mechanism; if decryption
is unavailable, mark affected values unresolved. Do not guess their contents.

For a sanitized fixture outside Git, this CLI supports an explicit root:

```bash
agnosticv --git=false --root /absolute/fixture-root --merge path/to/ci/dev.yaml --output=json
```

Current help does not advertise an `agnosticv lint` subcommand. Use the target
checkout's documented validator if installed, or canonical merge validation
plus applicable Ansible/YAML checks; do not silently disable schema validation.
Inspect `.agnosticv.yaml`, inherited common/account files, `#include`, stage
overrides, and `.schemas`. A merged value alone does not establish which source
defined it; trace provenance through those files.

Recursively resolve `__meta__.components` in the owning repository, evaluating
`when`, parameter routing (`all`, `current`, or named components), explicit
`parameter_values`, and propagated outputs. A wrapper's apparent framework may
not execute its child. Record the revision and stage for every component.
Conditional AWS/CNV branches are alternatives, not two clusters to provision.

Inspect dynamic URL lookups of `config/instances.yaml`, `config/networks.yaml`,
and container definitions in Showroom source. Resolve repository/ref without
executing source-provided lookup/plugin code and parse retrieved data safely.
Lookup failures are unresolved even when an expression defaults to an empty
list. Translate tags, units, firmware, images, disks, userdata, networks,
services and custom terminals; an unevaluated expression does not mean no VMs.

## Follow framework and workload source

Resolve `__meta__.deployer` configuration, including SCM URL/ref, execution
environment, and any external job-template binding. Confirm effective settings
from the owning metadata when the CI does not define them. Record unresolved
external deployer settings. Do not infer generation solely from `env_type`.

Inspect the referenced AgnosticD or AgnosticD-v2 config playbooks and defaults,
its dependency installation, provision/lifecycle/removal paths, and collection
requirements. Follow role names to exact collection repository revisions. Record
both requested refs and resolved SHAs; a branch label is not an immutable ref.
Inspect imported task files, scripts, lookup plugins, module arguments, registered
facts, and generated files used downstream. Treat unevaluated conditional or
dynamic imports as unresolved; do not infer they are harmless.

Include all workload phases actually used by that config. Names such as
`pre_software_workloads`, `software_workloads`, `post_software_final_workloads`,
`infra_workloads`, `student_workloads`, `workloads`, and `remove_workloads` are
discovery clues, not universal schemas. Legacy roles may be under
`ansible/roles_ocp_workloads/`, not `ansible/roles/`.
Legacy config-specific roles or relative file paths may require adaptation even
when their role names resemble v2 roles.

Trace required GPUs/accelerators, specialized devices, architecture, and
hardware-dependent lesson behavior. Required GPUs and specialized instance-specific
hardware currently block Troshka migration; distinguish these from ordinary
instance labels used only for CPU/RAM sizing. Inspect workload code for KVM,
libvirt, KubeVirt/OpenShift Virtualization, or other hypervisors inside guest
VMs. Record exactly which VMs require nested virtualization and the source evidence.

Follow GitOps bootstrap applications into child charts/manifests and referenced
content repositories. Workload-role source alone may omit storage, hardware,
external inference, and access requirements introduced by those applications.
Record the selected stage; when dev uses mutable refs and event pins revisions,
assess each distinctly rather than treating the event snapshot as current dev.

Distinguish shared-platform preparation from an ordinary tenant: workload-only
CIs may consume pooled clusters but change OAuth, operators, ingress, or worker
capacity for all tenants. Record provider and platform-owner boundaries; do not
run that preparation once per tenant. Establish the currently implemented
platform build/capture/allocation path and its template inputs, or report a
present blocker. Consuming tenant orders reuse the established allocation.

Search narrow source roots with `rg` for `guid`, `cloud_provider`, namespace,
credential inputs, API calls, DNS/TLS settings, resource IDs, inventory groups,
Showroom/Antora variables, and lifecycle hooks. Read the surrounding task before
classifying a match. Never dump secret directories or an entire home directory.

## Determine ownership and tenant allocation

Classify dedicated, shared tenant, or hybrid from actual provisioning calls and
parent-component outputs. A CI named “tenant” may still create cluster-scoped
resources; a workload-only config does not imply namespace confinement.

For shared tenants trace:

- Cluster pool/provider and the owner of cluster creation/deletion.
- Allocation handle, tenant identity, namespace(s), RBAC/quota, and ingress names.
- How API endpoint and credentials reach the EE, workloads, and Showroom.
- Whether credentials are tenant-scoped or whether privileged setup requires a
  separately bounded executor. Do not expose installer credentials to students.
- Cluster-scoped mutations: OAuth, operators, CRDs, cluster roles, SCCs, nodes,
  shared storage, and globally named services.
- Cleanup ownership, allocation release, and partial-provision failure behavior.

In v2, inspect `ansible/configs/openshift-workloads/` for supplied `clusters`
connections, and `ansible/configs/namespace/` for namespace-level access. Verify
their actual variable contracts in the selected revision; README examples may
drift from playbooks. Preserve existing allocation instead of replacing it with
per-order cluster provisioning.

## Build the dependency matrix

For every relevant dependency record source evidence, consumer, expected inputs,
proposed replacement, owner, phase, and validation criterion. Cover compute/OS,
disks, networking, inventory/groups, execution transport, cluster APIs, services,
DNS/TLS, credentials, access outputs, content, and every lifecycle action.

| Status | Meaning |
| --- | --- |
| `reusable` | Inspected source's required contract is available without changing that dependency |
| `infra_mapping` | Troshka/allocator must supply an equivalent input or service |
| `workload_adaptation` | Workload or content needs a concrete change |
| `external_required` | Lesson or required behavior still needs an external resource |
| `unresolved` | Source, semantics, runtime capability, or required binding is unknown |

Give feasibility `candidate` only when required mappings are substantiated;
`partial` when useful artifacts can be produced but required work remains;
`blocked` when a required capability cannot currently be supplied. Runtime
validation remains unverified until executed. The supported outcome for an
unattachable tenant can be “retain native tenant CI; Troshka integration blocked.”
