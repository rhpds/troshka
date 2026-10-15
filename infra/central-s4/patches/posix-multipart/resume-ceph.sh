#!/usr/bin/env bash
set -euo pipefail
trap 'rc=$?; printf "%s\n" "$rc" > /work/build.exit; exit "$rc"' EXIT
exec > >(tee -a /work/build.log) 2>&1
export CMAKE_BUILD_PARALLEL_LEVEL=16
export MAKEFLAGS=-j16
cd /work/src
test "$(git rev-parse HEAD)" = 3d0e4c36934c9146a9cd9b1c7f5b03f0cd9daa84
git apply --reverse --check /work/posix-multipart-pagination.patch
python3 /work/test-pagination.py src/rgw/driver/posix/rgw_sal_posix.cc
printf 'PHASE resume-configure %s\n' "$(date -u +%FT%TZ)"
cmake -S . -B build -DWITH_KRBD=OFF -DWITH_LIBCEPHFS=OFF -DWITH_LIBCEPHSQLITE=OFF -DWITH_PYTHON3=3
printf 'PHASE compile %s\n' "$(date -u +%FT%TZ)"
# Upstream standalone target omits the tracing header generation dependency.
# Build the existing tracing target explicitly; preserve the source-only fix.
ninja -C build -j16 rgw_op_tp
ninja -C build -j16 rgw-standalone rgw-standalone-admin
printf 'PHASE binary-ready %s\n' "$(date -u +%FT%TZ)"
build/bin/rgw-standalone --version
build/bin/rgw-standalone-admin --version
sha256sum build/bin/rgw-standalone build/bin/rgw-standalone-admin > /work/binaries.sha256
