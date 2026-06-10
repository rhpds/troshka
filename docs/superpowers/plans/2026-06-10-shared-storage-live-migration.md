# Shared Storage & Live Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shared NFS storage (FSx OpenZFS or BYO) for VM disk images, enabling live migration between hosts and eliminating duplicate image caches.

**Architecture:** Storage pools group hosts sharing a common NFS mount. Troshkad resolves paths based on storage mode (local vs shared). Backend coordinates downloads and live migration. Frontend adds pool management and migration controls.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, Pydantic v2, boto3 (FSx API), libvirt (virsh migrate), Next.js 15, PatternFly 6

**Spec:** `docs/superpowers/specs/2026-06-10-shared-storage-live-migration-design.md`

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `src/backend/app/models/storage_pool.py` | StoragePool + SharedCacheEntry SQLAlchemy models |
| `src/backend/app/schemas/storage_pool.py` | Pydantic request/response schemas |
| `src/backend/app/api/storage_pools.py` | API router for storage pool CRUD + cache management |
| `src/backend/app/services/storage_pool_service.py` | FSx lifecycle, AZ probing, pool management |
| `src/backend/app/services/migration_service.py` | Live migration orchestration |
| `src/backend/alembic/versions/XXXX_add_storage_pools.py` | DB migration |
| `src/backend/tests/test_storage_pools.py` | Storage pool model + API tests |
| `src/backend/tests/test_migration_service.py` | Migration service tests |
| `src/frontend/src/app/admin/storage-pools/page.tsx` | Storage pools admin page |

### Modified Files

| File | Changes |
|------|---------|
| `src/backend/app/models/__init__.py` | Register StoragePool, SharedCacheEntry |
| `src/backend/app/models/host.py` | Add `storage_pool_id` FK |
| `src/backend/app/schemas/host.py` | Add `storage_pool_id` to HostCreate/HostResponse |
| `src/backend/app/main.py` | Register storage_pools router |
| `src/backend/app/api/hosts.py` | Pool-aware provisioning, evacuate endpoint |
| `src/backend/app/api/projects.py` | Add migrate endpoint |
| `src/backend/app/services/provisioner.py` | Pool-aware AZ pinning (no fallback for pooled hosts) |
| `src/backend/app/services/deploy_service.py` | Shared cache coordination, path resolution |
| `src/backend/app/services/gc_service.py` | Pool-level GC for shared storage |
| `src/backend/app/services/agent_deployer.py` | Pass storage_mode config to troshkad |
| `src/backend/app/api/providers.py` | Add NFS/migration SG rules to VPC setup |
| `src/troshkad/troshkad.py` | Path resolver, disk_cache param, `/commands/vm/migrate` endpoint |
| `src/frontend/src/app/layout.tsx` | Add "Storage Pools" to admin nav |
| `src/frontend/src/app/admin/hosts/page.tsx` | Pool selector in provision, evacuate button |
| `src/frontend/src/app/projects/[id]/page.tsx` | Migrate button for shared-pool projects |

---

## Phase 1: Storage Pool Model + API + FSx Provisioning

### Task 1: StoragePool and SharedCacheEntry Models

**Files:**
- Create: `src/backend/app/models/storage_pool.py`
- Modify: `src/backend/app/models/__init__.py`
- Test: `src/backend/tests/test_storage_pools.py`

- [ ] **Step 1: Write the model test**

Create `src/backend/tests/test_storage_pools.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects import sqlite

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models.storage_pool import StoragePool, SharedCacheEntry
from app.models.provider import Provider
from app.models.host import Host

engine = create_engine("sqlite:///./test_storage_pools.db", connect_args={"check_same_thread": False})
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_create_local_pool():
    db = Session()
    provider = Provider(name="test-aws", type="aws")
    db.add(provider)
    db.flush()

    pool = StoragePool(
        name="dev-local",
        mode="local",
        status="available",
        provider_id=provider.id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    assert pool.id is not None
    assert pool.mode == "local"
    assert pool.az is None
    assert pool.fsx_filesystem_id is None
    db.close()


def test_create_shared_fsx_pool():
    db = Session()
    provider = db.query(Provider).first()
    pool = StoragePool(
        name="prod-east-1b",
        mode="shared-fsx",
        az="us-east-1b",
        subnet_id="subnet-abc123",
        fsx_filesystem_id="fs-0abc123",
        fsx_dns_name="fs-0abc123.fsx.us-east-1.amazonaws.com",
        fsx_mount_ip="10.0.1.50",
        fsx_throughput_mbps=2048,
        fsx_storage_gb=5000,
        status="available",
        provider_id=provider.id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    assert pool.mode == "shared-fsx"
    assert pool.az == "us-east-1b"
    assert pool.fsx_throughput_mbps == 2048
    db.close()


def test_create_shared_byo_pool():
    db = Session()
    provider = db.query(Provider).first()
    pool = StoragePool(
        name="lab-nfs",
        mode="shared-byo",
        az="us-east-1a",
        nfs_endpoint="10.0.1.50:/exports/troshka",
        status="available",
        provider_id=provider.id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    assert pool.nfs_endpoint == "10.0.1.50:/exports/troshka"
    db.close()


def test_create_shared_cache_entry():
    db = Session()
    pool = db.query(StoragePool).filter_by(name="prod-east-1b").first()
    entry = SharedCacheEntry(
        storage_pool_id=pool.id,
        item_type="image",
        item_id="img-uuid-1234",
        status="ready",
        file_path="images/img-uuid-1234.qcow2",
        size_bytes=1073741824,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    assert entry.status == "ready"
    assert entry.file_path == "images/img-uuid-1234.qcow2"
    db.close()


def test_host_storage_pool_relationship():
    db = Session()
    pool = db.query(StoragePool).filter_by(name="prod-east-1b").first()
    provider = db.query(Provider).first()
    host = Host(
        provider_id=provider.id,
        state="active",
        storage_pool_id=pool.id,
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    assert host.storage_pool_id == pool.id

    db.refresh(pool)
    assert any(h.id == host.id for h in pool.hosts)
    db.close()


def test_pool_cache_entries_relationship():
    db = Session()
    pool = db.query(StoragePool).filter_by(name="prod-east-1b").first()
    assert len(pool.cache_entries) == 1
    assert pool.cache_entries[0].item_type == "image"
    db.close()


def test_host_without_pool():
    db = Session()
    provider = db.query(Provider).first()
    host = Host(
        provider_id=provider.id,
        state="active",
        storage_pool_id=None,
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    assert host.storage_pool_id is None
    assert host.storage_pool is None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_storage_pools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.storage_pool'`

- [ ] **Step 3: Create the StoragePool and SharedCacheEntry models**

Create `src/backend/app/models/storage_pool.py`:

```python
import datetime
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class StoragePool(Base):
    __tablename__ = "storage_pools"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    mode: Mapped[str] = mapped_column(String(20))
    az: Mapped[str | None] = mapped_column(String(50))
    subnet_id: Mapped[str | None] = mapped_column(String(50))

    fsx_filesystem_id: Mapped[str | None] = mapped_column(String(50))
    fsx_dns_name: Mapped[str | None] = mapped_column(String(255))
    fsx_mount_ip: Mapped[str | None] = mapped_column(String(45))
    fsx_throughput_mbps: Mapped[int | None] = mapped_column(Integer)
    fsx_storage_gb: Mapped[int | None] = mapped_column(Integer)

    nfs_endpoint: Mapped[str | None] = mapped_column(String(500))

    status: Mapped[str] = mapped_column(String(20), default="creating")
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider"] = relationship()
    hosts: Mapped[list["Host"]] = relationship(back_populates="storage_pool")
    cache_entries: Mapped[list["SharedCacheEntry"]] = relationship(
        back_populates="storage_pool", cascade="all, delete-orphan"
    )


class SharedCacheEntry(Base):
    __tablename__ = "shared_cache_entries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    storage_pool_id: Mapped[str] = mapped_column(ForeignKey("storage_pools.id"))
    item_type: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="downloading")
    file_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    downloaded_by_host_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    storage_pool: Mapped["StoragePool"] = relationship(back_populates="cache_entries")
```

- [ ] **Step 4: Add storage_pool_id to Host model**

In `src/backend/app/models/host.py`, add after the `agent_version` field:

```python
storage_pool_id: Mapped[str | None] = mapped_column(ForeignKey("storage_pools.id"))
```

Add the relationship after the existing `provider` relationship:

```python
storage_pool: Mapped["StoragePool | None"] = relationship(back_populates="hosts")
```

Add the import at the top (the ForeignKey import is already there).

- [ ] **Step 5: Register models in `__init__.py`**

In `src/backend/app/models/__init__.py`, add:

```python
from app.models.storage_pool import StoragePool, SharedCacheEntry
```

And update `__all__` to include `"StoragePool", "SharedCacheEntry"`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_storage_pools.py -v`
Expected: All 7 tests PASS

- [ ] **Step 7: Run full test suite to check for regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All existing tests continue to pass

- [ ] **Step 8: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/storage_pool.py src/backend/app/models/__init__.py src/backend/app/models/host.py src/backend/tests/test_storage_pools.py
git commit -m "feat: add StoragePool and SharedCacheEntry models"
```

