# Changelog

User-visible changes are recorded here in monthly sections, newest first, with
related work grouped by feature area. Update the current month's section in the
same commit as the implementation, consolidating related entries instead of
listing every commit. There is no separate Unreleased section. Release tags
identify builds; monthly notes describe development and do not confirm deployment.

The historical backfill below groups all development from June 4 through
November 14, 2026 into monthly summaries, including the newest releases.
These summaries describe work recorded in Git, not deployment verification or
an exhaustive list of every fix. Exact release tags and all 3,053 historical
commits are preserved in the [historical index](docs/dev/changelog-history.md).

## November 2026 — Groups, quotas, and native storage accounting

- User access is database-only: OpenShift group lookup no longer gates login or
  roles. Admin → Users can set optional `access_starts_at` / `access_ends_at`;
  outside the window, sign-in and deploy/start/redeploy of owned projects are
  blocked, and reaching the end date auto-stops owned active projects.

- KubeVirt **Install Operator** on a fresh cluster no longer 403s creating the
  `troshka` ServiceAccount: namespaced mutate RoleBindings are bound before SA
  create (K8s returns Forbidden—not Conflict—when create is denied on an
  already-present SA).

- `deploy-full` is faster for partial releases: tag builds only rebuild changed
  images and carry-forward the rest from the previous `v*` tag; promote/host
  rollouts/app waits skip unchanged roles; CRD/RBAC apply is skipped when those
  paths are unchanged. Cluster kubeconfigs come from gitignored
  `scripts/deploy-hosts.local.yaml` (see `.example`). Use `[rebuild-all]` in the
  release commit message to force a full image rebuild.

- OpenShift deploy/restart preflight now probes the saved pull secret against
  `quay.io` (when not using a pull-through registry) and blocks cluster creation
  if the secret is missing, malformed, or rejected; error text includes
  https://console.redhat.com/openshift/downloads for a fresh pull secret. The
  create wizard validates the saved secret before Create, Settings rejects
  invalid secrets on save, and OKD/SCOS templates remain exempt.

- Projects list hides `deleting` / `cleanup_pending` cards until the Project row
  is released (after namespace absence), instead of clearing the “Deleting…”
  state as soon as DELETE returns and leaving the card visible for the full
  native teardown.

- `deploy-full.sh` polls for the tagged `build-images.yml` GitHub Actions run
  after tag push (default 5 minutes) instead of exiting immediately when Actions
  has not registered the workflow yet; `--skip-push --tag` remains the timeout
  resume path.

- Native delete is map-based: metering freezes when delete/undeploy is accepted,
  a VM→PVC→RBD teardown map is sealed before namespace drain (custody or
  live-namespace inventory — no domain-wide collector scan as a gate), the
  Project row is released after namespace absence, and known private images are
  reclaimed asynchronously (`orphan_reclaim`). Soft
  `storage_coverage_unavailable` / `incomplete` debts no longer hold the
  Project hostage; collector settlement remains optional audit. Origin recover
  that hits missing collection pins on the lifecycle worker
  (`FileNotFoundError` for `*-authorized-pins.json`) soft-fails into the same
  live-map seal, and sealed cleanups no longer re-enter recover on later
  reconcile ticks.

- Native storage discovery matches retiring cleanup intents by `root_key` (not
  full `ProjectRoot` equality), so namespace-absent roots still produce retiring
  evidence when `deployment_generation` drifted between enrollment and cleanup.
  Toolbox Ceph identity (`ceph fsid`) uses `oc --request-timeout` plus the
  existing remote `timeout --kill-after` / `command_timeout_seconds` bound so a
  stuck tools CLI fails fast instead of hanging collection workers.

- Shared `k8s_admission` normalize for native support admission and CR digests:
  whole-number float/int equality (`structural_contains` / `canonical_equal`),
  and OpenShift's 4-source `kube-api-access-*` projection (adds
  `openshift-service-ca.crt`) so ops-pod create no longer fails with
  "service-account projection changed" on OCP. CSI install observation reuses
  the same SA recognizer. Cancelled origin delete recovery treats a GC'd RQ
  original job as terminal when the project has no in-flight job reference;
  `load_launch` / custody / checkpoint root checks accept legacy raw digests
  and match live API ints through the protected launch under `digest_cr_spec`,
  so fencing can seal a late TroshkaProject root after worker rollouts.

