# Troshka Pod Nodes — Design Spec

**Date**: 2026-06-24
**Status**: Approved
**Goal**: Add pod support to Troshka — groups of containers sharing a network namespace, deployed via `podman pod`.

## Summary

Extend the existing `containerNode` type with optional `isPod`, `initContainers`, and `containers` fields. When `isPod` is true, the node represents a podman pod with multiple sub-containers sharing one network namespace. All existing single-container behavior is unchanged.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| New type vs extend | Extend `containerNode` | Reuses all existing wiring (veth, handles, deploy, template) |
| Canvas appearance | Expanded list, collapsible | Shows sub-containers inline with a ▸/▾ toggle |
| Storage mounts | Reuse existing storageNodes | Consistent model, no special pod-internal volumes |
| Properties panel | Inline accordions | Matches existing NIC/env/port patterns |
| Init failure | Fail fast | No retry; init containers are idempotent, user can redeploy |
| Pod-level resources | Hidden | Per-container CPU/memory only; matches podman semantics |

## Data Model

### ContainerNodeData (TypeScript — canvasStore)

```typescript
interface PodContainer {
  name: string;
  image: string;
  registryCredentialId?: string | null;
  cpus: number;
  memory: number;           // MB
  envVars: ContainerEnvVar[];
  ports: ContainerPort[];
  command: string | null;
  mounts: ContainerMount[]; // references storageNode handles
}

// Added to existing ContainerNodeData:
interface ContainerNodeData {
  // ...existing fields (label, name, image, cpus, memory, nics, etc.)...
  isPod?: boolean;
  initContainers?: PodContainer[];
  podContainers?: PodContainer[];   // named "podContainers" to avoid collision with YAML "containers:" section
}
```

When `isPod` is true:
- Top-level `image`, `cpus`, `memory`, `command` are ignored (hidden in UI)
- Top-level `nics` define the pod's network attachment (shared namespace)
- Top-level `mounts` define storageNode connections
- Top-level `restartPolicy` and `privileged` apply pod-wide
- Top-level `envVars` and `ports` serve as pod-level defaults

### Topology JSONB

No schema migration needed — topology is untyped JSONB. The new fields are simply present on pod container nodes:

```json
{
  "id": "uuid",
  "type": "containerNode",
  "data": {
    "name": "showroom",
    "isPod": true,
    "nics": [{"id": "nic-uuid", "name": "eth0", "ip": "10.0.0.100"}],
    "mounts": [{"diskNodeId": "vol-uuid", "mountPath": "/shared"}],
    "restartPolicy": "always",
    "privileged": false,
    "initContainers": [
      {"name": "git-cloner", "image": "quay.io/rhpds/showroom-git-cloner:latest", "cpus": 1, "memory": 256, "envVars": [{"key": "GIT_REPO_URL", "value": "..."}], "ports": [], "command": null, "mounts": [{"diskNodeId": "vol-uuid", "mountPath": "/shared"}]}
    ],
    "containers": [
      {"name": "nginx", "image": "quay.io/rhpds/nginx:1.25", "cpus": 1, "memory": 256, "envVars": [], "ports": [{"containerPort": 80, "protocol": "tcp"}], "command": null, "mounts": [{"diskNodeId": "vol-uuid", "mountPath": "/usr/share/nginx/html"}]},
      {"name": "wetty", "image": "quay.io/rhpds/wetty:v2.7.6", "cpus": 1, "memory": 512, "envVars": [{"key": "SSH_HOST", "value": "10.0.0.50"}], "ports": [{"containerPort": 3000, "protocol": "tcp"}], "command": null, "mounts": []}
    ],
    "icon": "🫛",
    "status": "stopped"
  }
}
```

## Canvas Rendering

Extend `ContainerNode.tsx` with conditional pod rendering:

- **Header**: icon 🫛 (instead of 📦), name, status dot
- **Body**: hides `image` and `cpus/memory` lines. Shows IP and aggregated ports from all sub-containers.
- **Collapsible container list**: toggle chevron (▸/▾) after the body section. Expanded by default. Lists each main container with a status dot and name. Init containers shown dimmer (ephemeral).
- **Footer**: same start/stop/restart/logs buttons. Logs button opens a tabbed log viewer — one tab per container in the pod.
- **Handles**: unchanged — NIC handles (top/bottom) and mount handles (left/right) work identically since the pod shares one network namespace.

No new React Flow node type — `containerNode` handles both modes based on `isPod`.

## Template Format

Pods use the existing `containers:` YAML section with `type: pod`:

```yaml
containers:
  showroom:
    type: pod
    restart_policy: always
    nics:
      - network: cluster
        ip: 10.0.0.100
    mounts:
      - disk: shared-vol
        mount_path: /shared
    init_containers:
      - name: git-cloner
        image: quay.io/rhpds/showroom-git-cloner:latest
        env:
          GIT_REPO_URL: "{{ showroom_git_repo }}"
        mounts:
          - disk: shared-vol
            mount_path: /shared
      - name: antora-builder
        image: quay.io/rhpds/antora:v1.2.4
        mounts:
          - disk: shared-vol
            mount_path: /shared
    containers:
      - name: nginx
        image: quay.io/rhpds/nginx:1.25
        cpus: 1
        memory_mb: 256
        ports: [80]
        mounts:
          - disk: shared-vol
            mount_path: /usr/share/nginx/html
      - name: content
        image: quay.io/rhpds/showroom-content:v1.4.2
        cpus: 1
        memory_mb: 256
        ports: [8080]
      - name: wetty
        image: quay.io/rhpds/wetty:v2.7.6
        cpus: 1
        memory_mb: 512
        ports: [3000]
        env:
          SSH_HOST: 10.0.0.50
```

