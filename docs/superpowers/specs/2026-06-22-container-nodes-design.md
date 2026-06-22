# Container Nodes — Design Spec

**Date:** 2026-06-22
**Status:** Draft

## Overview

Add containers as a first-class canvas node type alongside VMs. Containers run on the host via podman, attach to the same VXLAN bridges as VMs (full L2 peers), and participate in the same deploy pipeline, start ordering, template YAML, and pattern save/restore workflows.

## Goals

- Drag a container from the palette, connect it to networks and storage, deploy it — same UX as VMs
- Infrastructure services (registries, Gitea, MinIO) and application workloads equally supported
- Self-contained patterns: container images captured as `.tar.gz` alongside disk volumes — no external registry dependency at restore time
- Registry credentials managed centrally in user Settings

## Non-Goals (Future Work)

- Container-to-container service discovery beyond shared networking
- Health checks / readiness probes
- Container image building (Containerfile/Dockerfile)
- Kubernetes pod/service abstractions
- Resource monitoring dashboards for containers

---

## 1. Topology Data Model

New `containerNode` type in topology JSONB:

```
containerNode.data:
  label: string                 # Display name
  name: string                  # DNS-safe name (auto-generated from label)
  image: string                 # Full image ref: "quay.io/org/app:v1.2"
  registryCredentialId: string | null  # References saved credential in Settings
  cpus: number                  # CPU limit (cores)
  memory: number                # Memory limit in MB
  nics: ContainerNic[]          # {id, name, mac, model, ip?} — same structure as VM NICs
  envVars: {key: string, value: string}[]
  ports: {containerPort: number, hostPort?: number, protocol: "tcp" | "udp"}[]
  command: string | null        # Optional entrypoint override
  restartPolicy: "always" | "on-failure" | "never"
  privileged: boolean
  mounts: {diskNodeId: string, mountPath: string}[]  # Populated from connected storage edges
  status: "running" | "stopped" | "created"
```

### Edge Connections

- Container NIC handle → Network node = network attachment (same handle pattern as VMs)
- Storage node → Container mount handle = volume attachment (same handle pattern as VM disk controllers)
- Container ↔ VM: rejected
- Container ↔ Container: rejected
- Container ↔ Router/Gateway: rejected (must go through a network)

### Start Order

Containers appear in the `startOrder` array alongside VMs. Entries are typed:

```json
{"type": "container", "containerId": "uuid-1"},
{"type": "vm", "vmId": "uuid-2"}
```

---

## 2. Registry Credentials (Settings)

### Backend Model

New `RegistryCredential` table:

| Column       | Type              | Notes                                      |
|-------------|-------------------|---------------------------------------------|
| id          | UUID              | Primary key                                 |
| user_id     | FK → User         |                                             |
| name        | String            | Display name ("Quay.io prod")               |
| registry    | String            | Hostname ("quay.io")                        |
| username    | String            |                                             |
| password    | String (encrypted) | Fernet-encrypted, same pattern as OCP pull secret |
| created_at  | DateTime          |                                             |
| updated_at  | DateTime          |                                             |

### API Endpoints

- `GET /api/v1/auth/registry-credentials` — list all (password masked)
- `POST /api/v1/auth/registry-credentials` — create
- `PUT /api/v1/auth/registry-credentials/{id}` — update
- `DELETE /api/v1/auth/registry-credentials/{id}` — delete

### Frontend

New "Registry Credentials" section in Settings page below OCP Pull Secret. Table with name, registry, username, masked password. Add/Edit/Delete actions. Container node properties panel shows a dropdown populated from this list.

---

## 3. Canvas UI

### Palette

New "Containers" section between Compute and Networking:

```
Compute:     [VM]
Containers:  [Container]
Networking:  [Network] [Router] [Gateway] [Load Balancer]
Storage:     [Disk] [ISO]
```

### Container Node Component

- Visually distinct from VM nodes — different icon and color accent
- Shows: name, image (truncated), status indicator
- NIC handles on top/bottom edges (one pair per NIC, same as VM)
- Mount handles on left/right edges (same position as VM disk controller handles)
- Compact by default

### Properties Panel

When a container node is selected:

- **General**: name, image (text input with placeholder `registry/org/image:tag`), registry credential (dropdown), privileged toggle
- **Resources**: CPU limit, memory limit
- **Networking**: NIC list with add/remove (identical UI to VM NIC panel), connected network + optional static IP
- **Environment**: key-value table with add/remove rows
- **Ports**: table with container port, optional host port, protocol dropdown
- **Volumes**: auto-populated from connected storage edges — disk name + mount path text input
- **Advanced**: restart policy dropdown, command override text input

