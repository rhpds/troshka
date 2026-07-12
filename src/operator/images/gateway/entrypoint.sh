#!/bin/bash
set -e

nft add table inet nat
nft add chain inet nat postrouting '{ type nat hook postrouting priority 100 ; }'
nft add rule inet nat postrouting oifname "eth0" masquerade

nft add table inet filter
nft add chain inet filter forward '{ type filter hook forward priority 0 ; policy accept ; }'

echo "Gateway ready: NAT on eth0, forwarding enabled"

exec sleep infinity
