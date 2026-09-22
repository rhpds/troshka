# troshkad Showroom TLS Edge — Design

**Date:** 2026-09-22
**Status:** Draft for review

## Problem

On troshkad (cloud) providers — AWS/GCP/Azure, i.e. everything that is **not**
KubeVirt/OCP-Virt — the showroom is exposed by a gateway NAT port-forward
`EIP:443 → 172.30.<vni>.3:80`. The internal showroom nginx serves **plain HTTP
on :80**, so a browser hitting `https://<EIP>/` receives a non-TLS response on
the HTTPS port and fails to establish a secure connection.

On KubeVirt/OCP-Virt the same showroom pod (identical scaffold, HTTP `:80`) is
fronted by an **OCP Route with `termination: edge`** using the cluster wildcard
cert on the router; the router terminates TLS and forwards HTTP to `:80`. troshkad
has no router, so nothing terminates TLS.

## Goal

Terminate TLS for the troshkad showroom so `https://<showroom>` works, using a
free, auto-renewing Let's Encrypt certificate when a DNS zone is available, and a
self-signed certificate otherwise (always encrypted, never plain HTTP on :443).

## Non-goals

- No change to KubeVirt/OCP-Virt (they already terminate at the OCP router).
- No change to the showroom container/scaffold (stays HTTP `:80`).
- No new user-facing custom-hostname feature — the FQDN is auto-derived.
- Not terminating TLS inside the showroom pod (explicitly rejected in favor of a
  host/gateway edge, which mirrors the OCP-router model).

## Design overview

Insert a per-project TLS-terminating reverse proxy in the project's transit
netns, between the `:443` DNAT and the showroom's HTTP `:80`. The transit netns
already hosts troshkad-managed services (dnsmasq, chronyd, nftables); the
terminator joins them and plays the role the OCP router plays for KubeVirt.

```
Browser ──TLS──▶ EIP:443 ──DNAT──▶ [tls-edge :443 in netns troshka-<proj8>] ──HTTP──▶ 172.30.<vni>.3:80 (showroom nginx)
                                          ▲ per-project cert (LE via Route53 DNS-01, or self-signed)
```

Only troshkad (cloud) providers are affected. Route providers
(`_ROUTE_PROVIDERS`: ocpvirt/kubevirt) keep their edge-terminated Route and are
never given a terminator or an EIP `:443`→`:80` DNAT.

## Components

### 1. Showroom FQDN + DNS record (backend)

- **FQDN:** `showroom-<proj8>.<zone>` where `<proj8>` is the first 8 chars of the
  project id and `<zone>` is the default zone of the project's DNS provider
  config (the same config `dns_service` already consumes: `route53` with
  `hosted_zone_id`/`default_zone`, or `nsupdate` with `default_zone`).
- **Record:** an `A` record `showroom-<proj8>.<zone> → <EIP>` created via
  `dns_service.create_dns_records(...)` at deploy, deleted on destroy via
  `dns_service.delete_dns_records(...)`.
- **No DNS provider/zone configured for the project → skip LE**, use the
  self-signed path (§2b). This is the sole trigger for the fallback.

### 2. Certificate acquisition

The showroom cert is obtained **on the host by troshkad**, reusing the machinery
already installed by `agent_deployer` for the VNC console cert (certbot +
certbot-dns-route53 + the `certbot renew` cron).

New troshkad op: `POST /gateway/tls-cert` with params:
```
{
  "project_id": "<uuid>",
  "fqdn": "showroom-<proj8>.<zone>",   # empty => self-signed
  "self_signed_cn": "<EIP>",           # used when fqdn empty
  "route53": { ... aws creds/region ... }  # optional; enables DNS-01
}
```
Behavior:
- **(2a) LE path** (fqdn set): run
  `certbot certonly --dns-route53 -d <fqdn> --non-interactive --agree-tos
  -m noreply@redhat.com --preferred-challenges dns-01`, using the provided
  Route53 credentials in the environment. On success the cert lives at
  `/etc/letsencrypt/live/<fqdn>/{fullchain.pem,privkey.pem}`. Renewal is already
  handled by the host-wide `certbot renew` cron installed at bootstrap.
- **(2b) self-signed fallback** (fqdn empty, or certbot fails): generate a
  self-signed cert with `openssl req -x509 -newkey rsa:2048 -nodes` (CN =
  `self_signed_cn`, SAN = the EIP) into
  `/var/lib/troshka/gateway/<proj8>/tls/{fullchain.pem,privkey.pem}`.
- Returns `{ "cert_path": ..., "key_path": ..., "mode": "letsencrypt"|"self-signed" }`.
- **argv lists only** — the `fqdn`/`cn` are interpolated into command
  **arguments**, never a shell string (see Security).

### 3. TLS terminator (troshkad)

New troshkad op: `POST /gateway/tls-proxy` with params:
```
{
  "project_id": "<uuid>",
  "netns": "troshka-<proj8>",
  "listen": "172.30.<vni>.1:443",       # transit gateway IP inside the netns
  "upstream": "172.30.<vni>.3:80",      # showroom infra IP
  "cert_path": "...", "key_path": "..."
}
```
- Starts a terminator **inside the transit netns**, bound to the transit gateway
  IP (`172.30.<vni>.1`, where the EIP `:443` DNAT lands — see §4):
  `ip netns exec <netns> socat OPENSSL-LISTEN:443,bind=172.30.<vni>.1,reuseaddr,fork,cert=<combined pem>,verify=0 TCP:172.30.<vni>.3:80`
  (`socat` reads cert+key from one concatenated PEM; troshkad writes
  `fullchain.pem`+`privkey.pem` into a `combined.pem` with `0600` perms.)
