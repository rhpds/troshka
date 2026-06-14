# Console Setup Automation — Design Spec

## Goal

Make console DNS/TLS setup fully automated from the admin UI. Zero manual AWS CLI commands, SSH, or config file edits. An admin clicks "Setup Console" on a provider, enters a domain, and everything is configured.

## Current State

The direct console proxy (troshka-vncd) is implemented but setup requires manual steps:
- Creating a Route53 hosted zone
- Delegating NS records from the parent zone
- Creating IAM role + instance profile
- Editing `config.local.yaml` with zone ID and base domain
- Manually setting `console_domain` on existing hosts

## Design

### Provider Model Changes

Move console config from `config.yaml` to the Provider model. New columns:

| Column | Type | Purpose |
|--------|------|---------|
| `console_zone_id` | String(100), nullable | Route53 hosted zone ID |
| `console_base_domain` | String(255), nullable | e.g., `troshka.dev.rhdp.net` |
| `console_nameservers` | JSONB, nullable | NS records for delegation display |

A provider with `console_zone_id` set is "console-enabled" — all hosts provisioned under it get DNS records, certbot certs, and vncd automatically.

### Backend Endpoints

**`POST /api/v1/providers/{id}/setup-console`**

Input: `{"base_domain": "troshka.dev.rhdp.net"}`

Steps:
1. Validate domain format (must be a valid DNS name)
2. Check if a Route53 hosted zone already exists for this domain — if so, reuse it
3. If not, create the hosted zone
4. Create IAM role `troshka-certbot-role` + instance profile `troshka-certbot-profile` (idempotent — reuses if they exist)
5. Attach inline policy scoped to the new hosted zone (`route53:ChangeResourceRecordSets` on the zone, `route53:GetChange` + `route53:ListHostedZones` on `*`)
6. Store `console_zone_id`, `console_base_domain`, `console_nameservers` on the provider
7. Return `{"zone_id": "...", "nameservers": [...], "base_domain": "..."}`

**`DELETE /api/v1/providers/{id}/console`**

Steps:
1. Delete all A records in the hosted zone (for hosts under this provider)
2. Delete the hosted zone from Route53
3. Clear `console_zone_id`, `console_base_domain`, `console_nameservers` on the provider
4. Clear `console_domain` on all hosts under this provider
5. Note: IAM role/instance profile are shared across providers, so leave them (they're idempotent and harmless)

### Config Migration

Remove `console.hosted_zone_id` and `console.base_domain` from `config.yaml`. Update all code that reads `config.console.*` to instead read from the host's provider:

- `console_dns.py` → `upsert_dns_record` and `delete_dns_record` take `hosted_zone_id` as a parameter (already do)
- `provisioner.py` → `provision_host` reads console config from the provider passed via kwargs
- `agent_deployer.py` → `deploy_agent` already receives `console_domain` as a parameter
- `hosts.py` → provisioning thread reads from the provider model instead of `config.console`
- `providers.py` → VPC setup reads console zone from the provider model instead of `config.console`

### Frontend Changes

**Provider card** (in `/admin/providers/page.tsx`):

When console is **not configured**:
- "Setup Console" button (next to "Setup VPC")
- Clicking shows an inline form: text input for domain name + "Create" button

When console is **configured**:
- Shows the domain as a badge/label (e.g., `troshka.dev.rhdp.net`)
- Shows NS delegation info in a collapsible panel:
  > "To complete setup, add NS records for `troshka.dev.rhdp.net` in your parent DNS zone pointing to: `ns-852.awsdns-42.net`, ..."
- "Remove Console" button (danger variant, with confirmation)

**ProviderResponse schema** additions:
- `console_base_domain: str | None`
- `console_nameservers: list | None`
- `console_configured: bool` (derived: `console_zone_id is not None`)

### Host Provisioning Integration

Already wired in from the direct console proxy implementation. The provisioning thread in `hosts.py`:
1. Reads `provider.console_base_domain` (instead of `config.console.base_domain`)
2. Reads `provider.console_zone_id` (instead of `config.console.hosted_zone_id`)
3. Creates DNS A record: `{instance_id}.{base_domain}` → host public IP
4. Sets `host.console_domain`
5. Passes `console_domain` to `deploy_agent()` which installs vncd + certbot

Host removal already calls `delete_dns_record()` using the `host.console_domain` and provider credentials.

### Instance Profile Attachment

The `provision_host()` function already conditionally adds `IamInstanceProfile` to the `RunInstances` call. Change the condition from `config.console.hosted_zone_id` to checking the provider's `console_zone_id` (passed via kwargs).

### What Stays the Same

- troshka-vncd daemon, certbot, agent deployer installation — all unchanged
- Frontend console page (`/console`) — unchanged
- JWT signing/verification — unchanged
- Security group port 443 rule — unchanged (already added unconditionally)

### What Gets Removed

- `console:` section from `config.yaml`
- All `getattr(config.console, ...)` calls in backend code — replaced with provider model reads
