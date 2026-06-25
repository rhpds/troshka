# Pull-Through Registry Toggle

## Problem

OCP cluster installs via Troshka pull images directly from `registry.redhat.io` and `quay.io`. When a pull-through registry mirror is available, there's no way to configure it from the Troshka UI — the config only flows through agnosticv template YAML. This means:

- Clusters built from Troshka's built-in OCP templates never use the mirror
- Seed images captured from those clusters bake in direct registry references
- IBI restores from those seeds fail when only mirror-only pull secrets are available
- The full Red Hat pull secret is exposed to students in lab environments

## Solution

Add a boolean toggle on the User model and settings page: "Pull secret is for a pull-through registry." When enabled, Troshka derives the mirror config from the pull secret and injects it into OCP installs automatically.

## Data Model

**User model** — add four columns:

- `pull_through_registry: bool, default=False`
- `pull_through_registry_url: str, nullable` — registry hostname (e.g. `registry-quay-quay-enterprise.apps.example.com`)
- `pull_through_registry_user: str, nullable` — registry username
- `pull_through_registry_password: str, nullable` — registry password (encrypted, same as `ocp_pull_secret`)

When the toggle is on, the backend:

1. Constructs the pull secret JSON: `{"auths":{"<url>":{"auth":"<base64(user:pass)>"}}}`
2. Builds the pull-through config dict with hardcoded OCP org mappings:
   - `registry.redhat.io` → `{url}/registry_redhat_io`
   - `quay.io` → `{url}/quay_io`

The OCP Pull Secret field on the settings page is replaced by the three pull-through fields when the toggle is enabled. The user no longer needs to paste a pull secret JSON — Troshka builds it.

## API

Extend existing `/auth/ocp-pull-secret` endpoints:

**GET** response adds pull-through fields:
```json
{"has_secret": true, "masked": "...", "pull_through_registry": false, "pull_through_registry_url": ""}
```

**PUT** accepts pull-through fields alongside or instead of `pull_secret`:
```json
{
  "pull_through_registry": true,
  "pull_through_registry_url": "registry-quay.apps.example.com",
  "pull_through_registry_user": "puller",
  "pull_through_registry_password": "secret123"
}
```
When `pull_through_registry` is true with URL/user/password, the backend constructs and stores the pull secret JSON automatically. The `pull_secret` field is ignored.

**PATCH** `/auth/ocp-pull-secret` — toggle or update pull-through fields without full replacement.

**Validation:**
- Setting `pull_through_registry=true` without all three fields (url, user, password) → 400
- When toggle is off and no `pull_secret` provided → 400

## Backend Integration

In `projects.py` `/from-template` endpoint, after resolving the template:

- If `resolved` already has `pull_through_registry` (from agnosticv template YAML) → use it (agnosticv wins)
- Otherwise, if `user.pull_through_registry` is true → build the config from the user's stored URL/user/password and inject it into `resolved`
- Also construct `pull_secret_json` from the pull-through creds instead of the `ocp_pull_secret` field

This feeds into the existing `agent_template.py` code unchanged:
- `imageDigestMirrorSet` in install-config.yaml (line 859-869)
- `registries.conf` in bastion cloud-init (lines 444-460, 527-542)
- Podman mirror config on bastions

The config dict matches the existing shape:
```python
{
    "enabled": True,
    "url": "registry-quay.apps.example.com",
    "orgs": {
        "registry.redhat.io": "registry_redhat_io",
        "quay.io": "quay_io",
    }
}
```

## Frontend

Settings page (`src/frontend/src/app/settings/page.tsx`), in the OCP Pull Secret section:

- PatternFly `Switch` component: "Use a pull-through registry"
- When **off**: shows the existing pull secret textarea (paste raw JSON)
- When **on**: hides the textarea, shows three fields instead:
  - Registry URL (text input)
  - Username (text input)
  - Password (password input)
- Saving sends PUT with the pull-through fields; backend constructs the pull secret
- Helper text: "Mirror image pulls through this registry instead of pulling directly from registry.redhat.io and quay.io."

## Priority Rules

1. agnosticv template `pull_through_registry` config → highest priority (when present in template YAML)
2. User's `pull_through_registry` toggle → fallback for UI-driven deploys
3. No config → direct pulls (current behavior)

## Migration

Alembic migration adding four columns to `users` table:
- `pull_through_registry` boolean, `server_default='false'`
- `pull_through_registry_url` varchar(255), nullable
- `pull_through_registry_user` varchar(255), nullable
- `pull_through_registry_password` text, nullable (encrypted)

No data migration needed.

## What This Does NOT Cover

- Container signature policy (`policy.json`) relaxation — handled in agnosticd IBI installer role's ignition override, not in Troshka backend
- Non-OCP installs — pull-through is OCP-specific (registry.redhat.io + quay.io)
- Custom org mappings — use agnosticv template path for non-standard setups