- Managed as a **tracked, restartable process** with a pidfile under
  `/var/lib/troshka/gateway/<proj8>/tls/` (same pattern as vbmcd/dnsmasq). A
  `stop` variant (`DELETE /gateway/tls-proxy`) kills it and removes the pidfile.
- **Restore on troshkad restart:** add `_restore_tls_proxies()` alongside
  `_restore_bmc_services()`/`_restore_dnsmasq()` in `main()`, re-launching a
  terminator for each project dir that has a stored terminator descriptor.
- `socat` is added to the single-source `_REQUIRED_HOST_PACKAGES` (so it is
  installed at bootstrap and self-healed on update).

### 4. Port-forward wiring (backend)

`_inject_showroom_port_forward` (in `vxlan.py`) currently injects
`443 → 172.30.<vni>.3:80`. Change so that on **non-route providers** the EIP
`:443` DNAT targets the **terminator** instead of the showroom directly:
- Injected forward becomes `443 → 172.30.<vni>.1:443` (the transit gateway IP,
  where the terminator listens); the terminator proxies decrypted traffic to
  `172.30.<vni>.3:80`.
- Route providers: unchanged (no EIP `:443` forward; Route serves it).
- The showroom-managed forward stays flagged `managedByShowroom` for idempotent
  add/remove.

### 5. Deploy / destroy orchestration (backend)

In the troshkad deploy path (where `inject_showroom_gateway_port_forwards` and
the gateway network are set up), after the showroom container and gateway exist:
1. Resolve the project's DNS provider/zone → build FQDN (or empty).
2. If FQDN: `dns_service.create_dns_records` (A → EIP).
3. Call troshkad `/gateway/tls-cert` → get cert paths + mode.
4. Call troshkad `/gateway/tls-proxy` → start the terminator.
5. Surface the showroom URL as `https://<fqdn>` (LE) or `https://<EIP>` (self-signed)
   in the topology's gateway `externalEndpoints`/showroom access metadata that the
   UI reads (so the External Access panel shows the HTTPS URL).

On destroy: `DELETE /gateway/tls-proxy`, then `dns_service.delete_dns_records`
for the A record. Cert files are removed with the project's gateway dir.

### 6. Error handling & fallback

- No DNS zone → self-signed (mode reported back, logged; UI shows the EIP URL).
- certbot failure (rate limit, DNS propagation) → fall back to self-signed for
  this deploy; the cron `certbot renew` cannot help a never-issued cert, so a
  later redeploy re-attempts LE. Never leave `:443` serving plain HTTP.
- Terminator start failure → deploy logs a warning; showroom remains reachable
  only if a prior terminator is running. (Non-fatal: TLS is an enhancement, not
  a gate on the cluster deploy — unlike recert.)

## Interfaces (summary)

- troshkad: `POST /gateway/tls-cert`, `POST /gateway/tls-proxy`,
  `DELETE /gateway/tls-proxy`; `_restore_tls_proxies()` in `main()`.
- backend: `deploy_service` helpers `_showroom_fqdn(project, zone)`,
  `_ensure_showroom_tls(host, project, topology, eip)` (orchestrates §5),
  `_teardown_showroom_tls(host, project)`; `vxlan._inject_showroom_port_forward`
  targets the terminator.
- `troshkad._REQUIRED_HOST_PACKAGES` gains `socat`.

## Testing

Backend (SQLite, mocked troshkad/dns):
- `_showroom_fqdn` derives `showroom-<proj8>.<zone>`; empty when no zone.
- `_ensure_showroom_tls`: creates A record + calls tls-cert/tls-proxy when zone
  present; skips DNS + requests self-signed when absent (mock `dns_service`,
  `start_job`).
- `_inject_showroom_port_forward` points `:443` at the terminator on non-route
  providers; unchanged for route providers.
- teardown deletes the A record and stops the terminator.

troshkad (mocked subprocess, no real I/O):
- `/gateway/tls-cert`: LE branch builds the certbot argv with the fqdn; failure
  and empty-fqdn both fall back to openssl self-signed; argv lists only.
- `/gateway/tls-proxy`: builds the `ip netns exec … socat …` argv; writes a
  `0600` combined PEM; pidfile created; `DELETE` stops it.
- `_ensure_host_packages` includes `socat`.
- `_restore_tls_proxies` re-launches from stored descriptors.

## Security considerations

- All certbot/openssl/socat invocations are **argv lists, never `bash -c`** — the
  fqdn/CN/upstream derive from topology/DNS config (user-influenceable), so raw
  shell interpolation would be injectable (cf. the apiVip probe finding).
- Validate the FQDN against a strict label regex and the upstream/EIP as an IP
  before use.
- Cert private keys and the combined PEM are `0600`, owned by root, under the
  per-project gateway dir.
- Route53 credentials are passed to troshkad only for the DNS-01 call and not
  persisted beyond the certbot invocation environment.

## Rollout / backward compatibility

- Purely additive for troshkad; KubeVirt/OCP-Virt untouched.
- Existing running projects are unaffected until redeployed; a redeploy adds the
  terminator + cert.
- `socat` is installed at next bootstrap or troshkad update (single-source
  package set), so already-deployed hosts self-heal the dependency.
