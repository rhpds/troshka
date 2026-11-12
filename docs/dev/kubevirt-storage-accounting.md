# KubeVirt allocated RBD storage: reporting procedure

Use this procedure when the quota dialog shows **Allocated RBD storage**,
**Scope total unavailable**, **Measured subset**, **Last measured**, or missing
native coverage. This is observation-only accounting. It never enables native
quota enforcement, changes readiness, reservations, policy limits, or billing.
Provisioned PVC capacity remains separate from allocated storage and billing
continues to use provisioned capacity. No collector runs inside an HTTP request.

## Prerequisites and identity

Run from the repository root with the backend Python environment installed.
Use independently provisioned read-only Kubernetes and Ceph credentials. Do not
create secrets, widen permissions, or use workload mutation to obtain a report.
Kubernetes reads need namespace, TroshkaProject, PVC/PV, DataVolume and snapshot
discovery for the explicitly enrolled roots and trusted cache namespaces. Ceph
reads need the selected FSID/pools, immutable image identities, CSI journals,
parent/child links, image trash and snapshot inventory, and bounded merged `du`.

Direct collection and direct enrollment verification require installed `ceph`,
`rbd` and `rados` clients plus a read-only configuration/keyring. Toolbox collection
and enrollment verification instead use the existing independently pinned
`openshift-storage/rook-ceph-tools-*` pod/container, exact pod UID, FSID and pool
allowlist; `oc` and the remote `timeout --kill-after` command must be available.
The toolbox must already possess an appropriate read-only backend identity.
Do not describe an administrative toolbox identity as minimally privileged merely
because the collector issues read operations.

The standalone collector's inventory manifest follows
`scripts/collect_kubevirt_storage.py:Manifest`: collector ID, Ceph FSID, full project
UUIDs and immutable namespace/CR UIDs, trusted cache namespace UIDs, pool IDs and
names/CSI cluster IDs, and an exact client capability profile. Toolbox collection
also requires `ToolboxConfig`. Pin `command_version` to the actual `rbd --version`
output. Set `merge_snapshots_validated` only for an independently validated
profile, with its validation reference; a version string alone is insufficient.
Never infer ownership from eight-character namespace prefixes or build trusted
enrollment solely from a report submitted for import.

## Collect, import and inspect

