# Console Setup Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move console DNS configuration from config files to the Provider model with full UI automation — admin clicks "Setup Console", enters a domain, and everything is configured.

**Architecture:** Add three columns to the Provider model (zone ID, base domain, nameservers). Two new backend endpoints handle setup and teardown. The existing console_dns.py functions get a `hosted_zone_id` parameter instead of reading from config. The frontend adds a Setup Console button to the provider card with NS delegation instructions.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PatternFly 6, Route53 (boto3)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/backend/app/models/provider.py` | Add 3 console columns |
| Create | Alembic migration | Add columns to providers table |
| Modify | `src/backend/app/api/providers.py` | Add setup-console/delete-console endpoints, update ProviderResponse |
| Modify | `src/backend/app/services/console_dns.py` | Add `hosted_zone_id` param, remove config reads |
| Modify | `src/backend/app/api/hosts.py` | Read console config from provider model |
| Modify | `src/backend/app/services/provisioner.py` | Read console config from kwargs |
| Modify | `src/backend/config/config.yaml` | Remove console section |
| Modify | `src/frontend/src/app/admin/providers/page.tsx` | Add Setup Console UI |

---

### Task 1: Add console columns to Provider model

**Files:**
- Modify: `src/backend/app/models/provider.py:21-25`
- Create: Alembic migration

- [ ] **Step 1: Add columns to Provider model**

In `src/backend/app/models/provider.py`, add after `security_group_id` (line 23):

```python
    console_zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    console_base_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    console_nameservers: Mapped[list | None] = mapped_column(JSONB, default=None)
```

Add `JSONB` to the sqlalchemy.dialects.postgresql import if not already there.

- [ ] **Step 2: Create Alembic migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add console columns to providers"
```

Edit the generated migration:

```python
from sqlalchemy.dialects.postgresql import JSONB

def upgrade() -> None:
    op.add_column("providers", sa.Column("console_zone_id", sa.String(100), nullable=True))
    op.add_column("providers", sa.Column("console_base_domain", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("console_nameservers", JSONB, nullable=True))

def downgrade() -> None:
    op.drop_column("providers", "console_nameservers")
    op.drop_column("providers", "console_base_domain")
    op.drop_column("providers", "console_zone_id")
```

- [ ] **Step 3: Run migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 4: Update ProviderResponse**

In `src/backend/app/api/providers.py`, add to the `ProviderResponse` class (after `host_count`, around line 53):

```python
    console_base_domain: str | None = None
    console_nameservers: list | None = None
    console_configured: bool = False
```

Then update the `list_providers` endpoint where ProviderResponse objects are built. Find the list comprehension that constructs responses and add the three new fields:

```python
console_base_domain=p.console_base_domain,
console_nameservers=p.console_nameservers,
console_configured=p.console_zone_id is not None,
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_models.py -v
```

---

### Task 2: Add `hosted_zone_id` parameter to console_dns functions

**Files:**
- Modify: `src/backend/app/services/console_dns.py:58-115`

- [ ] **Step 1: Update `upsert_dns_record` signature**

In `src/backend/app/services/console_dns.py`, change `upsert_dns_record` (line 58) from:

```python
def upsert_dns_record(fqdn: str, ip: str, credentials: dict | None = None) -> None:
    hosted_zone_id = getattr(config.console, "hosted_zone_id", "")
    if not hosted_zone_id:
        logger.warning("console.hosted_zone_id not configured, skipping DNS")
        return
```

To:

```python
def upsert_dns_record(fqdn: str, ip: str, hosted_zone_id: str, credentials: dict | None = None) -> None:
    if not hosted_zone_id:
        logger.warning("No hosted_zone_id provided, skipping DNS")
        return
```

- [ ] **Step 2: Update `delete_dns_record` signature**

Change `delete_dns_record` (line 87) from:

```python
def delete_dns_record(fqdn: str, ip: str, credentials: dict | None = None) -> None:
    hosted_zone_id = getattr(config.console, "hosted_zone_id", "")
    if not hosted_zone_id:
        return
```

To:

```python
def delete_dns_record(fqdn: str, ip: str, hosted_zone_id: str, credentials: dict | None = None) -> None:
    if not hosted_zone_id:
        return
```

- [ ] **Step 3: Remove `config` import**

Remove `from app.core.config import config` from the imports (line 10) since the functions no longer read from config. Keep `boto3` and the other imports.

