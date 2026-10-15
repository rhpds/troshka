#!/bin/sh
# Two GiB of allocated, nonzero, uncompressed QCOW2 data. No guest is involved.
set -eu
disk=${1:-/data/benchmark.qcow2}
qemu-img create -f qcow2 "$disk" 2G
i=0
while [ "$i" -lt 16 ]; do
    # qemu-io allocates a buffer for each write; keep it below the memory cap.
    qemu-io -f qcow2 -c "write -P 0xa5 $((i * 134217728)) 134217728" "$disk"
    i=$((i + 1))
done
qemu-img info --output=json "$disk"
qemu-img map --output=json "$disk"
