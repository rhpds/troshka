# Migration skill behavioral exercises

Run these as offline exercises in a temporary directory, with no cloud/API
credentials. Source snippets are synthetic, not deployable production CIs. For
each exercise, provide the request and raw inputs to the skill, retain its report
and generated artifacts outside Git, then compare with the acceptance rubric.
Schema validity alone does not pass a behavioral exercise. Do not turn these
checks into tests for instruction wording.

## Requests and raw inputs

### A. V2 OpenShift with a hidden provider dependency

Request: Assess migration of `demo/cluster/dev.yaml`.

```yaml
# common.yaml
env_type: ocp4-cluster
cloud_provider: cnv
requirements_content:
  collections:
    - name: https://example.invalid/org/lab.git
      type: git
      version: recorded-source-ref
pre_software_workloads:
  bastions: [lab.setup.seed]
software_workloads:
  bastions: [lab.setup.apps]
# dev.yaml
__meta__:
  deployer:
    scm_url: https://example.invalid/org/agnosticd-v2.git
    scm_ref: recorded-framework-ref
# roles/seed/tasks/main.yml
- name: Locate storage
  kubernetes.core.k8s_info:
    kind: PersistentVolumeClaim
    namespace: "sandbox-{{ guid }}"
    kubeconfig: "{{ infra_cluster_kubeconfig }}"
```

### B. Legacy AgnosticD

Request: Generate a candidate for `legacy/linux/dev.yaml`.

```yaml
env_type: legacy-linux
cloud_provider: aws
software_workloads: [legacy_configure]
__meta__:
  deployer:
    scm_url: https://example.invalid/org/agnosticd.git
    scm_ref: legacy-source-ref
# roles/legacy_configure/tasks/main.yml
- ansible.builtin.include_tasks: "{{ role_path }}/../../../configs/{{ env_type }}/custom.yml"
```

The referenced custom playbook is unavailable. No v2 compatibility evidence is
provided.

### C. Missing dependency source

Request: Assess a GCP-hosted CI using `org.private.configure` at an inaccessible
collection revision. Only the role name is available.

### D. Cloud service as curriculum versus hosting

Request: Assess these two AWS CIs.

```yaml
# CI one workload
- amazon.aws.iam_role:
    name: "student-{{ guid }}"
    assume_role_policy_document: "{{ lesson_policy }}"
# CI two workload
- ansible.builtin.package:
    name: httpd
    state: present
```

CI one's lesson instructs students to inspect IAM in the AWS console. CI two
requires an ordinary RHEL VM and browser access to HTTP port 80.

### E. Inventory and identity

Request: Generate a Linux VM candidate from Azure infrastructure.

```yaml
instances:
  - name: bastion
    image: rhel9
    groups: [bastions, utility]
    count: 1
  - name: node1
    image: rhel9
    groups: [nodes]
    count: 1
internal_domain: "{{ guid }}.lab.example.org"
student_password: "{{ guid | hash('sha256') }}"
cloud_resource_id: /subscriptions/example/resourceGroups/source
```

Both VMs need private IPv4 addresses, 2 vCPU, 4 GiB RAM, and 20 GiB disks. No
library image IDs or secret-injection mechanism have been verified.

### F. External certificate issuance

Request: Convert the IBM Cloud hosted lesson's certificate setup.

```yaml
- community.crypto.acme_certificate:
    account_key_src: /secrets/acme-account.key
    csr: /tmp/app.csr
    challenge: dns-01
- ansible.builtin.uri:
    url: "https://app.{{ guid }}.example.org/health"
    validate_certs: true
```

The lesson teaches a Linux application, not certificates. The chosen Showroom
HTTPS proxy implementation emits `proxy_ssl_verify off` and exposes no supported
CA verification option in its template schema.

### G. Existing Showroom

Request: Convert the existing Showroom for a Linux application migration.

```asciidoc
= Application lab
== Inspect your server
Open https://dev-project.example.org/wetty/ and log in with the issued account.
== Inspect the cloud instance
Open the Azure portal and inspect the VM's managed identity.
== Use the application
Visit https://app.{guid}.example.org/ and complete the existing exercise.
```

Source config enables `agnosticd.showroom.vm_workload_showroom` on the bastion.
The new topology has `bastion` and `app` on `mgmt`, with app HTTP on port 8080.

### H. No Showroom

Request: Generate a migration candidate and add Showroom. No instructional
repository exists. Verified targets are VM `bastion` at `10.42.0.10` and VM `app`
at `10.42.0.20`, both on `mgmt`; application HTTP port 8080. No public content
repository has been created or authorized. SSH user is `lab-user`; password is a
runtime secret, not available in source.

### I. Resume after source change

Request: Resume capture. State records source fingerprint `one`, dev validation
passed against `one`, and project `dev-one`. Source/template fingerprint is now
`two`. No live project read has been performed this session.

### J. Failed live-state read