- Operations: the SonarQube pre-push hook is opt-in and disabled by default.
  Export `RUN_SONAR=1` (or `SONAR=1`) to run the gate; ordinary pushes skip it.

- Native origin CR-spec digests canonicalize whole-number JSON floats to ints
  before hashing (`digest_cr_spec`), so canvas/topology values like
  `position.x: 300.0` match Kubernetes API readback ints (`300`) on first bind.
  Non-integer floats stay distinct. Enrollment and other storage digests are
  unchanged.

- Project delete immediately cancels in-flight deploy/start/ops work and locks
  the canvas read-only for `deleting` and `cleanup_pending` (palette, properties,
  topology saves). The API rejects topology writes in those states as well.
  After the namespace is gone, cleanup-priority collection starts immediately and
  settlement re-runs as soon as a fresh report lands (10s poll while settling,
  instead of waiting up to a minute), without relaxing storage proof requirements.
  When delete cancels a native origin deploy after the TroshkaProject create
  lands but before the root receipt is bound, cleanup now seals that late root
  via `reconcile_cancelled` and adopts the exact UID into fencing authority so
  producer fencing is not stuck forever on `root_uid=None` while the CR remains.
  Cancelled recovery also matches protected launch specs through
  `digest_cr_spec`, so already-stuck intents that still store a raw float digest
  can seal the live CR without inventing a new root.

- Native origin capture enabled for local, troshka-dev, and infra01 while quotas
  stay off, so new generic kubevirt deploys retain namespace/root birth provenance
  for guarded cleanup. Per-domain `origin_admission` is set on prod collection
  profiles to match troshka-dev; existing projects still need reviewed online
  enrollment/reconciliation for delete authority.

- Online ancestry verification now resolves only the shared-cache images that
  clone parents require, instead of CSI-scanning every golden PVC in
  `troshka-cache`.

- Deployment: storage verification is opt-in. Use `--verify-storage` for existing
  evidence, `--fresh-accounting` for a fresh fleet audit, or `--accounting-project`
  for selected projects. Image and application health checks remain required.

- Shared storage accounting accepts removed historical clone links when a full
  cache scan confirms their absence, while rejecting unknown or incomplete edges.
  Manually deleted projects can reconcile retained accounting through an explicit
  operator verification of namespace and private-storage absence, preserving
  shared storage and the original cleanup audit history.
  Older roots without cleanup records use separate retrospective receipts tied to
  the original operator audit and complete preserved PVC inventory, including
  temporary VM disks; these receipts do not grant deletion authority.
  Custody reconciliation honors verified retired sibling roots when extending
  accounting enrollment for surviving projects.

- Networking: reserve lab/BMC host `.6` for ops pods, avoiding bastion `.50`
  collisions. Showroom uses only `.9` and reports conflicts instead of choosing
  another low address. Infrastructure transit addresses remain unchanged.

- Native deployment recovery retains fresh creation proof and a deployment
  attempt before dispatch. Failures proven to precede project resource submission
  can use Undeploy to return to draft or Delete locally. Historical and ambiguous
  failures remain held; retries and delayed workers retain their original identity.

- VM capture and disk reconfiguration use compatible KubeVirt power intent for
  modern and legacy VMs, and require verified VM shutdown before continuing.
  Failed stop requests, unavailable observations, and shutdown timeouts hold work.
  Origin captures resume retained checkpoints after acknowledgement or operator
  restart, preserving submission ambiguity and original producer identity fences.
  Snapshot custody reads share the collection scan deadline across Ceph checks,
  retaining bounded individual API requests instead of expiring early under the
  shorter ordinary inventory deadline. Snapshot accounting verification tolerates
  completed controller retry bookkeeping while preserving checks on active
  executions, operator identity, checkpoints and storage producer identities.
  Snapshot reconciliation preserves enrolled domain references after unrelated
  projects settle or are deleted, without proposing an unpublished enrollment
  transition; only its own project identity is prepared and locked.

