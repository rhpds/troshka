# Patterns & VM Snapshots Design

## Overview

Two related capabilities for reusable environment provisioning:

1. **Patterns** — capture an entire project (VMs, networking, disks, topology) as an immutable, reusable artifact. Stamp out hundreds of identical environments for labs and demos.
2. **VM Snapshots** — capture a single VM (config + disks) to the library for import into other projects.

## Terminology

- **Pattern**: a point-in-time capture of a full project — topology, VM configs, network configs, and disk images. Immutable once created. To update, deploy the pattern to a project, make changes, and save a new pattern.
- **VM Snapshot**: a capture of a single VM's configuration and disk state, stored as a `LibraryItem` with type `snapshot`.

---

## Data Model

### Pattern (new table)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `name` | String | e.g., "RHEL Security Lab v2" |
| `description` | Text | Optional |
| `owner_id` | FK → User | Creator |
| `visibility` | Enum | `private`, `shared`, `public` |
| `source_project_id` | UUID | Project it was captured from (nullable — not set for API-created patterns) |
| `topology` | JSONB | Full topology snapshot (nodes, edges, VM configs, network configs) |
| `state` | Enum | `creating`, `available`, `error`, `deleting` |
| `total_size_bytes` | BigInteger | Sum of all disk images |
| `tags` | JSONB | Searchable metadata |
| `created_at` | Timestamp | |

### PatternDisk (new table)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `pattern_id` | FK → Pattern | |
| `source_disk_id` | UUID | Original disk ID in the source project topology (for mapping edges/attachments) |
| `source_vm_id` | UUID | Which VM this disk belonged to |
| `s3_key` | String | S3 path to the disk image |
| `format` | String | qcow2 or raw |
| `size_bytes` | BigInteger | Actual image size |
| `virtual_size_bytes` | BigInteger | Logical disk size |
| `checksum_sha256` | String | Integrity check |
| `state` | Enum | `uploading`, `available`, `error` |

### PatternShare (new table)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `pattern_id` | FK → Pattern | |
| `user_id` | FK → User | |
| `created_at` | Timestamp | |

### LibraryItem changes (for VM Snapshots)

New field on `LibraryItem`:

- `vm_config` — JSONB, stores full VM configuration (vCPUs, RAM, NICs, disk controllers, boot method, cloud-init settings, console type, auto-start)

Existing field used:

- `source_vm_id` — UUID, optional lineage tracking (already in schema, currently unused)

Remove `source_project_id` from `LibraryItem` — not needed for VM snapshots (derivable from VM if needed).

### LibraryItemDisk (new table)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `library_item_id` | FK → LibraryItem | |
| `s3_key` | String | S3 path to disk image |
| `format` | String | qcow2 or raw |
| `size_bytes` | BigInteger | Actual image size |
| `virtual_size_bytes` | BigInteger | Logical disk size |
| `boot_order` | Integer | Disk ordering from source VM |
| `checksum_sha256` | String | Integrity |
| `state` | Enum | `uploading`, `available`, `error` |

Used by snapshot-type `LibraryItem` entries. Non-snapshot types (templates, ISOs) continue using `s3_key` directly on `LibraryItem`.

---

## API Design

### Patterns

```
POST   /api/v1/patterns/                         Create pattern (from project OR from payload)
GET    /api/v1/patterns/                         List patterns (filtered by visibility/tags)
GET    /api/v1/patterns/{id}                     Get pattern details + topology
DELETE /api/v1/patterns/{id}                     Delete pattern + S3 cleanup
PATCH  /api/v1/patterns/{id}                     Update metadata (name, description, tags, visibility)
```

### Pattern sharing

```
POST   /api/v1/patterns/{id}/share               Share with user(s)
DELETE /api/v1/patterns/{id}/share/{user_id}     Revoke share
```

### Pattern deployment

```
POST   /api/v1/patterns/{id}/deploy              Create single project from pattern
POST   /api/v1/patterns/{id}/bulk-deploy         Create N projects from pattern
```

### Pattern creation progress

