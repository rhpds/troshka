# Troshkad Design Spec

**Date**: 2026-06-08
**Status**: Draft
**Author**: prutledg + Claude

## Problem

The backend communicates with hosts via `run_ssh_script()` — SSH + `sudo bash -s`. This causes:

1. **Zombie processes** — SSH timeout leaves `sudo bash -s` alive, blocking all new `sudo` commands
2. **Libvirt deadlocks** — stuck `virsh` commands hold locks, cascading to block all VM operations
3. **SSH agent exhaustion** — requires `IdentitiesOnly=yes` and `sshauth=privkey` workarounds
4. **No progress feedback** — long operations (virt-install, ISO downloads) run blind

There are ~29 `run_ssh_script()` call sites across 8 service files.

## Solution

**troshkad** — a Python daemon running as root on each host, exposing a structured HTTPS API. The backend connects to troshkad to execute operations instead of SSHing scripts.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Connection direction | Backend → troshkad | Simpler daemon (just listens). Backend already knows host IPs. No reconnect/heartbeat logic. |
| Protocol | HTTPS REST API | Conventional, debuggable with curl, Python stdlib has adequate HTTP server. |
| Authentication | Pre-shared bearer token over TLS | Simple, sufficient over TLS. Token stored in backend DB per host. |
| TLS | Self-signed cert, fingerprint pinned | No CA needed. Fingerprint pinning is stronger than CA trust for known hosts. |
| Command model | Structured endpoints (one per operation) | Prevents command injection. Typed parameters, explicit validation. |
| Execution model | Async jobs (POST returns job_id, poll for status) | Handles connection drops. Backend can check for in-flight operations. Natural fit with existing frontend polling. |
| Implementation | Single-file stdlib Python | SCP-one-file updatable. No pip, no venv, no dependencies beyond Python 3.9+. |
| Port | 31337 (configurable) | Unlikely to conflict. Configurable via config file. |

## File Layout

```
/opt/troshka/
├── troshkad.py              # The daemon — single file, all logic
├── troshkad.conf             # Config (JSON)
├── tls/
│   ├── server.crt            # Self-signed cert (generated at install)
│   └── server.key            # Private key
```

**Systemd unit**: `/etc/systemd/system/troshkad.service`

- `ExecStart=/usr/bin/python3 /opt/troshka/troshkad.py`
- Runs as root (no `User=` directive)
- `Restart=always`, `RestartSec=5`
- `WorkingDirectory=/opt/troshka`

**Config file** (`troshkad.conf`):

```json
{
  "port": 31337,
  "token": "64-char-hex-token",
  "tls_cert": "/opt/troshka/tls/server.crt",
  "tls_key": "/opt/troshka/tls/server.key",
  "host_id": "uuid-from-backend",
  "max_concurrent_jobs": 4,
  "drain_timeout_seconds": 300
}
```

## API Surface

All requests require `Authorization: Bearer <token>` header. All POST operation endpoints return `{"job_id": "uuid", "status": "running"}`.

### VM Lifecycle

| Method | Path | Purpose | Key Parameters |
|--------|------|---------|----------------|
| POST | `/vms/create` | Create VM via virt-install | domain_name, vcpus, ram_mb, disks, networks, seed_iso |
| POST | `/vms/destroy` | Destroy + undefine VM | domain_name |
| POST | `/vms/start` | Start a defined VM | domain_name |
| POST | `/vms/stop` | Graceful shutdown | domain_name |
| POST | `/vms/reboot` | Reboot VM | domain_name |

### Storage

| Method | Path | Purpose | Key Parameters |
|--------|------|---------|----------------|
| POST | `/disks/create` | Create qcow2 disk | path, size_gb, format, backing_file |
| POST | `/disks/resize` | Resize existing disk | path, new_size_gb |
| POST | `/seeds/create` | Build cloud-init seed ISO | path, meta_data, user_data, network_config |
| POST | `/images/cache` | Download + cache base image | url, dest_path, expected_format |

### Networking

| Method | Path | Purpose | Key Parameters |
|--------|------|---------|----------------|
| POST | `/networks/setup` | Configure libvirt network + nftables | network_name, cidr, vni, bridge_name |
| POST | `/networks/teardown` | Remove network config | network_name |
| POST | `/eips/configure` | Set up EIP nftables rules | project_id, eip_mappings |

### Operations

| Method | Path | Purpose | Key Parameters |
|--------|------|---------|----------------|
| POST | `/gc/run` | Run garbage collector | (scoped by config) |
| POST | `/snapshots/create` | Snapshot a VM | domain_name, output_path |
| POST | `/patterns/export` | Export VM as pattern | domain_name, output_path |

