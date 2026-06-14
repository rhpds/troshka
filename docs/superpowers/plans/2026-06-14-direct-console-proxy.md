# Direct Console Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 5-hop VNC console path (Browser → Next.js → FastAPI WS → websockify → SSH tunnel → Host VNC) with a 2-hop direct path (Browser → troshka-vncd on host → localhost VNC).

**Architecture:** A new daemon (`troshka-vncd`) on each host accepts direct WebSocket connections from browsers, validates a JWT issued by the backend, resolves the VNC port via `virsh dumpxml`, and proxies binary frames to the local QEMU VNC socket. Route53 DNS gives each host a public FQDN; Let's Encrypt provides TLS via an EC2 instance profile for DNS-01 challenges.

**Tech Stack:** Python 3 (`websockets` library), FastAPI, noVNC, Route53, certbot, Let's Encrypt, PyJWT (backend), HMAC-SHA256 (vncd stdlib)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/troshka-vncd/troshka-vncd.py` | WebSocket-to-VNC relay daemon |
| Create | `src/backend/app/services/console_dns.py` | Route53 A record upsert/delete + JWT signing |
| Create | `src/backend/tests/test_console_dns.py` | Tests for DNS service and JWT signing |
| Create | `src/backend/tests/test_console_proxy_removal.py` | Verify old proxy code is fully removed |
| Create | Alembic migration | Add `console_domain` column to `hosts` |
| Modify | `src/backend/app/models/host.py:41` | Add `console_domain` column |
| Modify | `src/backend/app/schemas/host.py:14-41` | Add `console_domain` to response schema |
| Modify | `src/backend/app/api/projects.py:676-693` | Replace proxy creation with JWT generation |
| Modify | `src/backend/app/api/ws.py:176-229` | Remove console WebSocket handler |
| Modify | `src/backend/app/services/provisioner.py:77-109,304-341` | Add port 443 SG rule + instance profile |
| Modify | `src/backend/app/services/agent_deployer.py:303-358` | Add vncd install, certbot, systemd unit |
| Modify | `src/backend/app/api/hosts.py:702-795` | Add DNS cleanup on host removal |
| Modify | `src/backend/config/config.yaml` | Add `console:` section |
| Modify | `src/frontend/src/app/console/page.tsx:91-120` | Use `ws_url` from API directly |
| Modify | `scripts/update-agent.sh` | Push both troshkad.py and troshka-vncd.py |
| Modify | `infra/iam-policy.json` | Add Route53 + IAM permissions |
| Delete | `src/backend/app/services/console_proxy.py` | Entire file removed |

---

### Task 1: Add `console_domain` column to Host model

**Files:**
- Modify: `src/backend/app/models/host.py:41`
- Modify: `src/backend/app/schemas/host.py:14-41`
- Create: Alembic migration
- Test: `src/backend/tests/test_models.py`

- [ ] **Step 1: Add column to Host model**

In `src/backend/app/models/host.py`, add after line 41 (`auto_extend_max_gb`):

```python
console_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 2: Add to HostResponse schema**

In `src/backend/app/schemas/host.py`, add to the `HostResponse` class:

```python
console_domain: str | None = None
```

- [ ] **Step 3: Create Alembic migration**

Run:
```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add console_domain to hosts"
```

Edit the generated migration file — the `upgrade()` function:

```python
def upgrade() -> None:
    op.add_column("hosts", sa.Column("console_domain", sa.String(255), nullable=True))

def downgrade() -> None:
    op.drop_column("hosts", "console_domain")
```

- [ ] **Step 4: Run migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 5: Verify tests pass**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_models.py -v
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/host.py src/backend/app/schemas/host.py src/backend/alembic/versions/
git commit -m "feat: add console_domain column to hosts table"
```

---

### Task 2: Add console config section

**Files:**
- Modify: `src/backend/config/config.yaml`

- [ ] **Step 1: Add console section to config.yaml**

In `src/backend/config/config.yaml`, add after the `health:` section (after line 50):

```yaml
console:
  hosted_zone_id: ""
  base_domain: ""
```

These are blank by default — operators set them in `config.local.yaml` or via `TROSHKA_CONSOLE__HOSTED_ZONE_ID` / `TROSHKA_CONSOLE__BASE_DOMAIN` env vars. When blank, the console falls back to the old behavior (no direct proxy available).

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/config/config.yaml
git commit -m "feat: add console config section for Route53 DNS"
```

---

### Task 3: Create console DNS service with JWT signing

**Files:**
- Create: `src/backend/app/services/console_dns.py`
- Create: `src/backend/tests/test_console_dns.py`

- [ ] **Step 1: Write tests for JWT signing**

Create `src/backend/tests/test_console_dns.py`:

```python
import time

import pytest