```
GET    /api/v1/patterns/{id}/progress             Disk upload progress during creation
```

### VM Snapshots

```
POST   /api/v1/projects/{pid}/vms/{vid}/snapshot  Capture VM snapshot to library
GET    /api/v1/library/?type=snapshot              List snapshots (existing library endpoint)
```

### Import VM snapshot into project

```
POST   /api/v1/projects/{pid}/import-vm            Import snapshot VM into project topology
```

---

## Pattern Creation

### From a project (canvas or API)

1. User clicks "Save as Pattern" on canvas or project page, or calls `POST /api/v1/patterns/` with `source_project_id`
2. If any VMs are running, the UI shows a warning: "For best results, stop all VMs before creating a pattern. Running VMs may have inconsistent disk state." User can proceed or cancel.
3. Backend creates `Pattern` (state: `creating`) and `PatternDisk` entries (state: `uploading`). Topology JSON is copied from the project.
4. Host agent uploads each disk image to S3 under `patterns/{pattern_id}/{disk_id}.qcow2`. For running VMs, the agent uses QEMU snapshot to get a point-in-time copy. Progress is tracked per-disk.
5. Once all disks are uploaded and checksummed, the pattern transitions to `available`. If any disk fails, the pattern goes to `error`.

The topology JSON is sanitized — UUIDs for VMs, disks, networks are preserved as reference IDs for internal mapping (edges, disk attachments stay consistent), but when stamping out a new project, everything gets new UUIDs.

### From the API (programmatic, no source project)

1. Client sends `POST /api/v1/patterns/` with topology JSON and disk mappings referencing existing library items:

```json
{
  "name": "RHEL Security Lab",
  "topology": { ... },
  "disk_mappings": [
    {"source_disk_id": "disk-ref-in-topology", "library_item_id": "existing-library-item-uuid"},
    {"source_disk_id": "disk-ref-in-topology", "library_item_id": "existing-library-item-uuid"}
  ]
}
```

2. Backend creates `Pattern` + `PatternDisk` records. Disk images are copied from the referenced library items' S3 locations to the pattern's S3 prefix.
3. Pattern transitions to `available`.

Disk images must already exist in the library (uploaded through the existing library upload/import flow). The pattern creation endpoint does not accept arbitrary URLs.

---

## Pattern Deployment (Stamping Out Projects)

### Single project

1. User browses Library → Patterns, clicks "Create Project from Pattern", or calls `POST /api/v1/patterns/{id}/deploy`
2. New project created in `draft` state with topology cloned from the pattern — all new UUIDs, new VNIs allocated, new MAC addresses generated
3. Internal network addressing (CIDRs, DHCP ranges, DNS domains) is preserved identically — VXLAN isolation keeps projects separated
4. Project appears on the canvas where the user can review/customize before deploying
5. On deploy: host pulls pattern disk images from S3 to local cache (if not already cached), creates qcow2 CoW overlay disks for each VM backed by the cached base images

### Bulk deploy

1. User selects pattern, clicks "Bulk Deploy", or calls `POST /api/v1/patterns/{id}/bulk-deploy`
2. Specifies: count, naming convention (e.g., `rhel-lab-{n}` producing `rhel-lab-001` through `rhel-lab-200`), and whether to auto-deploy immediately or create as drafts
3. System creates all projects, distributes across available hosts using the existing placement service
4. Progress UI shows overall status (e.g., "147/200 deployed")
5. Base images cached once per host, thin CoW overlays per project

### Disk handling on host

- Pattern disk images cached at `/var/lib/troshka/cache/patterns/{pattern_id}/{disk_id}.qcow2` (read-only base)
- Each stamped project gets overlay disks at `/var/lib/troshka/{project_id}/{new_disk_id}.qcow2` backed by the cached base
- Overlays are thin — only writes are stored per project
- Cache eviction follows existing image cache logic (LRU when storage pressure)

---

## VM Snapshots

### Capture

