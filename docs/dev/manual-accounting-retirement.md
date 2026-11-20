# Accounting after manual project deletion

Prefer the automatic map-based delete path: metering stops when delete is
accepted, the Project row is released after namespace absence, and known private
RBD images are reclaimed asynchronously on the durable cleanup row
(`orphan_reclaim` → `finalized`). Collector settlement reports are optional
audit, not a gate.

Use this recovery only when an authorized manual deletion already removed the
Project row and left a finalized `NativeProjectNamespaceCleanup` record with
immutable `manual-legacy-retirement` evidence (legacy cases that never got a
teardown map).

The explicit operator service checks the original namespace, PVs, snapshot
contents, complete RBD directory/trash absence for every private image, and
continued shared-image availability. It checks the protected collector's cluster
and backend identities and refuses changed predecessors or unknown storage debt.
Only `storage_coverage_incomplete` can be resolved through this path; missing
image identities or unresolved producer outcomes require investigation.
This recovery supports retained binding, ancestry, and snapshot liabilities;
restore producers are conservatively rejected.

Run in the application's collection-worker environment, with its protected
collector configuration and credentials:

```python
from app.services.native_manual_retirement import reconcile_configured

result = reconcile_configured(
    cleanup_id,
    administrator="operator identity",
    reason="reference to the authorized manual cleanup",
    apply=False,
)
```

Review the dry-run result, then repeat with `apply=True`. Apply repeats all reads
and appends an immutable `manual-storage-verified` receipt; it performs no provider
mutations. Repeating a completed reconciliation returns `already_verified`.
The original cleanup, manual evidence, and automated settlement fields remain
unchanged. Accounting consumes the separate receipt through `settled_root_keys`.

Run `scripts/collector_release_check.py --mode fresh` for the affected application
after reconciliation. Successful receipt creation alone does not establish that
the rest of the domain's accounting is healthy. Never delete enrollment history
or manufacture an automated settlement report to pass a release gate.