- [ ] **Step 4: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_console_dns.py -v
```

Tests should still pass — the JWT tests don't call the DNS functions.

---

### Task 3: Update callers to pass `hosted_zone_id` from Provider

**Files:**
- Modify: `src/backend/app/api/hosts.py:237-246,760-765`

- [ ] **Step 1: Update provisioning thread DNS call**

In `src/backend/app/api/hosts.py`, find the console DNS record creation in the background thread (around line 237). Change from:

```python
        # Create console DNS record
        base_domain = getattr(config.console, "base_domain", "")
        if base_domain and h.instance_id and h.ip_address:
            from app.services.console_dns import console_domain_for_host, upsert_dns_record
            fqdn = console_domain_for_host(h.instance_id, base_domain)
            try:
                upsert_dns_record(fqdn, h.ip_address, credentials=provider_creds)
```

To:

```python
        # Create console DNS record
        if provider_console_domain and provider_console_zone and h.instance_id and h.ip_address:
            from app.services.console_dns import console_domain_for_host, upsert_dns_record
            fqdn = console_domain_for_host(h.instance_id, provider_console_domain)
            try:
                upsert_dns_record(fqdn, h.ip_address, provider_console_zone, credentials=provider_creds)
```

Where `provider_console_domain` and `provider_console_zone` are captured from the provider before the thread starts. Find where `provider_creds` is captured (before the thread function definition) and add:

```python
    provider_console_domain = provider.console_base_domain if provider else None
    provider_console_zone = provider.console_zone_id if provider else None
```

Remove the `from app.core.config import config as app_config` import if it was only used for console config.

- [ ] **Step 2: Update host removal DNS call**

In `src/backend/app/api/hosts.py`, find the DNS cleanup in `remove_host` (around line 760). Change from:

```python
    if host.console_domain and host.ip_address:
        try:
            from app.services.console_dns import delete_dns_record
            creds_for_dns = None
            if host.provider_id:
                prov = db.query(Provider).filter_by(id=host.provider_id).first()
                if prov:
                    creds_for_dns = prov.get_credentials()
            delete_dns_record(host.console_domain, host.ip_address, credentials=creds_for_dns)
```

To:

```python
    if host.console_domain and host.ip_address:
        try:
            from app.services.console_dns import delete_dns_record
            prov = db.query(Provider).filter_by(id=host.provider_id).first() if host.provider_id else None
            if prov and prov.console_zone_id:
                delete_dns_record(host.console_domain, host.ip_address, prov.console_zone_id, credentials=prov.get_credentials())
```

- [ ] **Step 3: Update provisioner instance profile check**

In `src/backend/app/services/provisioner.py`, find the instance profile conditional (around line 363). Change from:

```python
            if getattr(config.console, "hosted_zone_id", ""):
                launch_kwargs["IamInstanceProfile"] = {"Name": "troshka-certbot-profile"}
```

To:

```python
            if kwargs.get("console_zone_id"):
                launch_kwargs["IamInstanceProfile"] = {"Name": "troshka-certbot-profile"}
```

Then find where `provision_host()` is called in `hosts.py` and ensure the provider's `console_zone_id` is passed in kwargs. In the provisioning code that builds kwargs for `provision_host()`, add:

```python
console_zone_id=provider.console_zone_id if provider else None,
```

---

### Task 4: Add setup-console and delete-console endpoints

**Files:**
- Modify: `src/backend/app/api/providers.py`

- [ ] **Step 1: Add setup-console endpoint**

In `src/backend/app/api/providers.py`, add after the `create_vpc` function:

```python
class ConsoleSetupRequest(BaseModel):
    base_domain: str


