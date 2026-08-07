# Subsystem Feature Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### Redis Job Queue
- **Client**: `src/backend/app/core/redis.py` — singleton with full in-memory fallback
- **Workers**: `src/backend/app/workers/deploy_worker.py` — same image, different entrypoint
- **Queues**: `deploy` (deploy/destroy/stop/start), `provision` (host/pool provisioning), `default` (misc)
- **State helpers**: `set_progress()`, `mark_cancelled()`, `is_cancelled()`, `add_to_set()`, `get_lock()`
- **Distributed lock**: `get_lock(name)` returns `RedisLock` (Redis) or `_InMemoryLock` (fallback)
- **Distributed semaphore**: `RedisSemaphore(name, limit)` — replaces `threading.Semaphore`
- **Pub/sub bridge**: `notify_project()` publishes to Redis channel, each backend pod subscribes and delivers to local WS clients
- **In-flight tracking**: `record_deploy_start(host_id)` / `record_deploy_end(host_id)` — placement uses these to spread load across clusters
- **Rate limiting**: per-user deploy concurrency (20) and request rate (100/min) via Redis sliding window
- **New background job**: use `from app.core.redis import enqueue_job` then `enqueue_job(func, arg1, arg2, queue_name="deploy")`
- **Admin endpoint**: `GET /api/v1/admin/queue-status` — queue depths, active workers, in-flight deploys
- Dev: `./dev-services.sh start` auto-starts Redis container + RQ worker
### Group-Based Access Control
- Groups resolved at runtime from OpenShift `user.openshift.io/v1/groups` API via `kubernetes` Python client
- No group tables in DB — purely runtime resolution from the OCP cluster, cached 60s
- Backend ServiceAccount `troshka-backend` needs ClusterRole `troshka-group-reader` (get/list on `user.openshift.io` groups)
- Three group config keys: `allowed_groups` (access gate), `admin_groups` (admin role), `operator_groups` (operator role)
- Role resolution priority: email config (`admin_users`/`operator_users`) > group membership > default `"user"`
- Access check: if `allowed_groups` is set, user must be in any configured group (allowed/admin/operator) OR in `allowed_users` email fallback
- `_upsert_sso_user()` updates role on each login if group membership changed
- Graceful degradation: when SA token is absent (dev mode, non-OCP), group resolution is skipped entirely
- OCP groups store usernames (e.g., `prutledg`), matched via `X-Forwarded-User` header from OAuth proxy
- Key functions in `src/backend/app/core/auth.py`: `_fetch_openshift_groups()`, `_get_user_groups()`, `_role_for_groups()`, `_resolve_role()`, `_enforce_access()`
### Container Nodes
- Single containers: `containerNode` with `isPod: false` (default) — one podman container per node
- Template YAML: `containers:` section with `type: container`, `image`, `command`, `ports`, `env`, `volumes`
- Troshkad endpoints: `/containers/create`, `/containers/start`, `/containers/stop`, `/containers/restart`, `/containers/destroy`
- Batch state polling: `POST /containers/states` returns all container states in one call
- Container logs: `GET /containers/{id}/logs` via troshkad
- Veth networking: container gets a veth pair connected to the project bridge (same as VMs)
- Canvas: uses same `ContainerNode.tsx` component as pods, distinguished by `isPod` flag
- Deploy service routes to container vs pod endpoints based on `isPod`
### Pod Nodes (Container Groups)
- Pods are `containerNode` with `isPod: true` — not a separate node type
- Sub-containers stored in `initContainers` and `podContainers` arrays on topology JSONB
- TypeScript field is `podContainers` (not `containers`) to avoid name collision with YAML section
- Template YAML uses `type: pod` in the `containers:` section, with `init_containers` and `containers` sub-keys
- Troshkad endpoints: `/pods/create`, `/pods/start`, `/pods/destroy`
- Veth networking shared via pod infra container (same pattern as single containers)
- Init containers run sequentially, fail fast on non-zero exit
- Pod-level `cpus`/`memory` hidden — each sub-container has its own resources
- Canvas: collapsible sub-container list with ▸/▾ toggle, 🫛 icon
- Deploy service detects `isPod` and routes to pod endpoints instead of container endpoints
### Registry Credentials
- Per-user CRUD for container registry credentials (OCP installs, mirrors, etc.)
- API: `GET/POST /auth/registry-credentials`, `PUT/DELETE /auth/registry-credentials/{id}`
- Passwords encrypted via Fernet before storage, omitted from list response
- Model: `RegistryCredential` — `registry_url`, `username`, `password` (encrypted), `user_id` FK
### Project Timers
- Background daemon (`project_timer.py`) enforces auto-stop and auto-delete on projects
- Polls every 30s, spawns daemon threads for stop/destroy operations
- Skips projects in transitional states (deploying, stopping, starting, reconfiguring, migrating)
- Sends 5-minute warning notifications via WebSocket before auto-stop and auto-delete
- Project model fields: `run_timer_hours`, `lifetime_expires_at`, `poweroff_mode`
### WebSocket PubSub
- In-memory pub/sub (`ws_pubsub.py`) for real-time project/pattern state updates
- API: `subscribe(project_id, ws)`, `unsubscribe()`, `notify_project(project_id, message)`
- State poller: daemon thread polls every 5s, batch-fetches VM states per host (one call per host, not per VM)
- Pushes `project-state`, `deploy-progress`, `vm-state` messages; tracks `_last_states` to only send diffs
- Thread-safe sync-to-async bridge via `run_coroutine_threadsafe`
- Also supports `subscribe_pattern`/`notify_pattern` for pattern capture progress
### Offline Filesystem Modification
- Troshkad endpoint: `POST /vms/modify-fs` — runs commands against a stopped VM's disk using `guestfish`
- Requires `libguestfs-tools` (installed by agent installer)
- Used for kubelet cert cleanup on pattern OCP deploys (removes stale certs before VM start)
- VM must be stopped (not running) — `guestfish` needs exclusive disk access
### Exec API
- `POST /projects/{id}/vms/{vm_id}/exec` — execute commands on VMs
- `method` parameter: `guest-agent` (structured, no creds), `ssh` (requires network + credentials), `console` (OCR/pexpect), `serial` (PTY pexpect), `auto` (tries all in order)
- **Auto priority**: guest-agent → SSH → console → serial
- **Guest-agent exec**: `virsh qemu-agent-command` with `guest-exec` + poll `guest-exec-status`. Returns structured stdout/stderr/exit_code. No network, no credentials needed. Requires `qemu-guest-agent` with exec enabled.
- **Cloud-init**: automatically unblocks `guest-exec` in `/etc/sysconfig/qemu-ga` (handles both RHEL blocklist and allowlist formats). Controlled by `Project.guest_exec_enabled` (default true), toggle in Palette UI.
- **KubeVirt native exec**: guest-agent via virt-launcher pod exec (`_pod_exec_raw` helper for raw JSON), SSH via dnsmasq pod exec, console via WebSocket to KubeVirt console subresource. KubeVirt auto order omits serial (same as console).
- **Virt-launcher pod exec**: requires custom `troshka-virt-exec` SCC (in `infra/ocpvirt-rbac.yaml`) — standard RBAC `pods/exec` is not enough on OpenShift
- **k8s_stream gotcha**: `_preload_content=True` returns Python repr, not raw JSON — use `_preload_content=False` with manual read loop for JSON responses
- SSH key auth preferred over password when `ssh_key_id` is provided
- `from-template` API accepts `ssh_pub_key` directly for agnosticv key injection
### Pattern Save State
- Backend `Pattern.state`: "creating" → "capturing" → "available" or "error"
- Frontend patterns page shows read-only cards during save (buttons disabled, delete hidden)
- Auto-polls every 3s while any pattern is in creating/capturing state; 10s baseline poll + visibilitychange for tab-switch refresh
- **Cancellation**: deleting a capturing pattern cancels troshkad jobs (kills S3 uploads/flattens), cleans up S3 prefix, and removes host cache
- **Clock target capture**: optional checkbox in SavePatternModal — when checked, copies `project.clock_target` to `pattern.clock_target`. On deploy, the pattern's clock_target is restored to the new project.
### DNS Providers
- `dns_providers` API + admin page for managing external DNS (Route53, etc.)
- Projects can optionally attach a DNS provider + domain + GUID for automated DNS record management
