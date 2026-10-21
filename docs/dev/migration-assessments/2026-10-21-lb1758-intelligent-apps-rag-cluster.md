# LB1758 RAG cluster: migration skill assessment

Date: 2026-10-21. Mode: `assess`. Result: **partial**.

The workload stack is a plausible candidate for a Troshka-built shared backing
cluster, with explicit adaptations. This CI currently consumes a pooled cluster,
scales its CNV workers, and configures shared cluster services. It is not an
ordinary namespace tenant and should not be deployed once per student.

No local GPU requirement was found in the inspected pinned workload/GitOps
sources. Inference uses external LiteLLM/MaaS, so an AI workload label alone does
not trigger the GPU blocker. This is not a live scheduling or capacity result.
No guest nested-virtualization requirement was established; the KubeVirt calls
found are in the outer infrastructure provisioning layer.

## Scope and source evidence

Requested CI: `summit-2026.lb1758-intelligent-apps-rag-cluster`.
Local AgnosticV revision: `e0c0213554b610fabc25cd2238669c716c2b1395`.
The requested CI files are tracked; unrelated untracked directories were left
untouched. This is a local catalog snapshot, not a claim about current published
catalog state or deployed orders.

Canonical AgnosticV merges passed for both dev and event. Dev uses mutable main
refs. The detailed role/manifests assessment uses the event pins below; its result
must not be represented as full source inspection of current dev/main.

| Source | Inspected revision |
| --- | --- |
| AgnosticD-v2 event framework `lb1758-event-1.0.0` | `74e2418556aa4faf7e0ec7699792e9a2313147f1` |
| `rhpds/core_workloads` | `5dd2c5cd61d0bf9645e2110bc82011afa1c26095` |
| `rhpds/ai_workloads` | `99077f9008a0fce1657c24741083b4c3eca630e2` |
| `rhpds/rhpds.litellm_virtual_keys`, tag `1.0.0` | `dd87552f79d69d82000ba7a75ba800e1fe0f3884` |
| `rhpds/showroom`, tag `v1.6.6` | `4f938a08340993194c2bd5b6c9e8037b93793a63` |
| `blues-man/showroom-ai-assistant-collection` | `b68a7514ed5cc59fb2e5dfe835fbb6ec8712864a` |
| `rhpds/lb1768-intelligent-apps-rag-automation` | `ee85c9c6c7a9941e8de7dde5193a6298e254002c` |
| Related tenant Showroom content, main resolved during assessment | `5a76355ebde0b9811ecd31b4cd9dd4fc1f887974` |

Nine core workload role trees were read from exact local Git objects (87 text
files); 147 additional text files were read from pinned/resolved GitHub trees.
Framework scaling and workload-executor files were inspected at the event tag.
No downloaded workload code was executed. Secret includes were not decrypted.

The declared Assisted Installer collection `v0.0.9`, transitive role dependencies,
external chart/image contents, and the separate notebook/pipeline application
repository were not exhaustively inspected. Their compatibility remains
unverified. Related tenant/combined catalog source was inspected for architecture
and Showroom discovery, not fully assessed as independent migrations.

## Actual ownership and execution flow

Catalog source: `summit-2026/lb1758-intelligent-apps-rag-cluster/common.yaml`:

- Lines 26–33 select v2 `openshift-workloads`, `cloud_provider: none`, and supplied
  cluster API connection expressions.
- Lines 39–42 enable CNV scaling and request 10 added workers, each 16 cores and
  32 GiB memory: 160 vCPU and 320 GiB of worker allocation before control planes
  and supporting services. This is declared sizing, not measured utilization.
- Lines 78–93 list 13 ordered platform workloads.
- The `__meta__.components` entry requests `agd-v2/ocp-virt-labs-pool`, multinode,
  OpenShift 4.20, and propagates cluster plus outer-sandbox connection data.

The component resolves to the repository path `agd_v2/ocp-virt-labs-pool`, whose
base config uses CNV, starts with zero workers, and includes dynamic DNS and ACME
configuration. The selected stage and inherited pool settings require separate
effective-configuration validation before sizing a replacement cluster.

The pinned framework's
`ansible/configs/openshift-workloads/pre_infra.yml:8` invokes
`host_ocp4_assisted_scale`. Its `tasks/create_workers.yaml:82` creates KubeVirt
VMs on the outer sandbox cluster; it depends on that cluster's API, namespace,
networking, and Assisted Installer. Keeping this role unchanged would continue
provisioning outside Troshka.

The deprecated list-shaped `clusters` value is not by itself a demonstrated
failure: the pinned `openshift_workload_deployer/tasks/main.yml:14` explicitly
normalizes that form. This assessment does not infer errors from current default
examples when the selected framework implements compatibility handling.

The related `lb1758-intelligent-apps-rag-tenant` uses `config: namespace`, tenant
identity/namespace workloads, and the learner Showroom. The `combined` CI includes
a multi-tenant loop. These support the intended split: prepare a shared platform
once, then allocate and configure users/tenants on it.

## Dependency decisions