1. Prepare, independently verify, and enroll explicit target identities using
   [Native RBD reporting](native-storage-reporting.md#explicit-target-and-independent-enrollment).
   That guide owns the exact `scripts/import_kubevirt_storage.py` commands,
   explicit database target variable/UUID, pins and immutable enrollment digest.
   Its verification step supports both direct and constrained toolbox transport;
   follow the linked toolbox procedure when local Ceph clients are unavailable.

2. Create the operator-owned inventory manifest for those enrolled roots and
   inspect every PVC/PV/CSI mapping. Supply literal argv values; the application
   offers no generic command execution, uploaded scripts, or report-ingestion API.
   Start with these read-only inventory checks for each full project namespace;
   compare `metadata.uid`, `spec.projectId`, PVC UID, bound PV UID and CSI identity
   with the independently pinned manifest. These reads do not replace the
   collector's full discovery and final identity recheck:

   ```bash
   oc get namespace <project-namespace> \
     --kubeconfig=/private/path/read-only.kubeconfig -o json
   oc get troshkaprojects -n <project-namespace> \
     --kubeconfig=/private/path/read-only.kubeconfig -o json
   oc get pvc -n <project-namespace> \
     --kubeconfig=/private/path/read-only.kubeconfig -o json
   oc get pv <bound-pv-name> \
     --kubeconfig=/private/path/read-only.kubeconfig -o json
   ```

   Example direct collection (paths below are operator-controlled files):

   ```bash
   src/backend/venv/bin/python3 scripts/collect_kubevirt_storage.py \
     --inventory /private/path/native-inventory.json \
     --kubeconfig /private/path/read-only.kubeconfig \
     --transport direct \
     --ceph-conf /private/path/ceph.conf \
     --ceph-keyring /private/path/read-only.keyring \
     --output /private/path/native-report.json
   ```

   For an already pinned toolbox manifest:

   ```bash
   src/backend/venv/bin/python3 scripts/collect_kubevirt_storage.py \
     --inventory /private/path/native-toolbox-inventory.json \
     --kubeconfig /private/path/read-only.kubeconfig \
     --transport toolbox \
     --previous-report /private/path/previous-native-report.json \
     --output /private/path/native-report.json
   ```

   Omit `--previous-report` on the first collection. Subsequent scans carry
   forward retained project/cache identities so PVC deletion does not erase
   allocation. Output is atomically written with private permissions. Exit 0
   means complete collector evidence, 2 means an emitted incomplete report, and
   1 means initialization failed. Exit 0 alone does not establish freshness or
   enforcement readiness. Preserve incomplete attempts; do not replace errors
   with zero or discard a failed attempt to revive an older complete total.

3. Import through the existing guide's
   [import procedure](native-storage-reporting.md#import-and-read-behavior), with
   explicit target UUID, enrollment ID/digest and private regular report file.
   Import reconstructs graph attribution and preserves original start,
   completion and observation timestamps. It does not collect new measurements.
   An old report imported now remains old; identical replay does not refresh it.

4. Open the existing project quota dialog or organization/user quota section.
   The independent endpoint is
   `GET /api/v1/quotas/{project|user|organization}/{id}/native-storage`.
   Verify original UTC source time, covered/expected native projects, and every
   bound PVC against the collected identity set. The UI shows rounded GiB with
   exact bytes in accessible labels/tooltips. Either endpoint's 403/404 clears
   both native and quota data and the editor, including in-flight older results.
   Other report failures leave policy/baseline state independent.

## What the numbers mean

The metric is `rbd_allocated_extent_bytes`: logical allocated image/retained
snapshot extents using the validated merged-`du` whole-object semantics, with
parent inclusion off. It excludes replication and raw device consumption; it
does not measure unique BlueStore bytes, compression savings or physical disk
headroom. Private exclusive ancestors are added once to their project; verified
shared cache is platform-owned, counted once per pool, and excluded from each
project/user/organization total. Missing ownership remains unresolved.

| State | Interpretation |
| --- | --- |
| `complete`, allocated `0` | A real measured zero for the covered native projects. |
| `partial` | Authoritative total is null. **Measured subset** is only known allocation; missing images/projects can add bytes. |
| `stale` | Authoritative total is null. **Last measured** retains the original observation time and known bytes; it is not current headroom. |
| `unavailable` | No authoritative total. Missing measurements never become zero. |
| Expected projects `0` | No native projects in this scope; no zero-usage or unlimited-capacity claim. |

Reporting becomes stale after **15 minutes from the oldest source evidence**,
including scan start and image observations. Coverage counts complete, fresh
projects, not PVC count. Provisioned PVC capacity is separate and may also be
null. Admin-only platform and unresolved-retained diagnostics span selected
collection domains; they are not scope allocations and must never be added to
the displayed scope total. Deleted projects have no trusted deletion-time owner
snapshot, so retained ambiguity blocks user/org totals conservatively while
keeping a known live subset. Current owner/org transfers change current
aggregation without rewriting historical collector evidence.

## Troubleshooting and material acceptance limits

Missing or invalid fast-diff can make reads slow. The collector supervises fixed
argv with per-command timeout, scan deadline, bounded output and object/depth
limits. `timeout`, `scan_limit` and `output_limit` remain typed missing evidence;
do not remove limits or increase them merely to call an old result fresh.
The default scan timeout is 600 seconds. A separately authorized diagnostic
scan with a longer bound took **21m27s** (2026-11-11T22:56:36Z–23:18:03Z), found
66 project PVCs and 58 cache PVCs, and recorded 99 complete/35 unsupported image
observations with three partial project totals. It ran older collector source
before review fixes; it was not imported and is not final acceptance evidence.
That duration exceeds reporting's 15-minute freshness budget and does not satisfy
the separate 60-second enforcement freshness requirement.

Unsupported retained snapshots are a distinct correctness gap, not simply slow
`du`. One bounded sample contained six trash-namespace snapshots originally from
the user namespace and an ID-based children query found a live dependent clone.
This proves a retained-snapshot gap for that sample; it does not establish the
cause of all 35 unsupported images. Upstream v19.2.1
[DiskUsage.cc](https://github.com/ceph/ceph/blob/v19.2.1/src/tools/rbd/action/DiskUsage.cc#L174)
filters non-user snapshot namespaces. The public
[librbd starting-snapshot diff interface](https://github.com/ceph/ceph/blob/v19.2.1/src/include/rbd/librbd.h#L958)
uses a user snapshot name; setting the ending snapshot by ID does not provide an
ID-aware starting diff. See the
[implementation](https://github.com/ceph/ceph/blob/v19.2.1/src/librbd/librbd.cc#L2624)
and [Python binding](https://github.com/ceph/ceph/blob/v19.2.1/src/pybind/rbd/rbd.pyx#L3937).
Permitting trash rows in a parser or summing independently measured snapshots
cannot establish complete retained-version accounting. Keep those totals null
until a version-pinned, read-only, ID-aware adapter and full graph traversal are
implemented and independently validated. No such adapter ships in this workflow.

A bounded disposable USER-snapshot experiment on downstream Ceph
`19.2.1-423.el9cp`, commit `cb76ca76a78ea7346ab574122ab21a208cf75df0`, used 64 MiB
images and aligned 4 MiB writes. Base+two untouched clones measured 4/0/0 MiB;
a clone write measured 4/4/0 MiB; snapshot+overwrite measured 4/8/0 MiB; removing
that snapshot restored 4/4/0 MiB. All experiment images/snapshots were removed and
ENOENT verified. This supports parent exclusion and merged USER-snapshot
retention on that exact profile only. It does not validate trash/mirror snapshot
namespaces, every live ancestor, CSI retention lifecycle, or enforcement.

Replay tests use sanitized child → private zero parent → shared golden evidence
through the real temporary SQLite importer and FastAPI route, then compare the
frontend response fixture. The production full-PVC reconciliation/freshness gate
remains **unrun for the final reviewed implementation**. Leave native enforcement
unavailable and all existing quota enablement settings unchanged.

## References

- [Operator enrollment/import and authorization contract](native-storage-reporting.md)
- [Collector and its fixed manifest schema](../../scripts/collect_kubevirt_storage.py)
- [RBD graph tests and replay evidence](../../src/backend/tests/fixtures/kubevirt_storage/zero-private-parent.json)
- [Ceph Squid Python API](https://docs.ceph.com/en/squid/rbd/api/librbdpy/)
