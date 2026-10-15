# Small CDI import CPU benchmark

This opt-in benchmark runs the real CDI S3 importer against a new, fully populated
2GiB QCOW2. It creates no VMs and changes no existing importer, DataVolume, golden
image, source disk or cluster-wide CDI setting. Rendering a manifest is offline;
only an explicit `oc apply` starts work.

## Recorded result

The sanitized [2026-10-15 measurements](results/2026-10-15.json) contain individual
runs, CPU usage and group summaries. Every import in each sequence used the same
72-core node, importer image, S3 endpoint and RBD Filesystem storage classes.

| CPU limit | Conversion mean | Conversion range | Average cores during conversion | Full import mean |
| --- | --- | --- | --- | --- |
| 750m, baseline | 12.244s | 12.039–12.450s | 0.745 | 26.283s |
| 2, baseline | 5.918s | 5.793–6.043s | 1.473 | 15.456s |
| 2, scaling | 6.049s | 5.876–6.222s | 1.472 | 16.178s |
| 4, scaling | 5.877s | 5.747–6.007s | 1.467 | 15.385s |
| 10, scaling | 5.652s | 5.572–5.732s | 1.463 | 14.312s |

The practical knee was **2 CPUs**: mean conversion was within 7.0% of the best
10-CPU result. Increasing 2→4 saved 2.8%; increasing 4→10 saved another 3.8%.
The 2/4 CPU ranges overlapped. Sustained conversion CPU use stayed near 1.47
cores; 4 and 10 had no measured throttling, versus 0.263s mean at 2 CPUs.
Four CPUs can provide burst headroom; these measurements do not justify ten.

These are two repeats per limit on a small, dense disk. Storage/network variation,
cache state and conversion duration affect the results. They do not establish a
default for sparse/compressed images or production disks hundreds of GiB in size.
CPU peaks use approximately 100ms samples, which can straddle CFS quota periods;
average cores are the sustained-utilization measure. Baseline runs did not sample
CPU peaks.

## Reproduce manually

Use a fresh benchmark name and an existing test namespace. The S3 Secret must
already exist in that namespace; its credentials need access to the chosen bucket.
Supply the production CDI importer image **by digest**, with `python3`, `qemu-img`,
`qemu-io`, and `/usr/bin/virt-cdi-importer`. The controller image needs Python 3,
`boto3` and `kubernetes`; the Troshka backend image is one suitable option. Images
are pulled only; this workflow builds or pushes nothing.

```bash
python3 infra/benchmarks/cdi-cpu/render_manifest.py \
  --namespace benchmark-test \
  --name cdi-cpu-unique-run \
  --node '<optional-selected-node>' \
  --importer-image '<CDI-importer-image>@sha256:<digest>' \
  --controller-image '<controller-image>@sha256:<digest>' \
  --endpoint 'https://s3.example.test' \
  --bucket benchmark-bucket \
  --secret existing-s3-credentials \
  --access-key-field accessKeyId \
  --secret-key-field secretKey \
  --storage-class '<production-RBD-filesystem-class>' \
  --scratch-storage-class '<production-RBD-scratch-class>' \
  --cpu-limits '2,4,10,10,4,2' \
  --output /tmp/cdi-cpu-benchmark.json

# Inspect the rendered resources before choosing to start the benchmark.
oc apply -f /tmp/cdi-cpu-benchmark.json \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig"
```

Omit `--node` to let Kubernetes choose the controller node; generator/importer
pods then use that same node. The default sequence is
`750m,2,4,10,10,4,2,750m`; the original baseline sequence was `750m,2,2,750m`.
Only one importer runs at a time. Requests remain 100m CPU; the supplied values
change each test container's CPU limit. Importer memory stays at the production
600M limit. Data/scratch PVCs are fresh for each run, with 3Gi/4Gi requested.
The generator uses its own 4Gi source PVC and sixteen 128MiB writes, avoiding
qemu-io's single-write size and buffer-memory limits. Its map output should show
all 2GiB as present, nonzero, data and uncompressed.

## Background execution and measurements

The controller is a Kubernetes Job, so it survives a disconnected terminal or
chat. It uploads only `benchmarks/<name>/fully-populated-2g.qcow2`, then executes
the chosen import sequence. Kubernetes Secrets are referenced in pod environment
variables; generated manifests contain no credential values. Use a unique name
for every invocation, including retries, to avoid reusing another run's objects.

```bash
oc get job,pods -n benchmark-test -l troshka.io/benchmark=cdi-cpu-unique-run \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig"
oc logs job/cdi-cpu-unique-run -n benchmark-test \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig"
oc get configmap cdi-cpu-unique-run-results -n benchmark-test -o json \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig" > /tmp/cdi-cpu-results.json
```

The results ConfigMap retains per-pod logs/status and `results.json`. Conversion
is CDI's `New phase: Convert`→`New phase: Resize` interval, including validation.
Transfer is `TransferScratch`→`Convert`; **MB/s is decimal** (1,000,000 bytes/s)
using the uploaded QCOW2 object's actual byte count. Full-import time excludes
pod scheduling. Phase snapshots record cgroup CPU time and throttling; average
cores equal CPU seconds divided by phase wall seconds. The wrapper also samples
CPU usage every 100ms during conversion. CPU summaries require Linux cgroup v2.

## Cleanup

After success or a handled failure, the controller deletes its generator/importer
pods, run PVCs and unique S3 object, and requests deletion of its source PVC. Save
the results first, then remove the completed Job to release the controller pod's
source-PVC mount and finish PVC deletion:

```bash
oc delete job,serviceaccount,role,rolebinding cdi-cpu-unique-run -n benchmark-test \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig"
oc get pods,pvc -n benchmark-test -l troshka.io/benchmark=cdi-cpu-unique-run \
  --kubeconfig="$HOME/secrets/benchmark.kubeconfig"
```

Keep the results/scripts ConfigMaps for inspection or delete them after saving
sanitized results. An abrupt Job kill, node failure or deadline expiration can
bypass Python cleanup: inspect resources with the exact run label and remove
only those test resources and the exact `benchmarks/<name>/...` object. Temporary
RBAC is namespace scoped and should be removed with the Job. Completed recorded
runs were cleaned and their test objects verified absent with S3 HEAD 404.

Do not commit rendered manifests, raw runtime ConfigMaps/logs, credentials,
kubeconfigs, pod/PVC inventories or machine-local paths. The checked-in result
file intentionally contains only numeric measurements and public image provenance.
