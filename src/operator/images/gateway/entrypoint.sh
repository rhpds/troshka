#!/bin/bash
set -e

echo 1 > /proc/sys/net/ipv4/ip_forward
echo 0 > /proc/sys/net/ipv4/conf/all/rp_filter

# Assign gateway IPs to secondary interfaces
IFS=',' read -ra ADDRS <<< "${GATEWAY_ADDRS:-}"
idx=1
for addr in "${ADDRS[@]}"; do
  iface="net${idx}"
  if [ -n "$addr" ] && ip link show "$iface" >/dev/null 2>&1; then
    ip addr add "$addr" dev "$iface" 2>/dev/null || true
    ip link set "$iface" up
    echo 0 > "/proc/sys/net/ipv4/conf/$iface/rp_filter"
    echo "Assigned $addr to $iface"
  fi
  idx=$((idx + 1))
done

nft add table inet nat
nft add chain inet nat postrouting '{ type nat hook postrouting priority 100 ; }'
nft add rule inet nat postrouting oifname "eth0" masquerade

nft add table inet filter
nft add chain inet filter forward '{ type filter hook forward priority 0 ; policy accept ; }'

echo "Gateway ready: NAT on eth0, forwarding enabled"

exec sleep infinity