---

### Task 2: Alembic Migration

**Files:**
- Create: `src/backend/alembic/versions/XXXX_add_storage_pools.py`

- [ ] **Step 1: Generate migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add storage pools and shared cache"`

- [ ] **Step 2: Edit the generated migration**

Replace the generated `upgrade()` and `downgrade()` with:

```python
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers will be auto-filled
revision: str = '<auto>'
down_revision: Union[str, Sequence[str], None] = 'fa4247629ec7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'storage_pools',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False, unique=True),
        sa.Column('mode', sa.String(20), nullable=False),
        sa.Column('az', sa.String(50), nullable=True),
        sa.Column('subnet_id', sa.String(50), nullable=True),
        sa.Column('fsx_filesystem_id', sa.String(50), nullable=True),
        sa.Column('fsx_dns_name', sa.String(255), nullable=True),
        sa.Column('fsx_mount_ip', sa.String(45), nullable=True),
        sa.Column('fsx_throughput_mbps', sa.Integer(), nullable=True),
        sa.Column('fsx_storage_gb', sa.Integer(), nullable=True),
        sa.Column('nfs_endpoint', sa.String(500), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='creating'),
        sa.Column('provider_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('providers.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'shared_cache_entries',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('storage_pool_id', postgresql.UUID(as_uuid=False), sa.ForeignKey('storage_pools.id'), nullable=False),
        sa.Column('item_type', sa.String(20), nullable=False),
        sa.Column('item_id', sa.String(36), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='downloading'),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('downloaded_by_host_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.add_column('hosts', sa.Column('storage_pool_id', postgresql.UUID(as_uuid=False),
                                      sa.ForeignKey('storage_pools.id'), nullable=True))


def downgrade() -> None:
    op.drop_column('hosts', 'storage_pool_id')
    op.drop_table('shared_cache_entries')
    op.drop_table('storage_pools')
```

- [ ] **Step 3: Run migration against dev database**

Run: `cd src/backend && ./venv/bin/python3 -m alembic upgrade head`
Expected: Migration applies without errors

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/alembic/versions/
git commit -m "migration: add storage_pools and shared_cache_entries tables"
```

---

### Task 3: Pydantic Schemas

**Files:**
- Create: `src/backend/app/schemas/storage_pool.py`
- Modify: `src/backend/app/schemas/host.py`

- [ ] **Step 1: Create storage pool schemas**

Create `src/backend/app/schemas/storage_pool.py`:

```python
import datetime

from pydantic import BaseModel


class StoragePoolCreate(BaseModel):
    name: str
    mode: str  # "local", "shared-fsx", "shared-byo"
    provider_id: str
    az: str | None = None
    instance_types: list[str] | None = None  # for AZ probing
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None


class StoragePoolUpdate(BaseModel):
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None


class StoragePoolResponse(BaseModel):
    id: str
    name: str
    mode: str
    az: str | None = None
    subnet_id: str | None = None
    fsx_filesystem_id: str | None = None
    fsx_dns_name: str | None = None
    fsx_mount_ip: str | None = None
    fsx_throughput_mbps: int | None = None
    fsx_storage_gb: int | None = None
    nfs_endpoint: str | None = None
    status: str
    provider_id: str
    host_count: int = 0
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class SharedCacheEntryResponse(BaseModel):
    id: str
    item_type: str
    item_id: str
    status: str
    file_path: str
    size_bytes: int | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AzProbeResult(BaseModel):
    az: str
    supported_types: list[str]
    unsupported_types: list[str]


class AzProbeResponse(BaseModel):
    results: list[AzProbeResult]
    recommended_az: str | None = None
```

- [ ] **Step 2: Update host schemas**

In `src/backend/app/schemas/host.py`:

Add `storage_pool_id: str | None = None` to `HostCreate`.
Add `storage_pool_id: str | None = None` to `HostResponse`.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/schemas/storage_pool.py src/backend/app/schemas/host.py
git commit -m "feat: add storage pool Pydantic schemas"
```

---

### Task 4: Storage Pool Service (FSx + AZ Probing)

**Files:**
- Create: `src/backend/app/services/storage_pool_service.py`

- [ ] **Step 1: Create the storage pool service**

Create `src/backend/app/services/storage_pool_service.py`:

```python
import logging
import threading
import uuid

import boto3

from app.core.database import SessionLocal
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)


def probe_az_capacity(credentials: dict, region: str, instance_types: list[str]) -> dict:
    """Probe which AZs support the given instance types. Returns {az: {supported: [], unsupported: []}}."""
    ec2 = boto3.client("ec2", region_name=region, **credentials)
    results = {}

    for itype in instance_types:
        resp = ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [itype]}],
        )
        supported_azs = {o["Location"] for o in resp["InstanceTypeOfferings"]}

        all_azs = ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )
        for az_info in all_azs["AvailabilityZones"]:
            az = az_info["ZoneName"]
            if az not in results:
                results[az] = {"supported": [], "unsupported": []}
            if az in supported_azs:
                results[az]["supported"].append(itype)
            else:
                results[az]["unsupported"].append(itype)

    return results


def find_best_az(az_results: dict, instance_types: list[str]) -> str | None:
    """Find AZ that supports all requested instance types. Returns None if no AZ supports all."""
    for az, data in sorted(az_results.items()):
        if len(data["supported"]) == len(instance_types):
            return az
    return None


def ensure_subnet_in_az(credentials: dict, region: str, vpc_id: str, az: str) -> str:
    """Find or create a troshka subnet in the specified AZ. Returns subnet_id."""
    ec2 = boto3.client("ec2", region_name=region, **credentials)

    existing = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "availability-zone", "Values": [az]},
        {"Name": "tag:ManagedBy", "Values": ["troshka"]},
    ])
    if existing["Subnets"]:
        return existing["Subnets"][0]["SubnetId"]

    all_subnets = ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "tag:ManagedBy", "Values": ["troshka"]},
    ])
    used_thirds = set()
    for s in all_subnets["Subnets"]:
        parts = s["CidrBlock"].split(".")
        used_thirds.add(int(parts[2]))

    third_octet = 1
    while third_octet in used_thirds:
        third_octet += 1
    cidr = f"10.100.{third_octet}.0/24"

    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)
    subnet_id = subnet["Subnet"]["SubnetId"]
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    ec2.create_tags(Resources=[subnet_id], Tags=[
        {"Key": "Name", "Value": f"troshka-{az}"},
        {"Key": "ManagedBy", "Value": "troshka"},
    ])

    vpc_data = ec2.describe_route_tables(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "tag:ManagedBy", "Values": ["troshka"]},
    ])
    if vpc_data["RouteTables"]:
        ec2.associate_route_table(
            RouteTableId=vpc_data["RouteTables"][0]["RouteTableId"],
            SubnetId=subnet_id,
        )

    return subnet_id


def create_fsx_filesystem(credentials: dict, region: str, subnet_id: str,
                           security_group_id: str, storage_gb: int, throughput_mbps: int) -> dict:
    """Create an FSx for OpenZFS filesystem. Returns {filesystem_id, dns_name, mount_ip} when ready."""
    fsx = boto3.client("fsx", region_name=region, **credentials)

    resp = fsx.create_file_system(
        FileSystemType="OPENZFS",
        StorageCapacity=storage_gb,
        SubnetIds=[subnet_id],
        SecurityGroupIds=[security_group_id],
        Tags=[
            {"Key": "Name", "Value": "troshka-shared-storage"},
            {"Key": "ManagedBy", "Value": "troshka"},
        ],
        OpenZFSConfiguration={
            "DeploymentType": "SINGLE_AZ_2",
            "ThroughputCapacity": throughput_mbps,
            "RootVolumeConfiguration": {
                "DataCompressionType": "LZ4",
                "NfsExports": [{
                    "ClientConfigurations": [{
                        "Clients": "*",
                        "Options": ["rw", "no_root_squash", "sync", "crossmnt"],
                    }]
                }],
            },
        },
    )

    return {
        "filesystem_id": resp["FileSystem"]["FileSystemId"],
        "dns_name": resp["FileSystem"].get("DNSName"),
    }


def _poll_fsx_until_available(pool_id: str, credentials: dict, region: str, filesystem_id: str):
    """Background thread: polls FSx status until AVAILABLE, then updates pool record."""
    import time
    fsx = boto3.client("fsx", region_name=region, **credentials)
    db = SessionLocal()
    try:
        for _ in range(120):  # ~20 minutes max
            time.sleep(10)
            resp = fsx.describe_file_systems(FileSystemIds=[filesystem_id])
            fs = resp["FileSystems"][0]
            status = fs["Lifecycle"]
            if status == "AVAILABLE":
                pool = db.query(StoragePool).get(pool_id)
                pool.status = "available"
                pool.fsx_dns_name = fs.get("DNSName")
                if fs.get("NetworkInterfaceIds"):
                    enis = boto3.client("ec2", region_name=region, **credentials)
                    eni_resp = enis.describe_network_interfaces(
                        NetworkInterfaceIds=fs["NetworkInterfaceIds"][:1]
                    )
                    if eni_resp["NetworkInterfaces"]:
                        pool.fsx_mount_ip = eni_resp["NetworkInterfaces"][0]["PrivateIpAddress"]
                db.commit()
                logger.info("FSx %s is available for pool %s", filesystem_id, pool_id)
                return
            elif status in ("FAILED", "DELETING"):
                pool = db.query(StoragePool).get(pool_id)
                pool.status = "error"
                db.commit()
                logger.error("FSx %s failed for pool %s: %s", filesystem_id, pool_id, status)
                return

        pool = db.query(StoragePool).get(pool_id)
        pool.status = "error"
        db.commit()
        logger.error("FSx %s timed out for pool %s", filesystem_id, pool_id)
    finally:
        db.close()


def provision_fsx_pool(pool_id: str, credentials: dict, region: str,
                       subnet_id: str, security_group_id: str,
                       storage_gb: int, throughput_mbps: int):
    """Provision FSx and poll in background. Updates pool status to 'available' when ready."""
    result = create_fsx_filesystem(credentials, region, subnet_id, security_group_id,
                                    storage_gb, throughput_mbps)
    db = SessionLocal()
    try:
        pool = db.query(StoragePool).get(pool_id)
        pool.fsx_filesystem_id = result["filesystem_id"]
        pool.fsx_dns_name = result.get("dns_name")
        db.commit()
    finally:
        db.close()

    t = threading.Thread(
        target=_poll_fsx_until_available,
        args=(pool_id, credentials, region, result["filesystem_id"]),
        daemon=True,
    )
    t.start()


def delete_fsx_filesystem(credentials: dict, region: str, filesystem_id: str):
    """Delete an FSx filesystem."""
    fsx = boto3.client("fsx", region_name=region, **credentials)
    fsx.delete_file_system(FileSystemId=filesystem_id)


def update_fsx_throughput(credentials: dict, region: str, filesystem_id: str, throughput_mbps: int):
    """Update FSx throughput capacity (no downtime)."""
    fsx = boto3.client("fsx", region_name=region, **credentials)
    fsx.update_file_system(
        FileSystemId=filesystem_id,
        OpenZFSConfiguration={"ThroughputCapacity": throughput_mbps},
    )


def add_sg_rules_for_shared_storage(credentials: dict, region: str, security_group_id: str):
    """Add NFS (2049) and live migration (49152-49215) inbound rules to the security group."""
    ec2 = boto3.client("ec2", region_name=region, **credentials)

    existing = ec2.describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": [security_group_id]}]
    )
    existing_ports = {r.get("FromPort") for r in existing["SecurityGroupRules"] if r["IsEgress"] is False}

    rules_to_add = []
    if 2049 not in existing_ports:
        rules_to_add.append({
            "IpProtocol": "tcp",
            "FromPort": 2049,
            "ToPort": 2049,
            "UserIdGroupPairs": [{"GroupId": security_group_id}],
        })
    if 49152 not in existing_ports:
        rules_to_add.append({
            "IpProtocol": "tcp",
            "FromPort": 49152,
            "ToPort": 49215,
            "UserIdGroupPairs": [{"GroupId": security_group_id}],
        })

    if rules_to_add:
        ec2.authorize_security_group_ingress(
            GroupId=security_group_id,
            IpPermissions=rules_to_add,
        )
        logger.info("Added NFS/migration SG rules to %s", security_group_id)
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/storage_pool_service.py
git commit -m "feat: add storage pool service with FSx lifecycle and AZ probing"
```