### System

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check + capacity info |
| GET | `/jobs/{job_id}` | Poll job status/output |
| POST | `/admin/update` | Self-update (accepts `?force=true`) |

### Response Formats

**`GET /health`** (immediate, no job):

```json
{
  "status": "ok|draining",
  "version": "2026.06.08.1",
  "host_id": "uuid",
  "uptime_seconds": 3600,
  "running_jobs": 2,
  "capacity": {
    "vcpus_total": 16,
    "vcpus_used": 8,
    "ram_total_mb": 65536,
    "ram_used_mb": 32768,
    "storage_total_gb": 500,
    "storage_used_gb": 120
  }
}
```

**`GET /jobs/{job_id}`**:

```json
{
  "job_id": "uuid",
  "command": "vms/create",
  "status": "running|completed|failed",
  "output": ["line1", "line2"],
  "result": {},
  "started_at": "iso-timestamp",
  "completed_at": "iso-timestamp|null"
}
```

## Job Execution Model

Jobs are tracked in an in-memory dict (`_jobs`). No persistence — if troshkad dies, jobs are gone. The backend is the source of truth for operation outcomes (it checks actual state: VM exists, disk exists, etc.).

**Job lifecycle:**

1. Request arrives → validate auth → validate params
2. Create Job object, store in `_jobs`, spawn `threading.Thread(daemon=True)`
3. Return `{"job_id": "...", "status": "running"}` immediately
4. Worker thread runs the operation via `subprocess.run()` (list form, never `shell=True`)
5. Stdout/stderr lines appended to `job.output` in real-time
6. On completion: status set to `completed` or `failed`, `result` populated
7. Backend polls `GET /jobs/{job_id}` to track progress

**Concurrency:**

- `max_concurrent_jobs` from config (default 4)
- At max capacity: new requests return `503 {"error": "max_concurrent_jobs_reached"}`
- Backend retries later

**Cleanup:**

- Completed/failed jobs kept in memory for 1 hour, then pruned
- Periodic cleanup thread runs every 10 minutes

## Authentication & TLS

### Install-Time Setup

1. Generate 64-char hex token: `secrets.token_hex(32)`
2. Write token to `troshkad.conf`
3. Generate self-signed EC cert:
   ```
   openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
     -nodes -days 3650 -subj "/CN=troshkad" \
     -keyout server.key -out server.crt
   ```
4. Token and cert SHA-256 fingerprint returned to backend, stored in Host model

### Request Validation (troshkad)

1. TLS handshake via `ssl.SSLContext`
2. Extract `Authorization` header
3. Timing-safe comparison: `hmac.compare_digest()`
4. Reject with `401` on mismatch

### Connection Validation (backend)

1. Connect to `https://{host_ip}:31337`
2. Custom SSL context verifies cert fingerprint against stored `agent_cert_fingerprint`
3. Reject if fingerprint mismatch (prevents MITM)
4. Attach `Authorization: Bearer <token>` header

### TLS Configuration

- EC key (prime256v1)
- 10-year validity
- TLS 1.2 minimum
- Cert pinning by SHA-256 fingerprint

## Update & Drain Mechanism

### Normal Update (`POST /admin/update`)

1. Backend sends `{"script": "<base64 troshkad.py>", "version": "..."}`
2. Troshkad writes to `/opt/troshka/troshkad.py.new`
3. Syntax check: `compile(source, 'troshkad.py.new', 'exec')` — reject with `400` if invalid
4. Set state to `draining` — health returns `"status": "draining"`
5. New operation requests return `503 {"status": "draining"}`
6. Wait for running jobs to complete (up to `drain_timeout_seconds` = 300s)
7. If timeout: terminate remaining job subprocesses
8. Atomic rename: `troshkad.py.new` → `troshkad.py`
9. Return `200 {"status": "restarting"}`
10. Exit cleanly — systemd `Restart=always` brings up new version

### Force Update (`POST /admin/update?force=true`)

1. Same payload and syntax check
2. Skip drain — immediately terminate all running job subprocesses
3. Atomic rename, return 200, exit

### Backend During Drain

1. Backend sends update, gets `200` acknowledgment
2. Subsequent operation requests get `503 {"status": "draining"}`
3. Backend holds these commands (does not discard)
4. Polls `GET /health` until status returns to `"ok"` with new version
5. Replays held commands

## Installation & Idempotency

The install script runs during initial agent setup via SSH. This is the only SSH usage that remains.

**Steps:**

