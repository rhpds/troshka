# Temporary production source overlays

For a targeted Python debugging fix, an immutable ConfigMap can mount individual
source files over files in an existing container image. Explain this deployment
method to the user before applying it. A normal image build remains the permanent
delivery path; an overlay is temporary operational state.

## Prepare and apply

1. Identify the exact cluster, namespace, workload, deployed image digest, source
   path, and current mounts. Use explicit `oc --kubeconfig=$HOME/secrets/<file>`.
   Save the original Deployment specification and rollback patch privately.
   Do not print credentials or clone-token annotations.
2. Reproduce the bug and run focused regression tests. Compare the deployed
   module against the intended baseline. If local code contains unrelated edits,
   start from the deployed file and apply only the intended change. Verify that
   the remaining functions are identical (AST comparison is useful).
3. Create a uniquely named, immutable ConfigMap containing only required source
   modules. Never include secrets. Mount each file read-only using `subPath` at
   its exact runtime location. Keep existing hotpatch modules and mounts intact.
   Do not replace the entire source directory.
4. Inspect active background jobs before restarting backend or worker pods. Wait
   for unrelated jobs rather than interrupting them. Python processes retain
   already imported functions: mounting a file does not update a running job.
   ConfigMap `subPath` updates also require replacement pods; create a new
   ConfigMap revision and roll out the affected workload.
5. Verify rollout readiness, runtime file hashes, module imports, relevant
   behavior, and RQ worker registration. Confirm the actual project job is
   `started`, its worker exists, and deployment progress continues after backend
   restarts. Initialize Redis before inspecting `get_job_info` in a fresh process.

## Deployment-monitor handoffs

CDI disk imports and Kubernetes VMs continue independently of Troshka's RQ
monitor. A monitor restart should attach to existing resources rather than run
the full deployment flow, which may regenerate credentials or recreate work.

Use the tested `resume_kubevirt_deploy_monitor` entry point for interrupted native
KubeVirt deployments. It validates the existing CR, restores progress, polls it,
and runs the normal completion hooks. Enqueue through RQ with project/host
metadata, a 10800-second timeout, and a matching project job-reference TTL.
Never use an untracked `oc exec` Python process as the long-term monitor.

**Known handoff limitation:** stopping the old job before enqueueing its replacement
leaves a gap in `job:project:<id>`; the project timer can mark a still-running
cluster deployment as "Background job lost while deploying — please retry".
Do not treat that as a failed disk import or redeploy automatically. Verify the
operator CR and resume tracking. A future atomic handoff must also prevent the
old success callback from deleting the replacement job reference. The recovery
entry point by itself does not implement that atomic handoff.

## Rollback and permanent delivery

Restore the previous ConfigMap reference and mounts, roll out, and verify runtime
hashes and job tracking again. Keep old ConfigMaps until rollback is no longer
needed. Preserve PVCs, credentials, and guest contents.

When a tested image contains the fixes, remove the source overlays and their
volumes/mounts as part of that image rollout. Check GitOps desired state so a sync
does not unexpectedly revert an overlay or retain one over a newer image.
Record which image digest and overlay revision are actually running.

## Running importer CPU limits

Before restarting an expensive CDI import to change CPU limits, check whether
the target cluster exposes `pods/resize`. Validate the proposed resource-only
patch with `--subresource=resize --dry-run=server`, then verify a disposable pod
on the same node reaches the new cgroup CPU quota without a process restart.
Do not assume that API discovery or a successful dry run proves kubelet support.

For an authorized live resize, preserve memory, CPU requests, and QoS class.
Verify the applied container-status resources and `/sys/fs/cgroup/cpu.max`,
unchanged pod/container identity, zero additional restarts, and continuing
conversion logs. Delete the probe afterward. This changes one running pod;
future importer defaults require a separate, appropriately scoped change.
