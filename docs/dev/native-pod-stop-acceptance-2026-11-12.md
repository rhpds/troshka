# Scoped bare Pod maintenance acceptance — 2026-11-12

Corrective revision `b6217cfd` passed isolated development acceptance on
ocpvdev01. This validates an explicit graceful request for a diskless,
Project-owned Pod; it does not enable native quotas or establish complete
project shutdown, persistent-storage preservation under concurrent writers,
physical process termination, settlement or capacity release.

## Scope and failed first attempt

The dedicated namespace was `troshka-pod-stop-20261112`, UID
`95f12d37-3a45-4a52-bb34-529cae3934c8`. Its synthetic `stop-fixture`
TroshkaProject had UID `b1d61abb-d03b-4f11-93c0-aed96925ae03`, generation 1,
empty topology and action `destroy`. No existing project, image, pattern,
storage or ownership was changed. The fixture had no PVC, DataVolume, VM or VMI.

The first attempt at `ce7ab2d0` **failed**. Default dry-run incorrectly deleted
`stop-demo`, UID `369b3264-dbbf-4c01-9f5b-883de1dd9395`. Its resource version
changed from `2670195696` to `2670196700`, with deletion timestamp
`2026-11-13T00:22:51Z`, despite the journal recording `dry_run_accepted`.
No explicit apply was run against that target. The original erroneous journal
is preserved; it is not passing evidence.

Kubernetes decodes a nonempty DeleteOptions body instead of URL options.
The correction places `dryRun: ["All"]` in that body and strips inherited
dry-run values for apply. A fresh root-verified Pod GET must also show the
original UID/resource version and no deletion timestamp before dry-run success.
See the [maintenance guide](native-vm-halt.md) for the upstream source and
supported ownership/storage restrictions.

## Corrected acceptance

Independent review of `ce7ab2d0..b6217cfd` found no actionable issues.
The focused Pod/controller/VM/observer suite passed 203 tests. Normal commit
hooks passed, including full-backend Pyright, Black, ruff and secret scans.

A distinct diskless Pod, `stop-demo-fixed`, used UID
`4034bb5b-c12b-4d36-8307-e0c3be264ee4`, resource version `2670215374`, and
the exact Project controller owner reference. It ran a tiny UBI container on
`ocp-virtdev1-host4`, with no service-account token, no persistent volumes,
non-root execution, RuntimeDefault seccomp and all capabilities dropped.

1. Default server dry-run returned `dry_run_accepted`. An independent API read
   confirmed the same UID and resource version, Running state and no deletion
   timestamp.
2. Explicit apply sent one graceful DELETE using the same pinned identity.
   The command returned exit 2, `incomplete`, with `delete_request_accepted`
   because the Pod still existed during its immediate follow-up. This is the
   expected conservative result, not a reason to retry.
3. The same Pod was observed terminating with default 30-second grace and no
   finalizers. A bounded `oc wait --for=delete` then succeeded. No second DELETE,
   force deletion or finalizer removal was used.
4. A final namespace inventory contained no Pods, PVCs, DataVolumes, VMs or VMIs.
   The dedicated fixture namespace was deleted and its absence verified.
5. All ClusterOperators remained Available, with Available/Degraded conditions
   identical to the baseline. The pre-existing machine-config degradation
   remained. Admission and comparison deployments in the earlier quarantine
   test namespaces both remained 2/2 available.

All report flags for physical stop, settlement, capacity release and storage
absence remained false. API absence was observed separately after the apply
journal; the original journal was not rewritten to claim a stronger result.

## Evidence and limits

Local private evidence is under `/private/tmp/troshka-pod-stop-acceptance`:
original `dry-run.jsonl`, corrected `dry-run-fixed.jsonl`, `apply-fixed.jsonl`,
exact manifests, before/after Pod reads, final inventory and operator baselines.
No credentials were exported. These temporary local files are not a durable
repository artifact; this report records the bounded outcome and identities.

The supplied development kubeconfig disables TLS verification; its warnings
were retained. This is not production credential-isolation or transport-security
acceptance. The empty synthetic root provided a narrow maintenance fixture,
not an all-writer fence for normal projects. Persistent-storage cases remain
covered by focused tests, not this diskless live scenario. Full writer mediation,
controller quiescence, COW accounting and settlement gates remain open. No push,
production rollout, fleet configuration or native quota enablement occurred.