- Native project cleanup fences late producers, waits for VM/VMI absence and
  Troshka finalizers, then deletes the namespace normally. An exact terminating
  CR with only Kubernetes' orphan finalizer can proceed after retained inventory
  proves Troshka finalization and the completion transition is recorded.
  Retained cleanup state
  resumes retries and keeps storage or external cleanup blockers visible until
  independent settlement completes.
  Verified storage identities and complete coverage are retained before deletion;
  cancelled cleanup with missing custody can recover it while original resources
  remain, preserving the original request and requiring a renewed producer fence.
  Existing namespaces need only required storage and producer inventory access.
  Completed operator replacements resume retained cleanup, checkpoint observers
  no longer strand deployment completion, and successful support work can settle
  through its exact namespace and storage boundary. Unknown outcomes remain held.
  Verified terminal storage retirement can settle alongside another project's
  unresolved snapshot only after fresh head and snapshot dependency evidence proves private
  storage independence, including independently rechecked dependencies when a
  descendant's allocation read times out; its allocation remains unknown.
  Shared cache survival and unresolved ownership stay checked.
  Successor storage custody excludes exactly settled historical roots, preserving
  their receipts and rechecking settlement authority before publication.
  Snapshot journal configuration accepts the explicitly pinned downstream
  ODF 4.20 build pair alongside the upstream fixture profile; mixed pairs reject.
  Schema acceptance requires separate installed snapshot/restore and retirement
  qualification before enablement.
  Helm exposes an explicit native origin capture opt-in after qualification,
  disabled by default and independent of domain admission and quota enforcement.
  Enabled native collection also supplies protected profiles, transport, and the
  configured CLI to API, tunnel, and lifecycle workers for custody verification
  and retained cleanup when capture is disabled.
  Unverified discovered snapshots hold their pinned project's storage accounting;
  conflicting protected evidence and unknown root identities keep global holds.
  Fresh native deployments and namespace-only recovery read the exact project root
  through existing list access before namespace grants exist; incomplete reads hold.
  Origin storage custody follows authenticated cache enrollment history while
  preserving protected configuration, exact predecessors, and live cache checks.

- Project deletion and undeploy use verified original resource custody even after
  canvas or installation metadata changes, preserving live ownership and storage
  settlement checks. Single and bulk deletion now display rejected requests;
  failed deletion keeps the project and its saved canvas available. Retained disk
  ancestry no longer depends on editable canvas metadata. Cleanup tolerates a
  rollout restart timestamp when every other attested helper identity matches.
  Stale or missing storage observations can request a scoped evidence refresh
  even when periodic collection is disabled; users retry cleanup after collection.
  The refresh does not run trash cleanup or allocation reconciliation.
  Administrator-reviewed custody successors preserve original receipts and
  require explicit cleanup-authority selection, fresh verification, and an exact
  review digest before application. Replaced disks require explicit retirement
  evidence for their original identities; collection continues checking their
  absence before cleanup can settle storage. Failed observation refreshes can
  be retried without enabling periodic collection. Shared-cache verification
  checks each selected snapshot's exact children so unrelated projects can use
  the same cache without blocking cleanup. Disk-retirement verification preserves
  its freshness limit and rechecks Kubernetes identities and CSI mappings before
  accepting the evidence. Retirement reconciliation requires a subsequent storage
  scan and preserves conflict markers across enrollment changes; older in-flight
  reports remain audit history without restoring outdated cleanup evidence.

- Routine releases check existing storage-accounting health without launching
  fleet scans. Accounting changes can request fresh validation once after promotion,
  across the fleet or selected production projects and their shared-storage domains.
  Collector configuration, application health, and accounting acceptance guards remain.

- Online storage enrollment can reconcile newly deployed projects sharing cached
  base images while preserving matching existing ownership receipts. Reconciliation
  independently verifies all sibling clones and rejects changed ownership evidence.
  Explicit accounting-only enrollment can include failed deployments while retaining
  their failure state and denying cleanup authority from the new receipts.
  Previously attested network support omissions can be reconciled for accounting
  only when each missing identity is explicitly selected; retained receipts,
  storage validation, runtime restrictions, and strict cleanup checks are preserved.

