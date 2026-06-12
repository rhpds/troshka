# Direct Console Proxy — Design Spec

## Goal

Move VNC console connections off the Troshka backend and onto the hosts directly, eliminating the SSH tunnel + websockify bottleneck and reducing latency from 5 hops to 2.

## Architecture

```
Before: Browser → Next.js → FastAPI WS → websockify → SSH tunnel → Host:VNC
After:  Browser → wss://host.tc.rhdp.net:443/ws/{jwt} → troshka-vncd → localhost:VNC
```

## Components

### 1. Route53 Console Provider

- New configuration: hosted zone ID + base domain (e.g., `tc.rhdp.net`)
- Host DNS names auto-generated: `{instance_id}.{base_domain}`
- DNS record created/updated when host is provisioned or IP changes
- Host model stores `console_domain` field
- Dynamic DNS: on boot, host updates its own A record via Route53 API
- IAM permissions needed: `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, `route53:GetChange`

### 2. Host TLS Setup

- `certbot` + `certbot-dns-route53` plugin installed by agent installer
- Initial cert: `certbot --dns-route53 -d {instance_id}.{base_domain}`
- Auto-renewal via cron: `certbot renew`
- DNS-01 challenge — no port 80 needed
- Certs stored at `/etc/letsencrypt/live/{instance_id}.{base_domain}/`
- Future: switch to DNS-PERSIST-01 when available (Q2 2026) to eliminate renewal DNS updates

### 3. Console Daemon — `troshka-vncd`

- Separate process from troshkad (does not impact troshkad operations)
- Single Python file, minimal dependencies (`websockets` library)
- Listens on port 443, bound to host's primary IP only (avoids EIP conflicts)
- TLS using Let's Encrypt cert
- Accepts: `wss://{console_domain}:443/ws/{jwt_token}`
- JWT validation:
  - Signed with shared secret (derived from troshkad's existing agent token)
  - Claims: `{domain_name, host_id, exp}`
  - Short-lived: 5 minute expiry
  - Single-use: consumed on successful WebSocket upgrade
- VNC connection:
  - Extracts domain name from JWT
  - Resolves VNC port via `virsh dumpxml {domain_name}` (parses XML for VNC port)
  - Connects to `127.0.0.1:{vnc_port}` (local QEMU VNC socket)
  - Proxies binary frames bidirectionally
- No job queue, no state, no persistence — pure stateless WebSocket relay
- Managed by systemd, auto-restarts on failure
- Logs to journald

### 4. Token Generation (Backend)

- Existing `/console` endpoint changes:
  - No longer spawns SSH tunnel or websockify
  - Generates a signed JWT with `{domain_name, host_id, exp}`
  - Looks up `host.console_domain` 
  - Returns `{ws_url: "wss://{console_domain}/ws/{token}"}`
- Existing `console_proxy.py` becomes unused (can be removed)
- Token store (`_console_tokens`) replaced by JWT — no server-side state

### 5. Frontend Changes

- Console page connects to the host URL directly instead of backend proxy
- `ws_url` from the API already points to the host — no frontend logic change needed
- noVNC client, token flow, password buttons, screenshot — all unchanged

### 6. Security Group

- Port 443 opened on host security group for inbound from `0.0.0.0/0`
- EIP traffic uses secondary IPs — no conflict with port 443 on primary IP
- `troshka-vncd` binds to primary IP only, not `0.0.0.0`

## EIP Conflict Avoidance

- EIPs are secondary private IPs on the host's ENI
- nftables DNAT rules forward EIP traffic into project network namespaces
- Console traffic hits the host's primary IP → troshka-vncd
- No collision: different destination IPs, same port

## Security

- JWT tokens: short-lived (5 min), single-use (consumed on connect)
- TLS: Let's Encrypt certs, auto-renewed via certbot
- Auth flow unchanged: Troshka backend verifies user owns the project before issuing token
- No VNC credentials exposed — the daemon resolves VNC port internally
- Security group: port 443 open, but only JWT-authenticated connections accepted
- troshka-vncd has no write access to VMs — read-only (virsh dumpxml) + VNC relay

## What Stays the Same

- Console UI (toolbar, password buttons, screenshot, keyboard)
- Console page fetches token from Troshka backend (auth check happens here)
- VNC protocol, noVNC client
- All existing console features (paste, power actions, etc.)

## What Gets Removed

- `console_proxy.py` — SSH tunnel + websockify spawning
- WebSocket proxy in `ws.py` (`/api/v1/console/ws/{token}`)
- `websockify` dependency on the backend
- SSH tunnel overhead

## Performance Impact

- Latency: 5 hops → 2 hops
- Backend load: zero console traffic through Troshka after token generation
- Scalability: each host handles its own console connections independently
- No single point of contention for multi-user concurrent console access

## Dependencies

- `certbot` + `certbot-dns-route53` on each host
- `websockets` Python library on each host (or vendored in troshka-vncd)
- Route53 hosted zone for the console domain
- IAM permissions for Route53 on each host
