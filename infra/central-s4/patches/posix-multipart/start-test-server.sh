#!/usr/bin/env bash
set -euo pipefail
mkdir -p /var/lib/ceph/radosgw/{db,buckets,tmp}
mkdir -p /var/lib/ceph/radosgw/db/rgw_posix_lmdbs
radosgw-admin user create --uid multipart-test --display-name 'Disposable multipart test' \
  --access-key "$AWS_ACCESS_KEY_ID" --secret-key "$AWS_SECRET_ACCESS_KEY" >/dev/null
exec /usr/bin/radosgw --cluster ceph -n client.rgw --no-mon-config \
  --default-log-to-stderr=true --err-to-stderr=true --foreground