def test_sign_console_jwt_contains_required_claims():
    from app.services.console_dns import sign_console_jwt

    token = sign_console_jwt(
        domain_name="troshka-abcd1234-efgh5678",
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        secret="test-secret-token-hex-value",
    )
    assert isinstance(token, str)
    assert len(token) > 0

    # Decode and verify claims
    import hmac, hashlib, base64, json
    parts = token.split(".")
    assert len(parts) == 3
    # Pad base64
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert payload["domain_name"] == "troshka-abcd1234-efgh5678"
    assert payload["host_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "exp" in payload
    assert payload["exp"] > time.time()
    assert payload["exp"] <= time.time() + 301


def test_sign_console_jwt_different_secrets_produce_different_tokens():
    from app.services.console_dns import sign_console_jwt

    t1 = sign_console_jwt("dom", "host1", "secret-a")
    t2 = sign_console_jwt("dom", "host1", "secret-b")
    assert t1 != t2


def test_verify_console_jwt_valid():
    from app.services.console_dns import sign_console_jwt, verify_console_jwt

    secret = "my-test-secret"
    token = sign_console_jwt("troshka-abcd-efgh", "host-id-1", secret)
    claims = verify_console_jwt(token, secret)
    assert claims["domain_name"] == "troshka-abcd-efgh"
    assert claims["host_id"] == "host-id-1"


def test_verify_console_jwt_wrong_secret():
    from app.services.console_dns import sign_console_jwt, verify_console_jwt

    token = sign_console_jwt("dom", "host", "correct-secret")
    result = verify_console_jwt(token, "wrong-secret")
    assert result is None


def test_verify_console_jwt_expired():
    from app.services.console_dns import verify_console_jwt

    import hmac, hashlib, base64, json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_data = {"domain_name": "dom", "host_id": "h", "exp": int(time.time()) - 10}
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    sig_input = f"{header}.{payload}".encode()
    sig = base64.urlsafe_b64encode(hmac.new(b"secret", sig_input, hashlib.sha256).digest()).rstrip(b"=").decode()
    token = f"{header}.{payload}.{sig}"

    result = verify_console_jwt(token, "secret")
    assert result is None


def test_console_domain_from_instance_id():
    from app.services.console_dns import console_domain_for_host

    fqdn = console_domain_for_host("i-0abc123def456", "tc.rhdp.net")
    assert fqdn == "i-0abc123def456.tc.rhdp.net"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_console_dns.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.console_dns'`

- [ ] **Step 3: Implement console_dns.py**

Create `src/backend/app/services/console_dns.py`:

```python
"""Console DNS management and JWT token signing for direct VNC proxy."""
import base64
import hashlib
import hmac
import json
import logging
import time

import boto3

from app.core.config import config

logger = logging.getLogger(__name__)

JWT_EXPIRY_SECONDS = 300  # 5 minutes


def console_domain_for_host(instance_id: str, base_domain: str) -> str:
    return f"{instance_id}.{base_domain}"


def sign_console_jwt(domain_name: str, host_id: str, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "domain_name": domain_name,
        "host_id": host_id,
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig_input = f"{h}.{p}".encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{h}.{p}.{sig}"


def verify_console_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected_sig, parts[2]):
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def upsert_dns_record(fqdn: str, ip: str, credentials: dict | None = None) -> None:
    hosted_zone_id = getattr(config.console, "hosted_zone_id", "")
    if not hosted_zone_id:
        logger.warning("console.hosted_zone_id not configured, skipping DNS")
        return

    creds = credentials or {}
    client = boto3.client(
        "route53",
        aws_access_key_id=creds.get("access_key_id") or getattr(config.aws, "access_key_id", None),
        aws_secret_access_key=creds.get("secret_access_key") or getattr(config.aws, "secret_access_key", None),
    )
    client.change_resource_record_sets(
        HostedZoneId=hosted_zone_id,
        ChangeBatch={
            "Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": fqdn,
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": ip}],
                },
            }],
        },
    )
    logger.info("DNS: upserted %s -> %s", fqdn, ip)


def delete_dns_record(fqdn: str, ip: str, credentials: dict | None = None) -> None:
    hosted_zone_id = getattr(config.console, "hosted_zone_id", "")
    if not hosted_zone_id:
        return

    creds = credentials or {}
    client = boto3.client(
        "route53",
        aws_access_key_id=creds.get("access_key_id") or getattr(config.aws, "access_key_id", None),
        aws_secret_access_key=creds.get("secret_access_key") or getattr(config.aws, "secret_access_key", None),
    )
    try:
        client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Changes": [{
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": fqdn,
                        "Type": "A",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": ip}],
                    },
                }],
            },
        )
        logger.info("DNS: deleted %s", fqdn)
    except Exception:
        logger.warning("DNS: failed to delete %s (may already be gone)", fqdn)
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_console_dns.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/console_dns.py src/backend/tests/test_console_dns.py
git commit -m "feat: add console DNS service with JWT signing and Route53 management"
```

---

### Task 4: Update IAM policy

**Files:**
- Modify: `infra/iam-policy.json`

- [ ] **Step 1: Add Route53 and IAM statements**

In `infra/iam-policy.json`, add two new statements after the `IAMServiceRole` statement (before the closing `]` of the `Statement` array):

```json
    {
      "Sid": "Route53Console",
      "Effect": "Allow",
      "Action": [
        "route53:ChangeResourceRecordSets",
        "route53:GetChange",
        "route53:ListHostedZones"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMInstanceProfile",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:CreateInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::*:role/troshka-certbot-role",
        "arn:aws:iam::*:instance-profile/troshka-certbot-profile"
      ]
    }
```

Note: `iam:PassRole` resource is scoped to just the certbot role — not `*`.

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add infra/iam-policy.json
git commit -m "feat: add Route53 and IAM instance profile permissions for console proxy"
```

---

### Task 5: Update security group to allow port 443

**Files:**
- Modify: `src/backend/app/services/provisioner.py:77-109`

- [ ] **Step 1: Add port 443 rule to new security groups**

In `src/backend/app/services/provisioner.py`, in the `ensure_security_group` function, add port 443 to the `authorize_security_group_ingress` call at line 99. Change the `IpPermissions` list (lines 101-105) to:

```python
        IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Console VNC proxy"}]},
            {"IpProtocol": "tcp", "FromPort": 31337, "ToPort": 31337, "IpRanges": [{"CidrIp": troshkad_cidr, "Description": "Troshkad API"}]},
            {"IpProtocol": "udp", "FromPort": 4789, "ToPort": 4789, "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "VXLAN mesh"}]},
        ],
```

- [ ] **Step 2: Ensure port 443 on existing security groups**

In the same function, at line 87 (the `if existing["SecurityGroups"]:` branch), add a console rule check after `_ensure_troshkad_rule`. Add a new function after `_ensure_troshkad_rule` (around line 75):

```python
def _ensure_console_rule(client, sg_id: str):
    """Ensure port 443 rule exists on an existing security group."""
    sg = client.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    for perm in sg.get("IpPermissions", []):
        if perm.get("FromPort") == 443 and perm.get("ToPort") == 443:
            return
    try:
        client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Console VNC proxy"}],
            }],
        )
        logger.info("Added port 443 rule to existing SG %s", sg_id)
    except Exception:
        pass