- Release builds run the eight operator and helper images in parallel, while
  keeping production promotion gated on the complete image build.
  Native host builds transfer registry credentials into private temporary files,
  remove them after pushing, and report push failures instead of false success.
  Build archives exclude macOS metadata files so migration scripts remain loadable.
  Release accounting checks run at most two project scans concurrently, retaining
  freshness and final inventory checks and stopping new scans after a failure.
  Native storage readiness allows 50 seconds for sequential identity checks,
  with aligned worker deadlines and leases, while retaining the 60-second
  evidence freshness requirement.
  Project accounting scans resolve only their protected shared-image sources,
  avoiding repeated discovery of unrelated cache volumes while retaining fresh
  source identity and clone-graph checks; domain scans still inspect the full cache.

- Deployment validation identifies disconnected VM NICs in the canvas and rejects
  them before provisioning. Native deployments surface terminal VM errors without
  waiting two hours; worker deadlines allow time to report operator failures.
  Redeploy stops unless cleanup explicitly confirms success, preserving cleanup
  diagnostics and the existing host assignment.
  KubeVirt reconfiguration correctly handles removed VM nodes, preventing a
  slice error after network changes have already been applied.
  Apply Changes checks live network consumers before removing networks, and
  releases verify helper network attachments before restarting workloads.

- Project status badges use neutral gray for stopped projects in the project list
  and project detail view.

- CSI identity validation supports explicitly reviewed, container-scoped CRI
  configuration-to-manifest digest aliases without accepting unknown images or
  changing existing collector profile hashes.

- Added a private, reviewable Argo bootstrap with protected Helm adoption and
  explicit interrupted-sync recovery. Dev can follow `main` and watch `latest`
  application images through the OpenShift GitOps image updater.

- Full deployment can install approved collector profiles, rotate dedicated
  credentials, and enroll empty storage domains without replacing existing
  enrollments. Release verification now requires fresh accounting through the
  normal worker queues, including configured hosts with no projects. Dev must run
  the exact release images and pass fresh accounting before production promotion.
  Deployment aligns dev's `latest` images after concurrent builds finish and pins
  production's Argo Git revision to the promoted release without replacing Helm settings.

- Collector release preflight: full deployment upgrades host operators and RBAC using the named release before configuring and checking collectors. Every configured native host, including empty hosts, must have an unambiguous protected profile, enrollment anchor, and fresh accounting before production app promotion; failed host rollouts or incomplete accounting block promotion.

- OpenShift deployment validation: reject fresh OCP installs before placement or job submission when the project owner has no pull secret; direct users to configure an OCP pull secret or pull-through registry credentials, while allowing OKD and captured-cluster re-certification. Bulk Deploy reports failures by project while continuing other deployments. Confirmed installer failures show “OpenShift Install Failed” with a safe reason, and API timeouts explicitly identify OpenShift without replacing a confirmed install failure.
- About and release history: added an About overlay above Getting Started with the build version, author information, and expandable monthly release notes; reconstructed development history back to June 4. Notes reload each time About opens, so local changelog edits need no frontend restart; production reads the file packaged in its deployed image.
- Group administration and billing: added managed groups, delegated managers, searchable membership pickers, team billing, and a unified Groups interface for membership, quotas, and reporting.
- Usage attribution: preserved asset ownership and historical attribution during group transfers and fresh accounting setup; corrected invoice month boundaries and marked active spend unavailable when interval evidence was missing.
- Access control: made the database authoritative for user access and roles, and preserved quota access revocation during native usage reporting.
- Quotas: added delegated quotas, copy-on-write accounting, staged migration, startup allowances, and measured settlement; kept tenant quotas independent of platform cache totals.
- Native storage evidence: introduced a read-only KubeVirt collector and allocation reporting, verified enrollment and live PVC bindings, tracked clone lineage and retained snapshots, and preserved unknown usage when evidence was incomplete.
- Native lifecycle safety: added guarded deployment, observation, maintenance halt, quarantine tooling, verified cleanup, and recovery for interrupted initialization and teardown; tied restart verification to the corresponding stop episode.
  Pending cleanup continues through the dedicated queue even when routine native
  collection is disabled, so deletion still settles storage and removes owned trash.
- Native lifecycle validation: made guarded non-null storage ancestry, stop intents,
  baseline records, and cleanup roots explicit for CI type checking.