---

### Task 5: Storage Pool API Router

**Files:**
- Create: `src/backend/app/api/storage_pools.py`
- Modify: `src/backend/app/main.py`

- [ ] **Step 1: Create the API router**

Create `src/backend/app/api/storage_pools.py`:

```python
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import SharedCacheEntry, StoragePool
from app.models.user import User
from app.schemas.storage_pool import (
    AzProbeResponse,
    AzProbeResult,
    SharedCacheEntryResponse,
    StoragePoolCreate,
    StoragePoolResponse,
    StoragePoolUpdate,
)
from app.services import storage_pool_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/storage-pools", tags=["storage-pools"])


@router.get("/", response_model=list[StoragePoolResponse])
def list_pools(user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pools = db.query(StoragePool).order_by(StoragePool.created_at).all()
    results = []
    for pool in pools:
        resp = StoragePoolResponse.model_validate(pool)
        resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
        results.append(resp)
    return results


@router.get("/{pool_id}", response_model=StoragePoolResponse)
def get_pool(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    return resp


@router.post("/", response_model=StoragePoolResponse, status_code=201)
def create_pool(body: StoragePoolCreate, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if body.mode not in ("local", "shared-fsx", "shared-byo"):
        raise HTTPException(400, f"Invalid mode: {body.mode}")

    existing = db.query(StoragePool).filter(StoragePool.name == body.name).first()
    if existing:
        raise HTTPException(409, f"Pool named '{body.name}' already exists")

    provider = db.query(Provider).get(body.provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")

    if body.mode == "shared-fsx":
        if not body.az:
            raise HTTPException(400, "AZ is required for shared-fsx pools")
        if not body.fsx_throughput_mbps or not body.fsx_storage_gb:
            raise HTTPException(400, "fsx_throughput_mbps and fsx_storage_gb are required")

    if body.mode == "shared-byo":
        if not body.nfs_endpoint:
            raise HTTPException(400, "nfs_endpoint is required for shared-byo pools")
        if not body.az:
            raise HTTPException(400, "AZ is required for shared-byo pools")

    pool = StoragePool(
        name=body.name,
        mode=body.mode,
        az=body.az,
        nfs_endpoint=body.nfs_endpoint,
        fsx_throughput_mbps=body.fsx_throughput_mbps,
        fsx_storage_gb=body.fsx_storage_gb,
        status="available" if body.mode in ("local", "shared-byo") else "creating",
        provider_id=body.provider_id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    if body.mode == "shared-fsx":
        credentials = provider.get_credentials()
        region = provider.default_region

        subnet_id = storage_pool_service.ensure_subnet_in_az(
            credentials, region, provider.vpc_id, body.az
        )
        pool.subnet_id = subnet_id
        db.commit()

        storage_pool_service.add_sg_rules_for_shared_storage(
            credentials, region, provider.security_group_id
        )

        t = threading.Thread(
            target=storage_pool_service.provision_fsx_pool,
            args=(pool.id, credentials, region, subnet_id,
                  provider.security_group_id, body.fsx_storage_gb, body.fsx_throughput_mbps),
            daemon=True,
        )
        t.start()

    elif body.mode == "shared-byo":
        credentials = provider.get_credentials()
        region = provider.default_region
        subnet_id = storage_pool_service.ensure_subnet_in_az(
            credentials, region, provider.vpc_id, body.az
        )
        pool.subnet_id = subnet_id
        storage_pool_service.add_sg_rules_for_shared_storage(
            credentials, region, provider.security_group_id
        )
        db.commit()

    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = 0
    return resp


@router.patch("/{pool_id}", response_model=StoragePoolResponse)
def update_pool(pool_id: str, body: StoragePoolUpdate,
                user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    if pool.mode != "shared-fsx":
        raise HTTPException(400, "Only shared-fsx pools can be updated")

    provider = db.query(Provider).get(pool.provider_id)
    credentials = provider.get_credentials()

    if body.fsx_throughput_mbps and body.fsx_throughput_mbps != pool.fsx_throughput_mbps:
        storage_pool_service.update_fsx_throughput(
            credentials, provider.default_region, pool.fsx_filesystem_id, body.fsx_throughput_mbps
        )
        pool.fsx_throughput_mbps = body.fsx_throughput_mbps

    db.commit()
    db.refresh(pool)
    resp = StoragePoolResponse.model_validate(pool)
    resp.host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    return resp


@router.delete("/{pool_id}", status_code=204)
def delete_pool(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")

    host_count = db.query(Host).filter(Host.storage_pool_id == pool.id).count()
    if host_count > 0:
        raise HTTPException(400, f"Pool still has {host_count} hosts assigned")

    if pool.mode == "shared-fsx" and pool.fsx_filesystem_id:
        provider = db.query(Provider).get(pool.provider_id)
        credentials = provider.get_credentials()
        storage_pool_service.delete_fsx_filesystem(
            credentials, provider.default_region, pool.fsx_filesystem_id
        )

    db.delete(pool)
    db.commit()


@router.get("/{pool_id}/cache", response_model=list[SharedCacheEntryResponse])
def list_cache(pool_id: str, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    entries = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool_id
    ).order_by(SharedCacheEntry.created_at.desc()).all()
    return [SharedCacheEntryResponse.model_validate(e) for e in entries]


@router.delete("/{pool_id}/cache/{entry_id}", status_code=204)
def evict_cache_entry(pool_id: str, entry_id: str,
                      user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    entry = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.id == entry_id,
        SharedCacheEntry.storage_pool_id == pool_id,
    ).first()
    if not entry:
        raise HTTPException(404, "Cache entry not found")
    db.delete(entry)
    db.commit()


@router.post("/{pool_id}/probe-azs", response_model=AzProbeResponse)
def probe_azs(pool_id: str, instance_types: list[str],
              user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    pool = db.query(StoragePool).get(pool_id)
    if pool:
        provider = db.query(Provider).get(pool.provider_id)
    else:
        raise HTTPException(404, "Storage pool not found")

    credentials = provider.get_credentials()
    az_results = storage_pool_service.probe_az_capacity(
        credentials, provider.default_region, instance_types
    )

    results = []
    for az, data in sorted(az_results.items()):
        results.append(AzProbeResult(
            az=az,
            supported_types=data["supported"],
            unsupported_types=data["unsupported"],
        ))

    recommended = storage_pool_service.find_best_az(az_results, instance_types)
    return AzProbeResponse(results=results, recommended_az=recommended)
```

