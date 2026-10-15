# S4 POSIX multipart pagination fix

## Upstream follow-up: retire the 900-part workaround

**Open follow-up:** watch upstream [S4](https://github.com/rh-aiservices-bu/s4)
releases and its bundled Ceph RGW POSIX driver for this pagination fix. Troshka
is the only client of this S4 instance, so the production plan is to keep the
upstream S4 image with Troshka's 900-part safeguard. The custom image below is
an isolated diagnostic candidate, not a production dependency.

As checked on October 14, 2026, upstream `latest` matches the deployed image
digest `sha256:25f29f0953e17627bc3f8fb04f6be51a00b10a2599d3c8ab3a2bdb76511ebc90`
(built June 11, 2026) and still exhibits the bug. A newer tag alone is not
evidence of a fix. Before retiring the safeguard:

- Confirm a released S4 image actually contains the corrected RGW POSIX
  multipart pagination code. A Ceph source fix without a rebuilt S4 image is
  insufficient; record the tested image digest and source/release evidence.
- Run the isolated 999-, 1000-, and 1001-part tests against that upstream image.
  Verify completion, object length, streaming checksums, cross-part range reads,
  and multipart abort cleanup. Keep a regression beyond 1000 parts.
- Deploy the validated upstream image through the normal reviewed rollout.
  Only then remove the temporary 900-part cap from
  `src/backend/app/services/pattern_sync.py` and adjust its derived chunk sizing,
  size validation, and regression tests together.
- Retain normal S3 limits, memory bounds, size-aware deadlines, and failure
  reporting. Those safeguards remain useful after the pagination bug is fixed.

This is a repository follow-up, not an automated release monitor or an upstream
issue submission. No periodic check has been scheduled.

## Root cause

The deployed Ceph POSIX driver's `POSIXMultipartUpload::list_parts()` keeps
the previous page in its `parts` map. Completion walks pages of 1000 parts,
so page two compares requested part 1001 with stale uploaded part 1 and
returns `InvalidPart`. The patch adds only `parts.clear()` before listing.

`POSIXBucket::list()` sets `is_truncated` when `count >= max`; this behavior
is reproduced in the focused C++ regression. No truncation behavior changes
are included.

## Source and candidate provenance

- Official source: `ceph/ceph`, pinned commit
  `3d0e4c36934c9146a9cd9b1c7f5b03f0cd9daa84`.
- Runtime base: deployed S4 image
  `quay.io/rh-aiservices-bu/s4@sha256:25f29f0953e17627bc3f8fb04f6be51a00b10a2599d3c8ab3a2bdb76511ebc90`.
- Builder base: CentOS Stream 10,
  `quay.io/centos/centos@sha256:0487af11228a5e4defa56e6c65a59644282963bbcf5e0791cbb10325e603c609`.
- Local candidate tag: `localhost/s4:ceph-posix-parts-3d0e4c3`.
- Local image ID:
  `bc8641310a910073e81216709b23faaf80516afd4a750215d3ed0af728ea32f6`.
- Local manifest digest:
  `sha256:46ffb1bc29709225f9f0bc58608d48289e51ce1702ad060b90fd787cae57a67a`.
- Gateway SHA256:
  `281bf35549b278cb66277febb211b07aaff44380cd596d5d9519da3ef6c9a9ba`.
- Admin SHA256:
  `3b10251c2f57c5f7931c44af18a2f45eeb69d8ececd9cf207786702f9aacc320`.

The production gateway reports SHA
`50b3ba3c13208fe1f25875c17be79ca96f85709d`, which was not resolvable in
the public upstream or likely author forks. This candidate therefore uses a
different, verifiable development baseline. It is not a rebuild of identical
production sources or a production Ceph upgrade. Its reported version is
`umbrella (dev - Release)` with the pinned source SHA.

## Build and verification scripts

Run builds natively on amd64. The verified build used an isolated Podman
storage root on an owned host, without changing its existing VMs or normal
container storage. All files in this directory are sanitized source/scripts;
credentials, binaries, build caches and test data stay outside the repository.

1. Stage these files in a fresh host task directory, mounted at `/work` in
   the builder. The recorded task directory was
   `/var/lib/troshka/s4-multipart-build`.
2. Start a CentOS Stream 10 builder with `bash /work/build-ceph.sh`.
   Use `--cpus=16 --memory=64g --pids-limit=4096`, low CPU/I/O priority,
   and a dedicated Podman `--root`/`--runroot`. On the verified host,
   SELinux blocked the CentOS10 libc startup; `--security-opt label=disable`
   was used only for these test/build containers, with no host policy changes.
3. The build pins and verifies the official source revision, installs build
   dependencies inside the container, verifies the original regression fails,
   applies the patch, verifies the fixed regression, and builds
   `rgw-standalone` and `rgw-standalone-admin`.
4. `WITH_KRBD=OFF` accompanies `WITH_RBD=OFF`; `WITH_PYTHON3=3` is a version
   selector. Do not replace it with a boolean. `rgw_op_tp` must be built
   explicitly first: upstream attaches generated tracing-header dependencies
   to `rgw_common` but omits `rgw_common_standalone`.
5. `resume-ceph.sh` resumes an existing configured source/build tree without
   rebuilding from scratch. The recorded dependency container was committed
   locally as `localhost/s4-ceph-builder:deps`; it was never published.
6. `package-runtime.py`, run in the dependency environment with `/work`
   mounted, collects both binaries and their Ceph-built shared libraries.
   It also includes the dynamically loaded LTTng probe library.
   `Containerfile.candidate` layers these onto the pinned production S4 base.
7. `finish-local.sh CONTAINER_NAME` waits for the selected build container,
   packages a local image, and checks both binaries with `ldd` and `--version`.
   Podman **build** uses `--cpu-quota=400000 --cpu-period=100000`, not the
   unsupported `build --cpus` option. Runtime containers use `--cpus`.

The local candidate built successfully; both binaries pass version and shared
library checks against the pinned S4 runtime. It is **not a runnable or validated
S4 replacement**: actual startup testing failed as recorded below. No image was
published and no deployment manifest was changed.

## Regression tests

Focused source regression (requires a C++17 compiler):

```sh
python3 test-pagination.py /path/to/ceph/src/rgw/driver/posix/rgw_sal_posix.cc
```

The script compiles the actual extracted function body against a deterministic
bucket-list fake. Original code fails 1001, 1002, 2000, 2001 and 10000 parts;
the patch passes those plus 1, 999 and 1000 parts.

`run-live-tests.sh` performs actual S3 checks on the owned host. It uses a
fresh timestamped `live-*` directory and generates random credentials there with mode 0600;
it refuses to overwrite an existing credential file. Test servers bind only
`127.0.0.1:17480`, with separate disposable storage and limits of 2 CPUs and
4 GiB each. The client also has a 2-CPU/4-GiB limit. No production PVC or
credentials are used.

The original pinned S4 image must fail completion of 1001 uploaded 5-MiB
parts with `InvalidPart`. The candidate must complete 999, 1000 and 1001
5-MiB parts, match HEAD byte counts and streaming SHA256, and return the exact
range across a part boundary. It also verifies completed-upload disappearance
and abort cleanup through the S3 API. Test objects and buckets are removed;
the stopped disposable containers and their separate directories remain for
inspection until explicitly cleaned up.

POSIX returns HTTP 404 `NoSuchKey` for `ListParts` after an abort; the test
accepts that existing response or HTTP 404 `NoSuchUpload`, and also requires
the upload to disappear from `ListMultipartUploads`.

The generated credentials and container logs must not be copied into Git.
Only sanitized result summaries belong here. Production deployment and image
publication require separate authorization after workload debugging.

## Observed live test results, 2026-10-15 UTC

The pinned original production S4 image reproduced the failure with **1001
successfully uploaded 5-MiB parts**: `CompleteMultipartUpload` returned
`InvalidPart`. A repeat run confirmed the same failure and passed abort cleanup
checks (the upload disappeared from listing and subsequent `ListParts` returned
HTTP 404).

The diagnostic custom candidate failed startup during `radosgw-admin user
create`: its `rgw_lc_1` lifecycle worker segfaulted, with the stack identifying
the DBStore vtable. The gateway never served the candidate test requests.
Consequently **999/1000/1001 completion, HEAD size, streaming checksum, range,
and abort tests were not validated against this candidate**. Version and `ldd`
checks alone did not establish runtime compatibility.

Further custom-image runtime debugging was stopped after choosing the upstream
S4 plus Troshka 900-part safeguard strategy described above. The local candidate
image, exact source checkout, compiled artifacts and build logs remain on the
owned builder for diagnosis. Disposable test containers, data and credentials
were cleaned up; production resources were never used by these tests.
