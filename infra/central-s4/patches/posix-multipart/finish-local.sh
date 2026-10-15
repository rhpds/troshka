#!/usr/bin/env bash
set -euo pipefail
task_dir=/var/lib/troshka/s4-multipart-build
trap 'rc=$?; printf "%s\n" "$rc" > "$task_dir/package.exit"; exit "$rc"' EXIT
podman_task() {
  podman --root "$task_dir/podman" --runroot /run/s4-multipart-build "$@"
}
build_container=${1:-s4-multipart-builder-v3}
build_rc=$(podman_task wait "$build_container")
if [[ "$build_rc" != 0 ]]; then
  echo "Native build failed with exit $build_rc; packaging skipped"
  exit 1
fi
podman_task run --rm --name s4-multipart-packager --security-opt label=disable \
  --cpus=4 --memory=8g --pids-limit=1024 -v "$task_dir:/work" \
  localhost/s4-ceph-builder:deps python3 /work/package-runtime.py
podman_task build --security-opt label=disable --cpu-quota=400000 --cpu-period=100000 --memory=8g \
  -t localhost/s4:ceph-posix-parts-3d0e4c3 "$task_dir/candidate"
podman_task image inspect localhost/s4:ceph-posix-parts-3d0e4c3 \
  --format '{{.Id}} {{.Digest}}' > "$task_dir/candidate-image.txt"
podman_task run --rm --security-opt label=disable --network=none \
  --entrypoint /bin/sh localhost/s4:ceph-posix-parts-3d0e4c3 \
  -c 'ldd /usr/bin/radosgw; ldd /usr/bin/radosgw-admin; /usr/bin/radosgw --version; /usr/bin/radosgw-admin --version' \
  > "$task_dir/candidate-runtime.txt" 2>&1
if grep -q 'not found' "$task_dir/candidate-runtime.txt"; then
  echo 'Candidate has unresolved shared libraries'
  exit 1
fi
echo 'Local candidate packaged; S3 integration validation remains required'