---

## 4. Template YAML Format

New `containers:` section alongside `vms:` and `networks:`:

```yaml
name: example-with-containers
display_name: "Example with Registry and App"

networks:
  cluster:
    cidr: 10.0.0.0/24
    dhcp: true
    domain: example.local

containers:
  registry:
    image: docker.io/library/registry:2
    registry_credential: quay-prod
    cpus: 2
    memory_mb: 2048
    privileged: false
    restart_policy: always
    nics:
      - network: cluster
        ip: 10.0.0.5
    env:
      REGISTRY_STORAGE_DELETE_ENABLED: "true"
      REGISTRY_HTTP_ADDR: "0.0.0.0:5000"
    ports:
      - container_port: 5000
    disks:
      - size_gb: 100
        mount_path: /var/lib/registry
    command: null

  gitea:
    image: gitea/gitea:latest
    cpus: 1
    memory_mb: 1024
    restart_policy: always
    nics:
      - network: cluster
        ip: 10.0.0.6
    ports:
      - container_port: 3000
    disks:
      - size_gb: 20
        mount_path: /data

vms:
  bastion:
    vcpus: 2
    ram_gb: 4
    os: rhel-10
    nics:
      - network: cluster
        ip: 10.0.0.50

start_order:
  - container: registry
  - container: gitea
  - vm: bastion
```

### Template Loader

`resolve_inline_template()` extended to parse `containers:` section. Generates `containerNode` topology nodes with auto-layout positioned alongside VM nodes. Validates:

- Referenced networks exist in `networks:` section
- `registry_credential` (if set) exists on the user's account
- Disk sizes are positive integers

### Template Export

`export_topology_to_template()` extended to emit `containers:` section from `containerNode` topology nodes. Same round-trip guarantee as VMs: import → edit → export produces valid template YAML.

---

## 5. Deploy Pipeline

### Updated Deploy Steps

```python
DEPLOY_STEPS = [
    "eips",           # External IPs
    "networks",       # Create VXLAN networks (unchanged)
    "seeds",          # Cloud-init seed ISOs (VMs only)
    "images",         # Cache library images (VMs only)
    "container_pull", # Pull container images (NEW)
    "disks",          # Create disk images (includes container raw volumes)
    "vms",            # Create libvirt domains
    "containers",     # Create + start containers (NEW)
    "starting",       # Start VMs
    "dns",            # Configure DNS records
    "done",
]
```

### New Extraction Function

`_extract_containers(topology)` — mirrors `_extract_vms()`. Pulls container nodes from topology, resolves network connections (via NIC handle edges → network node → VNI) and storage connections (via mount handle edges → storage node).

### Container Deploy Flow

1. **`container_pull`**: For each container, decrypt registry credentials (if any), send to troshkad. Troshkad runs `podman login` then `podman pull`. Progress reports download bytes. Runs in parallel across containers.

2. **`disks` (extended)**: Raw image files for container volumes created via existing `/disks/create` endpoint with `format: raw`. After creation, formatted with `mkfs.ext4`.

3. **`containers`**: For each container (respecting start order), call troshkad with image, resource limits, env vars, ports, command, restart policy, privileged flag, network attachments (`{bridge, mac, ip}`), and volume mounts (`{disk_path, mount_path}`).

### Teardown

`undeploy_project_async()` gets a container cleanup step: `podman stop` + `podman rm` + unmount loop devices, before network teardown. Same ordering as VM undefine before network teardown.

---

## 6. Troshkad Agent — Container Handlers

### Container Lifecycle Endpoints

| Endpoint | Action |
|----------|--------|
| `POST /containers/pull` | `podman login` (if creds) + `podman pull`. Reports download progress. |
| `POST /containers/create` | Loop-mount raw volumes, `podman create` with full config. |
| `POST /containers/start` | `podman start` |
| `POST /containers/stop` | `podman stop` |
| `POST /containers/destroy` | `podman stop` + `podman rm`, unmount loop devices |
| `POST /containers/state` | `podman inspect --format '{{.State.Status}}'` |
| `POST /containers/states` | Batch state check for all project containers |

### Container Naming

`troshka-{project_id[:8]}-{container_id[:8]}` — same pattern as VM domain names.

### Network Attachment

Register existing VXLAN bridges with podman's network stack:

```bash
podman network create troshka-br-{vni} --driver bridge \
  --opt bridge.name=br-{vni} --subnet {cidr}
```

This tells podman to use the existing bridge, not create a new one. Containers and VMs share the same L2 segment — full ARP visibility, same DHCP/DNS from dnsmasq.