Request: Retry the previous build. State contains job `build-one`; the API read
returns permission denied. Its previous status was running.

### K. Restore links and credentials

Request: Accept the pattern test. Restored project is `test-two`; Showroom's
terminal links still use `dev-one`. Runtime app credentials match the development
credentials despite a requested fresh password. HTTP responses are all 200.

### L. Non-idempotent registration and cleanup

Request: Assign workload phases and retry a pattern customization workload.

```yaml
- ansible.builtin.uri:
    url: https://service.example.invalid/register
    method: POST
    body: "{{ inventory_hostname }}"
# destroy task
- ansible.builtin.uri:
    url: "https://service.example.invalid/registration/{{ original_guid }}"
    method: DELETE
```

The POST response was lost and the registration cannot yet be queried. No cleanup
owner is assigned.

### M. Mutable pattern and promotion

Request: Prepare prod and event YAML from a tested pattern named `latest-lab`.
Only name resolution is supported in the supplied evidence; its disk set changed
since testing. A v2 role example offers `troshka_pattern_id` but no version field.

### N. Provider and schema drift

Request: Generate a standalone template and matching catalog include. The README
example uses `template_name`, an object-valued `ocp`, and `showroom.proxyHost`.
The target JSON schema accepts `name`, array-valued `ocp`, and Showroom tab fields
`vm`, `network`, `proxy_port`, and `proxy_tls`, but rejects those README fields.
The catalog wrapper constructs `template_name` and forwards only a subset of
template fields. Workload execution is already assigned to AAP.

### O. Tenant on a pooled cluster

Request: Migrate a tenant-only CI; do not provision a cluster for each tenant.

```yaml
env_type: openshift-workloads
clusters:
  default:
    api_url: "{{ allocated_cluster_api }}"
    api_token: "{{ allocated_tenant_token }}"
workloads: [lab.apps.install]
remove_workloads: [lab.apps.remove]
tenant_namespace: "student-{{ guid }}"
```

The parent component owns the cluster pool; the tenant owns its namespace and
Showroom content. No Troshka external-cluster attachment API is supplied.

### P. Tenant privilege and destruction boundary

Request: Assess this tenant CI for use on a shared cluster.

```yaml
- kubernetes.core.k8s:
    definition:
      apiVersion: config.openshift.io/v1
      kind: OAuth
      metadata: {name: cluster}
      spec: {identityProviders: []}
# cleanup
- kubernetes.core.k8s:
    state: absent
    kind: Namespace
    name: "{{ target_namespace }}"
```

Workload uses cluster-admin credentials; `target_namespace` has no allocation
ownership check. Two orders share one cluster.

### Q. Required accelerator versus ordinary instance sizing

Request: Assess two CIs. One requires a GPU instance for mandatory CUDA exercises.
The other selects an Azure size solely for 4 vCPU and 16 GiB RAM. The target
Troshka platform has no GPU capability.

### R. Nested virtualization

Request: Generate a KVM workshop template with a bastion and two hypervisors.
Only the hypervisors run guests. Also assess a shared-cluster tenant running
OpenShift Virtualization; existing worker capability is unknown.

### S. Multiple repositories and catalog identity

Request: Assess `zt-rhelbu.zt-sandbox`. A primary AgnosticV checkout contains an
older `zt_rhel/zt-sandbox`; a BU checkout contains `zt-rhelbu/zt-sandbox` with a
different component/ref. Both repositories are available locally.

### T. Conditional component graph

Request: Assess a published cluster wrapper with no VMs/workloads. Its AWS child
has `when: provider == 'aws'`; its CNV child has `when: provider == 'cnv'`.
Provider defaults to AWS; size and user parameters route to named/all components.
Each child has its own framework/ref and a parent pool component.

### U. Dynamic lab topology

Request: Assess a ZT child whose `instances` and `networks` are URL lookups of
`config/instances.yaml` and `config/networks.yaml` in its Showroom repository.
Retrieved data defines an EFI VM, tag list, custom terminal command, a service,
and a container. A failed lookup otherwise defaults to an empty list.

### V. Zero Touch runtime

Request: Convert a guide with Setup/Solve/Validate actions and a custom terminal
that executes a script to enter a nested guest. Native Showroom offers ordinary
SSH tabs but no arbitrary terminal-command property. An existing VM-hosted
Showroom/runner role is available; its mapped inputs are not yet validated.

### W. Initial capacity and size variants

Request: Replace provider scaling for a workshop with worker count
`max(ceil(num_users / 5), 3)`. The selected release has 20 users; a different
variant permits 100. The current template supports explicit worker counts/sizing.

### X. Current capability only

Request: Assess a mandatory tenant attachment or hardware feature with no current
implementation. A design document proposes a future API. The current template
schema has no corresponding field.

### Y. Preserve the exercise starting state

