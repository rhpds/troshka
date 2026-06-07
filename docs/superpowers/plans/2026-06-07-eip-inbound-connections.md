# EIP Allocation & Inbound Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable real AWS Elastic IP allocation for inbound connections via gateway port-forwarding, with provider-level garbage collection.

**Architecture:** New `ElasticIp` model tracks EIP lifecycle (allocated → associated → released). A dedicated `eip_service.py` handles all AWS EIP operations. Placement checks EIP capacity. Deploy wires EIPs as secondary private IPs on the host ENI with nftables DNAT rules. A new `provider_gc_service.py` reconciles Troshka-tagged AWS resources against the DB.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, boto3, Next.js 15, PatternFly 6

**Spec:** `docs/superpowers/specs/2026-06-07-eip-inbound-connections-design.md`

---

## File Structure

### New files
- `src/backend/app/models/elastic_ip.py` — ElasticIp SQLAlchemy model
- `src/backend/alembic/versions/<hash>_add_elastic_ips_table.py` — Migration for elastic_ips table + host.max_eips column
- `src/backend/app/services/eip_service.py` — EIP lifecycle: allocate, associate, disassociate, release, migrate, SG sync
- `src/backend/app/services/provider_gc_service.py` — Provider-level GC: orphan EIPs, stale SG rules, orphan secondary IPs
- `src/backend/app/api/eips.py` — API routes: release EIP, provider GC endpoint
- `src/backend/tests/test_eip_service.py` — Unit tests for EIP service
- `src/backend/tests/test_provider_gc.py` — Unit tests for provider GC

### Modified files
- `src/backend/app/models/__init__.py` — Register ElasticIp model
- `src/backend/app/models/host.py` — Add `max_eips` column
- `src/backend/app/main.py` — Register eips router
- `src/backend/app/services/placement.py` — Add EIP capacity check to `find_available_host` and `calculate_project_requirements`
- `src/backend/app/services/provisioner.py` — Populate `max_eips` on host provision
- `src/backend/app/services/deploy_service.py` — Add EIP allocation/association step to deploy, disassociation to stop, release to destroy
- `src/backend/app/services/vxlan.py` — Update `generate_setup_script` to add secondary IPs and IP-specific DNAT rules
- `src/frontend/src/components/canvas/ExternalIpsPanel.tsx` — Read-only IP, status indicators, release API call
- `src/frontend/src/app/admin/hosts/page.tsx` — Show EIP capacity on host cards
- `src/frontend/src/app/admin/providers/page.tsx` — Add Clean button for provider GC

---

### Task 1: ElasticIp Model + Migration

**Files:**
- Create: `src/backend/app/models/elastic_ip.py`
- Modify: `src/backend/app/models/__init__.py`
- Modify: `src/backend/app/models/host.py:29` (add max_eips after storage_size_gb)

- [ ] **Step 1: Create the ElasticIp model**

Create `src/backend/app/models/elastic_ip.py`:

```python
import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ElasticIp(Base):
    __tablename__ = "elastic_ips"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    canvas_eip_id: Mapped[str] = mapped_column(String(100))
    allocation_id: Mapped[str] = mapped_column(String(100))
    public_ip: Mapped[str] = mapped_column(String(45))
    private_ip: Mapped[str | None] = mapped_column(String(45))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id", ondelete="SET NULL"))
    association_id: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="allocated")
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 2: Add max_eips to Host model**

In `src/backend/app/models/host.py`, add after the `storage_size_gb` line (line 29):

```python
    max_eips: Mapped[int] = mapped_column(Integer, default=0)
```

- [ ] **Step 3: Register ElasticIp in models __init__**

In `src/backend/app/models/__init__.py`, add the import and __all__ entry:

```python
from app.models.elastic_ip import ElasticIp
```

Add `"ElasticIp"` to the `__all__` list.

- [ ] **Step 4: Generate the Alembic migration**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add elastic_ips table and host max_eips"
```

Edit the generated migration file to contain:

```python
def upgrade() -> None:
    op.create_table(
        'elastic_ips',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('provider_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('providers.id'), nullable=False),
        sa.Column('project_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('projects.id', ondelete='SET NULL'), nullable=True),
        sa.Column('canvas_eip_id', sa.String(100), nullable=False),
        sa.Column('allocation_id', sa.String(100), nullable=False),
        sa.Column('public_ip', sa.String(45), nullable=False),
        sa.Column('private_ip', sa.String(45), nullable=True),
        sa.Column('host_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('hosts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('association_id', sa.String(100), nullable=True),
        sa.Column('state', sa.String(20), nullable=False, server_default='allocated'),
        sa.Column('tags', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column('hosts', sa.Column('max_eips', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('hosts', 'max_eips')
    op.drop_table('elastic_ips')
```

- [ ] **Step 5: Run migration and verify**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```
Expected: migration applies cleanly.

- [ ] **Step 6: Run existing tests to confirm no breakage**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all existing tests pass (conftest.py recreates tables from models, so the new model gets picked up).

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/models/elastic_ip.py src/backend/app/models/host.py src/backend/app/models/__init__.py src/backend/alembic/versions/*elastic_ips*
git commit -m "feat: add ElasticIp model and host max_eips column"
```

---

### Task 2: EIP Service — Core Lifecycle

**Files:**
- Create: `src/backend/app/services/eip_service.py`
- Create: `src/backend/tests/test_eip_service.py`

- [ ] **Step 1: Write tests for EIP service**

Create `src/backend/tests/test_eip_service.py`:

