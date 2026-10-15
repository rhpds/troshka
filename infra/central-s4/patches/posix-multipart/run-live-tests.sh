#!/usr/bin/env bash
set -euo pipefail
task_dir=/var/lib/troshka/s4-multipart-build
trap 'rc=$?; printf "%s\n" "$rc" > "$task_dir/live.exit"; exit "$rc"' EXIT
podman_task() { podman --root "$task_dir/podman" --runroot /run/s4-multipart-build "$@"; }
live_dir=${S4_LIVE_DIR:-$task_dir/live-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$live_dir"
chmod 700 "$live_dir"
python3 - "$live_dir/credentials.env" <<'PY'
import os, secrets, sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    f.write('AWS_ACCESS_KEY_ID='+secrets.token_hex(12)+'\n')
    f.write('AWS_SECRET_ACCESS_KEY='+secrets.token_hex(24)+'\n')
PY
podman_task run --rm --security-opt label=disable --cpus=2 --memory=4g \
  -v "$task_dir:/work" localhost/s4-ceph-builder:deps \
  python3 -m pip install --quiet --target=/work/client-deps boto3
for mode in ${S4_TEST_MODES:-original candidate}; do
  image=quay.io/rh-aiservices-bu/s4@sha256:25f29f0953e17627bc3f8fb04f6be51a00b10a2599d3c8ab3a2bdb76511ebc90
  [[ "$mode" != candidate ]] || image=localhost/s4:ceph-posix-parts-3d0e4c3
  data_dir="$live_dir/$mode"
  mkdir -p "$data_dir"
  chown 1001:0 "$data_dir"
  container_name="s4-live-$mode-$(basename "$live_dir")"
  podman_task run -d --name "$container_name" --security-opt label=disable \
    --cpus=2 --memory=4g --pids-limit=1024 \
    --env-file "$live_dir/credentials.env" -p 127.0.0.1:17480:7480 \
    -v "$data_dir:/var/lib/ceph/radosgw" \
    -v "$task_dir/start-test-server.sh:/test-server.sh:ro" \
    --entrypoint /bin/bash "$image" /test-server.sh
  set +e
  podman_task run --rm --name "s4-client-$mode" --security-opt label=disable \
    --network=host --cpus=2 --memory=4g --pids-limit=1024 \
    --env-file "$live_dir/credentials.env" -e PYTHONPATH=/work/client-deps \
    -v "$task_dir:/work" localhost/s4-ceph-builder:deps \
    python3 /work/test-live-s3.py http://127.0.0.1:17480 "$mode" \
    > "$live_dir/$mode.log" 2>&1
  test_rc=$?
  set -e
  podman_task stop "$container_name" >/dev/null
  [[ "$test_rc" = 0 ]] || exit "$test_rc"
done
echo 'Isolated original red / patched green S3 regressions completed'