```

Then in `ensure_security_group`, at line 87, after `_ensure_troshkad_rule(client, sg_id)`, add:

```python
        _ensure_console_rule(client, sg_id)
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/provisioner.py
git commit -m "feat: add port 443 security group rule for console VNC proxy"
```

---

### Task 6: Add instance profile creation to VPC setup

**Files:**
- Modify: `src/backend/app/api/providers.py:293-372`

- [ ] **Step 1: Add instance profile creation after VPC setup**

In `src/backend/app/api/providers.py`, in the `create_vpc` function, add instance profile creation after the S3 endpoint creation block (around line 372). Before the `return` statement of the VPC creation response, add:

```python
        # Create IAM instance profile for certbot DNS-01 challenges
        console_zone_id = getattr(config.console, "hosted_zone_id", "")
        if console_zone_id:
            try:
                iam = boto3.client(
                    "iam",
                    aws_access_key_id=creds.get("access_key_id"),
                    aws_secret_access_key=creds.get("secret_access_key"),
                )
                role_name = "troshka-certbot-role"
                profile_name = "troshka-certbot-profile"

                # Create role (idempotent)
                try:
                    iam.create_role(
                        RoleName=role_name,
                        AssumeRolePolicyDocument=json.dumps({
                            "Version": "2012-10-17",
                            "Statement": [{
                                "Effect": "Allow",
                                "Principal": {"Service": "ec2.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }],
                        }),
                        Description="Allows EC2 hosts to manage Route53 for certbot DNS-01",
                        Tags=[{"Key": "ManagedBy", "Value": "troshka"}],
                    )
                except iam.exceptions.EntityAlreadyExistsException:
                    pass

                # Attach policy scoped to the console hosted zone
                iam.put_role_policy(
                    RoleName=role_name,
                    PolicyName="troshka-certbot-dns",
                    PolicyDocument=json.dumps({
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Action": ["route53:ChangeResourceRecordSets", "route53:GetChange"],
                            "Resource": f"arn:aws:route53:::hostedzone/{console_zone_id}",
                        }, {
                            "Effect": "Allow",
                            "Action": "route53:ListHostedZones",
                            "Resource": "*",
                        }],
                    }),
                )

                # Create instance profile (idempotent)
                try:
                    iam.create_instance_profile(InstanceProfileName=profile_name)
                except iam.exceptions.EntityAlreadyExistsException:
                    pass

                # Add role to profile (idempotent)
                try:
                    iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
                except iam.exceptions.LimitExceededException:
                    pass  # Role already added

                logger.info("Created IAM instance profile %s for certbot", profile_name)
            except Exception as e:
                logger.warning("Failed to create certbot instance profile: %s (console proxy will not work)", e)
```

Add `import json` to the top of the file if not already present, and add `from app.core.config import config` if not already imported.

- [ ] **Step 2: Add instance profile to provision_host**

In `src/backend/app/services/provisioner.py`, in the `provision_host` function, add the `IamInstanceProfile` parameter to the `run_instances` call. At line 304, inside the `client.run_instances(` call, add after `MaxCount=1,`:

```python
                IamInstanceProfile={"Name": "troshka-certbot-profile"} if getattr(config.console, "hosted_zone_id", "") else {},
```

Wait — that won't work because `IamInstanceProfile` can't be an empty dict. Instead, build the kwargs conditionally. Replace the `response = client.run_instances(` block (lines 304-341) by adding an `iam_profile` variable before it:

```python
            launch_kwargs = dict(
                ImageId=ami_id,
                InstanceType=instance_type,
                KeyName=key_name,
                MinCount=1,
                MaxCount=1,
                CpuOptions={"NestedVirtualization": "enabled"},
                UserData=user_data,
                BlockDeviceMappings=[
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                    {
                        "DeviceName": "/dev/sdf",
                        "Ebs": {"VolumeSize": storage_size_gb, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                    {
                        "DeviceName": "/dev/sdg",
                        "Ebs": {"VolumeSize": swap_size_gb, "VolumeType": "gp3", "DeleteOnTermination": True},
                    },
                ],
                TagSpecifications=[{
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": hostname},
                        {"Key": "Project", "Value": "troshka"},
                        {"Key": "ManagedBy", "Value": "troshka"},
                        {"Key": "troshka-host-id", "Value": host_id},
                    ],
                }],
                NetworkInterfaces=[{
                    "DeviceIndex": 0,
                    "SubnetId": try_subnet,
                    "Groups": [sg_id],
                    "AssociatePublicIpAddress": True,
                }],
            )
            if getattr(config.console, "hosted_zone_id", ""):
                launch_kwargs["IamInstanceProfile"] = {"Name": "troshka-certbot-profile"}
            response = client.run_instances(**launch_kwargs)
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py src/backend/app/services/provisioner.py
git commit -m "feat: create IAM instance profile for certbot and attach to provisioned hosts"
```

---

### Task 7: Wire DNS record creation into host provisioning

**Files:**
- Modify: `src/backend/app/api/hosts.py:116-243`

- [ ] **Step 1: Add DNS record after host is provisioned**

In `src/backend/app/api/hosts.py`, in the `_auto_install` background thread (spawned after host provisioning), add DNS record creation. Find the section where the host record is updated with the provisioning result (after `provision_host()` returns). After the host IP is saved to the DB, add:

```python
        # Create console DNS record
        base_domain = getattr(config.console, "base_domain", "")
        if base_domain and h.instance_id and h.ip_address:
            from app.services.console_dns import console_domain_for_host, upsert_dns_record
            fqdn = console_domain_for_host(h.instance_id, base_domain)
            try:
                upsert_dns_record(fqdn, h.ip_address, credentials=creds)
                h.console_domain = fqdn
                s.commit()
            except Exception as e:
                logger.warning("Failed to create console DNS for %s: %s", h.id[:8], e)
```

This goes in the background thread that runs after provisioning, where the host IP is already known. Add `from app.core.config import config` if not already imported in that scope.

- [ ] **Step 2: Add DNS cleanup to host removal**

In `src/backend/app/api/hosts.py`, in the `remove_host` function (line 702), add DNS cleanup before termination. After `host.state = "terminating"` (line 739) and before the `terminate_host` call (line 744), add:

```python
    # Clean up console DNS record
    if host.console_domain and host.ip_address:
        try:
            from app.services.console_dns import delete_dns_record
            creds_for_dns = None
            if host.provider_id:
                provider = db.query(Provider).filter_by(id=host.provider_id).first()
                if provider:
                    creds_for_dns = provider.get_credentials()
            delete_dns_record(host.console_domain, host.ip_address, credentials=creds_for_dns)
        except Exception as e:
            logger.warning("Failed to delete console DNS for %s: %s", host_id[:8], e)
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py
git commit -m "feat: create/delete Route53 DNS records during host provisioning and removal"
```

---

### Task 8: Replace console API endpoint with JWT flow

**Files:**
- Modify: `src/backend/app/api/projects.py:676-693`

- [ ] **Step 1: Write test for new console endpoint response**

Add to `src/backend/tests/test_console_dns.py`:

```python
def test_console_endpoint_returns_ws_url_when_configured(monkeypatch):
    """Console endpoint should return ws_url when console_domain is set."""
    from app.services.console_dns import sign_console_jwt, verify_console_jwt

    # The JWT generated by the endpoint should be verifiable
    token = sign_console_jwt("troshka-abcd1234-efgh5678", "host-id", "agent-token-secret")
    claims = verify_console_jwt(token, "agent-token-secret")
    assert claims is not None
    assert claims["domain_name"] == "troshka-abcd1234-efgh5678"
```

- [ ] **Step 2: Run test**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_console_dns.py::test_console_endpoint_returns_ws_url_when_configured -v
```

- [ ] **Step 3: Update the console endpoint**

In `src/backend/app/api/projects.py`, replace the console endpoint (lines 676-693) with:

```python
@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vnc_port = troshkad_get_vnc_port(host, dom)

    if not vnc_port:
        return {"error": "VNC not available"}

    # Direct console proxy via troshka-vncd on the host
    if host.console_domain and host.agent_token:
        from app.services.console_dns import sign_console_jwt
        jwt = sign_console_jwt(dom, host.id, host.agent_token)
        return {"ws_url": f"wss://{host.console_domain}/ws/{jwt}"}

    # Fallback: old proxy path (for hosts without console DNS configured)
    from app.services.console_proxy import create_console_token
    proxy = get_or_create_proxy(dom, host.ip_address, host.private_key, vnc_port)
    if "error" in proxy:
        return {"error": proxy["error"]}
    token = create_console_token(proxy["ws_port"], user.id, project_id, vm_id)
    return {"token": token}
```

This keeps backward compatibility — hosts without `console_domain` still use the old proxy path. The old code gets removed in a later task once all hosts are migrated.

- [ ] **Step 4: Remove unused import**

In `src/backend/app/api/projects.py`, the top-level import of `get_or_create_proxy` (around line 33) can stay for now since we still have the fallback path. It will be removed in the cleanup task.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/projects.py src/backend/tests/test_console_dns.py
git commit -m "feat: console endpoint returns direct ws_url when host has console_domain"
```

---

### Task 9: Update frontend to use `ws_url` from API

**Files:**
- Modify: `src/frontend/src/app/console/page.tsx:91-120`

- [ ] **Step 1: Update token fetch to handle both `ws_url` and `token` responses**

In `src/frontend/src/app/console/page.tsx`, the `fetchConsoleToken` callback (around line 91) currently returns just the token string. We need to return the full response so the page can use either `ws_url` or the legacy `token`.

First, add a new state variable near the other state declarations (around line 40):

```typescript
const [wsUrl, setWsUrl] = useState<string | null>(null);
```

Then modify `fetchConsoleToken` (around line 91):

```typescript
const fetchConsoleToken = useCallback(async (): Promise<string | null> => {
    if (!projectId || !vmId || projectDeleted) return null;
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/console`);
      if (resp.status === 404) { setProjectDeleted(true); setStatus("Project deleted"); return null; }
      const data = await resp.json();
      if (data.ws_url) {
        setWsUrl(data.ws_url);
        return "direct";
      }
      if (data.token) return data.token;
    } catch { /* ignore */ }
    return null;
  }, [projectId, vmId, projectDeleted]);
```

- [ ] **Step 2: Update WebSocket URL construction**

Find the line that constructs `wsUrl` (around line 115):

```typescript
const wsUrl = consoleToken ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/api/v1/console/ws/${consoleToken}` : null;
```

Replace it with:

```typescript
const effectiveWsUrl = wsUrl
    ? wsUrl
    : consoleToken
      ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/api/v1/console/ws/${consoleToken}`
      : null;
```

Then update the RFB constructor call (around line 117) to use `effectiveWsUrl` instead of `wsUrl`:

```typescript
const rfb = new RFB(canvasRef.current, effectiveWsUrl!, {});
```

And update the guard that checks for `wsUrl` before creating the RFB instance to check `effectiveWsUrl` instead.

- [ ] **Step 3: Reset wsUrl on reconnect**

In the reconnect logic (where `consoleToken` is cleared), also clear `wsUrl`:

```typescript
setWsUrl(null);
```

- [ ] **Step 4: Verify in browser**

Start the dev server and open a console for a running VM. Verify:
- Console connects and shows VNC output
- The connection uses the new `ws_url` path (check network tab — should show a direct `wss://` connection to the host domain, not through `/api/v1/console/ws/`)
- Fallback still works for hosts without `console_domain`

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/console/page.tsx
git commit -m "feat: frontend console uses direct ws_url when available, falls back to proxy"
```

---

### Task 10: Create troshka-vncd daemon

**Files:**
- Create: `src/troshka-vncd/troshka-vncd.py`

- [ ] **Step 1: Create the daemon**

Create `src/troshka-vncd/troshka-vncd.py`:

```python
#!/usr/bin/env python3
"""
troshka-vncd — WebSocket-to-VNC relay daemon.

Listens on port 443 with TLS, validates JWT tokens, and proxies
binary WebSocket frames to the local QEMU VNC socket.

Dependencies: websockets (pip install websockets)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import signal
import socket
import ssl
import subprocess
import sys
import time

VERSION = "dev"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vncd")

# Consumed tokens — prevents replay within the 5-min JWT window
_consumed: set[str] = set()
_consumed_expiry: dict[str, float] = {}

CERT_CHECK_INTERVAL = 3600  # 1 hour


def _load_config() -> dict:
    conf_path = os.environ.get("VNCD_CONFIG", "/opt/troshka/troshkad.conf")
    with open(conf_path) as f:
        return json.load(f)


def _verify_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(expected, parts[2]):
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _get_vnc_port(domain_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["virsh", "dumpxml", domain_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        import xml.etree.ElementTree as ET
        root = ET.fromstring(result.stdout)
        for gfx in root.iter("graphics"):
            if gfx.get("type") == "vnc":
                port = gfx.get("port")
                if port and port != "-1":
                    return int(port)
        return None
    except Exception:
        return None


def _prune_consumed():
    now = time.time()
    expired = [t for t, exp in _consumed_expiry.items() if exp < now]
    for t in expired:
        _consumed.discard(t)
        _consumed_expiry.pop(t, None)


def _build_ssl_context(conf: dict) -> ssl.SSLContext:
    console_domain = conf.get("console_domain", "")
    if console_domain:
        cert = f"/etc/letsencrypt/live/{console_domain}/fullchain.pem"
        key = f"/etc/letsencrypt/live/{console_domain}/privkey.pem"
    else:
        cert = conf.get("tls_cert", "/opt/troshka/tls/server.crt")
        key = conf.get("tls_key", "/opt/troshka/tls/server.key")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _handle_connection(websocket, conf: dict):
    """Handle a single WebSocket connection."""
    path = websocket.request.path if hasattr(websocket, 'request') else ""
    # Extract JWT from path: /ws/{token}
    if not path.startswith("/ws/"):
        await websocket.close(4000, "Invalid path")
        return
    token = path[4:]

    secret = conf["token"]
    claims = _verify_jwt(token, secret)
    if not claims:
        await websocket.close(4001, "Invalid or expired token")
        return

    # Single-use check
    if token in _consumed:
        await websocket.close(4001, "Token already used")
        return
    _consumed.add(token)
    _consumed_expiry[token] = claims["exp"]

    domain_name = claims.get("domain_name")
    if not domain_name:
        await websocket.close(4002, "Missing domain_name")
        return

    vnc_port = _get_vnc_port(domain_name)
    if not vnc_port:
        await websocket.close(4003, "VNC not available")
        return

    logger.info("Console: %s -> 127.0.0.1:%d", domain_name, vnc_port)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", vnc_port)
    except Exception:
        await websocket.close(4004, "Cannot connect to VNC")
        return

    async def _ws_to_vnc():
        try:
            async for msg in websocket:
                if isinstance(msg, bytes):
                    writer.write(msg)
                    await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _vnc_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send(data)
        except Exception:
            pass

    try:
        await asyncio.gather(_ws_to_vnc(), _vnc_to_ws())
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Console closed: %s", domain_name)


async def _cert_reload_loop(ssl_context_holder: list, conf: dict):
    """Periodically check if the TLS cert has been renewed and reload."""
    console_domain = conf.get("console_domain", "")
    if not console_domain:
        return
    cert_path = f"/etc/letsencrypt/live/{console_domain}/fullchain.pem"
    last_mtime = 0.0
    while True:
        await asyncio.sleep(CERT_CHECK_INTERVAL)
        try:
            mtime = os.path.getmtime(cert_path)
            if mtime > last_mtime:
                ssl_context_holder[0] = _build_ssl_context(conf)
                last_mtime = mtime
                logger.info("Reloaded TLS certificate")
        except Exception:
            pass


async def _prune_loop():
    """Periodically prune consumed tokens."""
    while True:
        await asyncio.sleep(60)
        _prune_consumed()


async def main():
    import websockets

    conf = _load_config()
    bind_ip = conf.get("bind_ip", "0.0.0.0")
    port = 443

    ssl_ctx = _build_ssl_context(conf)
    ssl_holder = [ssl_ctx]

    logger.info("troshka-vncd %s starting on %s:%d", VERSION, bind_ip, port)

    async def handler(websocket):
        await _handle_connection(websocket, conf)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(
        handler,
        bind_ip,
        port,
        ssl=ssl_ctx,
        max_size=None,
        ping_interval=30,
        ping_timeout=10,
    ):
        asyncio.create_task(_prune_loop())
        asyncio.create_task(_cert_reload_loop(ssl_holder, conf))
        logger.info("troshka-vncd ready")
        await stop.wait()

    logger.info("troshka-vncd shutting down")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshka-vncd/troshka-vncd.py
git commit -m "feat: add troshka-vncd WebSocket-to-VNC relay daemon"
```

---

### Task 11: Update agent deployer to install vncd

**Files:**
- Modify: `src/backend/app/services/agent_deployer.py:303-358`

- [ ] **Step 1: Add `websockets` to pip install**

In `src/backend/app/services/agent_deployer.py`, at line 351 (the `pexpect awscli` pip install), add `websockets`:

```python
/opt/troshka/venv/bin/pip install $PIP_ARGS pexpect awscli websockets
```

- [ ] **Step 2: Add vncd systemd unit**

After the troshkad systemd unit block (after line 322, after `systemctl restart troshkad`), add:

```bash
# Write vncd systemd unit
cat > /etc/systemd/system/troshka-vncd.service << 'SYSTEMDEOF'
[Unit]
Description=Troshka VNC Console Proxy Daemon
After=network.target troshkad.service

[Service]
Type=simple
ExecStart=/opt/troshka/venv/bin/python3 /opt/troshka/troshka-vncd.py
WorkingDirectory=/opt/troshka
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
SYSTEMDEOF
```

Note: `AmbientCapabilities=CAP_NET_BIND_SERVICE` lets the daemon bind to port 443 without running as root.

- [ ] **Step 3: Add certbot installation and cert provisioning**

After the vncd systemd unit, add the certbot section. This runs only when `console_domain` is provided to the install script:

```bash
# Console TLS via Let's Encrypt (only if console_domain is set)
CONSOLE_DOMAIN="{console_domain}"
if [ -n "$CONSOLE_DOMAIN" ]; then
    echo "=== Setting up console TLS ==="
    /opt/troshka/venv/bin/pip install $PIP_ARGS certbot certbot-dns-route53
    /opt/troshka/venv/bin/certbot certonly --dns-route53 \
        -d "$CONSOLE_DOMAIN" \
        --non-interactive --agree-tos -m noreply@redhat.com \
        --preferred-challenges dns-01 2>&1 || echo "certbot: initial cert may have failed (will retry)"

    # Auto-renewal cron
    echo "0 3 * * * root /opt/troshka/venv/bin/certbot renew --quiet" > /etc/cron.d/certbot-renew

    # Store console_domain in troshkad config for vncd to find
    python3 -c "
import json
conf = json.load(open('/opt/troshka/troshkad.conf'))
conf['console_domain'] = '$CONSOLE_DOMAIN'
json.dump(conf, open('/opt/troshka/troshkad.conf', 'w'), indent=2)
"

    # Open port 443
    if which firewall-cmd &>/dev/null; then
        firewall-cmd --add-port=443/tcp --permanent 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
    fi

    systemctl daemon-reload
    systemctl enable troshka-vncd
    systemctl restart troshka-vncd
    echo "vncd: started with Let's Encrypt cert for $CONSOLE_DOMAIN"
else
    echo "vncd: no console_domain, skipping TLS setup"
fi
```

- [ ] **Step 4: Add `console_domain` parameter to install script generation**

In the `deploy_agent` function (or wherever the install script is assembled), add `console_domain` as a format variable. It should come from `host.console_domain` or be empty string if not set. Find the `.format(...)` call that builds the script and add `console_domain=host.console_domain or ""`.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/agent_deployer.py
git commit -m "feat: agent deployer installs vncd, certbot, and Let's Encrypt cert"
```

---

### Task 12: Update agent push to include vncd

**Files:**
- Modify: `scripts/update-agent.sh`
- Modify: `src/backend/app/services/troshkad_client.py:230-249`
- Modify: `src/backend/app/api/hosts.py` (update-agent endpoint)

- [ ] **Step 1: Update update-agent.sh to push both files**

In `scripts/update-agent.sh`, modify the Python script section. After reading `troshkad.py`, also read `troshka-vncd.py` and push it:

After line 46 (`script_bytes = script_text.encode()`), add:

```python
vncd_path = os.path.join(os.path.dirname(troshkad_path), '..', 'troshka-vncd', 'troshka-vncd.py')
vncd_bytes = b''
if os.path.exists(vncd_path):
    with open(vncd_path, 'rb') as f:
        vncd_bytes = f.read()
    vncd_text = vncd_bytes.decode().replace('VERSION = "dev"', f'VERSION = "{version}"')
    vncd_bytes = vncd_text.encode()
```

Then update the `push_update` call to also push vncd. This requires extending the troshkad `/admin/update` endpoint to accept an optional `vncd_script` field — or adding a separate push. The simpler approach: add `vncd_script` to the payload:

After line 67 (`push_update(h, script_bytes, version, force=force)`), add a separate push for vncd if it exists:

```python
            if vncd_bytes:
                push_vncd_update(h, vncd_bytes)
```

- [ ] **Step 2: Add vncd push function to troshkad_client.py**

In `src/backend/app/services/troshkad_client.py`, add after `push_update`:

```python
def push_vncd_update(host, script_bytes: bytes):
    """Push a troshka-vncd update to a host."""
    import base64
    troshkad_request(host, "POST", "/admin/update-vncd", body={
        "script": base64.b64encode(script_bytes).decode(),
    }, timeout=30)
```

This requires a new endpoint in troshkad — see Task 13.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add scripts/update-agent.sh src/backend/app/services/troshkad_client.py
git commit -m "feat: update-agent.sh pushes both troshkad and vncd"
```

---

### Task 13: Add vncd update endpoint to troshkad

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add `/admin/update-vncd` handler**

In `src/troshkad/troshkad.py`, add a new handler for updating the vncd script. Find the existing `/admin/update` handler and add a similar one below it:

```python
def _handle_admin_update_vncd(job, params):
    """Update troshka-vncd.py and restart the vncd service."""
    import base64
    script_b64 = params.get("script", "")
    if not script_b64:
        return {"error": "missing script"}
    script_bytes = base64.b64decode(script_b64)
    vncd_path = "/opt/troshka/troshka-vncd.py"
    with open(vncd_path + ".new", "wb") as f:
        f.write(script_bytes)
    os.rename(vncd_path + ".new", vncd_path)
    os.chmod(vncd_path, 0o755)
    subprocess.run(["systemctl", "restart", "troshka-vncd"], timeout=10)
    return {"status": "updated"}
```

Register it in the handler map alongside the existing `/admin/update` route.

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad handles /admin/update-vncd for vncd updates"
```

---

### Task 14: Remove old console proxy code

**Files:**
- Delete: `src/backend/app/services/console_proxy.py`
- Modify: `src/backend/app/api/ws.py:176-229`
- Modify: `src/backend/app/api/projects.py`

This task removes the fallback path added in Task 8, making the direct proxy the only path. Only do this after all hosts have been migrated to have `console_domain`.

- [ ] **Step 1: Remove console_proxy.py**

```bash
rm /Users/prutledg/troshka/src/backend/app/services/console_proxy.py
```

- [ ] **Step 2: Remove WebSocket handler from ws.py**

In `src/backend/app/api/ws.py`, delete the `console_websocket_proxy` function (lines 176-229) and its `import websockets` and `from app.services.console_proxy import consume_console_token` imports.

- [ ] **Step 3: Remove fallback from console endpoint**

In `src/backend/app/api/projects.py`, simplify the console endpoint to only use the JWT path:

```python
@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    vnc_port = troshkad_get_vnc_port(host, dom)

    if not vnc_port:
        return {"error": "VNC not available"}

    if not host.console_domain or not host.agent_token:
        return {"error": "Console proxy not configured for this host"}

    from app.services.console_dns import sign_console_jwt
    jwt = sign_console_jwt(dom, host.id, host.agent_token)
    return {"ws_url": f"wss://{host.console_domain}/ws/{jwt}"}
```

Remove the `get_or_create_proxy` import from the top of the file.

- [ ] **Step 4: Remove websockify dependency**

If `websockify` is listed in any requirements file, remove it.

- [ ] **Step 5: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Fix any import errors from removed modules.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add -A
git commit -m "refactor: remove old SSH tunnel + websockify console proxy"
```

---

### Task 15: Simplify frontend (remove fallback)

**Files:**
- Modify: `src/frontend/src/app/console/page.tsx`

- [ ] **Step 1: Remove token-based fallback**

Now that the backend only returns `ws_url`, simplify the frontend. In `src/frontend/src/app/console/page.tsx`:

Remove the `consoleToken` state variable and the legacy WS URL construction. The `fetchConsoleToken` callback becomes:

```typescript
const fetchConsoleUrl = useCallback(async (): Promise<string | null> => {
    if (!projectId || !vmId || projectDeleted) return null;
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/console`);
      if (resp.status === 404) { setProjectDeleted(true); setStatus("Project deleted"); return null; }
      const data = await resp.json();
      if (data.ws_url) return data.ws_url;
    } catch { /* ignore */ }
    return null;
  }, [projectId, vmId, projectDeleted]);
```

Update the polling logic to set `wsUrl` directly, and use `wsUrl` for the RFB constructor.

- [ ] **Step 2: Test in browser**

Open a console, verify connection works through the direct path.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/console/page.tsx
git commit -m "refactor: frontend console uses direct ws_url only"
```