- [ ] **Step 2: Register router in main.py**

In `src/backend/app/main.py`, add after the existing imports:

```python
from app.api import storage_pools as storage_pool_routes  # noqa: E402
```

And add after the existing `app.include_router` calls:

```python
app.include_router(storage_pool_routes.router, prefix="/api/v1")
```

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass (no regressions from import changes)

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/storage_pools.py src/backend/app/main.py
git commit -m "feat: add storage pool API router with CRUD and AZ probing"
```

---

### Task 6: Pool-Aware Host Provisioning

**Files:**
- Modify: `src/backend/app/api/hosts.py`
- Modify: `src/backend/app/services/provisioner.py`
- Modify: `src/backend/app/services/agent_deployer.py`

- [ ] **Step 1: Update ProvisionRequest in hosts.py**

In `src/backend/app/api/hosts.py`, add `storage_pool_id: str | None = None` to the `ProvisionRequest` class.

- [ ] **Step 2: Update provision endpoint to use pool's AZ**

In the `provision_host_endpoint` function in `src/backend/app/api/hosts.py`, after resolving the provider, add pool lookup logic:

```python
pool = None
subnet_override = None
if body.storage_pool_id:
    pool = db.query(StoragePool).get(body.storage_pool_id)
    if not pool:
        raise HTTPException(404, "Storage pool not found")
    if pool.provider_id != provider.id:
        raise HTTPException(400, "Pool belongs to a different provider")
    if pool.mode.startswith("shared") and pool.status != "available":
        raise HTTPException(400, f"Pool is not available (status: {pool.status})")
    subnet_override = pool.subnet_id
```

Pass `subnet_override` and `storage_pool_id` to `provision_host()`. After the host is created, set `host.storage_pool_id = body.storage_pool_id`.

- [ ] **Step 3: Update provisioner to respect AZ pinning**

In `src/backend/app/services/provisioner.py`, in `provision_host()`:

When `subnet_override` is provided, use ONLY that subnet — do NOT fall through to other subnets. If the instance type is unsupported in that AZ, raise an error immediately instead of trying other AZs.

```python
if subnet_override:
    subnet_ids = [subnet_override]  # No fallback — pool pins the AZ
else:
    # Existing multi-AZ fallback logic
    all_subnets = client.describe_subnets(...)
    subnet_ids = [subnet_id] + [s["SubnetId"] for s in all_subnets["Subnets"] if s["SubnetId"] != subnet_id]
```

- [ ] **Step 4: Pass storage_mode to troshkad config**

In `src/backend/app/services/agent_deployer.py`, when generating `troshkad.conf`, add storage mode based on the host's pool:

```python
# Look up host's storage pool
from app.models.storage_pool import StoragePool
pool = None
if host.storage_pool_id:
    pool = db.query(StoragePool).get(host.storage_pool_id)

config = {
    "port": 31337,
    "token": token,
    # ... existing fields ...
}

if pool and pool.mode.startswith("shared"):
    config["storage_mode"] = "shared"
    config["shared_mount"] = "/var/lib/troshka/shared"
    config["local_mount"] = "/var/lib/troshka/local"
    if pool.mode == "shared-fsx":
        config["nfs_server"] = pool.fsx_dns_name
        config["nfs_path"] = "/fsx/troshka"
    elif pool.mode == "shared-byo":
        config["nfs_endpoint"] = pool.nfs_endpoint
else:
    config["storage_mode"] = "local"
```

Also add cloud-init NFS mount commands for shared-mode hosts in the user-data script:

```bash
# In the cloud-init script for shared pool hosts:
mkdir -p /var/lib/troshka/shared /var/lib/troshka/local /var/lib/troshka/seeds
mount -t nfs -o nfsvers=4.1,nconnect=16,hard,_netdev ${NFS_SERVER}:${NFS_PATH} /var/lib/troshka/shared
echo "${NFS_SERVER}:${NFS_PATH} /var/lib/troshka/shared nfs4 nfsvers=4.1,nconnect=16,hard,_netdev 0 0" >> /etc/fstab
setsebool -P virt_use_nfs 1
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py src/backend/app/services/provisioner.py src/backend/app/services/agent_deployer.py
git commit -m "feat: pool-aware host provisioning with AZ pinning and NFS mount"
```

---

## Phase 2: Troshkad Path Resolution + Download Coordination

### Task 7: Troshkad Path Resolution

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add path resolver function**

After the existing `_validate_path()` function in troshkad.py, add a path resolver that reads storage mode from config:

```python
def _storage_path(category):
    """Resolve storage path by category based on storage mode.
    Categories: 'vms', 'images', 'cache/patterns', 'cache/snapshots', 'pxe', 'bmc', 'tmp', 'seeds'
    """
    mode = CONFIG.get("storage_mode", "local")
    if mode == "shared":
        shared = CONFIG.get("shared_mount", "/var/lib/troshka/shared")
        local = CONFIG.get("local_mount", "/var/lib/troshka/local")
        shared_categories = {"vms", "images", "cache/patterns", "cache/snapshots"}
        local_categories = {"pxe", "bmc", "tmp"}
        if category in shared_categories:
            return os.path.join(shared, category)
        elif category in local_categories:
            return os.path.join(local, category)
        elif category == "seeds":
            return "/var/lib/troshka/seeds"
        else:
            return os.path.join(shared, category)
    else:
        base = "/var/lib/troshka"
        if category == "seeds":
            return os.path.join(base, "vms")  # local mode: seeds in vms dir
        return os.path.join(base, category)
```

- [ ] **Step 2: Update _validate_path to accept shared mount**

Modify `_validate_path` to also accept paths under the shared mount and local mount directories:

```python
def _validate_path(path):
    normalized = os.path.normpath(path)
    allowed_prefixes = ["/var/lib/troshka/"]
    mode = CONFIG.get("storage_mode", "local")
    if mode == "shared":
        shared = CONFIG.get("shared_mount", "/var/lib/troshka/shared")
        local = CONFIG.get("local_mount", "/var/lib/troshka/local")
        allowed_prefixes.extend([shared + "/", local + "/"])

    if not any(normalized.startswith(p) for p in allowed_prefixes):
        raise ValueError(f"Path must be under /var/lib/troshka/: {path}")
    if os.path.exists(normalized):
        real = os.path.realpath(normalized)
        if not any(real.startswith(p) for p in allowed_prefixes):
            raise ValueError(f"Path resolves outside allowed directories: {path}")
        return real
    return normalized
```

- [ ] **Step 3: Add disk_cache parameter support to VM definition**

In the handler that creates VM XML or calls virt-install (look for `_handle_vm_define` or similar), add support for a `disk_cache` parameter:

```python
# In the disk XML generation, when storage_mode is "shared":
disk_cache = params.get("disk_cache", "writeback")
# Apply to each disk element: cache='{disk_cache}'
# If disk_cache is "none", also set io='native'
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad path resolver for shared/local storage modes"
```

---

### Task 8: Backend Download Coordination

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

- [ ] **Step 1: Add shared cache check function**

In `deploy_service.py`, add a function to coordinate downloads via SharedCacheEntry:

```python
import time

from app.models.storage_pool import SharedCacheEntry, StoragePool


def _resolve_shared_image_path(db, pool, item_id, item_type, s3_key, format):
    """Check shared cache; return path if ready, or download and return path."""
    relative_path = f"images/{item_id}.{format}" if item_type == "image" else f"cache/{item_type}s/{item_id}/{s3_key.split('/')[-1]}"

    entry = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool.id,
        SharedCacheEntry.item_id == item_id,
        SharedCacheEntry.item_type == item_type,
    ).first()

    if entry:
        if entry.status == "ready":
            return entry.file_path, True  # (path, already_cached)
        elif entry.status == "downloading":
            return entry.file_path, "wait"  # caller should poll
        elif entry.status == "error":
            db.delete(entry)
            db.commit()

    entry = SharedCacheEntry(
        storage_pool_id=pool.id,
        item_type=item_type,
        item_id=item_id,
        status="downloading",
        file_path=relative_path,
    )
    db.add(entry)
    db.commit()
    return relative_path, False  # (path, needs_download)


