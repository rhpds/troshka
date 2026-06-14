# Direct Console Proxy — Implementation Spec

## Goal

Replace the current 5-hop VNC console path (Browser → Next.js → FastAPI WS → websockify → SSH tunnel → Host VNC) with a 2-hop direct path (Browser → troshka-vncd on host → localhost VNC), eliminating latency and backend bottleneck.

## Architecture

```
Before: Browser → Next.js → FastAPI WS → websockify → SSH tunnel → Host:VNC
After:  Browser → wss://{instance_id}.tc.rhdp.net:443/ws/{jwt} → troshka-vncd → localhost:VNC
```

## Component 1: Route53 Console DNS

**Config additions** in `config.yaml`:
```yaml
console:
  hosted_zone_id: "Z..."
  base_domain: "tc.rhdp.net"
```

**New Host model column**: `console_domain` (String[255], nullable) — stores the FQDN, e.g., `i-0abc123.tc.rhdp.net`.

**New service module**: `src/backend/app/services/console_dns.py`
- `upsert_record(hosted_zone_id, fqdn, ip)` — creates/updates Route53 A record
- `delete_record(hosted_zone_id, fqdn)` — removes record on host termination

**Integration points**:
- `provisioner.py` → after host gets public IP, call `upsert_record()` and set `host.console_domain`
- Host termination → call `delete_record()`
- Health poller → if host IP changes, update the record

**IAM**: Add `route53:ChangeResourceRecordSets`, `route53:GetChange`, `route53:ListHostedZones` to `infra/iam-policy.json` for the troshka backend user.

## Component 2: Host TLS via Let's Encrypt

**Agent install script changes** (`agent_deployer.py`):
- Install `certbot` and `certbot-dns-route53` into `/opt/troshka/venv/`
- Run: `certbot certonly --dns-route53 -d {console_domain} --non-interactive --agree-tos -m noreply@redhat.com`
- Certs at: `/etc/letsencrypt/live/{console_domain}/fullchain.pem` and `privkey.pem`
- Cron: `certbot renew --quiet` (daily, certbot skips if not due)

**IAM — Instance Profile approach**:
- During VPC setup, create:
  - IAM Role `troshka-certbot-role` — trust policy for EC2 assume
  - Inline policy scoped to `route53:ChangeResourceRecordSets` + `route53:GetChange` on the specific hosted zone ARN only
  - Instance Profile `troshka-certbot-profile`
- `provision_host()` attaches instance profile to EC2 `RunInstances` call
- No AWS credentials stored on the host — certbot uses instance metadata

**New IAM permissions for the troshka backend user** (in `infra/iam-policy.json`):
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`
- `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`
- `iam:PassRole` (for assigning profile to EC2)
- `iam:GetRole`, `iam:GetInstanceProfile` (for idempotent checks)

## Component 3: Console Daemon — `troshka-vncd`

**New file**: `src/troshka-vncd/troshka-vncd.py`

Single-file Python daemon. Depends on `websockets` library (pip-installed in `/opt/troshka/venv/`).

**Behavior**:
- Listens on port 443, bound to host's primary private IP only (not 0.0.0.0 — avoids EIP conflicts on secondary IPs). DNS A record points to the public IP; AWS routes traffic to the instance where the kernel delivers it to the private IP binding.
- TLS using Let's Encrypt cert from `/etc/letsencrypt/live/{console_domain}/`
- WebSocket endpoint: `/ws/{jwt_token}`

**JWT validation**:
- HMAC-SHA256 signed with the existing troshkad agent token as secret
- Claims: `{domain_name, host_id, exp}`
- 5-minute expiry
- Single-use: consumed tokens tracked in an in-memory set, pruned periodically

**VNC relay**:
- Extract `domain_name` from JWT
- Run `virsh dumpxml {domain_name}` to find VNC port
- Connect to `127.0.0.1:{vnc_port}`
- Proxy binary frames bidirectionally until either side disconnects

**Cert reload**: Periodically check cert file mtime; reload SSL context on change (certbot renewal handled without restart).

**No state**: No job queue, no persistence, no database. Pure stateless WebSocket relay (aside from consumed-token set).

**Systemd**: `troshka-vncd.service`, `Restart=always`, `RestartSec=5s`. Logs to journald.

**Update mechanism**: Extended `update-agent.sh` and backend push endpoint push both `troshkad.py` and `troshka-vncd.py`.

## Component 4: Backend Changes

**Console API endpoint** (`projects.py`, `GET /{project_id}/vms/{vm_id}/console`):
- Still calls `troshkad_get_vnc_port()` to verify VM is running
- Generates JWT signed with `host.agent_token`: `{domain_name, host_id, exp: now+5min}`
- Looks up `host.console_domain`
- Returns `{"ws_url": "wss://{console_domain}/ws/{jwt}"}` (changed from `{"token": "..."}`)

**Removed**:
- `src/backend/app/services/console_proxy.py` — entire file deleted
- WebSocket handler in `ws.py` at `/api/v1/console/ws/{token}` — removed
- `websockify` backend dependency — removed
- All SSH tunnel / websockify imports and references in `projects.py`, `ws.py`

**New**:
- `src/backend/app/services/console_dns.py` — Route53 upsert/delete
- JWT signing helper (in `console_dns.py` or standalone `console_service.py`)
- `console_domain` column on Host model + Alembic migration
- Config: `console.hosted_zone_id`, `console.base_domain`

## Component 5: Frontend Changes

**Console page** (`src/frontend/src/app/console/page.tsx`):
- Currently: fetches `{"token": "..."}`, constructs `ws://{window.location.host}/api/v1/console/ws/{token}`
- Changes to: fetches `{"ws_url": "wss://..."}`, passes `ws_url` directly to noVNC `RFB` constructor

