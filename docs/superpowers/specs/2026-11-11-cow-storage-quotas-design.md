# COW-aware storage quotas

User approval: “do it”, following the proposal to count actual allocated blocks,
retain project-owned snapshots/files, count shared bases once at platform scope,
and keep provisioned capacity separate. Existing quota/UI changes must survive.

## Behavior

- Storage quota usage is allocated filesystem bytes, not qcow2 virtual capacity
  or sparse file length. Shared immutable bases are platform storage, counted
  once, not charged repeatedly to project owners. Retained project files remain
  counted while compute is stopped. Provisioned capacity is shown separately.
- Trusted agent measurements include identity, original observation time,
  completeness, project-owned files and storage-pool capacity/free space. Never
  convert missing/unsupported/incomplete telemetry to zero. Do not follow unsafe
  paths, infer ownership from arbitrary prefixes, or mutate disks to measure.
- Admission reserves bounded initial allocation overhead rather than full
  virtual capacity and checks current physical usage plus pending reservations.
  Preserve virtual-size/provider command validation separately. Old agents and
  baselines cannot silently use virtual values as physical usage.
- Guest writes can grow confirmed allocations without API requests. Reconcile
  trusted fresh measurements with generation/attribution/operation locking;
  ordinary compute confirmation must not gain an unrestricted growth bypass.
- Reevaluate project/user/organization storage allowance as physical usage grows,
  block additional growth and stop relevant compute on exhaustion. Preserve
  storage and never auto-restart. Use durable existing stop/fence/retry machinery
  or an equally explicit serialized contract; do not mislabel storage as spending.
- Check host/pool headroom and reserve space for safe operation. Polling is
  best-effort: guest writes may overshoot between observations; this is not a
  filesystem hard quota. Document timing, conservative bounds and prerequisites.
- Existing unsupported providers, captures and ambiguous storage ownership stay
  denied/unready; do not expand support implicitly. Existing retained unsupported
  resources cannot be hidden or omitted to establish readiness.
- Existing calculated prices/charge history are not silently repriced. Clearly
  distinguish physical quota quantities from existing billing units.
- Default quota enforcement remains off. No live agent rollout, application DB
  migration, enablement, commit, push or deployment in this task.

## Verification

Sparse/COW files with large virtual capacity and small allocated blocks; shared
base deduplication; hardlinks/path escapes/missing evidence; retained stopped
files; admission concurrency and initial reservation; spontaneous growth;
deletion/redeploy generation races; storage exhaustion/fencing/retry; pool low
space; legacy baseline rejection; UI units and unchanged exact money semantics.
Use real sparse-file stat observations and isolated databases where practical,
fake provider transport for integration. Existing relevant suites must pass.