Paths in the table refer to the source repositories and exact revisions above.

| Dependency | Status | Evidence and migration requirement |
| --- | --- | --- |
| Pooled CNV cluster allocation | `infra_mapping` | CI component and `agd_v2/ocp-virt-labs-pool/common.yaml`; map the platform-owner allocation to one appropriately sized Troshka cluster or retain the existing pool. Preserve outputs needed by tenant allocations. |
| CNV worker scaling | `workload_adaptation` | Framework pre_infra and `host_ocp4_assisted_scale/tasks/create_workers.yaml:82`; replace with Troshka topology/owned provisioning and disable this outer-cluster scaler on the migrated path. |
| GPU/specialized instance hardware | `unresolved` for full closure; no blocker found in inspected subset | No GPU device request or mandatory local accelerator was found in the inspected role/manifests. Remote inference is explicit. Notebook code, external images, capacity, and runtime scheduling still need verification. Any newly established mandatory local GPU requirement blocks migration today. |
| Nested virtualization | `unresolved` for runtime; no guest requirement established | Outer KubeVirt provisioning does not require guest workers to host VMs. Do not enable nested virtualization solely because the original provider is CNV. If later workload tracing establishes it, set `nested_virt: true` on every affected VM and verify host support. |
| Cluster authentication and global operators | `infra_mapping` | Core authentication, GitOps, and AI roles change global resources. Run once under the platform owner, not once per tenant. API connectivity, privileges, registry access, and version compatibility need validation. |
| Persistent storage | `workload_adaptation` | GitOps `tenant/workspace/templates/workbench.yaml:69` and `:409` hardcode Ceph RBD and CephFS storage classes; preserve RWO/RWX semantics through configurable class mappings. A default block-only class cannot satisfy the RWX claim. |
| Model inference and access-key service | `external_required` | LiteLLM key workload and lesson `02-08-querying-milvus.adoc:50`; retain supported external inference or separately redesign that dependency. No local-GPU substitute is assumed. |
| GUID-derived identity and passwords | `workload_adaptation` | CI password expressions, GitOps metadata keys, and AI assistant `prepare_variables.yaml:5`; remove hosting-name assumptions while retaining unique tenant ownership and fresh runtime credentials. |
| DNS and certificate issuance | `workload_adaptation` | Parent provider includes dynamic DNS/ACME; map the owned cluster to supported isolated names and trust, and refresh external access at deployment. Do not copy issuance dependencies blindly. |
| TLS verification | `workload_adaptation` | Event framework workload executor sets `K8S_AUTH_VERIFY_SSL: false`; AI assistant tasks also disable validation. Explicit local CA trust/verified access is required for the target design. Existing bypasses are not migration success evidence. |
| Capture freshness and replay | `workload_adaptation` | LiteLLM keys expire after seven days in this CI. Generate new runtime keys, user bindings, URLs, and credentials after restore; do not capture expiring credentials as permanent pattern configuration. |
| Release reproducibility | `workload_adaptation` | Event pins framework/collection/GitOps source, but some downstream images and npm dependencies use mutable tags. Record/pin effective content where needed to promote a tested artifact. |
| Troshka tenant attachment | `unresolved` | Existing project-local runner targeting does not establish external shared-tenant attachment. Preserve the native tenant allocation flow until a supported project/allocation binding is established. |

## Ordered workload assessment

All workload execution below is currently owned by AgnosticD/AAP. A migrated
catalog flow must keep one executor per phase and avoid duplicate Troshka
template auto-workloads. “API candidate” describes the inspected approach, not a
successful deployment or complete transitive compatibility result.

| Workload | Role in migration |
| --- | --- |
| `rhpds.litellm_virtual_keys.ocp4_workload_litellm_virtual_keys` | External key provisioning/revocation; refresh after restore and reconcile retries. |
| `agnosticd.core_workloads.ocp4_workload_authentication` | Platform-owner phase; config uses Keycloak, zero initial users, removes kubeadmin. Preserve a working administrative path and create tenant identities later. |
| `agnosticd.core_workloads.ocp4_workload_authorino` | API candidate for platform operator setup; validate selected operator/dependencies. |
| `agnosticd.core_workloads.ocp4_workload_amq_streams` | API candidate; validate shared Kafka services/storage. |
| `agnosticd.core_workloads.ocp4_workload_pipelines` | API candidate; validate pipeline execution, storage, tools and trust. |
| `agnosticd.core_workloads.ocp4_workload_openshift_gitops` | Platform-owner phase with cluster-admin configuration and route TLS changes. |
| `agnosticd.core_workloads.ocp4_workload_serverless` | API candidate; cluster-wide operator and serving/eventing prerequisites. |
| `agnosticd.core_workloads.ocp4_workload_servicemesh2` | API candidate; cluster-wide service mesh setup, not tenant-local mutation. |
| `agnosticd.ai_workloads.ocp4_workload_openshift_ai` | Operator/DSC setup; config selects fast-3.x and rhods-operator 3.2.0, with KServe, workbenches and pipelines managed. Does not itself prove local GPU need. |
| `agnosticd.core_workloads.ocp4_workload_web_terminal` | API candidate for terminal services; validate tenant credentials and scope. |
| `agnosticd.core_workloads.ocp4_workload_gitops_bootstrap` | Trace child applications, not just the role: image prepull, AI workbench image, Kafka, Knative, Milvus, MinIO. Adapt storage and identity values. |
| `agnosticd.showroom.ocp4_workload_ocp_console_embed` | Global console/ingress/webhook mutation; run once under platform ownership or replace with supported Troshka access. |
| `agnosticd.showroom_ai_assistant.ocp4_workload_showroom_ai_assistant` | Separate assistant application using runtime user data and remote model access; adapt GUID assertion, credentials, URLs and trust. |