@router.post("/{provider_id}/setup-console")
def setup_console(provider_id: str, req: ConsoleSetupRequest, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create Route53 hosted zone and IAM resources for direct console proxy."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    creds = provider.get_credentials()
    base_domain = req.base_domain.strip().lower()
    if not base_domain or "." not in base_domain:
        raise HTTPException(status_code=400, detail="Invalid domain name")

    try:
        r53 = boto3.client(
            "route53",
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        # Check if zone already exists
        existing = r53.list_hosted_zones_by_name(DNSName=base_domain, MaxItems="1")
        zone_id = None
        nameservers = []
        for zone in existing.get("HostedZones", []):
            if zone["Name"].rstrip(".") == base_domain:
                zone_id = zone["Id"].split("/")[-1]
                ns_resp = r53.get_hosted_zone(Id=zone_id)
                nameservers = ns_resp["DelegationSet"]["NameServers"]
                break

        if not zone_id:
            import time
            resp = r53.create_hosted_zone(
                Name=base_domain,
                CallerReference=f"troshka-console-{int(time.time())}",
                HostedZoneConfig={"Comment": "Troshka console proxy DNS"},
            )
            zone_id = resp["HostedZone"]["Id"].split("/")[-1]
            nameservers = resp["DelegationSet"]["NameServers"]
            logger.info("Created hosted zone %s for %s", zone_id, base_domain)

        # Create IAM role + instance profile (idempotent)
        iam = boto3.client(
            "iam",
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        role_name = "troshka-certbot-role"
        profile_name = "troshka-certbot-profile"

        try:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                }),
                Description="Allows EC2 hosts to manage Route53 for certbot DNS-01",
                Tags=[{"Key": "ManagedBy", "Value": "troshka"}],
            )
        except iam.exceptions.EntityAlreadyExistsException:
            pass

        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="troshka-certbot-dns",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "route53:ChangeResourceRecordSets",
                    "Resource": f"arn:aws:route53:::hostedzone/{zone_id}",
                }, {
                    "Effect": "Allow",
                    "Action": ["route53:GetChange", "route53:ListHostedZones"],
                    "Resource": "*",
                }],
            }),
        )

        try:
            iam.create_instance_profile(InstanceProfileName=profile_name)
        except iam.exceptions.EntityAlreadyExistsException:
            pass

        try:
            iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
        except iam.exceptions.LimitExceededException:
            pass

        # Store on provider
        provider.console_zone_id = zone_id
        provider.console_base_domain = base_domain
        provider.console_nameservers = nameservers
        db.commit()

        return {
            "zone_id": zone_id,
            "base_domain": base_domain,
            "nameservers": nameservers,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to setup console for provider %s", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 2: Add delete-console endpoint**

Add after setup-console:

```python
@router.delete("/{provider_id}/console")
def delete_console(provider_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Remove console DNS configuration and hosted zone."""
    from app.models.host import Host

    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if not provider.console_zone_id:
        raise HTTPException(status_code=400, detail="Console not configured")

    creds = provider.get_credentials()
    zone_id = provider.console_zone_id

    try:
        r53 = boto3.client(
            "route53",
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )

        # Delete all A records in the zone (NS and SOA are auto-managed)
        paginator = r53.get_paginator("list_resource_record_sets")
        changes = []
        for page in paginator.paginate(HostedZoneId=zone_id):
            for rrs in page["ResourceRecordSets"]:
                if rrs["Type"] in ("A", "CNAME"):
                    changes.append({"Action": "DELETE", "ResourceRecordSet": rrs})
        if changes:
            # Batch in groups of 100 (Route53 limit)
            for i in range(0, len(changes), 100):
                r53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": changes[i:i+100]})

        # Delete the hosted zone
        r53.delete_hosted_zone(Id=zone_id)
        logger.info("Deleted hosted zone %s", zone_id)

    except Exception as e:
        logger.warning("Failed to fully clean up hosted zone %s: %s", zone_id, e)

    # Clear console_domain on all hosts under this provider
    hosts = db.query(Host).filter_by(provider_id=provider_id).all()
    for h in hosts:
        h.console_domain = None
    provider.console_zone_id = None
    provider.console_base_domain = None
    provider.console_nameservers = None
    db.commit()

    return {"status": "removed"}
```

- [ ] **Step 3: Remove IAM setup from create_vpc**

In the same file, in `create_vpc()`, remove the entire IAM instance profile block (lines 378-436 approximately — the block starting with `# Create IAM instance profile for certbot DNS-01 challenges`). This is now handled by `setup_console`.

Add `import boto3` at the top if not already present (the `create_vpc` function imported it locally).

---

### Task 5: Remove config.yaml console section

**Files:**
- Modify: `src/backend/config/config.yaml`

- [ ] **Step 1: Remove console section**

In `src/backend/config/config.yaml`, delete lines 51-53:

```yaml
console:
  hosted_zone_id: ""
  base_domain: ""
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v 2>&1 | tail -10
```

Fix any remaining references to `config.console` that break.

---

### Task 6: Add console setup UI to providers page

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`

- [ ] **Step 1: Add state variables**

Near the other state declarations (around line 31), add:

```typescript
const [consoleDomain, setConsoleDomain] = useState<Record<string, string>>({});
const [consoleSetupResult, setConsoleSetupResult] = useState<Record<string, string>>({});
const [settingUpConsole, setSettingUpConsole] = useState<string | null>(null);
```

- [ ] **Step 2: Add setup and remove functions**

Add after the existing `setupInfra` function:

```typescript
const setupConsole = async (providerId: string) => {
    const domain = consoleDomain[providerId]?.trim();
    if (!domain) return;
    setSettingUpConsole(providerId);
    setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "" }));
    try {
      const resp = await fetch(`/api/v1/providers/${providerId}/setup-console`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_domain: domain }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setConsoleSetupResult((prev) => ({ ...prev, [providerId]: data.detail || "Setup failed" }));
      } else {
        setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "Console configured" }));
        loadData();
      }
    } catch {
      setConsoleSetupResult((prev) => ({ ...prev, [providerId]: "Connection failed" }));
    }
    setSettingUpConsole(null);
  };

  const removeConsole = async (providerId: string) => {
    if (!confirm("Remove console DNS configuration? This will delete the hosted zone and all DNS records.")) return;
    try {
      const resp = await fetch(`/api/v1/providers/${providerId}/console`, { method: "DELETE" });
      if (resp.ok) loadData();
    } catch { /* ignore */ }
  };
