#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; printf "%s\n" "$rc" > /work/build.exit; exit "$rc"' EXIT
exec > >(tee -a /work/build.log) 2>&1
export CMAKE_BUILD_PARALLEL_LEVEL=16
export MAKEFLAGS=-j16
export CEPH_GIT_REF=3d0e4c36934c9146a9cd9b1c7f5b03f0cd9daa84
printf 'PHASE bootstrap %s\n' "$(date -u +%FT%TZ)"
dnf install -y git-core patch python3 dnf-plugins-core epel-release
dnf config-manager --set-enabled crb
mkdir -p /work/src
cd /work/src
if [[ ! -d .git ]]; then
  git init
  git remote add origin https://github.com/ceph/ceph.git
  git fetch --depth=1 origin "$CEPH_GIT_REF"
  git checkout --detach FETCH_HEAD
fi
test "$(git rev-parse HEAD)" = "$CEPH_GIT_REF"
git submodule update --init --depth=1 --jobs=4 -- \
  src/erasure-code/jerasure/jerasure src/erasure-code/jerasure/gf-complete \
  src/rocksdb src/googletest src/xxHash src/isa-l src/zstd \
  src/crypto/isa-l/isa-l_crypto src/blkin src/fmt src/c-ares \
  src/s3select src/libkmip src/utf8proc src/BLAKE3
printf 'PHASE dependencies %s\n' "$(date -u +%FT%TZ)"
WITH_CRIMSON= FOR_MAKE_CHECK=0 ./install-deps.sh
printf 'PHASE patch-and-regression %s\n' "$(date -u +%FT%TZ)"
python3 /work/test-pagination.py src/rgw/driver/posix/rgw_sal_posix.cc > /work/regression-before.txt && { echo 'Expected original regression failure'; exit 1; } || test "$?" = 1
git apply /work/posix-multipart-pagination.patch
python3 /work/test-pagination.py src/rgw/driver/posix/rgw_sal_posix.cc | tee /work/regression-after.txt
git diff --check
printf 'PHASE configure %s\n' "$(date -u +%FT%TZ)"
./do_cmake.sh -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
  -DWITH_RADOSGW=ON -DWITH_RADOSGW_DBSTORE=ON -DWITH_RADOSGW_POSIX=ON \
  -DWITH_RADOSGW_RADOS=OFF -DWITH_RADOSGW_STANDALONE=ON \
  -DWITH_RADOSGW_SELECT_PARQUET=OFF -DWITH_RADOSGW_D4N=OFF \
  -DWITH_RADOSGW_LANCEDB=OFF -DWITH_RBD=OFF -DWITH_KRBD=OFF -DWITH_CEPHFS=OFF \
  -DWITH_LIBCEPHFS=OFF -DWITH_LIBCEPHSQLITE=OFF \
  -DWITH_MGR=OFF -DWITH_MGR_DASHBOARD_FRONTEND=OFF \
  -DWITH_RDMA=OFF -DWITH_SPDK=OFF -DWITH_TESTS=ON \
  -DWITH_PYTHON3=3 -DWITH_CEPHFS_JAVA=OFF -DWITH_MANPAGE=OFF
printf 'PHASE compile %s\n' "$(date -u +%FT%TZ)"
ninja -C build -j16 rgw_op_tp
ninja -C build -j16 rgw-standalone rgw-standalone-admin
printf 'PHASE binary-ready %s\n' "$(date -u +%FT%TZ)"
build/bin/rgw-standalone --version
build/bin/rgw-standalone-admin --version
sha256sum build/bin/rgw-standalone build/bin/rgw-standalone-admin > /work/binaries.sha256