Only LiteLLM key removal is explicitly listed in this CI's `remove_workloads`.
That is consistent with cluster disposal potentially owning the rest, but no
live disposal contract was verified. It is insufficient evidence for safe
namespace-only teardown. Define platform destruction, tenant cleanup, and
allocation release separately before ordering a migrated CI.

## Showroom discovery and conversion requirements

The requested cluster CI includes console embedding and an AI assistant, but
does not directly declare the learner guide workload/content repository. The
related tenant CI does: `agnosticd.showroom.ocp4_workload_showroom`, content at
`rhpds/intelligent-apps-python-rag-showroom`, main, with per-tenant namespace.

The inspected content includes terminal and OpenShift AI notebook instructions,
an explicit external inference endpoint, and a GUID-derived bastion URL in
`content/modules/ROOT/pages/module-02.adoc:137`. The tenant workspace defaults
also contain a source-cluster domain; keep rendered overrides and scope distinct
from defaults before deciding which values actually reach users.

Preserve the authored guide and update its runtime attributes, terminal targets,
cluster console/AI links, and credential delivery. Provide a platform access
entry point for the shared backing cluster, and retain a separate scoped guide
per tenant. The assistant application is not a replacement for that guide.
Content rendering, terminal interaction, OAuth redirects, API trust, and access
isolation need live validation. Source revisions and image labels alone do not
establish working links or trusted TLS.

## Recommended migration boundary

1. Define a platform-owner Troshka template that creates the backing OpenShift
   cluster at the required initial size, replacing the separate CNV scale step.
   Resolve storage and version prerequisites before choosing a final topology.
2. Apply the existing platform workloads through one executor, with adapted
   inputs and verified trust. Validate services and the existing lesson flow.
3. Capture only an owned, validated platform build, with runtime/tenant secrets
   excluded or refreshed through verified restore procedures. Check pattern
   storage/certificate/identity behavior before calling the artifact reusable.
4. Provision the shared backing cluster once per authorized platform/pool
   allocation, then reuse it for tenant orders. Keep the native tenant allocator
   until Troshka binding is supported. Do not deploy that cluster per tenant.
5. Validate two tenants, per-tenant Showroom, key refresh, cleanup isolation, and
   platform-owner teardown. Promote the exact validated platform artifact and
   pinned tenant workload/content revisions as separate release inputs.

## Validation and blockers

| Check | Result | Evidence class |
| --- | --- | --- |
| Requested CI resolved in local AgnosticV | passed | Source review |
| Canonical dev and event merges | passed | Static check; validation left enabled |
| Event refs resolved and selected source trees inspected | passed | Source review; partial transitive closure as described above |
| GPU/nested-virt requirement established | unverified | No required local GPU or guest nested-virt requirement found in inspected sources; external code/images remain |
| Generated migration template/new CI | not_applicable | Assessment mode only |
| Real cluster capacity, versions, storage, connectivity and trust | unverified | No runtime checks executed |
| Pattern capture and fresh restore | unverified | No live actions executed |
| Tenant attachment, two-tenant access and cleanup | unverified | Platform capability/ownership checks still required |

Primary blockers to a deployable candidate: replacement of provider-specific
scaling, effective parent-pool configuration and capacity, storage-class mapping,
verified TLS/client trust, deployment-time credentials/model keys, and a supported
tenant access/allocation integration. Required secrets and referenced external
services were not accessed or tested. No catalog changes or live resources were
created, modified, captured, released, or deleted by this assessment.

Secret-bearing canonical merges, fetched source, and manifests are retained
outside Git under `/private/tmp/troshka-lb1758-assessment/`. The sanitized report
contains no credential values, access tokens, live provision data, or resource IDs.

## Skill test findings

The real CI exercised important paths absent from simple VM examples: parent
cluster components, provider-specific pre-infrastructure scaling, deprecated
cluster connection normalization, global platform workloads, nested GitOps
applications, external inference, and separate tenant Showroom ownership.

The skill's discovery guidance now explicitly follows GitOps applications and
distinguishes platform preparation from ordinary tenant configuration. Hardware
guidance distinguishes outer CNV virtualization from guest nested virtualization,
and remote model inference from required local GPUs. These are source-derived
improvements; this run is an author-operated assessment, not an independent model
benchmark or evidence that the CI has been migrated successfully.