```python
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.core.database import get_db
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession


_db = TestSession()
_user = User(email="eip-test@example.com", display_name="EIP Tester", role="admin",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)

_provider = Provider(name="eip-test-provider", type="ec2", default_region="us-east-1", state="active")
_provider.set_credentials({"access_key_id": "fake", "secret_access_key": "fake"})
_db.add(_provider)

_host = Host(
    instance_id="i-eiptest",
    instance_type="m8i.xlarge",
    provider_id=_provider.id,
    state="active",
    host_type="shared",
    total_vcpus=16,
    total_ram_mb=65536,
    ip_address="10.0.0.1",
    private_key="fake-key",
    agent_status="connected",
    storage_size_gb=500,
    max_eips=14,
)
_db.add(_host)
_db.commit()
_db.refresh(_user)
_db.refresh(_provider)
_db.refresh(_host)

PROVIDER_ID = _provider.id
HOST_ID = _host.id
_db.close()


@patch("app.services.eip_service._get_ec2_client")
def test_allocate_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-abc123",
        "PublicIp": "54.1.2.3",
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=PROVIDER_ID).first()

    from app.services.eip_service import allocate_eip
    eip = allocate_eip(db, provider, "proj-123", "eip-canvas-1")

    assert eip.public_ip == "54.1.2.3"
    assert eip.allocation_id == "eipalloc-abc123"
    assert eip.state == "allocated"
    assert eip.project_id == "proj-123"
    assert eip.canvas_eip_id == "eip-canvas-1"

    mock_ec2.allocate_address.assert_called_once_with(Domain="vpc")
    mock_ec2.create_tags.assert_called_once()
    tags_arg = mock_ec2.create_tags.call_args
    tag_keys = {t["Key"] for t in tags_arg.kwargs.get("Tags", tags_arg[1].get("Tags", []))}
    assert "ManagedBy" in tag_keys
    assert "troshka-project-id" in tag_keys

    db.query(ElasticIp).filter_by(id=eip.id).delete()
    db.commit()
    db.close()


@patch("app.services.eip_service._get_ec2_client")
def test_release_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    eip = ElasticIp(
        provider_id=PROVIDER_ID,
        project_id="proj-release",
        canvas_eip_id="eip-rel-1",
        allocation_id="eipalloc-rel",
        public_ip="54.9.8.7",
        state="allocated",
    )
    db.add(eip)
    db.commit()
    eip_id = eip.id

    from app.services.eip_service import release_eip
    release_eip(db, eip)

    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-rel")
    assert db.query(ElasticIp).filter_by(id=eip_id).first() is None
    db.close()


@patch("app.services.eip_service.run_ssh_script")
@patch("app.services.eip_service._get_ec2_client")
def test_associate_eip(mock_get_client, mock_ssh):
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"NetworkInterfaces": [
            {"NetworkInterfaceId": "eni-abc", "Attachment": {"DeviceIndex": 0}}
        ]}]}]
    }
    mock_ec2.assign_private_ip_addresses.return_value = {
        "AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.0.0.50"}]
    }
    mock_ec2.associate_address.return_value = {"AssociationId": "eipassoc-xyz"}
    mock_get_client.return_value = mock_ec2
    mock_ssh.return_value = {"success": True, "output": "", "exit_code": 0}

    db = TestSession()
    host = db.query(Host).filter_by(id=HOST_ID).first()
    eip = ElasticIp(
        provider_id=PROVIDER_ID,
        project_id="proj-assoc",
        canvas_eip_id="eip-assoc-1",
        allocation_id="eipalloc-assoc",
        public_ip="54.5.5.5",
        state="allocated",
    )
    db.add(eip)
    db.commit()

    from app.services.eip_service import associate_eip
    associate_eip(db, eip, host)

    assert eip.state == "associated"
    assert eip.private_ip == "10.0.0.50"
    assert eip.host_id == HOST_ID
    assert eip.association_id == "eipassoc-xyz"

    mock_ec2.associate_address.assert_called_once_with(
        AllocationId="eipalloc-assoc",
        NetworkInterfaceId="eni-abc",
        PrivateIpAddress="10.0.0.50",
    )

    db.query(ElasticIp).filter_by(id=eip.id).delete()
    db.commit()
    db.close()


@patch("app.services.eip_service.run_ssh_script")
@patch("app.services.eip_service._get_ec2_client")
def test_disassociate_eip(mock_get_client, mock_ssh):
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"NetworkInterfaces": [
            {"NetworkInterfaceId": "eni-abc", "Attachment": {"DeviceIndex": 0}}
        ]}]}]
    }
    mock_get_client.return_value = mock_ec2
    mock_ssh.return_value = {"success": True, "output": "", "exit_code": 0}

    db = TestSession()
    host = db.query(Host).filter_by(id=HOST_ID).first()
    eip = ElasticIp(
        provider_id=PROVIDER_ID,
        project_id="proj-disassoc",
        canvas_eip_id="eip-dis-1",
        allocation_id="eipalloc-dis",
        public_ip="54.3.3.3",
        private_ip="10.0.0.60",
        host_id=HOST_ID,
        association_id="eipassoc-dis",
        state="associated",
    )
    db.add(eip)
    db.commit()

    from app.services.eip_service import disassociate_eip
    disassociate_eip(db, eip, host)

    assert eip.state == "allocated"
    assert eip.private_ip is None
    assert eip.host_id is None
    assert eip.association_id is None
    mock_ec2.disassociate_address.assert_called_once_with(AssociationId="eipassoc-dis")

    db.query(ElasticIp).filter_by(id=eip.id).delete()
    db.commit()
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.eip_service'`

- [ ] **Step 3: Implement EIP service**

Create `src/backend/app/services/eip_service.py`:

```python
"""EIP lifecycle management — allocate, associate, disassociate, release."""
import logging

import boto3
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.services.deploy_service import run_ssh_script

logger = logging.getLogger(__name__)


def _get_ec2_client(provider):
    creds = provider.get_credentials()
    return boto3.client(
        "ec2",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )


def _get_primary_eni(ec2, instance_id: str) -> str:
    desc = ec2.describe_instances(InstanceIds=[instance_id])
    for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
        if eni["Attachment"]["DeviceIndex"] == 0:
            return eni["NetworkInterfaceId"]
    raise ValueError(f"No primary ENI found for {instance_id}")


def _detect_primary_iface(host_ip: str, private_key: str) -> str:
    result = run_ssh_script(host_ip, private_key,
        "ip route show default | awk '{print $5}' | head -1", timeout=10)
    iface = result.get("output", "").strip().splitlines()
    return iface[-1] if iface else "eth0"


def allocate_eip(db: Session, provider, project_id: str, canvas_eip_id: str) -> ElasticIp:
    ec2 = _get_ec2_client(provider)
    result = ec2.allocate_address(Domain="vpc")
    allocation_id = result["AllocationId"]
    public_ip = result["PublicIp"]

    tags = [
        {"Key": "ManagedBy", "Value": "troshka"},
        {"Key": "troshka-provider-id", "Value": provider.id},
        {"Key": "troshka-project-id", "Value": project_id},
        {"Key": "troshka-canvas-eip-id", "Value": canvas_eip_id},
    ]
    ec2.create_tags(Resources=[allocation_id], Tags=tags)

    eip = ElasticIp(
        provider_id=provider.id,
        project_id=project_id,
        canvas_eip_id=canvas_eip_id,
        allocation_id=allocation_id,
        public_ip=public_ip,
        state="allocated",
        tags={t["Key"]: t["Value"] for t in tags},
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    logger.info("Allocated EIP %s (%s) for project %s", allocation_id, public_ip, project_id[:8])
    return eip


def associate_eip(db: Session, eip: ElasticIp, host) -> None:
    from app.models.provider import Provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    ec2 = _get_ec2_client(provider)

    eni_id = _get_primary_eni(ec2, host.instance_id)

    assign_result = ec2.assign_private_ip_addresses(
        NetworkInterfaceId=eni_id,
        SecondaryPrivateIpAddressCount=1,
    )
    private_ip = assign_result["AssignedPrivateIpAddresses"][0]["PrivateIpAddress"]

    assoc_result = ec2.associate_address(
        AllocationId=eip.allocation_id,
        NetworkInterfaceId=eni_id,
        PrivateIpAddress=private_ip,
    )

    iface = _detect_primary_iface(host.ip_address, host.private_key)
    run_ssh_script(host.ip_address, host.private_key,
        f"ip addr add {private_ip}/32 dev {iface} 2>/dev/null || true", timeout=10)

    eip.private_ip = private_ip
    eip.host_id = host.id
    eip.association_id = assoc_result["AssociationId"]
    eip.state = "associated"
    db.commit()

    logger.info("Associated EIP %s → %s on host %s", eip.public_ip, private_ip, host.id[:8])


def disassociate_eip(db: Session, eip: ElasticIp, host) -> None:
    from app.models.provider import Provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    ec2 = _get_ec2_client(provider)

    ec2.disassociate_address(AssociationId=eip.association_id)

    eni_id = _get_primary_eni(ec2, host.instance_id)
    ec2.unassign_private_ip_addresses(
        NetworkInterfaceId=eni_id,
        PrivateIpAddresses=[eip.private_ip],
    )

    iface = _detect_primary_iface(host.ip_address, host.private_key)
    run_ssh_script(host.ip_address, host.private_key,
        f"ip addr del {eip.private_ip}/32 dev {iface} 2>/dev/null || true", timeout=10)

    eip.private_ip = None
    eip.host_id = None
    eip.association_id = None
    eip.state = "allocated"
    db.commit()

    logger.info("Disassociated EIP %s from host %s", eip.public_ip, host.id[:8])


def release_eip(db: Session, eip: ElasticIp) -> None:
    if eip.state == "associated" and eip.host_id:
        from app.models.host import Host
        host = db.query(Host).filter_by(id=eip.host_id).first()
        if host:
            disassociate_eip(db, eip, host)

    from app.models.provider import Provider
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    ec2 = _get_ec2_client(provider)
    ec2.release_address(AllocationId=eip.allocation_id)

    logger.info("Released EIP %s (%s)", eip.allocation_id, eip.public_ip)
    db.delete(eip)
    db.commit()


def migrate_eip(db: Session, eip: ElasticIp, from_host, to_host) -> None:
    disassociate_eip(db, eip, from_host)
    associate_eip(db, eip, to_host)
    logger.info("Migrated EIP %s from host %s to %s", eip.public_ip, from_host.id[:8], to_host.id[:8])


def get_host_eip_usage(db: Session, host_id: str) -> int:
    return db.query(ElasticIp).filter_by(host_id=host_id, state="associated").count()
```

- [ ] **Step 4: Run tests**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py -v
```
Expected: all 4 tests pass.

- [ ] **Step 5: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/eip_service.py src/backend/tests/test_eip_service.py
git commit -m "feat: add EIP service with allocate, associate, disassociate, release"
```

---

### Task 3: Security Group Reconciliation

**Files:**
- Modify: `src/backend/app/services/eip_service.py` (add sync_security_group_rules)
- Modify: `src/backend/tests/test_eip_service.py` (add SG test)

- [ ] **Step 1: Write test for SG sync**

Append to `src/backend/tests/test_eip_service.py`:

```python
@patch("app.services.eip_service._get_ec2_client")
def test_sync_security_group_rules(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "troshka-pf:proj-old:8080"}]},
        ]}]
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=PROVIDER_ID).first()
    provider.security_group_id = "sg-test123"
    db.commit()

    desired = [{"project_id": "proj-new", "ext_port": 443, "protocol": "tcp"}]

    from app.services.eip_service import sync_security_group_rules
    result = sync_security_group_rules(db, provider, desired)

    assert result["added"] == 1
    assert result["removed"] == 1
    mock_ec2.authorize_security_group_ingress.assert_called_once()
    mock_ec2.revoke_security_group_ingress.assert_called_once()
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py::test_sync_security_group_rules -v
```
Expected: FAIL — `ImportError: cannot import name 'sync_security_group_rules'`

- [ ] **Step 3: Implement SG reconciliation**

Add to `src/backend/app/services/eip_service.py`:

```python
def sync_security_group_rules(db: Session, provider, desired_rules: list[dict]) -> dict:
    """Reconcile SG ingress rules. Only touches rules with 'troshka-pf:' description prefix."""
    if not provider.security_group_id:
        return {"added": 0, "removed": 0, "error": "No security group configured"}

    ec2 = _get_ec2_client(provider)
    sg_id = provider.security_group_id

    sg = ec2.describe_security_groups(GroupIds=[sg_id])
    current_perms = sg["SecurityGroups"][0]["IpPermissions"]

    current_pf_rules = {}
    for perm in current_perms:
        for ip_range in perm.get("IpRanges", []):
            desc = ip_range.get("Description", "")
            if desc.startswith("troshka-pf:"):
                key = f"{perm['IpProtocol']}:{perm['FromPort']}"
                current_pf_rules[key] = {
                    "protocol": perm["IpProtocol"],
                    "port": perm["FromPort"],
                    "description": desc,
                }

    desired_set = {}
    for rule in desired_rules:
        key = f"{rule.get('protocol', 'tcp')}:{rule['ext_port']}"
        desired_set[key] = {
            "protocol": rule.get("protocol", "tcp"),
            "port": rule["ext_port"],
            "description": f"troshka-pf:{rule['project_id']}:{rule['ext_port']}",
        }

    to_add = {k: v for k, v in desired_set.items() if k not in current_pf_rules}
    to_remove = {k: v for k, v in current_pf_rules.items() if k not in desired_set}

    if to_add:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": r["protocol"],
                "FromPort": r["port"],
                "ToPort": r["port"],
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": r["description"]}],
            } for r in to_add.values()],
        )

    if to_remove:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": r["protocol"],
                "FromPort": r["port"],
                "ToPort": r["port"],
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": r["description"]}],
            } for r in to_remove.values()],
        )

    added = len(to_add)
    removed = len(to_remove)
    if added or removed:
        logger.info("SG %s sync: +%d -%d rules", sg_id, added, removed)
    return {"added": added, "removed": removed}
```

- [ ] **Step 4: Run tests**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py -v
```
Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/services/eip_service.py src/backend/tests/test_eip_service.py
git commit -m "feat: add security group rule reconciliation for port forwards"
```

---

### Task 4: Placement Integration

**Files:**
- Modify: `src/backend/app/services/placement.py:19-38` (calculate_project_requirements)
- Modify: `src/backend/app/services/placement.py:71-85` (find_available_host)
- Modify: `src/backend/app/services/provisioner.py:191-206` (provision_host return)

- [ ] **Step 1: Add EIP count to calculate_project_requirements**

In `src/backend/app/services/placement.py`, update `calculate_project_requirements` to also count external IPs:

```python
def calculate_project_requirements(topology: dict) -> dict:
    """Calculate total resource requirements from a project's topology."""
    nodes = topology.get("nodes", [])
    vms = [n for n in nodes if n.get("type") == "vmNode"]

    total_vcpus = 0
    total_ram_mb = 0
    vm_count = 0

    for vm in vms:
        data = vm.get("data", {})
        total_vcpus += data.get("vcpus", 2)
        total_ram_mb += data.get("ram", 4) * 1024
        vm_count += 1

    external_ips = topology.get("externalIps", [])

    return {
        "vm_count": vm_count,
        "total_vcpus": total_vcpus,
        "total_ram_mb": total_ram_mb,
        "requested_eips": len(external_ips),
    }
```

- [ ] **Step 2: Add EIP capacity check to find_available_host**

In `src/backend/app/services/placement.py`, update `find_available_host` to accept and check EIP requirements:

```python
def find_available_host(db: Session, required_vcpus: int, required_ram_mb: int, required_eips: int = 0) -> Host | None:
    """Find an active host with enough free capacity (with overcommit)."""
    from app.services.eip_service import get_host_eip_usage

    hosts = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
    ).all()

    for host in hosts:
        alloc_vcpus, alloc_ram = get_allocatable(host)
        free_vcpus = alloc_vcpus - host.used_vcpus
        free_ram = alloc_ram - host.used_ram_mb
        if free_vcpus < required_vcpus or free_ram < required_ram_mb:
            continue
        if required_eips > 0:
            eip_used = get_host_eip_usage(db, host.id)
            if host.max_eips - eip_used < required_eips:
                continue
        return host

    return None
```

- [ ] **Step 3: Pass EIP count through place_project**

In `src/backend/app/services/placement.py`, update the `place_project` function call to `find_available_host` (around line 97):

Change:
```python
    host = find_available_host(db, reqs["total_vcpus"], reqs["total_ram_mb"])
```
To:
```python
    host = find_available_host(db, reqs["total_vcpus"], reqs["total_ram_mb"], reqs["requested_eips"])
```

- [ ] **Step 4: Populate max_eips on host provisioning**

In `src/backend/app/services/provisioner.py`, in `provision_host`, after the `describe_instance_types` call (line 190), add max_eips to the return dict. Change the return block:

```python
    return {
        "host_id": host_id,
        "instance_id": instance_id,
        "instance_type": instance_type,
        "public_ip": inst.get("PublicIpAddress"),
        "private_ip": inst.get("PrivateIpAddress"),
        "ami_id": ami_id,
        "state": "active",
        "total_vcpus": type_info.get("VCpuInfo", {}).get("DefaultVCpus", 0),
        "total_ram_mb": type_info.get("MemoryInfo", {}).get("SizeInMiB", 0),
        "max_eips": type_info.get("NetworkInfo", {}).get("Ipv4AddressesPerInterface", 1) - 1,
        "key_pair_name": key_name,
        "private_key": private_key,
        "storage_size_gb": storage_size_gb,
    }
```