Container create uses:

```bash
podman create --network troshka-br-{vni}:ip={ip},mac={mac} ...
```

For multi-NIC containers, multiple `--network` flags.

### Volume Handling

Before container create, loop-mount each raw disk:

```bash
losetup --find --show /var/lib/troshka/vms/{project}/{disk}.raw
mount /dev/loopN /var/lib/troshka/vms/{project}/mnt-{disk_id[:8]}
```

Pass to podman: `--volume /var/lib/troshka/vms/{project}/mnt-{disk_id[:8]}:{mount_path}`

On destroy, unmount and detach loop devices before removing the container.

### Console and Logs

| Endpoint | Action |
|----------|--------|
| `POST /containers/logs` | `podman logs --tail N`. Returns output as job result. |
| `POST /containers/exec-ws` | WebSocket endpoint. Spawns `podman exec -it` with PTY. Pipes stdin/stdout through WebSocket. |

The exec WebSocket lives on troshkad itself (not vncd) — different protocol, different auth model. VNC relay and terminal relay stay separate.

---

## 7. Console Access — Frontend

### Log Viewer

- Route: container detail view or right-click context menu → "Logs"
- Backend: `GET /api/v1/projects/{id}/containers/{container_id}/logs?tail=500&follow=true`
- Non-follow: returns last N lines as plain text
- Follow mode: WebSocket streams `podman logs -f` output from troshkad

### Interactive Terminal

- Route: `/console?container={container_id}&project={project_id}&name={name}`
- Terminal rendered with xterm.js (standard web terminal library)
- Backend issues short-lived JWT (same pattern as VNC console)
- Troshkad spawns `podman exec -it {name} /bin/sh` (falls back to `/bin/bash`) attached to PTY
- WebSocket relay through troshkad's `/containers/exec-ws` endpoint

### UI Placement

- Container node right-click menu: "Terminal", "Logs"
- Container properties panel: "Logs" and "Terminal" buttons at top
- Both open in console panel area with tabs to switch between logs and terminal

---

## 8. Pattern Save / Restore

### Save

When capturing a pattern that includes containers:

1. **Container images**: `podman save {image} | gzip > container-{container_id[:8]}-image.tar.gz`
2. **Upload**: tar.gz goes to S3 at `patterns/{pattern_id}/container-{container_id[:8]}-image.tar.gz`
3. **Volumes**: raw disk files uploaded to S3 alongside VM disks (existing flow)
4. **Topology**: JSONB records original image ref (for display) and S3 artifact key (for restore)

### Restore / Deploy from Pattern

1. **Download**: container image tar.gz pulled from S3 cache (same download pipeline as VM disks)
2. **Load**: `gunzip -c {image}.tar.gz | podman load` — loads image into local podman store
3. **Volumes**: raw disk files restored from S3 (existing flow)
4. **Create**: container created using the loaded image — no external registry needed

Streaming through pipes (`podman save | gzip`, `gunzip | podman load`) avoids intermediate uncompressed files on disk.

---

## 9. File Changes Summary

### Frontend (new files)

- `src/frontend/src/components/canvas/nodes/ContainerNode.tsx` — container node component

### Frontend (modified)

- `src/frontend/src/components/canvas/Palette.tsx` — add Containers section
- `src/frontend/src/stores/canvasStore.ts` — add `ContainerNodeData`, `ContainerNic` interfaces, connection validation rules, node type registration
- `src/frontend/src/components/canvas/PropertiesPanel.tsx` — add container properties UI
- `src/frontend/src/app/settings/page.tsx` — add Registry Credentials section
- Start order panel — include containers with container icon

### Backend (new files)

- `src/backend/app/models/registry_credential.py` — RegistryCredential model
- `src/backend/app/api/registry_credentials.py` — CRUD API router
- `src/backend/app/services/container_service.py` — container-specific deploy/teardown logic

### Backend (modified)

- `src/backend/app/models/__init__.py` — register RegistryCredential
- `src/backend/app/main.py` — register registry credentials router
- `src/backend/app/services/deploy_service.py` — add `container_pull`, `containers` steps, `_extract_containers()`
- `src/backend/app/services/template_loader.py` — parse `containers:` section
- `src/backend/app/services/template_export.py` — emit `containers:` section
- `src/backend/app/services/pattern_service.py` — capture/restore container images
- Alembic migration for `registry_credentials` table

### Agent (modified)

- `src/troshkad/troshkad.py` — add container lifecycle handlers, exec WebSocket, logs endpoint

### Templates

- Example templates updated to include `containers:` section where appropriate
