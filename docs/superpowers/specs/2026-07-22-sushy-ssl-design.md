# Sushy SSL/TLS Support

## Summary

Add HTTPS support to sushy-emulator (Redfish BMC) alongside existing HTTP, enabling consumers like RHSE that require TLS on the Redfish endpoint. Both HTTP (port 8000) and HTTPS (port 8443) are always available — no toggle, no breaking changes.

## Motivation

Red Hat Sovereign Enclave (RHSE) consumes bare metal hosts via BareMetalHost CRDs and requires HTTPS on the Redfish endpoint. The BareMetalHost spec supports `disableCertificateVerification: true` for self-signed certs, so a proper CA chain is not needed.

## Design

### Cert Generation

Per-VM self-signed RSA 2048-bit certificate generated at BMC setup time using Python stdlib (`ssl` module for KubeVirt, `subprocess` + `openssl` or stdlib for troshkad). Each cert includes the BMC IP as a SAN. Valid 10 years (ephemeral lab environments).

### Libvirt Provider (troshkad)

**New helper:** `_generate_self_signed_cert(cert_path, key_path, cn, ip)` in troshkad.py. Uses `subprocess.run(["openssl", ...])` — consistent with existing cert generation in agent_deployer.py. `openssl` is always available on RHEL hosts.

**`/bmc/setup` handler:** For each BMC-enabled VM, after generating the existing HTTP sushy config:

1. Generate self-signed cert at `/var/lib/troshka/bmc/{project_id}/sushy-{vm_short}.crt` and `.key`
2. Write a second sushy config file `sushy-{vm_short}-ssl.conf` with:
   - `SUSHY_EMULATOR_LISTEN_PORT = 8443`
   - `SUSHY_EMULATOR_SSL_CERT = '/var/lib/troshka/bmc/{project_id}/sushy-{vm_short}.crt'`
   - `SUSHY_EMULATOR_SSL_KEY = '/var/lib/troshka/bmc/{project_id}/sushy-{vm_short}.key'`
   - All other settings identical to the HTTP config
3. Start a second `sushy-emulator` process with the SSL config
4. Track PID at `sushy-{vm_short}-ssl.pid`

**`_restore_bmc_services()`:** Already scans `sushy-*.conf` files — the new `sushy-*-ssl.conf` files are picked up automatically. Cert/key files persist on disk.

**`/bmc/teardown`:** Already does `rm -rf /var/lib/troshka/bmc/{project_id}/` — certs, SSL configs, and SSL PIDs are cleaned up automatically.

**`/bmc/status`:** Extend to check SSL PIDs alongside HTTP PIDs.

### KubeVirt Provider (operator)

**entrypoint.py:** Generate self-signed cert at startup (write to `/tmp/sushy.crt`, `/tmp/sushy.key`). Run two `HTTPServer` instances in separate threads:

- Thread 1: port 8000, plain HTTP (unchanged)
- Thread 2: port 8443, SSL-wrapped with `ssl.SSLContext`

**bmc.py (Pod builder):** Add container port 8443 to the Pod spec alongside the existing port 8000.

No Kubernetes Secrets needed — cert is ephemeral, regenerated on Pod restart.

### Client URLs

**No changes to existing clients.** The 5 curl commands in `agent_template.py` continue using `http://:8000`. The `redfish-virtualmedia://` scheme in `deployed_topology` is unchanged.

RHSE/metal3/ironic consumers use `https://{bmc_ip}:8443` with `disableCertificateVerification: true` on the BareMetalHost CRD.

### What Doesn't Change

- HTTP on port 8000 — always available, unchanged
- IPMI (vbmc) on port 623 — no TLS (IPMI protocol)
- Troshkad mTLS — separate infrastructure
- Storage pool CA — not reused
- `redfish-virtualmedia://` URL scheme in deployed_topology
- BMC bridge/networking
- htpasswd Basic Auth (now also over TLS on port 8443)
- agent_template.py curl commands

## File Changes

| File | Change |
|------|--------|
| `src/troshkad/troshkad.py` | New `_generate_self_signed_cert()` helper; `/bmc/setup` generates cert + writes SSL config + starts SSL process; `/bmc/status` checks SSL PIDs |
| `src/operator/images/sushy/entrypoint.py` | Cert generation at startup; dual-port HTTPServer (8000 HTTP, 8443 HTTPS) |
| `src/operator/helpers/bmc.py` | Add container port 8443 to Pod spec |

## Testing

- Deploy a project with BMC-enabled VMs
- Verify `curl -sk https://{bmc_ip}:8443/redfish/v1/Systems` returns valid Redfish response
- Verify `curl -s http://{bmc_ip}:8000/redfish/v1/Systems` still works (backward compat)
- Verify `/bmc/status` reports both HTTP and SSL processes healthy
- Verify teardown kills both processes
- Verify restore restarts both processes after troshkad restart