- [ ] **Step 5: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/placement.py src/backend/app/services/provisioner.py
git commit -m "feat: add EIP capacity check to placement and host provisioning"
```

---

### Task 5: Deploy/Undeploy/Destroy EIP Integration

**Files:**
- Modify: `src/backend/app/services/deploy_service.py:860-965` (deploy_project_async)
- Modify: `src/backend/app/services/deploy_service.py:968-1015` (stop_project_async)
- Modify: `src/backend/app/services/deploy_service.py:1083-1113` (destroy_project_sync)

- [ ] **Step 1: Add EIP step to deploy_project_async**

In `src/backend/app/services/deploy_service.py`, in `deploy_project_async`, add a new step between the networking step (after line 900) and the cloud-init step (line 903). Insert after the network setup success check:

```python
        # Step 1b: Allocate and associate EIPs
        external_ips = topology.get("externalIps", [])
        if external_ips:
            _deploy_progress[project_id] = {"step": "eips", "detail": "allocating elastic IPs"}
            logger.info("Deploy %s: allocating %d EIPs", project_id[:8], len(external_ips))
            from app.services.eip_service import allocate_eip, associate_eip, sync_security_group_rules
            from app.models.elastic_ip import ElasticIp
            from app.models.provider import Provider

            provider = s.query(Provider).filter_by(id=project.provider_id).first()
            if not provider:
                project.state = "error"
                project.deploy_error = "No provider configured for EIP allocation"
                s.commit()
                _deploy_progress.pop(project_id, None)
                return

            for ext_ip in external_ips:
                canvas_id = ext_ip.get("id", "")
                existing = s.query(ElasticIp).filter_by(
                    project_id=project_id, canvas_eip_id=canvas_id
                ).first()
                if existing:
                    eip = existing
                else:
                    eip = allocate_eip(s, provider, project_id, canvas_id)

                if eip.state != "associated":
                    associate_eip(s, eip, host)

                ext_ip["ip"] = eip.public_ip
                ext_ip["_private_ip"] = eip.private_ip

            project.topology = topology
            s.commit()

            # Sync SG rules for port forwards
            gateway_node = next(
                (n for n in topology.get("nodes", [])
                 if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"),
                None,
            )
            if gateway_node and gateway_node.get("data", {}).get("gatewayMode") == "nat-portforward":
                desired_sg = []
                for pf in gateway_node.get("data", {}).get("portForwards", []):
                    if pf.get("extPort"):
                        desired_sg.append({
                            "project_id": project_id,
                            "ext_port": int(pf["extPort"]),
                            "protocol": "tcp",
                        })
                sync_security_group_rules(s, provider, desired_sg)
```

- [ ] **Step 2: Add EIP disassociation to stop_project_async**

In `src/backend/app/services/deploy_service.py`, in `stop_project_async`, after the network teardown (around line 997), add:

```python
        # Disassociate EIPs (but don't release — keep for redeploy)
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import disassociate_eip
        project_eips = s.query(ElasticIp).filter_by(project_id=project_id, state="associated").all()
        for eip in project_eips:
            try:
                disassociate_eip(s, eip, host)
            except Exception:
                logger.warning("Failed to disassociate EIP %s on stop", eip.public_ip)
```

- [ ] **Step 3: Add EIP re-association to start_project_async**

In `src/backend/app/services/deploy_service.py`, in `start_project_async`, after the network setup succeeds (around line 1053), add:

```python
        # Re-associate EIPs for this project
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import associate_eip
        project_eips = s.query(ElasticIp).filter_by(project_id=project_id, state="allocated").all()
        for eip in project_eips:
            try:
                associate_eip(s, eip, host)
            except Exception:
                logger.warning("Failed to re-associate EIP %s on start", eip.public_ip)

        # Re-sync SG rules
        if project_eips:
            from app.models.provider import Provider
            from app.services.eip_service import sync_security_group_rules
            provider = s.query(Provider).filter_by(id=project.provider_id).first()
            if provider:
                topo = project.topology or {}
                gw_node = next(
                    (n for n in topo.get("nodes", [])
                     if n.get("type") == "networkNode" and n.get("data", {}).get("subtype") == "gateway"
                     and n.get("data", {}).get("gatewayMode") == "nat-portforward"),
                    None,
                )
                if gw_node:
                    desired_sg = [
                        {"project_id": project_id, "ext_port": int(pf["extPort"]), "protocol": "tcp"}
                        for pf in gw_node.get("data", {}).get("portForwards", [])
                        if pf.get("extPort")
                    ]
                    sync_security_group_rules(s, provider, desired_sg)
```

- [ ] **Step 4: Add EIP release to destroy_project_sync**

In `src/backend/app/services/deploy_service.py`, in `destroy_project_sync`, after the destroy script runs (around line 1102), add:

```python
        # Release all EIPs for this project
        from app.models.elastic_ip import ElasticIp
        from app.services.eip_service import release_eip
        project_eips = s.query(ElasticIp).filter_by(project_id=project_id).all()
        for eip in project_eips:
            try:
                release_eip(s, eip)
            except Exception:
                logger.warning("Failed to release EIP %s on destroy", eip.public_ip)
```

- [ ] **Step 5: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/deploy_service.py
git commit -m "feat: integrate EIP allocation into deploy/stop/destroy lifecycle"
```

---

### Task 6: Gateway DNAT with EIP Private IPs

**Files:**
- Modify: `src/backend/app/services/vxlan.py:177-189` (build_host_network_config — gateway section)
- Modify: `src/backend/app/services/vxlan.py:319-335` (generate_setup_script — gateway section)

- [ ] **Step 1: Pass EIP mappings through network config**

In `src/backend/app/services/vxlan.py`, update `build_host_network_config` to include EIP private IP mappings in the gateway config. Change the gateway config block (lines 177-189):

```python
    # Build gateway config if present
    gateway_config = None
    for node in nodes:
        if node.get("type") == "networkNode" and node.get("data", {}).get("subtype") == "gateway":
            data = node.get("data", {})
            external_ips = topology.get("externalIps", [])
            eip_map = {eip["id"]: eip for eip in external_ips}

            port_forwards = []
            for pf in data.get("portForwards", []):
                pf_entry = dict(pf)
                ext_ip = eip_map.get(pf.get("extIpId", ""), {})
                pf_entry["_private_ip"] = ext_ip.get("_private_ip", "")
                port_forwards.append(pf_entry)

            gateway_config = {
                "name": data.get("name"),
                "mode": data.get("gatewayMode", "nat"),
                "outbound_policy": data.get("outboundPolicy", "allow-all"),
                "outbound_ports": data.get("outboundPorts", ""),
                "port_forwards": port_forwards,
                "eip_private_ips": [eip.get("_private_ip", "") for eip in external_ips if eip.get("_private_ip")],
            }
            break
```

- [ ] **Step 2: Update generate_setup_script for IP-specific DNAT**

In `src/backend/app/services/vxlan.py`, update the gateway section of `generate_setup_script` (lines 319-335):

```python
    # Gateway NAT
    gw = config.get("gateway")
    if gw and gw.get("mode") in ("nat", "nat-portforward"):
        gateway_cmds.append("sysctl -w net.ipv4.ip_forward=1")

        # Add secondary private IPs for EIPs
        for priv_ip in gw.get("eip_private_ips", []):
            if priv_ip:
                gateway_cmds.append(f"PRIMARY_IFACE=$(ip route show default | awk '{{print $5}}' | head -1)")
                gateway_cmds.append(f"ip addr add {priv_ip}/32 dev $PRIMARY_IFACE 2>/dev/null || true")

        # Masquerade outbound from all project bridges
        for net in config.get("networks", []):
            bridge = net["bridge_name"]
            gateway_cmds.append(f"nft add rule inet nat postrouting oifname != \"{bridge}\" iifname \"{bridge}\" masquerade")

        # Port forwards with EIP-specific DNAT
        if gw.get("mode") == "nat-portforward":
            for pf in gw.get("port_forwards", []):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                priv_ip = pf.get("_private_ip", "")
                if ext_port and int_ip and int_port:
                    if priv_ip:
                        gateway_cmds.append(f"nft add rule inet nat prerouting ip daddr {priv_ip} tcp dport {ext_port} dnat to {int_ip}:{int_port}")
                    else:
                        gateway_cmds.append(f"nft add rule inet nat prerouting tcp dport {ext_port} dnat to {int_ip}:{int_port}")
```

- [ ] **Step 3: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/vxlan.py
git commit -m "feat: add EIP secondary IPs and IP-specific DNAT to gateway setup"
```

---

### Task 7: EIP Release API + Provider GC Endpoint

**Files:**
- Create: `src/backend/app/api/eips.py`
- Modify: `src/backend/app/main.py` (register router)

- [ ] **Step 1: Create EIP API routes**

Create `src/backend/app/api/eips.py`:

```python
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["eips"])