Everything else unchanged: keyboard popup, password buttons, screenshot, power controls, VM state socket, scaled/1:1 mode.

## Component 6: Security Group

**Add to `ensure_security_group()`** in `provisioner.py`:
- Port 443/TCP inbound from `0.0.0.0/0` — for console WebSocket connections

No conflict with EIPs: EIPs are secondary private IPs with nftables DNAT into network namespaces; port 443 hits the primary IP where vncd is bound.

**Existing host migration**: Add port 443 rule check to the existing ensure pattern (similar to troshkad rule logic) so existing security groups get updated on next provisioner interaction.

## Component 7: Instance Profile for Route53

**Created during VPC setup** (`provisioner.py`):
- IAM Role: `troshka-certbot-role` with EC2 trust policy
- Inline Policy: `route53:ChangeResourceRecordSets` + `route53:GetChange` scoped to single hosted zone ARN
- Instance Profile: `troshka-certbot-profile`
- `provision_host()` adds `IamInstanceProfile={'Name': 'troshka-certbot-profile'}` to `RunInstances`

## Security Model

- **JWT tokens**: HMAC-SHA256, short-lived (5 min), single-use
- **TLS**: Let's Encrypt certs, auto-renewed via certbot cron
- **Auth flow unchanged**: Backend verifies user owns project before issuing JWT
- **No VNC credentials exposed**: vncd resolves VNC port internally via virsh
- **Port 443 open**: Only JWT-authenticated WebSocket connections accepted
- **vncd is read-only**: Only runs `virsh dumpxml` + VNC relay, no write access to VMs
- **Instance profile scoping**: Hosts can only modify DNS in the console hosted zone, nothing else

## Scaling

- Port 443 handles hundreds of concurrent WebSocket connections (standard TCP socket multiplexing)
- vncd uses async Python (`websockets` library on `asyncio`) — non-blocking per connection
- Typical host: 20-40 VMs, so 20-40 peak concurrent console sessions — very comfortable
- VNC bandwidth is the real constraint, not connection count

## What Stays the Same

- Console UI (toolbar, password buttons, screenshot, keyboard, power controls)
- noVNC client library
- VNC protocol
- VM state socket
- Console keyboard popup and postMessage flow
- Auth model (backend verifies project ownership before issuing token)

## What Gets Removed

- `console_proxy.py` (SSH tunnel + websockify management)
- WebSocket proxy in `ws.py` (`/api/v1/console/ws/{token}`)
- `websockify` backend dependency
- SSH tunnel overhead for console
- Server-side token store (`_console_tokens` dict)