1. User right-clicks VM on canvas → "Save VM Snapshot", or calls `POST /api/v1/projects/{pid}/vms/{vid}/snapshot`
2. Same running-VM warning as patterns — recommend stopping first
3. User provides name, description
4. Backend creates `LibraryItem` (type: `snapshot`) with `vm_config` populated from the VM's current configuration, plus `LibraryItemDisk` entries for each disk
5. Host agent uploads disk image(s) to S3
6. Snapshot appears in Library → Images

### Import into another project

1. User drags VM from Library panel onto canvas, or calls `POST /api/v1/projects/{pid}/import-vm` with `snapshot_id`
2. VM node appears pre-configured from `vm_config` (vCPUs, RAM, NICs, disk controllers, boot method, cloud-init)
3. Disk nodes created and wired to VM automatically, matching the original layout
4. User can modify any configuration before deploying
5. On deploy: snapshot disk images pulled to host cache, CoW overlays created

---

## Patterns & Immutability

Patterns are immutable once created. To update a pattern:

1. Deploy the pattern to a new project (creates draft on canvas)
2. Make changes — add/remove VMs, reconfigure networking, update software, etc.
3. Stop VMs
4. Save as a new pattern

Old patterns remain available. The naming convention can indicate lineage (e.g., "RHEL Security Lab v2").

---

## Access Control

Patterns use a three-tier visibility model:

- **Private** — only the creator can see and deploy from the pattern
- **Shared** — creator shares with specific users via `PatternShare`. Shared users can deploy but not edit/delete
- **Public** — visible to all users. Admins can promote patterns to public. Anyone can deploy from a public pattern

This mirrors the existing Library system's user/public split.

---

## UI Changes

### Library redesign

The Library section in the sidebar gains a sub-navigation:

- **Images** — ISOs, templates, VM snapshots (single-file/single-VM assets)
- **Patterns** — project-level captures (multi-VM, topology, disks)

### "Save as Pattern" button

Appears in two locations:
- Canvas toolbar
- Project page

### "Save VM Snapshot" action

Available via right-click context menu on a VM node on the canvas.

### Import VM from library

Drag a VM snapshot from the Library panel onto the canvas. Creates a fully-configured VM node.

### Pattern browser

Library → Patterns shows a browsable/searchable list with:
- Name, description, tags
- VM count, total disk size
- Visibility badge (private/shared/public)
- "Create Project" and "Bulk Deploy" actions

---

## Host Agent Changes

### New command: `capture_disk`

Uploads a VM's disk image from the host to S3. For running VMs, uses QEMU snapshot to get a consistent point-in-time copy. Reports upload progress.

Used by both pattern creation and VM snapshot capture.

### Extended: disk creation with CoW overlay

When a disk references a pattern or snapshot base image, the agent creates a qcow2 CoW overlay backed by the cached base image instead of copying the full image.

### Cache paths

- Pattern images: `/var/lib/troshka/cache/patterns/{pattern_id}/{disk_id}.qcow2`
- Snapshot/template images: `/var/lib/troshka/cache/{item_id}/` (existing)

---

## Canvas & API Parity

All operations work through both the canvas UI and the REST API as first-class interfaces:

| Operation | Canvas | API |
|-----------|--------|-----|
| Create pattern from project | "Save as Pattern" button | `POST /api/v1/patterns/` with `source_project_id` |
| Create pattern programmatically | N/A | `POST /api/v1/patterns/` with topology + disk_mappings |
| Create project from pattern | Library → Patterns → "Create Project" | `POST /api/v1/patterns/{id}/deploy` |
| Bulk deploy from pattern | "Bulk Deploy" in Patterns UI | `POST /api/v1/patterns/{id}/bulk-deploy` |
| Snapshot a VM | Right-click → "Save VM Snapshot" | `POST /api/v1/projects/{pid}/vms/{vid}/snapshot` |
| Import VM snapshot | Drag from library onto canvas | `POST /api/v1/projects/{pid}/import-vm` with `snapshot_id` |

The API is the source of truth — the canvas calls the same endpoints. Automation and CI/CD pipelines can manage patterns entirely through the API.