@router.delete("/projects/{project_id}/eips/{canvas_eip_id}", status_code=200)
def release_project_eip(
    project_id: str,
    canvas_eip_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Release a specific EIP from a project (when user removes it from canvas)."""
    from app.models.project import Project
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    eip = db.query(ElasticIp).filter_by(
        project_id=project_id, canvas_eip_id=canvas_eip_id
    ).first()
    if not eip:
        return {"status": "not_allocated"}

    from app.services.eip_service import release_eip
    release_eip(db, eip)
    return {"status": "released", "public_ip": eip.public_ip}


@router.get("/projects/{project_id}/eips")
def list_project_eips(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all EIPs allocated for a project."""
    from app.models.project import Project
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    eips = db.query(ElasticIp).filter_by(project_id=project_id).all()
    return [
        {
            "id": eip.id,
            "canvas_eip_id": eip.canvas_eip_id,
            "public_ip": eip.public_ip,
            "state": eip.state,
            "host_id": eip.host_id,
        }
        for eip in eips
    ]


@router.post("/providers/{provider_id}/gc")
def provider_gc(
    provider_id: str,
    dry_run: bool = False,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Run provider-level garbage collection on AWS resources."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type not in ("ec2",):
        raise HTTPException(status_code=400, detail="GC only supported for EC2 providers")

    from app.services.provider_gc_service import reconcile_provider
    return reconcile_provider(db, provider, dry_run=dry_run)
```

- [ ] **Step 2: Register router in main.py**

In `src/backend/app/main.py`, add after the patterns import (line 37):

```python
from app.api import eips as eip_routes  # noqa: E402
```

And add after the patterns router include (line 48):

```python
app.include_router(eip_routes.router, prefix="/api/v1")
```

- [ ] **Step 3: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass (provider GC service doesn't exist yet but won't be imported until endpoint is called).

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/api/eips.py src/backend/app/main.py
git commit -m "feat: add EIP release API and provider GC endpoint"
```

---

### Task 8: Provider-Level Garbage Collector

**Files:**
- Create: `src/backend/app/services/provider_gc_service.py`
- Create: `src/backend/tests/test_provider_gc.py`

- [ ] **Step 1: Write test for provider GC**

Create `src/backend/tests/test_provider_gc.py`:

```python
from unittest.mock import MagicMock, patch

from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from tests.conftest import TestSession


_db = TestSession()
_provider = Provider(name="gc-test-provider", type="ec2", default_region="us-east-1", state="active")
_provider.set_credentials({"access_key_id": "fake", "secret_access_key": "fake"})
_provider.security_group_id = "sg-gctest"
_db.add(_provider)
_db.commit()
_db.refresh(_provider)
GC_PROVIDER_ID = _provider.id
_db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_releases_orphan_eips(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-orphan1",
                "PublicIp": "54.0.0.1",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "troshka"},
                    {"Key": "troshka-project-id", "Value": "nonexistent-project-id"},
                ],
            },
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": []}]
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=False)

    assert result["eips_released"] == 1
    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-orphan1")
    db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_dry_run_does_not_release(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-orphan2",
                "PublicIp": "54.0.0.2",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "troshka"},
                    {"Key": "troshka-project-id", "Value": "nonexistent-project-id-2"},
                ],
            },
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": []}]
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=True)

    assert result["eips_released"] == 0
    assert result["eips_would_release"] == 1
    mock_ec2.release_address.assert_not_called()
    db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_removes_stale_sg_rules(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {"Addresses": []}
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 9090, "ToPort": 9090,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "troshka-pf:dead-project:9090"}]},
        ]}],
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=False)

    assert result["sg_rules_removed"] == 1
    mock_ec2.revoke_security_group_ingress.assert_called_once()
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_gc.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.provider_gc_service'`

- [ ] **Step 3: Implement provider GC service**

Create `src/backend/app/services/provider_gc_service.py`:

```python
"""Provider-level garbage collector — reconcile AWS resources with DB state."""
import logging

import boto3
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.models.project import Project

logger = logging.getLogger(__name__)


def _get_ec2_client(provider):
    creds = provider.get_credentials()
    return boto3.client(
        "ec2",
        region_name=provider.default_region,
        aws_access_key_id=creds.get("access_key_id"),
        aws_secret_access_key=creds.get("secret_access_key"),
    )


def _gc_orphan_eips(db: Session, provider, ec2, dry_run: bool) -> dict:
    """Find and release Troshka-tagged EIPs whose project no longer exists."""
    result = ec2.describe_addresses(Filters=[
        {"Name": "tag:ManagedBy", "Values": ["troshka"]},
    ])

    orphans = []
    for addr in result.get("Addresses", []):
        tags = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
        project_id = tags.get("troshka-project-id", "")
        if not project_id:
            orphans.append(addr)
            continue
        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            orphans.append(addr)

    released = 0
    for addr in orphans:
        alloc_id = addr["AllocationId"]
        if dry_run:
            logger.info("GC dry-run: would release orphan EIP %s (%s)", alloc_id, addr.get("PublicIp"))
            continue

        if addr.get("AssociationId"):
            try:
                ec2.disassociate_address(AssociationId=addr["AssociationId"])
            except Exception:
                logger.warning("Failed to disassociate orphan EIP %s", alloc_id)

        ec2.release_address(AllocationId=alloc_id)
        db_eip = db.query(ElasticIp).filter_by(allocation_id=alloc_id).first()
        if db_eip:
            db.delete(db_eip)
        released += 1
        logger.info("GC: released orphan EIP %s (%s)", alloc_id, addr.get("PublicIp"))

    db.commit()

    # Also clean stale DB rows with no matching AWS resource
    all_aws_alloc_ids = {a["AllocationId"] for a in result.get("Addresses", [])}
    stale_rows = db.query(ElasticIp).filter(
        ElasticIp.provider_id == provider.id,
        ~ElasticIp.allocation_id.in_(all_aws_alloc_ids) if all_aws_alloc_ids else True,
    ).all()
    stale_deleted = 0
    for row in stale_rows:
        if row.allocation_id not in all_aws_alloc_ids:
            if not dry_run:
                db.delete(row)
                stale_deleted += 1
    if stale_deleted:
        db.commit()
        logger.info("GC: deleted %d stale DB rows", stale_deleted)

    return {
        "eips_released": released,
        "eips_would_release": len(orphans) if dry_run else 0,
        "stale_db_rows_deleted": stale_deleted,
    }


def _gc_stale_sg_rules(db: Session, provider, ec2, dry_run: bool) -> dict:
    """Remove SG ingress rules for projects that no longer exist."""
    if not provider.security_group_id:
        return {"sg_rules_removed": 0}

    sg = ec2.describe_security_groups(GroupIds=[provider.security_group_id])
    current_perms = sg["SecurityGroups"][0]["IpPermissions"]

    stale_rules = []
    for perm in current_perms:
        for ip_range in perm.get("IpRanges", []):
            desc = ip_range.get("Description", "")
            if not desc.startswith("troshka-pf:"):
                continue
            parts = desc.split(":")
            if len(parts) >= 2:
                project_id = parts[1]
                project = db.query(Project).filter_by(id=project_id).first()
                if not project or project.state not in ("active", "deploying"):
                    stale_rules.append({
                        "protocol": perm["IpProtocol"],
                        "port": perm["FromPort"],
                        "description": desc,
                    })

    removed = 0
    if stale_rules and not dry_run:
        ec2.revoke_security_group_ingress(
            GroupId=provider.security_group_id,
            IpPermissions=[{
                "IpProtocol": r["protocol"],
                "FromPort": r["port"],
                "ToPort": r["port"],
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": r["description"]}],
            } for r in stale_rules],
        )
        removed = len(stale_rules)
        logger.info("GC: removed %d stale SG rules", removed)

    return {
        "sg_rules_removed": removed,
        "sg_rules_would_remove": len(stale_rules) if dry_run else 0,
    }


def reconcile_provider(db: Session, provider, dry_run: bool = False) -> dict:
    """Full provider-level GC: orphan EIPs + stale SG rules."""
    ec2 = _get_ec2_client(provider)
    report = {"provider_id": provider.id, "provider_name": provider.name}

    eip_result = _gc_orphan_eips(db, provider, ec2, dry_run)
    report.update(eip_result)

    sg_result = _gc_stale_sg_rules(db, provider, ec2, dry_run)
    report.update(sg_result)

    logger.info("Provider GC %s: %s", provider.name, report)
    return report
```

- [ ] **Step 4: Run tests**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_gc.py -v
```
Expected: all 3 tests pass.

- [ ] **Step 5: Run full test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/provider_gc_service.py src/backend/tests/test_provider_gc.py
git commit -m "feat: add provider-level garbage collector for EIPs and SG rules"
```

---

### Task 9: Frontend — ExternalIpsPanel Updates

**Files:**
- Modify: `src/frontend/src/components/canvas/ExternalIpsPanel.tsx`
- Modify: `src/frontend/src/stores/canvasStore.ts:68-72` (ExternalIp interface)

- [ ] **Step 1: Update ExternalIp interface**

In `src/frontend/src/stores/canvasStore.ts`, update the `ExternalIp` interface to include state:

```typescript
export interface ExternalIp {
  id: string;
  name: string;
  ip: string;
  _private_ip?: string;
  state?: "pending" | "allocated" | "associated";
}
```

- [ ] **Step 2: Update ExternalIpsPanel**

Replace `src/frontend/src/components/canvas/ExternalIpsPanel.tsx` with:

```tsx
"use client";

import React from "react";
import { useCanvasStore } from "@/stores/canvasStore";
import type { ExternalIp } from "@/stores/canvasStore";

interface Props {
  projectId?: string;
  onClose: () => void;
}

export default function ExternalIpsPanel({ projectId, onClose }: Props) {
  const externalIps = useCanvasStore((s) => s.externalIps);
  const setExternalIps = useCanvasStore((s) => s.setExternalIps);

  const addIp = () => {
    setExternalIps([...externalIps, {
      id: `eip-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`,
      name: `IP-${externalIps.length + 1}`,
      ip: "",
    }]);
  };

  const updateIp = (i: number, changes: Partial<ExternalIp>) => {
    const updated = [...externalIps];
    updated[i] = { ...updated[i], ...changes };
    setExternalIps(updated);
  };

  const removeIp = async (i: number) => {
    const eip = externalIps[i];
    if (eip.ip && projectId) {
      try {
        await fetch(`/api/v1/projects/${projectId}/eips/${eip.id}`, { method: "DELETE" });
      } catch {}
    }
    setExternalIps(externalIps.filter((_, idx) => idx !== i));
  };

  const statusDot = (eip: ExternalIp) => {
    if (eip.state === "associated") return { color: "#4caf50", title: "Associated (active)" };
    if (eip.state === "allocated") return { color: "#ff9800", title: "Allocated (not associated)" };
    if (eip.ip) return { color: "#4caf50", title: "Assigned" };
    return { color: "#666", title: "Not yet allocated" };
  };

  return (
    <div className="start-order-overlay" onClick={onClose}>
      <div className="start-order-modal" style={{ width: 450 }} onClick={(e) => e.stopPropagation()}>
        <div className="start-order-header">
          <span>External IPs</span>
          <button onClick={onClose}>✕</button>
        </div>
        <div className="start-order-body">
          <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", marginBottom: 12 }}>
            Allocate external IPs for this project. EIPs are assigned on first deploy and remain stable across redeploys.
          </p>
          {externalIps.length === 0 && (
            <p style={{ fontSize: 12, color: "var(--troshka-text-dim)", textAlign: "center", padding: 20 }}>
              No external IPs allocated. Click below to add one.
            </p>
          )}
          {externalIps.map((eip, i) => {
            const dot = statusDot(eip);
            return (
              <div key={eip.id} className="start-order-item">
                <div style={{ padding: 10, display: "flex", gap: 8, alignItems: "end" }}>
                  <div style={{ display: "flex", alignItems: "center", paddingBottom: 6 }}>
                    <span style={{
                      width: 8, height: 8, borderRadius: "50%",
                      backgroundColor: dot.color, display: "inline-block",
                    }} title={dot.title} />
                  </div>
                  <div style={{ flex: "0 0 100px" }}>
                    <label style={{ fontSize: 11, color: "var(--troshka-text-dim)", display: "block", marginBottom: 3 }}>Name</label>
                    <input
                      className="props-input"
                      value={eip.name}
                      onChange={(e) => updateIp(i, { name: e.target.value })}
                      placeholder="e.g. Primary"
                      style={{ fontSize: 12 }}
                    />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 11, color: "var(--troshka-text-dim)", display: "block", marginBottom: 3 }}>IP Address</label>
                    <input
                      className="props-input"
                      value={eip.ip}
                      readOnly
                      placeholder="Assigned on first deploy"
                      style={{ fontFamily: "monospace", fontSize: 12, opacity: eip.ip ? 1 : 0.5 }}
                    />
                  </div>
                  <button
                    style={{ background: "none", border: "none", color: "var(--troshka-red)", cursor: "pointer", fontSize: 14, padding: 4 }}
                    onClick={() => removeIp(i)}
                    title={eip.ip ? "Release and remove" : "Remove"}
                  >✕</button>
                </div>
              </div>
            );
          })}
        </div>
        <div className="start-order-footer">
          <button className="start-order-btn cancel" onClick={addIp}>+ Add IP</button>
          <button className="start-order-btn save" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add src/frontend/src/components/canvas/ExternalIpsPanel.tsx src/frontend/src/stores/canvasStore.ts
git commit -m "feat: update ExternalIpsPanel with read-only IPs and status indicators"
```

---

### Task 10: Frontend — Host EIP Capacity + Provider Clean Button

**Files:**
- Modify: `src/frontend/src/app/admin/hosts/page.tsx` (add EIP display to capacity section)
- Modify: `src/frontend/src/app/admin/providers/page.tsx` (add Clean button)

- [ ] **Step 1: Add EIP capacity to host cards**

In `src/frontend/src/app/admin/hosts/page.tsx`, find the capacity display section (where vCPUs and RAM are shown) and add EIP capacity. Look for the section that displays `used_vcpus` / `total_vcpus` and add after the RAM line:

```tsx
{h.max_eips > 0 && (
  <span>EIPs: {h.used_eips || 0}/{h.max_eips}</span>
)}
```

The host interface also needs `max_eips` and `used_eips` fields added. Find the `HostInfo` interface and add:

```typescript
max_eips: number;
used_eips: number;
```

The backend host list endpoint needs to return `used_eips`. In `src/backend/app/api/hosts.py`, where the host list response is built, add:

```python
from app.services.eip_service import get_host_eip_usage
# ... in the response dict for each host:
"used_eips": get_host_eip_usage(db, host.id),
"max_eips": host.max_eips,
```

- [ ] **Step 2: Add Clean button to provider cards**

In `src/frontend/src/app/admin/providers/page.tsx`, add a Clean button in the provider card action area (next to the Test, Edit, Delete buttons). Add after the Delete button for EC2 providers:

```tsx
{p.type === "ec2" && p.state === "active" && (
  <Button variant="secondary" onClick={async () => {
    const resp = await fetch(`/api/v1/providers/${p.id}/gc`, { method: "POST" });
    if (resp.ok) {
      const report = await resp.json();
      const parts = [];
      if (report.eips_released > 0) parts.push(`Released ${report.eips_released} orphan EIPs`);
      if (report.sg_rules_removed > 0) parts.push(`Removed ${report.sg_rules_removed} stale SG rules`);
      if (report.stale_db_rows_deleted > 0) parts.push(`Cleaned ${report.stale_db_rows_deleted} stale DB records`);
      if (parts.length === 0) parts.push("No orphans found");
      alert(parts.join("\n"));
    } else {
      alert("Provider GC failed — check server logs");
    }
  }}>
    Clean
  </Button>
)}
```

- [ ] **Step 3: Verify the frontend dev server compiles**

Check `http://localhost:3100` renders without errors. Navigate to admin hosts and admin providers pages.

- [ ] **Step 4: Commit**

```bash
git add src/frontend/src/app/admin/hosts/page.tsx src/frontend/src/app/admin/providers/page.tsx src/backend/app/api/hosts.py
git commit -m "feat: add EIP capacity display on hosts and Clean button on providers"
```

---

### Task 11: Final Integration Test

- [ ] **Step 1: Run full backend test suite**

Run:
```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 2: Verify frontend compiles**

Check `http://localhost:3100` — no compilation errors. Verify:
- ExternalIpsPanel shows read-only IP fields with status dots
- Host cards show EIP capacity (if max_eips > 0)
- Provider cards have Clean button (for EC2 providers)

- [ ] **Step 3: Final commit (if any fixups)**

```bash
git add -A && git commit -m "fix: final integration fixups for EIP feature"
```