- Collector operation: moved native collection into scoped RQ workers and installed explicit collector permissions during releases. Collector roles now exclude Secrets; full Secret custody checks remain with the worker/operator, while storage settlement still requires namespace and disk absence. Use protected TLS-verified collector credentials for CSI readiness and confirm receipt-backed cache sources through discovery on the first scan. Full deployment now applies the common read-only collector RBAC YAML across the OCPV host fleet, including empty hosts. New project deployments receive scoped collector bindings automatically; single-provider enrollment can explicitly use the protected collector transport.
- Existing environments: added enrollment of existing native projects without downtime, support for explicitly reviewed legacy topology pairs, and preservation of edited foundation organizations during database preparation.
- Deployment and networking: added exact searchable OpenShift release selection, aligned the CI RQ dependency with the worker API, and repaired orphaned-network cleanup retries and permissions during namespace deletion.
- Release milestones: includes the November 2–3, November 11–12, and November 14 tagged releases, through v2026.11.14-214005. Test-only releases are recorded in the historical index.

## October 2026 — Metering, power controls, and workshop access

- Metering and billing: introduced project metering, reconciliation, budget auto-stop, dedicated-host and nested usage factors, monthly invoices, owner-scoped reporting, and invoice export improvements.
- Power controls: added per-VM pause, resume, and hibernate, plus a project Off mode selector; aligned state reporting and ensured paused VMs continued to accrue compute usage.
- OpenShift access: added laptop oc access through Troshka API tunnels, moved long-lived relays to a dedicated tunnel service, and improved reconnect behavior, authentication, and API forwarding.
- Workshop experience: refreshed Showroom access on open, preserved workload logs during recovery, provisioned workshop VS Code, and routed AAP access by hostname.
- Canvas and libraries: added ownership filters, an OpenShift Info flyout, clearer template validation errors, stop confirmation, and light/dark theme repairs for previews and canvas views.
- Nested networking: added nested-MAC network controls, OVN forwarding repairs, and a consistent host CPU model for nested-virtualization project VMs.
- OpenShift lifecycle: clarified rebuild versus re-certification choices, replayed template workload chains on redeploy, and improved disk-write completion, media eject, and installer recovery behavior.
- Operations: added an AgnosticV-to-Troshka migration skill, complete tagged-release deployment and rollout verification, dependency updates, and more reliable pre-push and CI quality checks.
- Release milestone: v2026.10.27-workshop-web-tabs; later October fixes were included in the November release sequence.

## September 2026 — Multi-cluster canvas and automated workloads

- Cluster canvas: introduced resizable OpenShift cluster boundaries, drag-and-drop membership, count-driven VM creation, per-role disks, network attachment, shared DNS configuration, and cluster-aware template import/export.
- OpenShift installation: made ops-pod installation available across host types, added bastionless templates, per-cluster logs and progress, elapsed timers, credentials display, and recovery of interrupted install monitors.
- Re-certification: automated pattern re-certification and health monitoring, added a usable-control-plane milestone, and improved credential delivery, console readiness checks, and stale-pod recovery.
- Workload execution: added ad-hoc AgnosticD runs with configurable execution environments, cluster or VM targeting, Ansible groups, extra variables, run history, live logs, and restart recovery.
- Scoped access: added project-scoped API keys and workload access permissions, mounted ops-pod secrets as files, and narrowed provider/operator mutations through namespace bindings and resource-specific permissions.
- Networking: added per-network MTU configuration and host-aware placement, propagated MTU settings into OpenShift and KubeVirt, and added Showroom TLS termination and certificate handling.
- Showroom and terminals: added cluster terminal tabs with oc and merged kubeconfigs, persistent terminal sessions, service-starting feedback, and automatic console-proxy configuration.
- Patterns and storage: expanded pattern-location tracking and synchronization, exposed capture progress, and improved cleanup of abandoned imports, images, and namespaces.
- Operational controls: added semantic Apply Changes previews, host-agent update warnings, database-aware worker scaling, a host placement toggle, ingress-domain settings, and native-host container build scripts.
- Reliability and validation: improved KubeVirt teardown, BMC media handling, stuck-pod recovery, dependency maintenance, and sharded test execution for constrained CI runners.

## August 2026 — Multi-host deployment and pattern portability

