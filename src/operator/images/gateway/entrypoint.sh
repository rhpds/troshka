#!/bin/bash
set -e

echo 1 > /proc/sys/net/ipv4/ip_forward

nft add table nat
nft add chain nat postrouting '{ type nat hook postrouting priority 100 ; }'
nft add rule nat postrouting oifname "eth0" masquerade

echo "Gateway NAT active, forwarding enabled"

exec sleep infinity