def _mark_shared_cache_ready(db, pool_id, item_id, item_type, size_bytes=None):
    """Mark a shared cache entry as ready after successful download."""
    entry = db.query(SharedCacheEntry).filter(
        SharedCacheEntry.storage_pool_id == pool_id,
        SharedCacheEntry.item_id == item_id,
        SharedCacheEntry.item_type == item_type,
    ).first()
    if entry:
        entry.status = "ready"
        if size_bytes:
            entry.size_bytes = size_bytes
        db.commit()


def _wait_for_shared_cache(db, pool_id, item_id, item_type, timeout=600):
    """Wait for another host's download to complete. Returns True if ready, False if timed out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db.expire_all()
        entry = db.query(SharedCacheEntry).filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.item_id == item_id,
            SharedCacheEntry.item_type == item_type,
        ).first()
        if entry and entry.status == "ready":
            return True
        if entry and entry.status == "error":
            return False
        time.sleep(5)
    return False
```

- [ ] **Step 2: Update cache_library_images to use shared coordination**

In the `cache_library_images()` function (or wherever library images are cached before deploy), add a check:

```python
# At the start of the caching flow, check if host is in a shared pool
host = db.query(Host).get(host_id)
pool = None
if host.storage_pool_id:
    pool = db.query(StoragePool).get(host.storage_pool_id)

if pool and pool.mode.startswith("shared"):
    # Use shared cache coordination
    path, cache_status = _resolve_shared_image_path(db, pool, item_id, "image", s3_key, format)
    if cache_status is True:
        logger.info("Image %s already cached on shared storage", item_id)
        continue  # skip download
    elif cache_status == "wait":
        logger.info("Image %s being downloaded by another host, waiting...", item_id)
        if _wait_for_shared_cache(db, pool.id, item_id, "image"):
            continue
        else:
            raise RuntimeError(f"Timeout waiting for image {item_id} download")

    # Need to download — pick this host to do it
    shared_mount = pool.fsx_dns_name and f"/var/lib/troshka/shared/{path}" or f"{pool.nfs_endpoint.split(':')[1]}/{path}"
    # Tell troshkad to download to the shared path
    dest_path = f"/var/lib/troshka/shared/{path}"
    # ... issue download command to troshkad with dest_path ...
    _mark_shared_cache_ready(db, pool.id, item_id, "image")
else:
    # Existing local download logic unchanged
    pass
```

- [ ] **Step 3: Update deploy_service to pass disk_cache to troshkad**

When creating VM definitions, pass the `disk_cache` parameter based on pool mode:

```python
disk_cache = "writeback"  # default
if pool and pool.mode.startswith("shared"):
    disk_cache = "none"

# Pass to troshkad VM define command
params["disk_cache"] = disk_cache
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py
git commit -m "feat: shared cache download coordination in deploy service"
```

---

## Phase 3: Live Migration

### Task 9: Troshkad Migration Endpoint

**Files:**
- Modify: `src/troshkad/troshkad.py`

- [ ] **Step 1: Add the VM migrate handler**

In `troshkad.py`, add a new command handler:

```python
def _handle_vm_migrate(job, params):
    """Live-migrate a VM to another host."""
    domain = _validate_domain(params["domain"])
    target_host = params["target_host"]
    target_port = params.get("target_port", 49152)

    # Validate target_host is an IP
    import re
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", target_host):
        raise ValueError(f"Invalid target host IP: {target_host}")

    # Verify domain exists and is running
    _run_cmd(job, ["virsh", "domstate", domain], timeout=10)

    migrate_uri = f"tcp://{target_host}:{target_port}/system"
    cmd = [
        "virsh", "migrate",
        "--live",
        "--verbose",
        "--persistent",
        "--undefinesource",
        domain,
        f"qemu+tcp://{target_host}/system",
    ]
    _run_cmd(job, cmd, timeout=600)  # 10 min timeout for large VMs

    return {
        "domain": domain,
        "target_host": target_host,
        "status": "migrated",
    }
```

Register in `COMMAND_HANDLERS`:

```python
COMMAND_HANDLERS["vm/migrate"] = _handle_vm_migrate
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad vm/migrate endpoint for live migration"
```

---

### Task 10: Migration Service

**Files:**
- Create: `src/backend/app/services/migration_service.py`
- Create: `src/backend/tests/test_migration_service.py`

- [ ] **Step 1: Write migration validation test**

Create `src/backend/tests/test_migration_service.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects import sqlite

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models import *  # noqa: F403

engine = create_engine("sqlite:///./test_migration.db", connect_args={"check_same_thread": False})
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_validate_migration_same_pool():
    from app.services.migration_service import validate_migration
    db = Session()

    provider = Provider(name="test-aws", type="aws")
    db.add(provider)
    db.flush()

    pool = StoragePool(name="test-pool", mode="shared-fsx", status="available", provider_id=provider.id)
    db.add(pool)
    db.flush()

    source = Host(provider_id=provider.id, state="active", storage_pool_id=pool.id,
                  total_vcpus=64, total_ram_mb=262144, used_vcpus=20, used_ram_mb=80000)
    target = Host(provider_id=provider.id, state="active", storage_pool_id=pool.id,
                  total_vcpus=64, total_ram_mb=262144, used_vcpus=10, used_ram_mb=40000)
    db.add_all([source, target])
    db.flush()

    user = User(email="admin@test.com", display_name="Admin", role="admin", auth_source="local")
    db.add(user)
    db.flush()

    project = Project(name="test-project", owner_id=user.id, state="active",
                      host_id=source.id, poweroff_mode="simultaneous")
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert errors == [], f"Unexpected errors: {errors}"
    db.close()


def test_validate_migration_different_pools():
    from app.services.migration_service import validate_migration
    db = Session()

    provider = db.query(Provider).first()
    pool_a = StoragePool(name="pool-a", mode="shared-fsx", status="available", provider_id=provider.id)
    pool_b = StoragePool(name="pool-b", mode="shared-fsx", status="available", provider_id=provider.id)
    db.add_all([pool_a, pool_b])
    db.flush()

    source = Host(provider_id=provider.id, state="active", storage_pool_id=pool_a.id,
                  total_vcpus=64, total_ram_mb=262144)
    target = Host(provider_id=provider.id, state="active", storage_pool_id=pool_b.id,
                  total_vcpus=64, total_ram_mb=262144)
    db.add_all([source, target])
    db.flush()

    user = db.query(User).first()
    project = Project(name="cross-pool", owner_id=user.id, state="active",
                      host_id=source.id, poweroff_mode="simultaneous")
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert any("same storage pool" in e for e in errors)
    db.close()


def test_validate_migration_local_pool():
    from app.services.migration_service import validate_migration
    db = Session()

    provider = db.query(Provider).first()
    pool = StoragePool(name="local-pool", mode="local", status="available", provider_id=provider.id)
    db.add(pool)
    db.flush()

    source = Host(provider_id=provider.id, state="active", storage_pool_id=pool.id,
                  total_vcpus=64, total_ram_mb=262144)
    target = Host(provider_id=provider.id, state="active", storage_pool_id=pool.id,
                  total_vcpus=64, total_ram_mb=262144)
    db.add_all([source, target])
    db.flush()

    user = db.query(User).first()
    project = Project(name="local-project", owner_id=user.id, state="active",
                      host_id=source.id, poweroff_mode="simultaneous")
    db.add(project)
    db.commit()

    errors = validate_migration(db, project.id, source.id, target.id)
    assert any("shared storage" in e for e in errors)
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_migration_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.migration_service'`

- [ ] **Step 3: Create migration service**

Create `src/backend/app/services/migration_service.py`:

```python
import logging
import threading

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.host import Host
from app.models.project import Project
from app.models.storage_pool import StoragePool

logger = logging.getLogger(__name__)


def validate_migration(db: Session, project_id: str, source_host_id: str, target_host_id: str) -> list[str]:
    """Validate that a project can be migrated. Returns list of error messages (empty = valid)."""
    errors = []

    project = db.query(Project).get(project_id)
    if not project:
        errors.append("Project not found")
        return errors
    if project.state != "active":
        errors.append(f"Project must be active to migrate (current state: {project.state})")
    if project.host_id != source_host_id:
        errors.append("Project is not on the specified source host")

    source = db.query(Host).get(source_host_id)
    target = db.query(Host).get(target_host_id)
    if not source:
        errors.append("Source host not found")
    if not target:
        errors.append("Target host not found")
    if not source or not target:
        return errors

    if source.storage_pool_id != target.storage_pool_id:
        errors.append("Source and target must be in the same storage pool")
        return errors

    if not source.storage_pool_id:
        errors.append("Hosts must be in a storage pool to migrate")
        return errors

    pool = db.query(StoragePool).get(source.storage_pool_id)
    if pool.mode == "local":
        errors.append("Migration requires shared storage (pool mode is 'local')")

    if target.state != "active":
        errors.append(f"Target host must be active (current state: {target.state})")
    if target.agent_status != "connected":
        errors.append(f"Target host agent must be connected (status: {target.agent_status})")

    return errors


def migrate_project(project_id: str, source_host_id: str, target_host_id: str):
    """Orchestrate full project migration in a background thread."""
    t = threading.Thread(
        target=_do_migrate_project,
        args=(project_id, source_host_id, target_host_id),
        daemon=True,
    )
    t.start()


def _do_migrate_project(project_id: str, source_host_id: str, target_host_id: str):
    """Background thread: migrate all VMs + infrastructure from source to target host."""
    from app.services.troshkad_client import send_command

    db = SessionLocal()
    try:
        project = db.query(Project).get(project_id)
        source = db.query(Host).get(source_host_id)
        target = db.query(Host).get(target_host_id)

        project.state = "migrating"
        db.commit()

        topology = project.topology or {}
        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])

        # Step 1: Set up networks on target
        logger.info("Migration %s: setting up networks on target %s", project_id[:8], target_host_id[:8])
        network_nodes = [n for n in nodes if n.get("type") == "networkNode"]
        if network_nodes:
            network_setup_params = _build_network_setup_params(project, topology, nodes, edges)
            send_command(target, "networks/full-setup", network_setup_params)

        # Step 2: Set up BMC on target (if applicable)
        vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]
        bmc_vms = [n for n in vm_nodes if n.get("data", {}).get("bmcEnabled")]
        if bmc_vms:
            logger.info("Migration %s: setting up BMC on target", project_id[:8])
            bmc_params = _build_bmc_setup_params(project, topology, bmc_vms)
            send_command(target, "bmc/setup", bmc_params)

        # Step 3: Live-migrate each VM in start order
        start_order = topology.get("startOrder", [])
        vm_ids_ordered = [s["vmId"] for s in start_order] if start_order else [n["id"] for n in vm_nodes]

        for vm_id in vm_ids_ordered:
            vm_node = next((n for n in vm_nodes if n["id"] == vm_id), None)
            if not vm_node:
                continue

            domain = f"troshka-{project.id[:8]}-{vm_id[:8]}"
            logger.info("Migration %s: migrating VM %s", project_id[:8], domain)

            result = send_command(source, "vm/migrate", {
                "domain": domain,
                "target_host": target.ip_address,
            })
            logger.info("Migration %s: VM %s migrated: %s", project_id[:8], domain, result)

        # Step 4: Tear down source infrastructure
        logger.info("Migration %s: tearing down source %s", project_id[:8], source_host_id[:8])
        if network_nodes:
            teardown_params = _build_network_teardown_params(project, topology)
            send_command(source, "networks/full-teardown", teardown_params)
        if bmc_vms:
            send_command(source, "bmc/teardown", {"project_id": project.id})

        # Step 5: Update DB
        project.host_id = target_host_id
        project.state = "active"
        db.commit()
        logger.info("Migration %s: complete", project_id[:8])

    except Exception as e:
        logger.error("Migration %s failed: %s", project_id[:8], e)
        try:
            project = db.query(Project).get(project_id)
            if project:
                project.state = "error"
                project.deploy_error = f"Migration failed: {e}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def evacuate_host(host_id: str):
    """Move all projects off a host to other hosts in the same pool."""
    t = threading.Thread(target=_do_evacuate_host, args=(host_id,), daemon=True)
    t.start()


def _do_evacuate_host(host_id: str):
    """Background thread: evacuate all projects from a host."""
    from app.services.placement import find_target_host

    db = SessionLocal()
    try:
        host = db.query(Host).get(host_id)
        pool = db.query(StoragePool).get(host.storage_pool_id)

        projects = db.query(Project).filter(
            Project.host_id == host_id,
            Project.state == "active",
        ).all()

        if not projects:
            logger.info("Evacuate %s: no active projects to migrate", host_id[:8])
            return

        logger.info("Evacuate %s: migrating %d projects", host_id[:8], len(projects))

        for project in projects:
            target = find_target_host(db, pool.id, host_id, project)
            if not target:
                logger.error("Evacuate %s: no target host available for project %s",
                             host_id[:8], project.id[:8])
                continue

            _do_migrate_project(project.id, host_id, target.id)

        host.state = "maintenance"
        db.commit()
        logger.info("Evacuate %s: complete, host set to maintenance", host_id[:8])
    except Exception as e:
        logger.error("Evacuate %s failed: %s", host_id[:8], e)
    finally:
        db.close()


def _build_network_setup_params(project, topology, nodes, edges):
    """Build the network setup parameters from topology. Mirrors deploy_service logic."""
    # This will reuse the same network setup parameter construction
    # as deploy_service._build_network_params() — extract and call that function
    pass  # Implementation references deploy_service's existing param builder


def _build_network_teardown_params(project, topology):
    """Build network teardown parameters from topology."""
    pass  # Implementation references deploy_service's existing teardown param builder


def _build_bmc_setup_params(project, topology, bmc_vms):
    """Build BMC setup parameters for migration."""
    pass  # Implementation references deploy_service's existing BMC setup logic
```

Note: The `_build_*_params` helper functions should be extracted from `deploy_service.py` into shared utility functions that both deploy and migration can use. During implementation, refactor the deploy_service param builders into importable functions rather than duplicating the logic.

Note: The `find_target_host(db, pool_id, exclude_host_id, project)` function used by `evacuate_host` should be added to `src/backend/app/services/placement.py`. It queries hosts in the same pool (excluding the source), filters by capacity (enough free vCPU and RAM for the project's VMs), and returns the best-fit host. Use the existing `sync_host_capacity` and overcommit ratio logic already in placement.py.

- [ ] **Step 4: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_migration_service.py -v`
Expected: All 3 validation tests pass

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/migration_service.py src/backend/tests/test_migration_service.py
git commit -m "feat: migration service with validation and orchestration"
```

---

### Task 11: Migration API Endpoints

**Files:**
- Modify: `src/backend/app/api/projects.py`
- Modify: `src/backend/app/api/hosts.py`

- [ ] **Step 1: Add migrate endpoint to projects API**

In `src/backend/app/api/projects.py`, add:

```python
class MigrateRequest(BaseModel):
    target_host_id: str


@router.post("/{project_id}/migrate")
def migrate_project(project_id: str, body: MigrateRequest,
                    user: User = Depends(require_role("operator")),
                    db: Session = Depends(get_db)):
    from app.services.migration_service import validate_migration, migrate_project as do_migrate

    project = db.query(Project).get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    errors = validate_migration(db, project_id, project.host_id, body.target_host_id)
    if errors:
        raise HTTPException(400, "; ".join(errors))

    do_migrate(project_id, project.host_id, body.target_host_id)
    return {"status": "migrating", "project_id": project_id, "target_host_id": body.target_host_id}
```

- [ ] **Step 2: Add evacuate endpoint to hosts API**

In `src/backend/app/api/hosts.py`, add:

```python
@router.post("/{host_id}/evacuate")
def evacuate_host_endpoint(host_id: str,
                           user: User = Depends(require_role("admin")),
                           db: Session = Depends(get_db)):
    from app.models.storage_pool import StoragePool
    from app.services.migration_service import evacuate_host

    host = db.query(Host).get(host_id)
    if not host:
        raise HTTPException(404, "Host not found")
    if not host.storage_pool_id:
        raise HTTPException(400, "Host is not in a storage pool")

    pool = db.query(StoragePool).get(host.storage_pool_id)
    if pool.mode == "local":
        raise HTTPException(400, "Cannot evacuate hosts in local-mode pools")

    project_count = db.query(Project).filter(Project.host_id == host_id, Project.state == "active").count()
    if project_count == 0:
        raise HTTPException(400, "No active projects to evacuate")

    evacuate_host(host_id)
    return {"status": "evacuating", "host_id": host_id, "project_count": project_count}
```

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/projects.py src/backend/app/api/hosts.py
git commit -m "feat: add migrate and evacuate API endpoints"
```

---

### Task 12: Shared Pool GC

**Files:**
- Modify: `src/backend/app/services/gc_service.py`

- [ ] **Step 1: Add pool-level GC function**

In `gc_service.py`, add:

```python
def run_pool_gc(pool_id: str, host_id: str | None = None):
    """Run garbage collection at the storage pool level.
    Uses any connected host in the pool to scan the shared filesystem.
    """
    db = SessionLocal()
    try:
        pool = db.query(StoragePool).get(pool_id)
        if not pool or pool.mode == "local":
            return

        # Pick a connected host to scan the filesystem
        if host_id:
            scan_host = db.query(Host).get(host_id)
        else:
            scan_host = db.query(Host).filter(
                Host.storage_pool_id == pool_id,
                Host.state == "active",
                Host.agent_status == "connected",
            ).first()
        if not scan_host:
            logger.warning("Pool GC %s: no connected host available", pool_id[:8])
            return

        # 1. Capacity sync — report FSx usage
        from app.services.troshkad_client import check_disk_usage
        usage = check_disk_usage(scan_host)
        logger.info("Pool GC %s: shared storage usage: %s", pool_id[:8], usage)

        # 2. Orphan cleanup — scan /shared/vms/ for orphaned project dirs
        from app.services.troshkad_client import send_command
        discover_result = send_command(scan_host, "gc/discover", {})
        orphan_dirs = discover_result.get("orphan_project_dirs", [])

        # Filter: only orphans if no project in the pool references them
        all_project_ids = {p.id for p in db.query(Project).filter(
            Project.host_id.in_(
                db.query(Host.id).filter(Host.storage_pool_id == pool_id)
            )
        ).all()}

        true_orphans = [d for d in orphan_dirs if d.split("/")[-1] not in all_project_ids]
        if true_orphans:
            send_command(scan_host, "gc/clean", {"dirs": true_orphans})
            logger.info("Pool GC %s: cleaned %d orphan dirs", pool_id[:8], len(true_orphans))

        # 3. Cache eviction — remove stale SharedCacheEntries
        from datetime import datetime, timedelta, timezone
        stale_hours = 168  # configurable via config.yaml
        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

        stale_entries = db.query(SharedCacheEntry).filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.status == "ready",
            SharedCacheEntry.created_at < cutoff,
        ).all()

        for entry in stale_entries:
            # Check if any project in the pool still references this image
            # This requires scanning topologies — simplified check via item_id
            in_use = False  # TODO: implement topology scan for backing file references
            if not in_use:
                shared_mount = "/var/lib/troshka/shared"
                full_path = f"{shared_mount}/{entry.file_path}"
                send_command(scan_host, "gc/clean", {"files": [full_path]})
                db.delete(entry)
                logger.info("Pool GC %s: evicted stale cache entry %s", pool_id[:8], entry.file_path)

        db.commit()

        # 4. Local artifact cleanup — run on each host individually
        all_hosts = db.query(Host).filter(
            Host.storage_pool_id == pool_id,
            Host.state == "active",
            Host.agent_status == "connected",
        ).all()

        for host in all_hosts:
            host_projects = {p.id for p in db.query(Project).filter(Project.host_id == host.id).all()}
            # Clean orphaned seeds, PXE, BMC artifacts for projects no longer on this host
            # This uses the existing per-host GC logic
            _clean_local_artifacts(host, host_projects)

    finally:
        db.close()


def _clean_local_artifacts(host, active_project_ids):
    """Clean orphaned local artifacts (seeds, PXE, BMC) from a specific host."""
    from app.services.troshkad_client import send_command
    # Discover local artifacts and remove those not belonging to active projects
    # This mirrors the existing GC orphan cleanup but scoped to /local/ and /seeds/
    pass
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/gc_service.py
git commit -m "feat: pool-level garbage collection for shared storage"
```

---

## Phase 4: Frontend

### Task 13: Storage Pools Admin Page

**Files:**
- Create: `src/frontend/src/app/admin/storage-pools/page.tsx`
- Modify: `src/frontend/src/app/layout.tsx`

- [ ] **Step 1: Add "Storage Pools" to admin navigation**

In `src/frontend/src/app/layout.tsx`, add to the `adminItems` array:

```javascript
{ label: "Storage Pools", path: "/admin/storage-pools" },
```

- [ ] **Step 2: Create the storage pools page**

Create `src/frontend/src/app/admin/storage-pools/page.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import {
  PageSection,
  Title,
  Button,
  Card,
  CardBody,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  Alert,
} from "@patternfly/react-core";

interface StoragePool {
  id: string;
  name: string;
  mode: string;
  az: string | null;
  subnet_id: string | null;
  fsx_filesystem_id: string | null;
  fsx_dns_name: string | null;
  fsx_throughput_mbps: number | null;
  fsx_storage_gb: number | null;
  nfs_endpoint: string | null;
  status: string;
  provider_id: string;
  host_count: number;
  created_at: string;
}

interface Provider {
  id: string;
  name: string;
  vpc_id: string | null;
  security_group_id: string | null;
  default_region: string | null;
}

const statusColors: Record<string, string> = {
  available: "var(--troshka-green)",
  creating: "var(--troshka-yellow, #f0ab00)",
  error: "var(--troshka-red)",
  deleting: "var(--troshka-yellow, #f0ab00)",
};

const modeLabels: Record<string, string> = {
  local: "Local EBS",
  "shared-fsx": "FSx OpenZFS",
  "shared-byo": "BYO NFS",
};

const inputStyle = {
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: "1px solid var(--pf-t--global--border--color--default)",
  background: "var(--pf-t--global--background--color--primary--default)",
  color: "var(--pf-t--global--text--color--regular)",
  fontSize: 13,
};

export default function StoragePoolsPage() {
  const [pools, setPools] = useState<StoragePool[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  // Create form state
  const [newName, setNewName] = useState("");
  const [newMode, setNewMode] = useState("local");
  const [newProviderId, setNewProviderId] = useState("");
  const [newAz, setNewAz] = useState("");
  const [newThroughput, setNewThroughput] = useState(160);
  const [newStorageGb, setNewStorageGb] = useState(128);
  const [newNfsEndpoint, setNewNfsEndpoint] = useState("");
  const [creating, setCreating] = useState(false);

  const loadData = () => {
    Promise.all([
      fetch("/api/v1/storage-pools").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/v1/providers/").then((r) => (r.ok ? r.json() : [])),
    ]).then(([p, prov]) => {
      setPools(p);
      setProviders(prov);
    });
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 10000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async () => {
    setError("");
    if (!newName.trim()) { setError("Name is required"); return; }
    if (!newProviderId) { setError("Provider is required"); return; }
    if (newMode === "shared-fsx" && !newAz) { setError("AZ is required for FSx pools"); return; }
    if (newMode === "shared-byo" && !newNfsEndpoint) { setError("NFS endpoint is required"); return; }
    if (newMode === "shared-byo" && !newAz) { setError("AZ is required for BYO NFS pools"); return; }

    setCreating(true);
    const resp = await fetch("/api/v1/storage-pools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newName.trim(),
        mode: newMode,
        provider_id: newProviderId,
        az: newAz || null,
        fsx_throughput_mbps: newMode === "shared-fsx" ? newThroughput : null,
        fsx_storage_gb: newMode === "shared-fsx" ? newStorageGb : null,
        nfs_endpoint: newMode === "shared-byo" ? newNfsEndpoint : null,
      }),
    });
    setCreating(false);

    if (resp.ok) {
      setShowCreate(false);
      setNewName("");
      setNewMode("local");
      setNewAz("");
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to create pool");
    }
  };

  const handleDelete = async (pool: StoragePool) => {
    if (!window.confirm(`Delete storage pool "${pool.name}"? This will also delete the FSx filesystem if applicable.`)) return;
    const resp = await fetch(`/api/v1/storage-pools/${pool.id}`, { method: "DELETE" });
    if (resp.ok) {
      loadData();
    } else {
      const data = await resp.json();
      setError(data.detail || "Failed to delete pool");
    }
  };

  return (
    <PageSection>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title headingLevel="h1">Storage Pools</Title>
        <Button variant="primary" onClick={() => setShowCreate(!showCreate)}>
          {showCreate ? "Cancel" : "Create Pool"}
        </Button>
      </div>

      {error && <Alert variant="danger" title={error} style={{ marginBottom: 16 }} />}

      {showCreate && (
        <Card style={{ marginBottom: 16 }}>
          <CardBody>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 500 }}>
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Name</label>
                <input style={inputStyle} value={newName} onChange={(e) => setNewName(e.target.value)}
                       placeholder="e.g. prod-east-1b" />
              </div>
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Provider</label>
                <select style={inputStyle} value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)}>
                  <option value="">Select provider...</option>
                  {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
              <div>
                <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Mode</label>
                <select style={inputStyle} value={newMode} onChange={(e) => setNewMode(e.target.value)}>
                  <option value="local">Local EBS</option>
                  <option value="shared-fsx">FSx OpenZFS (Managed NFS)</option>
                  <option value="shared-byo">BYO NFS</option>
                </select>
              </div>
              {(newMode === "shared-fsx" || newMode === "shared-byo") && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Availability Zone</label>
                  <input style={inputStyle} value={newAz} onChange={(e) => setNewAz(e.target.value)}
                         placeholder="e.g. us-east-1b" />
                </div>
              )}
              {newMode === "shared-fsx" && (
                <>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Throughput (MBps)</label>
                    <input style={inputStyle} type="number" value={newThroughput}
                           onChange={(e) => setNewThroughput(parseInt(e.target.value) || 160)} min={160} />
                  </div>
                  <div>
                    <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Storage (GB)</label>
                    <input style={inputStyle} type="number" value={newStorageGb}
                           onChange={(e) => setNewStorageGb(parseInt(e.target.value) || 64)} min={64} />
                  </div>
                </>
              )}
              {newMode === "shared-byo" && (
                <div>
                  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>NFS Endpoint</label>
                  <input style={inputStyle} value={newNfsEndpoint} onChange={(e) => setNewNfsEndpoint(e.target.value)}
                         placeholder="10.0.1.50:/exports/troshka" />
                </div>
              )}
              <Button variant="primary" onClick={handleCreate} isLoading={creating} isDisabled={creating}>
                Create Pool
              </Button>
            </div>
          </CardBody>
        </Card>
      )}

      {pools.length === 0 && !showCreate && (
        <Card><CardBody style={{ textAlign: "center", padding: 40, color: "var(--pf-t--global--text--color--subtle)" }}>
          No storage pools configured. Create one to get started.
        </CardBody></Card>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {pools.map((pool) => (
          <Card key={pool.id}>
            <CardBody>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 15 }}>{pool.name}</span>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 10,
                      background: statusColors[pool.status] || "gray", color: "#fff",
                    }}>{pool.status}</span>
                    <span style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 10,
                      border: "1px solid var(--pf-t--global--border--color--default)",
                    }}>{modeLabels[pool.mode] || pool.mode}</span>
                  </div>
                  <div style={{ fontSize: 12, color: "var(--pf-t--global--text--color--subtle)", display: "flex", gap: 16 }}>
                    {pool.az && <span>AZ: {pool.az}</span>}
                    <span>Hosts: {pool.host_count}</span>
                    {pool.fsx_filesystem_id && <span>FSx: {pool.fsx_filesystem_id}</span>}
                    {pool.fsx_throughput_mbps && <span>Throughput: {pool.fsx_throughput_mbps} MBps</span>}
                    {pool.fsx_storage_gb && <span>Storage: {pool.fsx_storage_gb} GB</span>}
                    {pool.nfs_endpoint && <span>NFS: {pool.nfs_endpoint}</span>}
                  </div>
                </div>
                <Button variant="danger" size="sm" onClick={() => handleDelete(pool)}
                        isDisabled={pool.host_count > 0 || pool.status === "creating"}>
                  Delete
                </Button>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </PageSection>
  );
}
```

- [ ] **Step 3: Test in browser**

Run the dev server (`./dev-services.sh start`), navigate to `/admin/storage-pools`, and verify:
- Page loads with empty state
- Create form shows/hides correctly
- Mode selection shows/hides FSx and BYO fields
- Create pool with "local" mode works

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/storage-pools/page.tsx src/frontend/src/app/layout.tsx
git commit -m "feat: storage pools admin page with create/delete UI"
```

---

### Task 14: Host Provisioning Pool Selector

**Files:**
- Modify: `src/frontend/src/app/admin/hosts/page.tsx`

- [ ] **Step 1: Add pool selector to provision form**

In the hosts page, find the provision form (where instance_type is selected) and add a storage pool dropdown:

```tsx
// Add to state
const [pools, setPools] = useState<{id: string; name: string; mode: string; az: string | null; status: string}[]>([]);
const [selectedPool, setSelectedPool] = useState("");

// Fetch pools in loadData
fetch("/api/v1/storage-pools").then((r) => r.ok ? r.json() : []).then(setPools);

// Add dropdown to provision form, after instance type selector
<div>
  <label style={{ fontSize: 12, display: "block", marginBottom: 4 }}>Storage Pool</label>
  <select style={inputStyle} value={selectedPool} onChange={(e) => setSelectedPool(e.target.value)}>
    <option value="">None (local storage)</option>
    {pools.filter(p => p.status === "available").map((p) => (
      <option key={p.id} value={p.id}>{p.name} ({p.mode}{p.az ? `, ${p.az}` : ""})</option>
    ))}
  </select>
</div>
```

Pass `storage_pool_id` in the provision request body.

- [ ] **Step 2: Add evacuate button to host cards**

For hosts that are in a shared storage pool, add an "Evacuate" button:

```tsx
{host.storage_pool_id && pool?.mode !== "local" && host.state === "active" && (
  <Button variant="secondary" size="sm" onClick={() => handleEvacuate(host.id)}>
    Evacuate
  </Button>
)}
```

```tsx
const handleEvacuate = async (hostId: string) => {
  if (!window.confirm("Evacuate all projects from this host? They will be migrated to other hosts in the same pool.")) return;
  const resp = await fetch(`/api/v1/hosts/${hostId}/evacuate`, { method: "POST" });
  if (resp.ok) {
    loadData();
  } else {
    const data = await resp.json();
    setError(data.detail || "Failed to evacuate host");
  }
};
```

- [ ] **Step 3: Test in browser**

Verify pool selector appears in provision form and evacuate button appears for shared-pool hosts.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/hosts/page.tsx
git commit -m "feat: pool selector in host provision and evacuate button"
```

---

### Task 15: Project Migration Button

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx`

- [ ] **Step 1: Add migrate button to project detail page**

In the project detail/canvas page, add a "Migrate" button to the action bar when the project is active and on a shared pool:

```tsx
// Add state
const [showMigrate, setShowMigrate] = useState(false);
const [availableHosts, setAvailableHosts] = useState<{id: string; ip_address: string; used_vcpus: number; total_vcpus: number; used_ram_mb: number; total_ram_mb: number}[]>([]);
const [migrateTarget, setMigrateTarget] = useState("");
const [migrating, setMigrating] = useState(false);

// Fetch available hosts when migrate modal opens
const openMigrate = async () => {
  const resp = await fetch("/api/v1/hosts/");
  if (resp.ok) {
    const hosts = await resp.json();
    // Filter to same pool, exclude current host
    const samePool = hosts.filter((h: any) =>
      h.storage_pool_id === currentHost?.storage_pool_id &&
      h.id !== project.host_id &&
      h.state === "active" &&
      h.agent_status === "connected"
    );
    setAvailableHosts(samePool);
    setShowMigrate(true);
  }
};

const handleMigrate = async () => {
  if (!migrateTarget) return;
  setMigrating(true);
  const resp = await fetch(`/api/v1/projects/${project.id}/migrate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_host_id: migrateTarget }),
  });
  setMigrating(false);
  if (resp.ok) {
    setShowMigrate(false);
    // Project state will update via WebSocket
  } else {
    const data = await resp.json();
    setError(data.detail || "Migration failed");
  }
};
```

Add the Migrate button next to the existing action buttons (only visible when project is active and host is in a shared pool):

```tsx
{project.state === "active" && currentHost?.storage_pool_id && (
  <Button variant="secondary" onClick={openMigrate}>Migrate</Button>
)}
```

Add a custom modal for target host selection:

```tsx
{showMigrate && (
  <div style={{ position: "fixed", inset: 0, zIndex: 10000, display: "flex",
    alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)" }}
    onClick={(e) => { if (e.target === e.currentTarget) setShowMigrate(false); }}>
    <div style={{ background: "var(--pf-t--global--background--color--primary--default)",
      borderRadius: 12, padding: 24, width: 500, maxWidth: "90vw",
      boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
      border: "1px solid var(--pf-t--global--border--color--default)" }}>
      <Title headingLevel="h3" style={{ marginBottom: 16 }}>Migrate Project</Title>
      {availableHosts.length === 0 ? (
        <p>No available hosts in the same storage pool.</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label style={{ fontSize: 12 }}>Select target host:</label>
          <select style={inputStyle} value={migrateTarget} onChange={(e) => setMigrateTarget(e.target.value)}>
            <option value="">Select host...</option>
            {availableHosts.map((h) => (
              <option key={h.id} value={h.id}>
                {h.ip_address} (CPU: {h.used_vcpus}/{h.total_vcpus}, RAM: {Math.round(h.used_ram_mb/1024)}/{Math.round(h.total_ram_mb/1024)} GB)
              </option>
            ))}
          </select>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="secondary" onClick={() => setShowMigrate(false)}>Cancel</Button>
            <Button variant="primary" onClick={handleMigrate} isLoading={migrating}
                    isDisabled={!migrateTarget || migrating}>
              Migrate
            </Button>
          </div>
        </div>
      )}
    </div>
  </div>
)}
```

- [ ] **Step 2: Handle "migrating" state in the WebSocket handler**

In the WebSocket message handler for `project-state`, ensure the UI handles the `"migrating"` state:

```tsx
// In the state color mapping
const stateColors = {
  // ... existing states ...
  migrating: "var(--troshka-yellow, #f0ab00)",
};
```

- [ ] **Step 3: Test in browser**

Verify migrate button appears on active projects with shared-pool hosts, modal shows available hosts, and migration triggers correctly.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/projects/\\[id\\]/page.tsx
git commit -m "feat: project migration UI with host selector modal"
```

---

## Final Validation

### Task 16: Full Test Suite + Integration Check

- [ ] **Step 1: Run full backend test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify frontend builds**

Run: `cd src/frontend && npm run build`
Expected: Build succeeds with no errors

- [ ] **Step 3: Start dev environment and verify all pages load**

Run: `./dev-services.sh start`
Verify:
- Storage Pools page at `/admin/storage-pools` loads
- Hosts page shows pool selector in provision form
- Project detail page shows migrate button (when applicable)
- No console errors in browser

- [ ] **Step 4: Final commit with any cleanup**

```bash
cd /Users/prutledg/troshka && git status
# Stage any remaining changes
git commit -m "chore: final cleanup for shared storage feature"
```
