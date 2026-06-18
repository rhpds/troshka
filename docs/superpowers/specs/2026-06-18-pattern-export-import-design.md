# Pattern Export/Import Design Spec

## Problem

Troshka patterns (saved VM environments with disk images) are locked to a single Troshka instance and its S3 bucket. There's no way to:
- Move a pattern between Troshka instances (e.g. dev → prod, or between organizations)
- Back up a pattern outside of S3
- Import a pattern built on another instance into a different S3 image library

The existing `export-template` only exports the infrastructure topology YAML (VMs, networks, disk sizes) — not the actual disk images or installed software.

## Goal

Enable full pattern portability: export a pattern as a self-contained archive that includes topology metadata + all disk images, and import that archive into another Troshka instance's S3 storage.

## Architecture

### What a Pattern Contains

| Component | Storage | Typical Size |
|-----------|---------|-------------|
| Topology JSONB | DB `patterns.topology` column | ~10-50 KB |
| Metadata | S3 `patterns/{id}/metadata.json` | ~5 KB |
| Pattern disks | S3 `patterns/{id}/{disk_id}.qcow2` | 5-200 GB each |
| PatternDisk records | DB `pattern_disks` table | N rows |

A typical OCP pattern: 1 bastion (20 GB) + 3 control plane (30 GB each) = ~110 GB total. A RAN lab with SNOs could be 200+ GB.

### Export Flow

```
User clicks "Export Pattern"
  → Backend creates export job (background thread)
    → Generates manifest.json (topology + disk inventory + checksums)
    → Streams all pattern disks from S3
    → Packages into a .tar archive (no compression — qcow2 doesn't compress well)
    → Uploads archive to S3 at exports/{pattern_id}/{timestamp}.tar
    → Returns presigned download URL (valid 24h)
  → Frontend polls progress, shows download link when ready
```

**Why tar, not zip**: qcow2 files are already sparse/compressed internally. ZIP/gzip adds CPU cost with negligible size reduction. Tar is a simple container with no compression overhead.

**Why S3 staging**: Patterns can be 200+ GB. Streaming directly to the browser would time out. Upload to S3 first, then give the user a presigned URL for direct S3 download.

### Export Archive Structure

```
{pattern_name}.tar
├── manifest.json          # Topology, disk inventory, checksums
├── disks/
│   ├── {disk_id_1}.qcow2  # Flattened disk images
│   ├── {disk_id_2}.qcow2
│   └── ...
```

### manifest.json

```json
{
  "version": 1,
  "type": "troshka-pattern",
  "exported_at": "2026-06-18T12:00:00Z",
  "source_instance": "troshka.example.com",
  "pattern": {
    "name": "5G RAN Lab v4.20",
    "description": "Hub cluster + 3 SNO targets",
    "topology": { /* full topology JSONB */ },
    "tags": {}
  },
  "disks": [
    {
      "id": "disk-uuid-1",
      "filename": "disks/disk-uuid-1.qcow2",
      "source_vm_id": "vm-node-uuid",
      "source_vm_name": "bastion",
      "source_disk_id": "storage-node-uuid",
      "format": "qcow2",
      "size_bytes": 21474836480,
      "virtual_size_bytes": 214748364800,
      "checksum_sha256": "abc123..."
    }
  ],
  "ocp_metadata": {
    "cluster_name": "hub",
    "base_domain": "5g-deployment.lab"
  },
  "template_yaml": { /* exported infra_template for reference */ }
}
```

### Import Flow

```
User uploads .tar archive (or provides S3/HTTP URL)
  → Backend creates import job (background thread)
    → Extracts and validates manifest.json
    → Validates checksums of all disk images
    → Uploads each disk to S3 at patterns/{new_id}/{disk_id}.qcow2
    → Creates Pattern + PatternDisk DB records
    → Remaps topology IDs (new UUIDs for nodes, edges, NICs, disks)
    → Sets pattern state to "available"
  → Frontend polls progress, shows pattern when ready
```

### Import Sources

1. **File upload** — user uploads .tar directly via multipart (chunked, same as library upload)
2. **URL** — user provides an HTTPS URL to download the archive from (for cross-instance transfer without downloading locally)
3. **S3 presigned URL** — the export from another instance generates a presigned URL that can be pasted directly

## API Endpoints

### Export