1. `mkdir -p /opt/troshka/tls`
2. Write `troshkad.py` to `/opt/troshka/troshkad.py`
3. Generate TLS cert only if not present: `[ -f /opt/troshka/tls/server.crt ] || openssl ...`
4. Generate config + token only if not present: `[ -f /opt/troshka/troshkad.conf ] || python3 -c "..."`
5. Write systemd unit to `/etc/systemd/system/troshkad.service`
6. `systemctl daemon-reload && systemctl enable --now troshkad`
7. Output token and cert fingerprint as JSON for backend to capture
8. Open firewall: `firewall-cmd --add-port=31337/tcp --permanent && firewall-cmd --reload`

**Idempotency guarantees:**

- `mkdir -p`: safe to re-run
- TLS cert: guarded by existence check — won't regenerate
- Config/token: guarded by existence check — won't regenerate
- `troshkad.py`: always overwritten (intentional — gets latest version)
- Systemd unit: always overwritten, `daemon-reload` picks up changes
- `systemctl enable --now`: safe if already enabled/running
- `firewall-cmd --add-port`: idempotent in firewalld

**Re-install result:** Script updated, cert and token preserved, service restarts with new code. Backend's stored token and fingerprint remain valid.

## Backend Integration

### New Module: `src/backend/app/services/troshkad_client.py`

```python
def troshkad_request(host, method, path, body=None, timeout=30):
    """HTTPS request to troshkad with cert pinning + bearer auth.
    Returns parsed JSON."""

def start_job(host, path, params):
    """POST to an operation endpoint, return job_id."""

def poll_job(host, job_id):
    """GET /jobs/{job_id}, return job dict."""

def wait_for_job(host, job_id, timeout=600, poll_interval=5):
    """Poll until completed/failed or timeout. Return final job state."""
```

### Migration Pattern

Each `run_ssh_script()` call is replaced with:

```python
# Before
result = run_ssh_script(host.ip_address, host.private_key, script)
if not result["success"]:
    raise DeployError(result["output"])

# After
job_id = start_job(host, "/vms/create", {"domain_name": ..., "vcpus": ..., ...})
job = wait_for_job(host, job_id, timeout=600)
if job["status"] == "failed":
    raise DeployError(job["result"]["error"])
```

Migration is service-by-service, not big-bang.

### Host Model Changes

New columns on the `Host` model:

- `agent_token: Mapped[str | None]` — bearer token
- `agent_cert_fingerprint: Mapped[str | None]` — SHA-256 of self-signed cert
- `agent_version: Mapped[str | None]` — last known troshkad version

### Health Check Integration

`GET /health` replaces SSH echo tests for `last_health_at`. The capacity fields in the health response feed capacity sync (currently done via SSH script in the GC).

### SSH Removal

Once all services are migrated, `run_ssh_script()` and the `private_key` field become dead code. SSH is only needed for initial install.

## Security

### Network

- Port 31337 open in AWS Security Group, scoped to backend's public IP
- Backend discovers its IP via `checkip.amazonaws.com` at host provision time
- On connection failure (timeout/refused, not auth error): re-check public IP, update SG if changed, retry

### Authentication

- 256-bit token (64 hex chars) — brute-force infeasible
- Timing-safe comparison (`hmac.compare_digest`)
- Token in header only (not URL) — avoids log leakage
- TLS required — token never in plaintext

### Command Injection Prevention

- No arbitrary script execution — structured endpoints only
- All parameters validated and type-checked
- `subprocess.run()` with list form — never `shell=True` with interpolation
- All domain names, paths, and IDs are UUID-based (enforced by backend)

### Update Security

- Update payload over authenticated TLS channel
- Syntax-checked before replacing the running script
- Atomic file rename prevents partial writes

### Resource Protection

- `max_concurrent_jobs` prevents resource exhaustion
- Job cleanup prevents unbounded memory growth
- `drain_timeout_seconds` prevents indefinite drain states

### Trust Boundary

The backend is the trusted control plane. A compromised backend can fully control any host — this is by design. Troshkad runs as root because it manages VMs — also by design. No request rate limiting because only the backend communicates with troshkad.

## Version Scheme

Simple date-based: `YYYY.MM.DD.N` where N is a sequence number within the day. Example: `2026.06.08.1`. Stored in a `VERSION` constant at the top of `troshkad.py`. Returned in `GET /health` response.

## Out of Scope

- **Clustering/HA for troshkad**: Single daemon per host is sufficient
- **Persistent job storage**: Backend is the source of truth for operation outcomes
- **Automatic troshkad updates**: Updates are operator-initiated via the backend
- **Agent-to-backend callbacks**: Backend always initiates; troshkad only responds
- **Log persistence**: Troshkad logs to stdout/stderr, captured by journald
