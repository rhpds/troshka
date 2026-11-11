# RBD COW measurement validation

## Tested semantics

On 2026-11-11, a disposable-image experiment verified the meaning of per-image
`rbd du --merge-snapshots --format json` on the deployed Ceph client and cluster:

```text
ceph version 19.2.1-423.el9cp (cb76ca76a78ea7346ab574122ab21a208cf75df0) squid (stable)
```

The measured quantity is **logical allocated RBD extents**, including retained
user-snapshot extents. It excludes inherited parent extents. It does not measure
raw device consumption, replication overhead, compression savings or provisioned
PVC capacity. A successful zero result is valid; a failed query is not zero.

## Procedure and observations

Three uniquely named disposable thin images were created in the existing RBD
pool. Each advertised 64 MiB. The base enabled layering, exclusive-lock,
object-map, fast-diff and deep-flatten. A single sequential 4 MiB write populated
the base before taking a protected snapshot and creating two clones.

All measurements used the command above separately for each image. The JSON
aggregate's `id` was retained with its `used_size`; `provisioned_size` remained
67,108,864 bytes for every image.

| State | Base allocation | Clone A allocation | Clone B allocation |
| --- | ---: | ---: | ---: |
| Base populated, both clones untouched | 4,194,304 | 0 | 0 |
| Write 4 MiB to clone A | 4,194,304 | 4,194,304 | 0 |
| Snapshot A, overwrite the same extent | 4,194,304 | 8,388,608 | 0 |
| Remove A's retained snapshot | 4,194,304 | 4,194,304 | 0 |

This demonstrates why shared ancestors must be attributed separately and counted
once, and why snapshots cannot simply be excluded from a project's allocation.
It also confirms that a merged image aggregate already includes user snapshots;
adding separate snapshot measurements would double-count them.

All test snapshots and images were removed. Independent `rbd info` checks for
each test image returned ENOENT, rather than an authentication error or timeout.
No existing project volume was modified. No quota baseline was imported and
enforcement was not enabled.

## Limits of this evidence

This validates the measurement primitive on the exact version above. It does not
validate hidden, mirror or trash snapshot namespaces, CSI deletion/reclaim,
complete live-project inventory, workload shutdown, or quota enforcement. The
collector must reject unsupported snapshot semantics and incomplete ownership
evidence. Other versions require their own validation; this record is not a
wildcard capability declaration.

The experiment used fast-diff images. Separate read-only observations of existing
CSI intermediate images without fast-diff took minutes. A timeout must preserve
an unavailable result and must not be treated as evidence of no allocation.
