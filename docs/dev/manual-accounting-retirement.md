# Accounting after manual project deletion

Use this recovery only after an authorized manual deletion has removed the
Project row and left a finalized `NativeProjectNamespaceCleanup` record with
immutable `manual-legacy-retirement` evidence. Normal cleanup continues to use
automated storage settlement.

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
