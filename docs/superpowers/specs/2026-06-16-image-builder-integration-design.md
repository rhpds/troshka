# Red Hat Image Builder Integration — Design Spec

## Summary

Integrate Red Hat Insights Image Builder API into Troshka to automatically build custom RHEL host images with all required packages (qemu-kvm, libvirt, nftables, etc.) pre-installed. Images are built in the cloud and uploaded directly to GCP/Azure, eliminating the need for RHSM registration at boot time.

## Problem

BYOS images on GCP/Azure have no package repos configured. Cloud-init `dnf install` fails without RHSM registration. PAYG images work but cost ~$0.06-0.13/hr extra. A custom image with packages pre-installed solves both problems — no repo dependency, no PAYG premium.

## Architecture

```
Settings page → store Red Hat offline token
Provider page → "Build Host Image" button
  → Backend calls Insights Image Builder API
  → Builds RHEL image with custom packages
  → Uploads directly to GCP/Azure
  → Sets as provider's default_image
```

## API Flow

### 1. Authentication

- User provides Red Hat offline token in Settings page (`/settings`)
- Stored encrypted in config (like other secrets)
- Backend exchanges offline token for access token: `POST https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token`

### 2. Create Compose

```
POST https://console.redhat.com/api/image-builder/v1/compose
Authorization: Bearer {access_token}

{
  "distribution": "rhel-10",
  "image_requests": [{
    "architecture": "x86_64",
    "image_type": "gcp",  // or "azure"
    "upload_request": {
      "type": "gcp",
      "options": {
        "share_with_accounts": ["troshka-provider@project.iam.gserviceaccount.com"]
      }
    }
    // Azure:
    // "type": "azure",
    // "options": {
    //   "tenant_id": "...",
    //   "subscription_id": "...",
    //   "resource_group": "troshka-rg",
    //   "image_name": "troshka-host-rhel10"
    // }
  }],
  "customizations": {
    "packages": [
      "qemu-kvm", "libvirt", "virt-install", "dnsmasq", "nftables",
      "python3", "xorriso", "ncat", "sshpass", "nfs-utils",
      "cloud-init", "cloud-utils-growpart"
    ],
    "services": {
      "enabled": ["libvirtd", "nftables", "sshd"]
    }
  }
}
```

### 3. Poll Compose Status

```
GET https://console.redhat.com/api/image-builder/v1/composes/{compose_id}
```

Poll every 30s until `status == "success"` (~10-15 minutes).

### 4. Get Image Reference

Response includes the cloud image ID/name that was uploaded. Set it as `provider.default_image`.

## One-Time Cloud Setup

### Azure
Image Builder needs to be authorized as an application in the Azure tenant:
- App ID: `b3e1c24c-3025-4c1b-b694-35caba6fcb6e` (Red Hat's Image Builder)
- Grant Contributor role on the resource group

### GCP
- Share the resulting image with the service account email
- Or use the project ID directly in upload options

## Implementation

### Backend

**New file**: `src/backend/app/services/image_builder_service.py`
- `get_access_token(offline_token)` — exchange offline token for bearer token
- `start_compose(token, distribution, image_type, upload_options, packages)` — POST compose
- `poll_compose(token, compose_id)` — GET status
- `build_host_image(provider, rhel_version)` — orchestrate the full flow

**New API endpoint**: `POST /providers/{id}/build-image`
- Accepts: `{"rhel_version": "rhel-10"}` (optional, defaults to rhel-10)
- Starts compose in background thread
- Polls until complete
- Updates `provider.default_image` with result

**Settings storage**: Red Hat offline token stored in config DB or `config.local.yaml`

### Frontend

**Settings page** (`/settings`):
- New field: "Red Hat Offline Token" (password input)
- Link to generate token: `https://access.redhat.com/management/api`
- Save button

**Provider page**:
- "Build Host Image" button (next to Discover Images)
- Shows when provider type is `gcp` or `azure`
- Progress indicator while building (~10-15 min)
- RHEL version dropdown (9 or 10)
- Status: "Building...", "Uploading...", "Done — image set as default"

### Model Changes

None — `default_image` already stores the image reference. The offline token goes in settings config, not DB.

## Files to Create/Modify

| File | Changes |
|------|---------|
| `services/image_builder_service.py` | New — API client |
| `api/providers.py` | New endpoint: build-image |
| `frontend/settings/page.tsx` | Add offline token field |
| `frontend/admin/providers/page.tsx` | Build Image button |
| `config/config.yaml` | Add `redhat.offline_token` setting |

## Testing

1. Get offline token from https://access.redhat.com/management/api
2. Save in settings
3. Click "Build Host Image" on GCP provider
4. Wait ~15 min for compose
5. Image appears in GCP and is set as default
6. Provision a host — cloud-init skips package installs (already installed)

## Future Enhancements

- Cache built images — don't rebuild if packages haven't changed
- Support custom package lists per provider
- Periodic image refresh (new RHEL minor versions)
- AWS support (currently uses Access2 AMIs which work fine, but custom images could be useful)