Without `type: pod`, existing single-container behavior is unchanged.

### Import (`template_loader.py`)

`resolve_inline_template()` / `generate_topology_from_template()`:
- Detect `type: pod` in container config
- Set `isPod: true` on topology node data
- Parse `init_containers` → `initContainers` array of `PodContainer` objects
- Parse `containers` → `containers` array of `PodContainer` objects
- Pod-level `nics` and `mounts` processed as before (same storageNode resolution)
- Per-sub-container `mounts` reference the same `diskNodeId` handles with different `mountPath`

### Export (`export_topology_to_template()`)

- Detect `isPod: true` on container node
- Emit `type: pod`, `init_containers`, `containers` sub-keys
- Reverse-map sub-container mounts to disk names via edges

## Troshkad (Host Agent)

### New Endpoints

**`POST /pods/create`**

Request body:
```json
{
  "project_id": "uuid",
  "pod_name": "showroom",
  "nics": [{"id": "nic-id", "bridge": "br-100", "mac": "52:54:00:...", "ip": "10.0.0.100/24", "gateway": "10.0.0.1"}],
  "init_containers": [
    {"name": "git-cloner", "image": "quay.io/rhpds/showroom-git-cloner:latest", "env": {"GIT_REPO_URL": "..."}, "mounts": ["/host/path:/shared"]}
  ],
  "containers": [
    {"name": "nginx", "image": "quay.io/rhpds/nginx:1.25", "cpus": 1, "memory": 256, "ports": [80], "env": {}, "mounts": ["/host/path:/usr/share/nginx/html"]}
  ],
  "restart_policy": "always",
  "privileged": false
}
```

Steps:
1. `podman pod create --name troshka-{project_id[:8]}-{pod_name} --network none --infra-container-name {pod_name}-infra`
2. Get infra container PID, create veth pairs per NIC, set MACs and IPs (reuse existing veth logic from `_handle_container_create`)
3. Create each init container: `podman create --pod {pod_name} --name {pod_name}-init-{name} -v ... -e ... {image}`
4. Create each main container: `podman create --pod {pod_name} --name {pod_name}-{name} --cpus {cpus} --memory {memory}m -v ... -e ... -p ... {image}`

**`POST /pods/start`**

Request body: `{"pod_name": "troshka-...-showroom"}`

Steps:
1. Run each init container sequentially: `podman start {name}` → `podman wait {name}` → check exit code
2. If any init container exits non-zero: return error with container name and exit code (fail fast)
3. `podman pod start {pod_name}` — starts all main containers

**`POST /pods/destroy`**

Request body: `{"pod_name": "troshka-...-showroom"}`

Steps:
1. `podman pod rm -f {pod_name}`
2. Clean up veth pairs (same cleanup as single containers)

### Extended Endpoints

- **`/containers/states`**: scan `podman pod list --format json --filter name=troshka-` alongside existing container scan. Return pod state (derived from infra container) plus per-container states.
- **`/containers/logs`**: accept `container` query param — when target is a pod, fetch logs from the named sub-container via `podman logs {pod_name}-{container_name}`.
- **`/containers/stop`**: detect if target is a pod, use `podman pod stop` instead of `podman stop`.
- **`/containers/start`**: detect if target is a pod, use the init→main sequencing from `/pods/start`.

## Deploy Service

Pod nodes deploy within the existing container pipeline:

**Step 3c (image pull)**: iterate over all images in `initContainers` + `containers` arrays. Same `_pull_container_image` logic.

**Step 3d (volume creation)**: unchanged — storageNodes connected to the pod get raw volumes created.

**Step 4c (container creation)**: detect `isPod` on topology node. Call `/pods/create` (instead of `/containers/create`) with the full sub-container spec.

**Step 4d (container start)**: call `/pods/start` for pods. Troshkad handles init→main sequencing. If any init container fails, mark the pod node as `error` in deploy progress.

**Start order**: pods participate identically to single containers — `entryType: "container"` with the pod's node ID.

**Destroy**: call `/pods/destroy` instead of `/containers/destroy`. Same cleanup order (pods/containers first, then networks).

**Pattern save/restore**: no special handling — `isPod`, `initContainers`, `containers` are plain JSONB fields. The topology remap already handles `mounts[].diskNodeId` references.

## Properties Panel

When a `containerNode` with `isPod: true` is selected:

- **Pod section** (top): name, restart policy, privileged toggle, NIC list (same as today)
- **Init Containers section**: accordion list of init containers, each expandable to show image, env vars, mounts, command. Add/remove buttons.
- **Main Containers section**: accordion list of main containers, each expandable to show image, cpus, memory, env vars, ports, mounts, command. Add/remove buttons.
- **Hidden fields**: top-level image, cpus, memory (not relevant for pods)

Each sub-container accordion shows the same field editors as the current single-container panel (image input, env var list, port list, mount list, command textarea) — just nested under the container name.

## Palette (Library Picker)

Add a "Pod" drag item alongside the existing "Container" item. Dragging it onto the canvas creates a `containerNode` with `isPod: true` and one empty main container. The user adds init/main containers via PropertiesPanel.

## Out of Scope

- Pod-to-pod communication (already works via networks)
- Pod health checks beyond "all main containers running"
- Pod resource limits (podman doesn't support pod-level cgroup limits)
- Init container restart policies (always run-once by design)
- Sidecar container semantics (Kubernetes-specific, not needed)