```
POST /api/v1/patterns/{pattern_id}/export
  → 202 Accepted
  → Body: { "export_id": "uuid", "state": "packaging" }

GET /api/v1/patterns/{pattern_id}/export/{export_id}/progress
  → { "state": "packaging|uploading|ready|error",
      "progress_pct": 45,
      "total_bytes": 112000000000,
      "transferred_bytes": 50400000000,
      "download_url": "https://s3.../exports/...",  // when state=ready
      "expires_at": "2026-06-19T12:00:00Z" }

DELETE /api/v1/patterns/{pattern_id}/export/{export_id}
  → 204 (cleanup S3 archive)
```

### Import

```
POST /api/v1/patterns/import
  Content-Type: multipart/form-data OR application/json
  Body (multipart): file=@pattern.tar
  Body (json): { "url": "https://..." }
  → 202 Accepted
  → Body: { "import_id": "uuid", "state": "downloading" }

GET /api/v1/patterns/import/{import_id}/progress
  → { "state": "downloading|validating|uploading|creating|ready|error",
      "progress_pct": 30,
      "pattern_id": "uuid",  // when state=ready
      "error": "..." }
```

## Implementation Details

### Export Job Steps

1. **Collect disk inventory** — query `PatternDisk` records for the pattern
2. **Generate manifest** — topology + disk metadata + checksums (from DB, already computed during capture)
3. **Stream tar assembly** — for each disk:
   - Open S3 GetObject stream
   - Write tar header + stream body to output tar
   - Track bytes transferred for progress
4. **Write manifest** — add `manifest.json` as first entry in tar
5. **Upload to S3** — multipart upload to `exports/{pattern_id}/{export_id}.tar`
6. **Generate presigned URL** — 24h expiry
7. **Update state** — mark as ready with download URL

### Import Job Steps

1. **Download/receive archive** — stream to temp file on disk (or directly from S3 URL)
2. **Extract manifest** — read `manifest.json`, validate version and structure
3. **Validate checksums** — for each disk in manifest, compute SHA256 and compare
4. **Create Pattern** — new ID, topology from manifest (with ID remapping)
5. **Upload disks** — for each disk:
   - Extract from tar
   - Upload to S3 at `patterns/{new_pattern_id}/{new_disk_id}.qcow2`
   - Create `PatternDisk` record
6. **Finalize** — set pattern state to "available"

### Topology ID Remapping on Import

When importing, ALL internal IDs must be regenerated (same logic as pattern deploy):
- Node IDs (VM, network, storage, gateway)
- Edge IDs, source/target references
- Edge sourceHandle/targetHandle references
- NIC IDs + MACs
- Disk controller IDs
- `bootDevices[]`, `startOrder[].vmId`, `startOrder[].waitForVm`
- `externalIps[].vmId`, `hiddenNodeIds[]`
- PatternDisk `source_disk_id` and `source_vm_id` mappings

### Progress Tracking

Module-level dict (same pattern as deploy/capture progress):

```python
_export_progress: dict[str, dict] = {}
_import_progress: dict[str, dict] = {}
```

### Temp Storage

- Export: streams directly from S3 → tar → S3, minimal local disk usage
- Import: needs temp space for the full archive + extracted disks. Use `/tmp/troshka-import/{import_id}/` with cleanup after completion.

### Size Limits

- No hard limit on export size (patterns can be 500+ GB)
- Import: validate manifest before extracting disks — reject if total size exceeds available S3 quota
- Presigned download URLs: 24h expiry, single-use recommended

## Frontend UI

### Export Button

On pattern detail page, add "Export" button (next to Share):
- Click → confirmation modal showing pattern name, disk count, total size
- "Export" button starts the job
- Progress bar with bytes transferred / total
- When ready: "Download" button with presigned URL
- Archive auto-deletes from S3 after 24h (or manual cleanup)

### Import

On patterns list page, add "Import Pattern" button:
- Click → modal with two tabs: "Upload File" and "From URL"
- Upload: drag-and-drop or file picker for .tar
- URL: paste presigned URL or HTTPS link
- Progress bar during download/validation/upload
- When ready: redirects to new pattern detail page

## Security

- Export: requires pattern ownership or admin
- Import: requires authenticated user, pattern created under their account
- Presigned URLs: S3-signed, time-limited, no auth bypass
- Manifest validation: reject unknown versions, validate all required fields
- Checksum verification: SHA256 on every disk before importing
- No arbitrary file extraction — only process files listed in manifest

## Future Considerations

- **Incremental export**: if two patterns share backing images, only export the diff
- **Pattern marketplace**: public pattern registry with import-from-URL
- **Cross-region replication**: export to one S3 region, import to another
- **Streaming import**: pipe S3 GetObject directly into import without local temp file (requires known tar structure)