```

- [ ] **Step 3: Add console status to provider card**

In the provider card rendering, after the VPC status display (around line 360, where it shows `VPC: <vpc_id>` or `⚠ No VPC`), add console status:

```typescript
{p.type !== "s3" && (
    p.console_configured
        ? <span> · Console: <code style={{ fontSize: 11 }}>{p.console_base_domain}</code></span>
        : null
)}
```

- [ ] **Step 4: Add Setup Console button**

In the provider card button group (around line 451, near the "Setup VPC" button), add:

```typescript
{p.type !== "s3" && p.vpc_id && !p.console_configured && (
    <Button variant="secondary" onClick={() => setConsoleDomain((prev) => ({ ...prev, [p.id]: prev[p.id] || "" }))}>
        Setup Console
    </Button>
)}
```

- [ ] **Step 5: Add console setup form**

After the VPC discovery/setup UI section (after the `vpcOptions[p.id]` block), add the console setup form:

```typescript
{consoleDomain[p.id] !== undefined && !p.console_configured && (
    <Card style={{ marginTop: 12 }}>
        <CardBody>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>Setup Console DNS</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                    style={inputStyle}
                    placeholder="e.g., troshka.dev.rhdp.net"
                    value={consoleDomain[p.id] || ""}
                    onChange={(e) => setConsoleDomain((prev) => ({ ...prev, [p.id]: e.target.value }))}
                />
                <Button
                    variant="primary"
                    isLoading={settingUpConsole === p.id}
                    isDisabled={!consoleDomain[p.id]?.trim() || settingUpConsole === p.id}
                    onClick={() => setupConsole(p.id)}
                >
                    Create
                </Button>
                <Button variant="plain" onClick={() => setConsoleDomain((prev) => { const n = { ...prev }; delete n[p.id]; return n; })}>
                    Cancel
                </Button>
            </div>
            {consoleSetupResult[p.id] && (
                <div style={{ marginTop: 8, fontSize: 13, color: consoleSetupResult[p.id].includes("failed") ? "#ef4444" : "#22c55e" }}>
                    {consoleSetupResult[p.id]}
                </div>
            )}
        </CardBody>
    </Card>
)}
```

- [ ] **Step 6: Add NS delegation info and Remove button**

After the console status display, when console is configured, show delegation info:

```typescript
{p.console_configured && p.console_nameservers && (
    <Card style={{ marginTop: 12 }}>
        <CardBody>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>Console DNS: {p.console_base_domain}</div>
            <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", marginBottom: 8 }}>
                Add NS records for <code>{p.console_base_domain}</code> in your parent zone pointing to:
            </div>
            <div style={{ fontSize: 12, fontFamily: "monospace", marginBottom: 8 }}>
                {p.console_nameservers.map((ns: string) => <div key={ns}>{ns}</div>)}
            </div>
            <Button variant="danger" onClick={() => removeConsole(p.id)}>Remove Console</Button>
        </CardBody>
    </Card>
)}
```

- [ ] **Step 7: Add ProviderInfo type fields**

Find the `ProviderInfo` type interface (or wherever the provider response type is defined) and add:

```typescript
console_base_domain?: string;
console_nameservers?: string[];
console_configured?: boolean;
```

- [ ] **Step 8: Verify in browser**

Open http://localhost:3100/admin/providers and check:
- Provider card shows "Setup Console" button (after VPC is configured)
- Clicking shows domain input form
- After setup, shows domain badge and NS records
- "Remove Console" button works

---

### Task 7: Update config.local.yaml

**Files:**
- Modify: `src/backend/config/config.local.yaml`

- [ ] **Step 1: Remove console section from config.local.yaml**

If `config.local.yaml` has a `console:` section (it does from our earlier testing), remove it. This file is not committed but needs to be cleaned up locally.

- [ ] **Step 2: Restart backend and verify**

```bash
cd /Users/prutledg/troshka && ./dev-services.sh restart backend
```

Verify the backend starts without errors and the console still works for the existing host (which has `console_domain` already set in DB).