- Multi-host projects: added WireGuard mesh orchestration, capacity-based placement across hosts, affinity and anti-affinity groups, cross-host state polling, console routing, and coordinated teardown.
- Placement reliability: corrected stale capacity accounting, host assignment formats, per-host topology handling, and private-IP mesh endpoint selection.
- KubeVirt access: expanded Redfish virtual-media support and improved boot-order updates and BMC recovery.
- Console sessions: introduced session gating and Force Connect, reduced reconnect loops, and improved multi-viewer behavior on troshkad hosts.
- Pattern portability: introduced local object-store capture, per-disk progress, pattern locations, central synchronization, storage-readiness checks, and public or per-user sharing controls.
- Storage tools: added asynchronous disk wipe with progress reporting and improved scratch-space handling during capture.
- App updates: added update availability and image-digest details in the UI, rollout actions, and development restart support for backend workers.
- Provider setup: added optional host provisioning on provider registration and centralized package delivery for dedicated OpenShift Virtualization hosts.
- Release milestone: v1.1.0 on August 6 combined the early-August deployment and console improvements with a broad test and code-quality sweep; subsequent August development is included in later tags.

## July 2026 — KubeVirt-native environments and scalable workers

- KubeVirt-native provider: introduced the Troshka operator and custom resources, provider administration, automatic operator installation, configurable namespaces, and native project deployment and teardown.
- Native VM lifecycle: added start, stop, restart, Apply Changes, routed VNC consoles, guest-agent/SSH/console execution, and deployment progress from DataVolume downloads.
- External access: added MetalLB external IPs and gateway port forwarding for KubeVirt-native projects.
- Native storage: added golden-image PVC caching, authenticated image downloads, capacity reporting, and pattern capture through VolumeSnapshots and object-storage exports.
- OpenShift support: added native cluster monitoring, optional bastion use, re-certification jobs before VM boot, deploy-time cloud-init regeneration, and clearer status and elapsed-time reporting.
- Host resilience: added mutual TLS and rate limiting to troshkad, NFS resilience, and automatic networking and BMC restoration when hosts reconnect.
- Background work: introduced Redis/RQ job queues, horizontal worker scaling, queue positions, failed-job management, in-flight deployment tracking, and an admin Job Queue page.
- Deployment operations: added Helm packaging, OpenShift group-based access controls, operator update controls, image-update integration, a full-release deployment script, and improved concurrent deployment feedback.
- Release milestone: v1.0.0 on July 31 captured the initial June–July development period, alongside testing and code-quality improvements.

## June 2026 — Initial platform and lab-building capabilities

- Project foundation: started the repository on June 4 with the monorepo scaffold, configuration, database models, test infrastructure, and a canvas editor synchronized with the backend API.
- VM lifecycle: added host provisioning, automatic capacity expansion, deploy and Apply Changes flows, per-VM power controls, VNC consoles, cloud-init, SSH keys, boot-order controls, and deployment progress.
- Canvas editing: added undo/redo, topology validation, unique-name checks, automatic layout, snapshots, reusable patterns, bulk deployment, and template YAML import/export.
- Libraries: added S3-backed images and ISOs, URL imports, multipart transfers, sharing, central read-only libraries, pattern synchronization, and S4 object storage support.
- Isolated networking: added VXLAN networking, per-project network namespaces, DHCP/static addressing, gateways, external IP lifecycle management, port forwarding, DNS providers, and HAProxy load balancers.
- Host agents: introduced troshkad with authenticated TLS endpoints, background jobs, health checks, self-update, diagnostics, garbage collection, and real-time VM state updates.
- Hardware-style controls: added PXE and UEFI boot support, virtual BMC/Redfish integration, live boot-order display, serial access, VM file transfer, and offline guest-filesystem modification.
- Shared storage: added storage pools, shared caches, live and cold migration, migration certificates, pattern-buffer workers, storage warnings, and automatic or manual capacity extension.
- Cloud providers: expanded AWS provisioning and added OpenShift Virtualization, GCP, and Azure support, provider image discovery, managed storage integration, and Red Hat Image Builder workflows.
- OpenShift labs: added agent-based installation templates, version selection, health monitoring, pattern capture preparation, clock backdating, registry mirrors, and SNO re-certification integration.
- Lab delivery: added container and pod canvas nodes, AgnosticD integration, Showroom access, auto-stop/delete timers, multi-VM console views, and the Getting Started guide.
- App deployment: introduced backend/frontend container definitions, OpenShift manifests, deployment playbooks, SSO-related overlays, and runtime API proxying for standalone frontend builds.