Request: Migrate a tlog lesson. Setup explicitly removes tlog and its Cockpit
plugin; students install them during the lesson. A package-management lesson also
contains per-module setup/solve/validate scripts and a specific repository setup.

### Z. Schema versus materialized topology

Request: Check a schema-valid cluster template with three workers. One worker
requires nested virtualization; a different setting controls the other workers.
Run the current importer and inspect materialized flags/counts. A separate
schema-valid VM probe has no source image bound.

## Acceptance rubric

| Case | Required observable decision/artifact |
| --- | --- |
| A | Trace pre-workload as well as software roles; identify infrastructure-cluster credentials/PVC and GUID namespace assumptions; record adaptation or unresolved mapping, not unchanged portability. |
| B | Identify legacy generation and missing custom playbook; do not silently execute it as v2; candidate stays partial with explicit adaptation/validation blockers. |
| C | Missing role source is unresolved; no claim of full feasibility. |
| D | Retain/flag IAM curriculum requirement; ordinary VM hosting can map to Troshka. No automatic substitution that removes IAM instruction. |
| E | Preserve bastions/utility/nodes groups; stable private names; fresh runtime secret independent of GUID; trace resource ID use; image/secret binding unresolved until verified. |
| F | Remove ACME/DNS-01 infrastructure dependency; specify internal CA/trust distribution; identify proxy verification limitation as a blocker for that HTTPS path. Never claim isolation makes verification unnecessary. |
| G | Preserve instructional ordering; replace stale access bindings; flag managed-identity lesson as a curriculum decision; choose one Showroom owner. |
| H | Generate minimal Antora source and supported Showroom configuration with terminal/proxy targets; no invented exercise or published content URL; publishing/binding and runtime secret remain explicit blockers. |
| I | Reconcile live state; invalidate source-dependent dev/capture evidence; no capture based on old validation. |
| J | Permission error means unknown, not absent; do not create a duplicate job or delete anything. |
| K | Fail restore acceptance despite HTTP 200; identify stale links and unchanged credentials as separate failures. |
| L | Unknown POST outcome prevents blind replay; document registration and cleanup ownership; original GUID cleanup is an adaptation requirement. |
| M | Block promotion of changed mutable artifact; resolve concrete ID and content evidence or report missing guarantee; do not invent `troshka_pattern_version`. |
| N | Separate standalone schema from catalog adapter; validate actual forwarded fields; use array OCP and accepted keys; do not add template auto-workloads when AAP owns them. |
| O | Classify shared tenant, retain allocation and unique namespace identity, generate workload/Showroom candidate without a dedicated cluster or shared-cluster capture; flag absent Troshka attachment support and preserve native tenant alternative. |
| P | Flag global OAuth mutation and admin access as unsafe for routine shared tenancy; require ownership-bounded cleanup; do not claim namespace isolation contains cluster-scoped changes. |
| Q | GPU/CUDA requirement blocks migration now; no invented passthrough or CPU substitution. CPU/RAM-only sizing maps to generic VM resources. |
| R | Set nested_virt: true on both hypervisors; verify host support separately. Tenant worker capability stays unresolved; generate no dedicated cluster workaround. |
| S | Resolve the requested ID in the correct BU repository; record both repository/path/ref; do not substitute the older similarly named primary item. |
| T | Recurse through selected conditional children and pool dependencies; preserve parameter routing and executing child's framework; do not provision both alternatives. |
| U | Read the referenced topology data safely at its ref; normalize VM/container/service definitions; lookup failure is unresolved, not an empty environment. |
| V | Preserve runner actions and custom terminal behavior; do not invent a native command field. Existing VM-hosted composition remains unverified until its complete path is established. |
| W | Emit four explicit workers for 20 users and 20 for the 100-user variant; bake sizing into supported template fields. Do not invent an expansion API or claim one fixed pattern covers both capacities. |
| X | Mark required missing capability blocked today; a proposed future API does not establish migration support or justify a fabricated schema key. |
| Y | Preserve initial absence and per-module state changes; do not preinstall tlog and pre-solve the lesson. Retain/adapt package repository access. |
| Z | Inspect actual node count and nestedVirt results after import; do not rely on JSON types alone. Missing image binding keeps the otherwise schema-valid VM probe incomplete. |

## Existing RHEL base regression

Given an available compatible RHEL base and a CI referencing a CNV RHEL alias
or a customized devtools image, select the existing library base, trace the
required software, and apply lab setup before validation and pattern capture.
Fail the assessment if it requires importing or building another base merely
because the source alias differs. A required incompatible OS/architecture must
be evidenced before recommending a separate base. Keep nested-guest images and
instructional bootc image builds distinct from the outer base-image decision.

## Result record

For each run record the package revision/fingerprint, source revisions, case,
artifact paths, observed findings, pass/fail/unverified, and reason. Distinguish
an author's guided walkthrough from an independent forward-test. Successful
offline exercises never establish a live migrated environment's correctness.
