# GCP and Azure Provider Drivers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GCP and Azure as provider types to Troshka with full parity: host provisioning, managed NFS storage, console TLS, EIPs, and storage auto-extend.

**Architecture:** Each provider is a new `ProviderDriver` subclass (`gcp.py`, `azure.py`) in `src/backend/app/services/providers/`. Phase 1 builds shared schema/UI changes, Phase 2 implements GCP end-to-end, Phase 3 implements Azure. Troshkad (host agent) requires zero changes — it's fully provider-agnostic.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, `google-cloud-compute`/`google-cloud-filestore`/`google-cloud-dns` (GCP), `azure-mgmt-compute`/`azure-mgmt-network`/`azure-mgmt-storage`/`azure-mgmt-dns` (Azure), Next.js 15 + PatternFly 6 (frontend).

**Spec:** `docs/superpowers/specs/2026-06-16-gcp-azure-providers-design.md`

---

## Phase 1: Shared Foundation

### Task 1: Add Python dependencies

**Files:**
- Modify: `src/backend/requirements.txt`

- [ ] **Step 1: Add GCP and Azure SDK packages to requirements.txt**

Append to `src/backend/requirements.txt`:

```
# GCP provider
google-cloud-compute>=1.20.0
google-cloud-filestore>=1.10.0
google-cloud-dns>=0.35.0

# Azure provider
azure-identity>=1.17.0
azure-mgmt-compute>=32.0.0
azure-mgmt-network>=26.0.0
azure-mgmt-storage>=21.0.0
azure-mgmt-dns>=8.2.0
azure-mgmt-marketplaceordering>=1.1.0
```

- [ ] **Step 2: Install into venv**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/pip install -r requirements.txt`

- [ ] **Step 3: Verify imports**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -c "from google.cloud import compute_v1; from azure.identity import ClientSecretCredential; print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/requirements.txt
git commit -m "deps: add GCP and Azure SDK packages"
```

---

### Task 2: Extend Provider model with GCP and Azure columns

**Files:**
- Modify: `src/backend/app/models/provider.py`
- Test: `src/backend/tests/test_provider_model.py`

- [ ] **Step 1: Write test for new Provider columns**

Create `src/backend/tests/test_provider_model.py`:

```python
import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test_provider_model.db"

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models.provider import Provider

engine = create_engine(
    "sqlite:///./test_provider_model.db",
    connect_args={"check_same_thread": False},
)
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_gcp_provider_columns():
    db = Session()
    p = Provider(
        name="test-gcp",
        type="gcp",
        default_region="us-central1",
        gcp_project_id="my-project-123",
        gcp_network_id="projects/my-project/global/networks/troshka-vpc",
        gcp_subnet_id="projects/my-project/regions/us-central1/subnetworks/troshka-sub",
        gcp_firewall_policy="troshka-fw",
        gcp_zone="us-central1-a",
    )
    p.set_credentials({"service_account_json": {"type": "service_account"}})
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.gcp_project_id == "my-project-123"
    assert p.gcp_zone == "us-central1-a"
    assert p.get_credentials()["service_account_json"]["type"] == "service_account"
    db.close()


def test_azure_provider_columns():
    db = Session()
    p = Provider(
        name="test-azure",
        type="azure",
        default_region="eastus",
        azure_subscription_id="00000000-0000-0000-0000-000000000000",
        azure_resource_group="troshka-rg",
        azure_vnet_id="/subscriptions/.../resourceGroups/troshka-rg/providers/Microsoft.Network/virtualNetworks/troshka-vnet",
        azure_subnet_id="/subscriptions/.../subnets/troshka-sub",
        azure_nsg_id="/subscriptions/.../networkSecurityGroups/troshka-nsg",
        azure_location="eastus",
    )
    p.set_credentials({
        "client_id": "cid",
        "client_secret": "csec",
        "tenant_id": "tid",
        "subscription_id": "sid",
    })
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.azure_subscription_id == "00000000-0000-0000-0000-000000000000"
    assert p.azure_location == "eastus"
    creds = p.get_credentials()
    assert creds["client_id"] == "cid"
    db.close()
```

- [ ] **Step 2: Run test — verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_provider_model.py -v`

Expected: FAIL — `TypeError` because Provider doesn't have the new columns yet.

- [ ] **Step 3: Add columns to Provider model**

In `src/backend/app/models/provider.py`, add after the `max_eips` column (line 30):

```python
    # GCP-specific
    gcp_project_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gcp_network_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_subnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_firewall_policy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gcp_zone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Azure-specific
    azure_subscription_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    azure_resource_group: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    azure_vnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_subnet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_nsg_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    azure_location: Mapped[str | None] = mapped_column(String(50), nullable=True)
```

- [ ] **Step 4: Run test — verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_provider_model.py -v`

Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/provider.py src/backend/tests/test_provider_model.py
git commit -m "feat: add GCP and Azure columns to Provider model"
```

---

### Task 3: Extend StoragePool model with Filestore and Azure Files columns

**Files:**
- Modify: `src/backend/app/models/storage_pool.py`
- Modify: `src/backend/tests/test_storage_pools.py`

- [ ] **Step 1: Add tests for new pool modes**

Append to `src/backend/tests/test_storage_pools.py`:

```python
def test_create_filestore_pool():
    db = Session()
    provider = db.query(Provider).first()
    pool = StoragePool(
        name="gcp-filestore-pool",
        mode="shared-filestore",
        az="us-central1-a",
        filestore_instance_id="projects/my-proj/locations/us-central1-a/instances/troshka-fs",
        filestore_ip="10.0.1.100",
        filestore_share_name="troshka",
        filestore_tier="ZONAL",
        filestore_capacity_gb=1024,
        status="available",
        provider_id=provider.id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    assert pool.mode == "shared-filestore"
    assert pool.filestore_ip == "10.0.1.100"
    assert pool.filestore_capacity_gb == 1024
    db.close()


def test_create_azure_files_pool():
    db = Session()
    provider = db.query(Provider).first()
    pool = StoragePool(
        name="azure-files-pool",
        mode="shared-azure-files",
        azure_storage_account="troshkasa",
        azure_file_share_name="troshka",
        azure_file_share_url="troshkasa.file.core.windows.net:/troshkasa/troshka",
        azure_files_capacity_gb=256,
        azure_files_iops=3000,
        azure_files_throughput=125,
        status="available",
        provider_id=provider.id,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)

    assert pool.mode == "shared-azure-files"
    assert pool.azure_storage_account == "troshkasa"
    assert pool.azure_files_iops == 3000
    db.close()
```

- [ ] **Step 2: Run test — verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_storage_pools.py::test_create_filestore_pool -v`

Expected: FAIL — column doesn't exist.

- [ ] **Step 3: Add columns to StoragePool model**

In `src/backend/app/models/storage_pool.py`, add after the `ceph_subvolume_group` column (line 39):

```python
    # GCP Filestore
    filestore_instance_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    filestore_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    filestore_share_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    filestore_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    filestore_capacity_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Azure Files NFS
    azure_storage_account: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    azure_file_share_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    azure_file_share_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    azure_files_capacity_gb: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    azure_files_iops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    azure_files_throughput: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_storage_pools.py -v`

Expected: All storage pool tests pass including the two new ones.

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/storage_pool.py src/backend/tests/test_storage_pools.py
git commit -m "feat: add Filestore and Azure Files columns to StoragePool model"
```

---

### Task 4: Create Alembic migration for all new columns

**Files:**
- Create: `src/backend/alembic/versions/<generated>_add_gcp_azure_columns.py`

- [ ] **Step 1: Generate migration**

Run:
```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add gcp and azure provider and storage pool columns"
```

- [ ] **Step 2: Edit the generated migration**

Open the generated file and write the upgrade/downgrade. Use `postgresql.UUID(as_uuid=False)` for FK columns if any, and `sa.String` / `sa.Integer` for the new columns. All new columns are nullable (no defaults needed):

```python
"""add gcp and azure provider and storage pool columns"""

from alembic import op
import sqlalchemy as sa

# revision identifiers (filled in by alembic)
revision = "<generated>"
down_revision = "f4a772faef01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provider — GCP columns
    op.add_column("providers", sa.Column("gcp_project_id", sa.String(100), nullable=True))
    op.add_column("providers", sa.Column("gcp_network_id", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("gcp_subnet_id", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("gcp_firewall_policy", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("gcp_zone", sa.String(50), nullable=True))

    # Provider — Azure columns
    op.add_column("providers", sa.Column("azure_subscription_id", sa.String(50), nullable=True))
    op.add_column("providers", sa.Column("azure_resource_group", sa.String(100), nullable=True))
    op.add_column("providers", sa.Column("azure_vnet_id", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("azure_subnet_id", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("azure_nsg_id", sa.String(255), nullable=True))
    op.add_column("providers", sa.Column("azure_location", sa.String(50), nullable=True))

    # StoragePool — GCP Filestore columns
    op.add_column("storage_pools", sa.Column("filestore_instance_id", sa.String(255), nullable=True))
    op.add_column("storage_pools", sa.Column("filestore_ip", sa.String(45), nullable=True))
    op.add_column("storage_pools", sa.Column("filestore_share_name", sa.String(100), nullable=True))
    op.add_column("storage_pools", sa.Column("filestore_tier", sa.String(20), nullable=True))
    op.add_column("storage_pools", sa.Column("filestore_capacity_gb", sa.Integer(), nullable=True))

    # StoragePool — Azure Files NFS columns
    op.add_column("storage_pools", sa.Column("azure_storage_account", sa.String(100), nullable=True))
    op.add_column("storage_pools", sa.Column("azure_file_share_name", sa.String(100), nullable=True))
    op.add_column("storage_pools", sa.Column("azure_file_share_url", sa.String(500), nullable=True))
    op.add_column("storage_pools", sa.Column("azure_files_capacity_gb", sa.Integer(), nullable=True))
    op.add_column("storage_pools", sa.Column("azure_files_iops", sa.Integer(), nullable=True))
    op.add_column("storage_pools", sa.Column("azure_files_throughput", sa.Integer(), nullable=True))


def downgrade() -> None:
    # StoragePool — Azure Files
    op.drop_column("storage_pools", "azure_files_throughput")
    op.drop_column("storage_pools", "azure_files_iops")
    op.drop_column("storage_pools", "azure_files_capacity_gb")
    op.drop_column("storage_pools", "azure_file_share_url")
    op.drop_column("storage_pools", "azure_file_share_name")
    op.drop_column("storage_pools", "azure_storage_account")

    # StoragePool — Filestore
    op.drop_column("storage_pools", "filestore_capacity_gb")
    op.drop_column("storage_pools", "filestore_tier")
    op.drop_column("storage_pools", "filestore_share_name")
    op.drop_column("storage_pools", "filestore_ip")
    op.drop_column("storage_pools", "filestore_instance_id")

    # Provider — Azure
    op.drop_column("providers", "azure_location")
    op.drop_column("providers", "azure_nsg_id")
    op.drop_column("providers", "azure_subnet_id")
    op.drop_column("providers", "azure_vnet_id")
    op.drop_column("providers", "azure_resource_group")
    op.drop_column("providers", "azure_subscription_id")

    # Provider — GCP
    op.drop_column("providers", "gcp_zone")
    op.drop_column("providers", "gcp_firewall_policy")
    op.drop_column("providers", "gcp_subnet_id")
    op.drop_column("providers", "gcp_network_id")
    op.drop_column("providers", "gcp_project_id")
```

- [ ] **Step 3: Run migration against dev database**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head`

Expected: Migration applies cleanly.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/alembic/versions/
git commit -m "migration: add GCP and Azure columns to providers and storage_pools"
```

---

### Task 5: Update driver factory for GCP and Azure

**Files:**
- Modify: `src/backend/app/services/providers/__init__.py`
- Test: `src/backend/tests/test_provider_model.py` (append)

- [ ] **Step 1: Add test for driver factory dispatch**

Append to `src/backend/tests/test_provider_model.py`:

```python
from app.services.providers import get_provider_driver
from app.services.providers.base import ProviderDriver


def test_driver_factory_gcp():
    db = Session()
    p = db.query(Provider).filter_by(type="gcp").first()
    driver = get_provider_driver(p)
    assert isinstance(driver, ProviderDriver)
    assert type(driver).__name__ == "GCPDriver"
    db.close()


def test_driver_factory_azure():
    db = Session()
    p = db.query(Provider).filter_by(type="azure").first()
    driver = get_provider_driver(p)
    assert isinstance(driver, ProviderDriver)
    assert type(driver).__name__ == "AzureDriver"
    db.close()
```

- [ ] **Step 2: Run test — verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_provider_model.py::test_driver_factory_gcp -v`

Expected: FAIL — `ValueError: Unknown provider type: gcp`

- [ ] **Step 3: Create stub GCP driver**

Create `src/backend/app/services/providers/gcp.py`:

```python
"""GCP provider driver.

Provisions Compute Engine instances with nested virtualization,
manages GCP networking, Cloud DNS, static IPs, and Filestore.
"""

from app.services.providers.base import ProviderDriver


class GCPDriver(ProviderDriver):
    pass
```

- [ ] **Step 4: Create stub Azure driver**

Create `src/backend/app/services/providers/azure.py`:

```python
"""Azure provider driver.

Provisions Azure VMs with nested virtualization,
manages VNet/NSG networking, Azure DNS, public IPs, and Azure Files NFS.
"""

from app.services.providers.base import ProviderDriver


class AzureDriver(ProviderDriver):
    pass
```

- [ ] **Step 5: Update factory in `__init__.py`**

Replace the content of `src/backend/app/services/providers/__init__.py`:

```python
from app.services.providers.base import ProviderDriver


def get_provider_driver(provider) -> ProviderDriver:
    """Return the appropriate driver for a provider's type."""
    if provider.type == "ec2":
        from app.services.providers.ec2 import EC2Driver

        return EC2Driver()
    elif provider.type == "ocpvirt":
        from app.services.providers.ocpvirt import OCPVirtDriver

        return OCPVirtDriver()
    elif provider.type == "gcp":
        from app.services.providers.gcp import GCPDriver

        return GCPDriver()
    elif provider.type == "azure":
        from app.services.providers.azure import AzureDriver

        return AzureDriver()
    raise ValueError(f"Unknown provider type: {provider.type}")
```

- [ ] **Step 6: Run tests — verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_provider_model.py -v`

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/__init__.py src/backend/app/services/providers/gcp.py src/backend/app/services/providers/azure.py src/backend/tests/test_provider_model.py
git commit -m "feat: add GCP and Azure driver stubs and factory dispatch"
```

---

### Task 6: Update Provider API for GCP and Azure creation/response

**Files:**
- Modify: `src/backend/app/api/providers.py`

- [ ] **Step 1: Add GCP/Azure fields to ProviderCreate schema**

In `src/backend/app/api/providers.py`, add fields to `ProviderCreate` (after the OCP Virt fields around line 30):

```python
    # GCP fields
    gcp_project_id: str = ""
    service_account_json: str = ""  # JSON string of SA key

    # Azure fields
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_subscription_id: str = ""
    azure_location: str = ""
```

- [ ] **Step 2: Add GCP/Azure fields to ProviderResponse schema**

In `ProviderResponse`, add after `security_group_id`:

```python
    # GCP
    gcp_project_id: str | None = None
    gcp_network_id: str | None = None
    gcp_subnet_id: str | None = None
    gcp_firewall_policy: str | None = None
    gcp_zone: str | None = None

    # Azure
    azure_subscription_id: str | None = None
    azure_resource_group: str | None = None
    azure_vnet_id: str | None = None
    azure_subnet_id: str | None = None
    azure_nsg_id: str | None = None
    azure_location: str | None = None
```

- [ ] **Step 3: Handle GCP/Azure in create_provider()**

In the `create_provider` function, add branches after the OCPVirt branch (around line 130):

```python
    elif body.type == "gcp":
        if not body.gcp_project_id or not body.service_account_json:
            raise HTTPException(
                status_code=400,
                detail="GCP providers require gcp_project_id and service_account_json",
            )
        import json as json_mod
        try:
            sa_json = json_mod.loads(body.service_account_json)
        except json_mod.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="service_account_json must be valid JSON"
            )
        creds = {"service_account_json": sa_json}
        provider.gcp_project_id = body.gcp_project_id
    elif body.type == "azure":
        if not all([body.azure_tenant_id, body.azure_client_id,
                     body.azure_client_secret, body.azure_subscription_id]):
            raise HTTPException(
                status_code=400,
                detail="Azure providers require tenant_id, client_id, client_secret, subscription_id",
            )
        creds = {
            "tenant_id": body.azure_tenant_id,
            "client_id": body.azure_client_id,
            "client_secret": body.azure_client_secret,
            "subscription_id": body.azure_subscription_id,
        }
        provider.azure_subscription_id = body.azure_subscription_id
        provider.azure_location = body.azure_location or body.default_region or None
```

- [ ] **Step 4: Add GCP/Azure fields to ProviderResponse construction in list_providers()**

In the `list_providers` function and in `create_provider` response, add the new fields to the `ProviderResponse(...)` constructor. Add after `security_group_id`:

```python
            gcp_project_id=p.gcp_project_id,
            gcp_network_id=p.gcp_network_id,
            gcp_subnet_id=p.gcp_subnet_id,
            gcp_firewall_policy=p.gcp_firewall_policy,
            gcp_zone=p.gcp_zone,
            azure_subscription_id=p.azure_subscription_id,
            azure_resource_group=p.azure_resource_group,
            azure_vnet_id=p.azure_vnet_id,
            azure_subnet_id=p.azure_subnet_id,
            azure_nsg_id=p.azure_nsg_id,
            azure_location=p.azure_location,
```

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py
git commit -m "feat: add GCP and Azure fields to provider API schemas"
```

---

### Task 7: Update storage_extend.py for new pool modes

**Files:**
- Modify: `src/backend/app/services/storage_extend.py`

- [ ] **Step 1: Update should_extend_pool() to accept new modes**

In `src/backend/app/services/storage_extend.py`, change the `should_extend_pool` function (line 40):

Replace:
```python
    if pool.mode != "shared-fsx":
        return False
```

With:
```python
    if pool.mode not in ("shared-fsx", "shared-filestore", "shared-azure-files"):
        return False
```

And update the size check to work across modes:

Replace:
```python
    if (
        pool.auto_extend_max_gb
        and (pool.fsx_storage_gb or 0) >= pool.auto_extend_max_gb
    ):
        return False
```

With:
```python
    current_gb = pool.fsx_storage_gb or pool.filestore_capacity_gb or pool.azure_files_capacity_gb or 0
    if pool.auto_extend_max_gb and current_gb >= pool.auto_extend_max_gb:
        return False
```

- [ ] **Step 2: Add stub extend functions for Filestore and Azure Files**

Append to `src/backend/app/services/storage_extend.py`:

```python
def extend_pool_filestore(pool, db, increment_gb: int | None = None):
    """Extend a GCP Filestore instance. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.filestore_capacity_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.filestore_capacity_gb or 0):
        raise ValueError(
            f"Cannot extend: already at max ({pool.filestore_capacity_gb} GB)"
        )

    from app.models.provider import Provider

    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_filestore_capacity

    old_size = pool.filestore_capacity_gb or 0
    update_filestore_capacity(creds, pool.filestore_instance_id, new_size)

    pool.filestore_capacity_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info(
        "Extended Filestore %s from %d to %d GB for pool %s",
        pool.filestore_instance_id,
        old_size,
        new_size,
        pool.name,
    )
    return {
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "filestore_instance_id": pool.filestore_instance_id,
    }


def extend_pool_azure_files(pool, db, increment_gb: int | None = None):
    """Extend an Azure Files NFS share. Returns new size or raises."""
    increment = increment_gb or pool.auto_extend_increment_gb
    new_size = (pool.azure_files_capacity_gb or 0) + increment

    if pool.auto_extend_max_gb:
        new_size = min(new_size, pool.auto_extend_max_gb)
    if new_size <= (pool.azure_files_capacity_gb or 0):
        raise ValueError(
            f"Cannot extend: already at max ({pool.azure_files_capacity_gb} GB)"
        )

    from app.models.provider import Provider

    provider = db.query(Provider).get(pool.provider_id)
    if not provider:
        raise ValueError("No provider associated with pool")
    creds = provider.get_credentials()

    from app.services.storage_pool_service import update_azure_files_capacity

    old_size = pool.azure_files_capacity_gb or 0
    update_azure_files_capacity(
        creds,
        provider.azure_resource_group,
        pool.azure_storage_account,
        pool.azure_file_share_name,
        new_size,
    )

    pool.azure_files_capacity_gb = new_size
    db.commit()
    _mark_extended(f"pool:{pool.id}")
    logger.info(
        "Extended Azure Files share %s/%s from %d to %d GB for pool %s",
        pool.azure_storage_account,
        pool.azure_file_share_name,
        old_size,
        new_size,
        pool.name,
    )
    return {
        "old_size_gb": old_size,
        "new_size_gb": new_size,
        "storage_account": pool.azure_storage_account,
    }
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All pass. The new functions aren't called yet — they'll be integrated when the storage pool service gets the corresponding `update_filestore_capacity` and `update_azure_files_capacity` functions in Phase 2/3.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/storage_extend.py
git commit -m "feat: add Filestore and Azure Files extend functions to storage_extend"
```

---

### Task 8: Update frontend provider pages for GCP/Azure

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`

This task adds the provider type dropdown options and conditional credential fields. The full implementation of GCP/Azure-specific buttons (Setup Network, Discover Images) will be added in Phase 2/3 when the backend endpoints exist.

- [ ] **Step 1: Add GCP and Azure to the provider type dropdown**

Find the provider type select/dropdown in the providers page. Add `gcp` and `azure` as options alongside `ec2` and `ocpvirt`. The label should be "GCP" and "Azure" respectively.

- [ ] **Step 2: Add conditional credential fields**

When type is `gcp`: show "GCP Project ID" and "Service Account JSON" (textarea) fields.
When type is `azure`: show "Tenant ID", "Client ID", "Client Secret", "Subscription ID", and "Location" fields.

Existing EC2 fields (Access Key, Secret Key) should only show when type is `ec2`.
OCP Virt fields (API URL, Token) should only show when type is `ocpvirt`.

- [ ] **Step 3: Add GCP/Azure network info to provider detail section**

In the provider card/detail area, conditionally render:
- For `gcp`: Network ID, Subnet ID, Firewall Policy, Zone
- For `azure`: Resource Group, VNet ID, Subnet ID, NSG ID, Location
- For `ec2`: VPC ID, Subnet ID, Security Group ID (existing)

- [ ] **Step 4: Update "Setup VPC" button label**

For GCP and Azure providers, change the button label from "Setup VPC" to "Setup Network". Disable the button for now (backend endpoint doesn't exist yet) — it will be enabled in Phase 2/3.

- [ ] **Step 5: Verify in browser**

Run `./dev-services.sh start` (if not running), navigate to http://localhost:3100/admin/providers, and verify:
- Type dropdown shows all four options
- Selecting GCP shows project ID + SA JSON fields
- Selecting Azure shows tenant/client/subscription/location fields
- No console errors

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/providers/page.tsx
git commit -m "feat: add GCP and Azure provider type options to admin UI"
```

---

**Phase 1 complete.** At this point: models have all new columns, migration is ready, driver stubs exist and dispatch correctly, API schemas accept GCP/Azure fields, storage extend functions are stubbed, and the frontend shows the new provider types.

---

## Phase 2: GCP Driver (end-to-end)

> Phase 2 implements the full GCP driver. Each method in `gcp.py` is built incrementally. The driver is ~800 lines organized as helper functions + the `GCPDriver` class.

### Task 9: GCP driver — authentication helper and instance type constants

**Files:**
- Modify: `src/backend/app/services/providers/gcp.py`

- [ ] **Step 1: Add GCP client helpers and constants**

Replace the stub content of `src/backend/app/services/providers/gcp.py` with:

```python
"""GCP provider driver.

Provisions Compute Engine instances with nested virtualization,
manages GCP networking, Cloud DNS, static IPs, and Filestore.
"""

import logging

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

GCP_DEFAULT_INSTANCE_TYPE = "n2-highmem-32"

GCP_CURATED_INSTANCE_TYPES = [
    "n2-highmem-4",
    "n2-highmem-8",
    "n2-highmem-16",
    "n2-highmem-32",
    "n2-highmem-48",
    "n2-highmem-64",
    "n2-highmem-80",
]

GCP_RAM_PER_VCPU_GB = {
    "n2-highmem-4": 32,
    "n2-highmem-8": 64,
    "n2-highmem-16": 128,
    "n2-highmem-32": 256,
    "n2-highmem-48": 384,
    "n2-highmem-64": 512,
    "n2-highmem-80": 640,
}


def _get_compute_client(credentials: dict):
    """Build a google.cloud.compute_v1.InstancesClient from SA JSON."""
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.InstancesClient(credentials=cred)


def _get_disks_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.DisksClient(credentials=cred)


def _get_addresses_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.AddressesClient(credentials=cred)


def _get_networks_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.NetworksClient(credentials=cred)


def _get_subnetworks_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.SubnetworksClient(credentials=cred)


def _get_firewalls_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.FirewallsClient(credentials=cred)


def _get_images_client(credentials: dict):
    from google.cloud import compute_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return compute_v1.ImagesClient(credentials=cred)


def _parse_instance_type(instance_type: str) -> tuple[int, int]:
    """Parse 'n2-highmem-32' into (vcpus, ram_mb)."""
    vcpu_count = int(instance_type.rsplit("-", 1)[-1])
    ram_gb = GCP_RAM_PER_VCPU_GB.get(instance_type, vcpu_count * 8)
    return vcpu_count, ram_gb * 1024


def _generate_ssh_keypair():
    """Generate an SSH keypair for provisioning."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_key = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public_key


class GCPDriver(ProviderDriver):
    pass
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/gcp.py
git commit -m "feat: GCP driver helpers — auth clients, instance types, SSH keygen"
```

---

### Task 10: GCP driver — provision_host()

**Files:**
- Modify: `src/backend/app/services/providers/gcp.py`

- [ ] **Step 1: Implement provision_host on GCPDriver**

Add to the `GCPDriver` class in `gcp.py`:

```python
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        from google.cloud import compute_v1

        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = kwargs.get("zone") or provider.gcp_zone or provider.default_region + "-a"
        region = zone.rsplit("-", 1)[0]

        instance_type = instance_type or GCP_DEFAULT_INSTANCE_TYPE
        vcpus, ram_mb = _parse_instance_type(instance_type)

        private_pem, public_key = _generate_ssh_keypair()
        ssh_user = "troshka"

        image = kwargs.get("ami_id") or provider.default_ami
        if not image:
            raise ValueError("No image specified and no default_ami on provider")

        nfs_server = kwargs.get("nfs_server", "")
        nfs_path = kwargs.get("nfs_path", "")
        host_type = kwargs.get("host_type", "shared")

        cloud_init = _build_cloud_init(
            nfs_server=nfs_server,
            nfs_path=nfs_path,
            host_type=host_type,
        )

        instance_name = f"troshka-{host_id[:12]}"

        boot_disk = compute_v1.AttachedDisk(
            auto_delete=True,
            boot=True,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                disk_size_gb=50,
                source_image=image,
                disk_type=f"zones/{zone}/diskTypes/pd-ssd",
            ),
        )

        data_disk = compute_v1.AttachedDisk(
            auto_delete=False,
            boot=False,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                disk_name=f"troshka-data-{host_id[:12]}",
                disk_size_gb=storage_size_gb,
                disk_type=f"zones/{zone}/diskTypes/pd-ssd",
            ),
        )

        network_interface = compute_v1.NetworkInterface(
            network=provider.gcp_network_id,
            subnetwork=provider.gcp_subnet_id or kwargs.get("subnet_override"),
            access_configs=[
                compute_v1.AccessConfig(
                    name="External NAT",
                    type_="ONE_TO_ONE_NAT",
                )
            ],
        )

        metadata = compute_v1.Metadata(
            items=[
                compute_v1.Items(
                    key="ssh-keys",
                    value=f"{ssh_user}:{public_key}",
                ),
                compute_v1.Items(
                    key="user-data",
                    value=cloud_init,
                ),
            ]
        )

        instance = compute_v1.Instance(
            name=instance_name,
            machine_type=f"zones/{zone}/machineTypes/{instance_type}",
            disks=[boot_disk, data_disk],
            network_interfaces=[network_interface],
            metadata=metadata,
            advanced_machine_features=compute_v1.AdvancedMachineFeatures(
                enable_nested_virtualization=True,
            ),
            labels={
                "managed-by": "troshka",
                "troshka-host-id": host_id[:63],
            },
        )

        client = _get_compute_client(creds)
        operation = client.insert(
            project=project, zone=zone, instance_resource=instance
        )
        operation.result()

        created = client.get(project=project, zone=zone, instance=instance_name)
        public_ip = None
        private_ip = None
        for nic in created.network_interfaces:
            private_ip = nic.network_i_p
            for ac in nic.access_configs:
                if ac.nat_i_p:
                    public_ip = ac.nat_i_p
                    break

        logger.info(
            "Provisioned GCP instance %s (%s) in %s — %s / %s",
            instance_name, instance_type, zone, public_ip, private_ip,
        )

        return {
            "host_id": host_id,
            "instance_id": instance_name,
            "instance_type": instance_type,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
            "private_key": private_pem,
            "key_pair_name": None,
            "storage_size_gb": storage_size_gb,
            "max_eips": 8,
            "_ssh_host": public_ip,
            "_ssh_port": 22,
            "_ssh_user": ssh_user,
        }
```

- [ ] **Step 2: Add the _build_cloud_init helper function**

Add before the `GCPDriver` class:

```python
def _build_cloud_init(nfs_server: str = "", nfs_path: str = "", host_type: str = "shared") -> str:
    """Build cloud-init YAML for GCP host. Data disk is /dev/sdb."""
    nfs_section = ""
    if nfs_server and nfs_path:
        nfs_section = f"""
- mkdir -p /var/lib/troshka/shared
- mount -t nfs -o nfsvers=4.1,nconnect=16,hard,_netdev {nfs_server}:{nfs_path} /var/lib/troshka/shared
- echo "{nfs_server}:{nfs_path} /var/lib/troshka/shared nfs4 nfsvers=4.1,nconnect=16,hard,_netdev 0 0" >> /etc/fstab
- setsebool -P virt_use_nfs 1"""

    return f"""#cloud-config
runcmd:
- mkfs.xfs -f /dev/sdb || true
- mkdir -p /var/lib/troshka
- mount /dev/sdb /var/lib/troshka
- echo "/dev/sdb /var/lib/troshka xfs defaults,nofail 0 2" >> /etc/fstab
- mkdir -p /var/lib/troshka/vms /var/lib/troshka/images /var/lib/troshka/seeds /var/lib/troshka/local /var/lib/troshka/cache{nfs_section}
- dnf install -y qemu-kvm libvirt virt-install dnsmasq nftables python3
- systemctl enable --now libvirtd nftables
- sysctl -w vm.overcommit_memory=1
- echo "vm.overcommit_memory=1" >> /etc/sysctl.d/99-troshka.conf
- echo 1 > /sys/kernel/mm/ksm/run
- echo "echo 1 > /sys/kernel/mm/ksm/run" >> /etc/rc.d/rc.local
- chmod +x /etc/rc.d/rc.local
"""
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/gcp.py
git commit -m "feat: GCP driver — provision_host with nested virt and cloud-init"
```

---

### Task 11: GCP driver — terminate, status, power, resize, storage extend

**Files:**
- Modify: `src/backend/app/services/providers/gcp.py`

- [ ] **Step 1: Implement terminate_host**

```python
    def terminate_host(self, provider, instance_id):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"

        client = _get_compute_client(creds)
        try:
            operation = client.delete(
                project=project, zone=zone, instance=instance_id
            )
            operation.result()
        except Exception as e:
            if "was not found" in str(e):
                logger.warning("Instance %s already deleted", instance_id)
                return
            raise

        disks_client = _get_disks_client(creds)
        data_disk_name = instance_id.replace("troshka-", "troshka-data-")
        try:
            op = disks_client.delete(project=project, zone=zone, disk=data_disk_name)
            op.result()
        except Exception as e:
            if "was not found" not in str(e):
                logger.warning("Failed to delete data disk %s: %s", data_disk_name, e)

        logger.info("Terminated GCP instance %s", instance_id)
```

- [ ] **Step 2: Implement get_host_status**

```python
    def get_host_status(self, provider, instance_id):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"

        client = _get_compute_client(creds)
        try:
            inst = client.get(project=project, zone=zone, instance=instance_id)
        except Exception as e:
            if "was not found" in str(e):
                return None
            raise

        state_map = {
            "RUNNING": "running",
            "TERMINATED": "stopped",
            "STOPPED": "stopped",
            "SUSPENDED": "stopped",
            "STAGING": "pending",
            "PROVISIONING": "pending",
        }
        state = state_map.get(inst.status, inst.status.lower())

        public_ip = None
        private_ip = None
        for nic in inst.network_interfaces:
            private_ip = nic.network_i_p
            for ac in nic.access_configs:
                if ac.nat_i_p:
                    public_ip = ac.nat_i_p

        return {
            "instance_id": instance_id,
            "state": state,
            "public_ip": public_ip,
            "private_ip": private_ip,
        }
```

- [ ] **Step 3: Implement power management**

```python
    def get_host_powerstate(self, provider, instance_id):
        status = self.get_host_status(provider, instance_id)
        return status["state"] if status else "terminated"

    def start_host(self, provider, instance_id):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"
        client = _get_compute_client(creds)
        op = client.start(project=project, zone=zone, instance=instance_id)
        op.result()

    def stop_host(self, provider, instance_id):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"
        client = _get_compute_client(creds)
        op = client.stop(project=project, zone=zone, instance=instance_id)
        op.result()
```

- [ ] **Step 4: Implement resize_host**

```python
    def resize_host(self, provider, instance_id, new_instance_type):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"
        client = _get_compute_client(creds)

        self.stop_host(provider, instance_id)

        import time
        for _ in range(60):
            status = self.get_host_status(provider, instance_id)
            if status and status["state"] == "stopped":
                break
            time.sleep(5)

        from google.cloud import compute_v1
        op = client.set_machine_type(
            project=project,
            zone=zone,
            instance=instance_id,
            instances_set_machine_type_request_resource=compute_v1.InstancesSetMachineTypeRequest(
                machine_type=f"zones/{zone}/machineTypes/{new_instance_type}",
            ),
        )
        op.result()

        self.start_host(provider, instance_id)

        vcpus, ram_mb = _parse_instance_type(new_instance_type)
        return {
            "instance_type": new_instance_type,
            "total_vcpus": vcpus,
            "total_ram_mb": ram_mb,
        }
```

- [ ] **Step 5: Implement extend_host_storage**

```python
    def extend_host_storage(self, provider, host, db, increment_gb=None):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"

        increment = increment_gb or host.auto_extend_increment_gb
        new_size = host.storage_size_gb + increment
        if host.auto_extend_max_gb:
            new_size = min(new_size, host.auto_extend_max_gb)
        if new_size <= host.storage_size_gb:
            raise ValueError(
                f"Cannot extend: already at max ({host.storage_size_gb} GB)"
            )

        data_disk_name = host.instance_id.replace("troshka-", "troshka-data-")
        old_size = host.storage_size_gb

        from google.cloud import compute_v1

        disks_client = _get_disks_client(creds)
        op = disks_client.resize(
            project=project,
            zone=zone,
            disk=data_disk_name,
            disks_resize_request_resource=compute_v1.DisksResizeRequest(
                size_gb=new_size,
            ),
        )
        op.result()

        if host.agent_status == "connected":
            from app.services.troshkad_client import start_job, wait_for_job

            job_id = start_job(host, "/host/resize-storage", {})
            wait_for_job(host, job_id, timeout=30)

        host.storage_size_gb = new_size
        db.commit()
        logger.info(
            "Extended GCP disk %s from %d to %d GB for host %s",
            data_disk_name, old_size, new_size, host.id[:8],
        )
        return {"old_size_gb": old_size, "new_size_gb": new_size}
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/gcp.py
git commit -m "feat: GCP driver — terminate, status, power, resize, storage extend"
```

---

### Task 12: GCP driver — console DNS (Cloud DNS)

**Files:**
- Modify: `src/backend/app/services/providers/gcp.py`

- [ ] **Step 1: Add Cloud DNS helper**

Add before the `GCPDriver` class:

```python
def _get_dns_client(credentials: dict):
    from google.cloud import dns
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    return dns.Client(project=sa_json.get("project_id"), credentials=cred)
```

- [ ] **Step 2: Implement console methods on GCPDriver**

```python
    def setup_console(self, provider, base_domain):
        creds = provider.get_credentials()
        dns_client = _get_dns_client(creds)

        zone_name = base_domain.replace(".", "-")
        zone = dns_client.zone(zone_name, dns_name=base_domain + ".")
        if not zone.exists():
            zone.create()

        nameservers = list(zone.name_servers or [])

        return {
            "console_base_domain": base_domain,
            "console_zone_id": zone_name,
            "console_nameservers": nameservers,
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        creds = provider.get_credentials()
        dns_client = _get_dns_client(creds)

        zone_name = provider.console_zone_id
        zone = dns_client.zone(zone_name)

        record_set = zone.resource_record_set(
            hostname + ".", "A", 60, [ip_address]
        )
        changes = zone.changes()
        changes.add_record_set(record_set)
        changes.create()

    def delete_console_record(self, provider, host, hostname, ip_address):
        creds = provider.get_credentials()
        dns_client = _get_dns_client(creds)

        zone_name = provider.console_zone_id
        zone = dns_client.zone(zone_name)

        record_set = zone.resource_record_set(
            hostname + ".", "A", 60, [ip_address]
        )
        changes = zone.changes()
        changes.delete_record_set(record_set)
        try:
            changes.create()
        except Exception as e:
            if "notFound" not in str(e):
                raise

    def delete_console(self, provider):
        creds = provider.get_credentials()
        dns_client = _get_dns_client(creds)

        zone_name = provider.console_zone_id
        if not zone_name:
            return
        zone = dns_client.zone(zone_name)
        try:
            zone.delete()
        except Exception as e:
            logger.warning("Failed to delete Cloud DNS zone %s: %s", zone_name, e)
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/gcp.py
git commit -m "feat: GCP driver — Cloud DNS console setup and record management"
```

---

### Task 13: GCP driver — EIP management

**Files:**
- Modify: `src/backend/app/services/providers/gcp.py`

- [ ] **Step 1: Implement EIP methods**

```python
    def allocate_eip(self, provider, host, eip_id):
        from google.cloud import compute_v1

        creds = provider.get_credentials()
        project = provider.gcp_project_id
        region = (provider.gcp_zone or provider.default_region + "-a").rsplit("-", 1)[0]

        client = _get_addresses_client(creds)
        address_name = f"troshka-eip-{eip_id[:12]}"

        address = compute_v1.Address(
            name=address_name,
            address_type="EXTERNAL",
            network_tier="PREMIUM",
        )
        op = client.insert(project=project, region=region, address_resource=address)
        op.result()

        created = client.get(project=project, region=region, address=address_name)
        public_ip = created.address

        logger.info("Allocated GCP static IP %s (%s)", address_name, public_ip)
        return {"public_ip": public_ip, "allocation_id": address_name}

    def associate_eip(self, provider, host, allocation_id):
        from google.cloud import compute_v1

        creds = provider.get_credentials()
        project = provider.gcp_project_id
        zone = provider.gcp_zone or provider.default_region + "-a"
        region = zone.rsplit("-", 1)[0]

        addr_client = _get_addresses_client(creds)
        addr = addr_client.get(project=project, region=region, address=allocation_id)
        public_ip = addr.address

        client = _get_compute_client(creds)
        inst = client.get(project=project, zone=zone, instance=host.instance_id)

        nic = inst.network_interfaces[0]
        access_config = compute_v1.AccessConfig(
            name="External NAT",
            type_="ONE_TO_ONE_NAT",
            nat_i_p=public_ip,
        )

        if nic.access_configs:
            client.delete_access_config(
                project=project,
                zone=zone,
                instance=host.instance_id,
                access_config="External NAT",
                network_interface="nic0",
            )

        op = client.add_access_config(
            project=project,
            zone=zone,
            instance=host.instance_id,
            network_interface="nic0",
            access_config_resource=access_config,
        )
        op.result()

        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        creds = provider.get_credentials()
        project = provider.gcp_project_id
        region = (provider.gcp_zone or provider.default_region + "-a").rsplit("-", 1)[0]

        client = _get_addresses_client(creds)
        try:
            op = client.delete(project=project, region=region, address=allocation_id)
            op.result()
        except Exception as e:
            if "was not found" not in str(e):
                raise

    def update_eip_ports(self, provider, host, allocation_id, ports):
        pass
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/gcp.py
git commit -m "feat: GCP driver — static IP allocation, association, release"
```

---

### Task 14: GCP network setup and image discovery endpoints

**Files:**
- Modify: `src/backend/app/api/providers.py`

- [ ] **Step 1: Add GCP network setup endpoint**

Add a new endpoint in `providers.py`:

```python
@router.post("/{provider_id}/create-network-gcp")
def create_network_gcp(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "gcp":
        raise HTTPException(status_code=404, detail="GCP provider not found")

    from google.cloud import compute_v1
    from google.oauth2 import service_account

    creds = provider.get_credentials()
    sa_json = creds.get("service_account_json", {})
    credential = service_account.Credentials.from_service_account_info(sa_json)
    project = provider.gcp_project_id
    region = provider.default_region or "us-central1"

    networks_client = compute_v1.NetworksClient(credentials=credential)
    network = compute_v1.Network(
        name="troshka-vpc",
        auto_create_subnetworks=False,
    )
    op = networks_client.insert(project=project, network_resource=network)
    op.result()
    created_network = networks_client.get(project=project, network="troshka-vpc")

    subnets_client = compute_v1.SubnetworksClient(credentials=credential)
    subnet = compute_v1.Subnetwork(
        name="troshka-subnet",
        ip_cidr_range="10.100.1.0/24",
        network=created_network.self_link,
        region=region,
    )
    op = subnets_client.insert(project=project, region=region, subnetwork_resource=subnet)
    op.result()
    created_subnet = subnets_client.get(
        project=project, region=region, subnetwork="troshka-subnet"
    )

    firewalls_client = compute_v1.FirewallsClient(credentials=credential)
    rules = [
        ("troshka-allow-ssh", [compute_v1.Allowed(I_p_protocol="tcp", ports=["22"])]),
        ("troshka-allow-console", [compute_v1.Allowed(I_p_protocol="tcp", ports=["443"])]),
        ("troshka-allow-agent", [compute_v1.Allowed(I_p_protocol="tcp", ports=["31337"])]),
        ("troshka-allow-vxlan", [compute_v1.Allowed(I_p_protocol="udp", ports=["4789"])]),
    ]
    for name, allowed in rules:
        fw = compute_v1.Firewall(
            name=name,
            network=created_network.self_link,
            allowed=allowed,
            source_ranges=["0.0.0.0/0"],
            target_tags=["troshka-host"],
        )
        op = firewalls_client.insert(project=project, firewall_resource=fw)
        op.result()

    provider.gcp_network_id = created_network.self_link
    provider.gcp_subnet_id = created_subnet.self_link
    provider.gcp_firewall_policy = "troshka-fw"
    provider.gcp_zone = region + "-a"
    db.commit()

    return {"status": "ok", "network": created_network.self_link}
```

- [ ] **Step 2: Add GCP image discovery endpoint**

```python
@router.get("/{provider_id}/discover-images-gcp")
def discover_images_gcp(
    provider_id: str,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or provider.type != "gcp":
        raise HTTPException(status_code=404, detail="GCP provider not found")

    from google.cloud import compute_v1
    from google.oauth2 import service_account

    creds = provider.get_credentials()
    sa_json = creds.get("service_account_json", {})
    credential = service_account.Credentials.from_service_account_info(sa_json)

    images_client = compute_v1.ImagesClient(credentials=credential)
    results = []

    for image_project in ["rhel-byos-cloud", "rhel-cloud"]:
        source = "BYOS" if "byos" in image_project else "PAYG"
        try:
            for img in images_client.list(project=image_project):
                name = img.name or ""
                if not any(name.startswith(p) for p in ["rhel-byos-9", "rhel-byos-10", "rhel-9", "rhel-10"]):
                    continue
                if img.deprecated and img.deprecated.state == "DEPRECATED":
                    continue
                results.append({
                    "name": name,
                    "self_link": img.self_link,
                    "family": img.family or "",
                    "source": source,
                    "creation_timestamp": img.creation_timestamp or "",
                })
        except Exception as e:
            logger.warning("Failed to list images from %s: %s", image_project, e)

    results.sort(key=lambda x: x["creation_timestamp"], reverse=True)
    return results
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py
git commit -m "feat: GCP network setup and image discovery API endpoints"
```

---

### Task 15: GCP Filestore pool provisioning

**Files:**
- Modify: `src/backend/app/services/storage_pool_service.py`
- Modify: `src/backend/app/api/storage_pools.py`

- [ ] **Step 1: Add Filestore creation function to storage_pool_service.py**

Add to `src/backend/app/services/storage_pool_service.py`:

```python
def create_filestore_instance(
    credentials: dict,
    project: str,
    zone: str,
    network: str,
    capacity_gb: int,
    share_name: str = "troshka",
    tier: str = "ZONAL",
) -> dict:
    from google.cloud import filestore_v1
    from google.oauth2 import service_account

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    client = filestore_v1.CloudFilestoreManagerClient(credentials=cred)

    instance = filestore_v1.Instance(
        tier=getattr(filestore_v1.Instance.Tier, tier, filestore_v1.Instance.Tier.ZONAL),
        file_shares=[
            filestore_v1.FileShareConfig(
                name=share_name,
                capacity_gb=capacity_gb,
            )
        ],
        networks=[
            filestore_v1.NetworkConfig(
                network=network,
                modes=[filestore_v1.NetworkConfig.AddressMode.MODE_IPV4],
            )
        ],
        labels={"managed-by": "troshka"},
    )

    parent = f"projects/{project}/locations/{zone}"
    instance_id = f"troshka-fs-{zone}"
    operation = client.create_instance(
        parent=parent, instance_id=instance_id, instance=instance
    )
    result = operation.result()

    ip_address = None
    if result.networks:
        ip_addresses = result.networks[0].ip_addresses
        if ip_addresses:
            ip_address = ip_addresses[0]

    return {
        "instance_name": result.name,
        "ip_address": ip_address,
        "share_name": share_name,
    }


def update_filestore_capacity(credentials: dict, instance_name: str, new_capacity_gb: int):
    from google.cloud import filestore_v1
    from google.oauth2 import service_account
    from google.protobuf import field_mask_pb2

    sa_json = credentials.get("service_account_json", {})
    cred = service_account.Credentials.from_service_account_info(sa_json)
    client = filestore_v1.CloudFilestoreManagerClient(credentials=cred)

    instance = filestore_v1.Instance(
        name=instance_name,
        file_shares=[
            filestore_v1.FileShareConfig(
                name="troshka",
                capacity_gb=new_capacity_gb,
            )
        ],
    )
    update_mask = field_mask_pb2.FieldMask(paths=["file_shares"])
    operation = client.update_instance(instance=instance, update_mask=update_mask)
    operation.result()


def provision_filestore_pool(
    pool_id: str,
    credentials: dict,
    project: str,
    zone: str,
    network: str,
    capacity_gb: int,
    share_name: str = "troshka",
    tier: str = "ZONAL",
):
    db = SessionLocal()
    try:
        result = create_filestore_instance(
            credentials, project, zone, network, capacity_gb, share_name, tier
        )
        pool = db.query(StoragePool).get(pool_id)
        pool.filestore_instance_id = result["instance_name"]
        pool.filestore_ip = result["ip_address"]
        pool.filestore_share_name = result["share_name"]
        pool.filestore_capacity_gb = capacity_gb
        pool.filestore_tier = tier
        pool.status = "available"
        db.commit()
        logger.info("Filestore pool %s is available", pool_id[:8])
    except Exception as e:
        logger.error("Filestore provisioning failed for pool %s: %s", pool_id[:8], e)
        pool = db.query(StoragePool).get(pool_id)
        pool.status = "error"
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Add Filestore pool creation handling to storage_pools API**

In the storage pool create endpoint in `src/backend/app/api/storage_pools.py`, add a branch for `shared-filestore` mode alongside the existing `shared-fsx` branch. The pattern is: create the StoragePool record with `status="creating"`, then spawn a background thread to call `provision_filestore_pool()`.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/storage_pool_service.py src/backend/app/api/storage_pools.py
git commit -m "feat: GCP Filestore pool provisioning and capacity extend"
```

---

### Task 16: Update agent deployer for GCP

**Files:**
- Modify: `src/backend/app/services/agent_deployer.py`

- [ ] **Step 1: Find the SSH user detection logic**

Search for where SSH user is set (e.g., `ec2-user`, `cloud-user`). Add GCP handling:

```python
if provider.type == "gcp":
    ssh_user = "troshka"
elif provider.type == "azure":
    ssh_user = "troshka"
```

- [ ] **Step 2: Find the data disk device path logic**

Search for where the data disk mount path is configured (e.g., `/dev/nvme1n1`). Add GCP/Azure paths:

```python
if provider.type == "gcp":
    data_disk_device = "/dev/sdb"
elif provider.type == "azure":
    data_disk_device = "/dev/disk/azure/scsi1/lun0"
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/agent_deployer.py
git commit -m "feat: agent deployer — GCP and Azure SSH user and disk device paths"
```

---

### Task 17: Frontend — GCP Setup Network and Discover Images buttons

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`

- [ ] **Step 1: Enable "Setup Network" button for GCP providers**

Wire the button to call `POST /providers/{id}/create-network-gcp`. Show success/error toast. On success, refresh provider data to show the new network IDs.

- [ ] **Step 2: Add "Discover Images" button for GCP providers**

Wire to `GET /providers/{id}/discover-images-gcp`. Show results in a modal/dropdown. Allow selecting an image to set as default. Label BYOS vs PAYG images.

- [ ] **Step 3: Add Filestore pool mode to storage pool creation**

In the storage pool create dialog, when the provider is GCP, add "Filestore (GCP)" to the mode dropdown. Show capacity and tier fields.

- [ ] **Step 4: Verify in browser**

Test the full flow in the admin UI for a GCP provider.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/providers/page.tsx src/frontend/src/app/admin/storage-pools/page.tsx
git commit -m "feat: frontend — GCP network setup, image discovery, Filestore pool UI"
```

---

**Phase 2 complete.** GCP driver is fully implemented with all 16 ProviderDriver methods, network setup, image discovery, Filestore pools, and frontend integration.

---

## Phase 3: Azure Driver (end-to-end)

> Phase 3 follows the same structure as Phase 2 but for Azure. The Azure driver has additional complexity around resource cleanup (NICs, public IPs are independent resources) and marketplace terms acceptance.

### Task 18: Azure driver — authentication helpers and instance type constants

**Files:**
- Modify: `src/backend/app/services/providers/azure.py`

- [ ] **Step 1: Add Azure client helpers and constants**

Replace the stub content of `azure.py` with authentication helpers, curated instance types, SSH keygen (reuse same `_generate_ssh_keypair` pattern as GCP), and cloud-init builder. Follow the same structure as Task 9 but with Azure SDK clients (`ClientSecretCredential`, `ComputeManagementClient`, `NetworkManagementClient`, `StorageManagementClient`, `DnsManagementClient`).

Azure curated types:
```python
AZURE_DEFAULT_INSTANCE_TYPE = "Standard_E32s_v5"
AZURE_CURATED_INSTANCE_TYPES = [
    "Standard_E4s_v5", "Standard_E8s_v5", "Standard_E16s_v5",
    "Standard_E32s_v5", "Standard_E48s_v5", "Standard_E64s_v5",
    "Standard_E96s_v5",
]
```

Cloud-init: same content as GCP but data disk is `/dev/disk/azure/scsi1/lun0`.

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/azure.py
git commit -m "feat: Azure driver helpers — auth clients, instance types, SSH keygen"
```

---

### Task 19: Azure driver — provision_host()

**Files:**
- Modify: `src/backend/app/services/providers/azure.py`

- [ ] **Step 1: Implement provision_host**

Key steps unique to Azure:
1. Create public IP resource (`PublicIPAddress`, Standard SKU, static)
2. Create NIC (`NetworkInterface`) with public IP, in provider's subnet + NSG
3. Accept marketplace terms if BYOS image (`MarketplaceAgreements.create()`)
4. Create VM with: RHEL BYOS image URN, OS disk 50 GB, data disk (Premium SSD, `storage_size_gb`), cloud-init via `os_profile.custom_data` (base64)
5. Poll until `provisioning_state == "Succeeded"`
6. Return dict matching ProviderDriver interface

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/azure.py
git commit -m "feat: Azure driver — provision_host with BYOS image and data disk"
```

---

### Task 20: Azure driver — terminate, status, power, resize, storage extend

**Files:**
- Modify: `src/backend/app/services/providers/azure.py`

- [ ] **Step 1: Implement terminate_host**

Azure-specific: must delete VM first, then OS disk, data disk, NIC, public IP in correct order. Each is a separate `begin_delete()` call.

- [ ] **Step 2: Implement get_host_status, power management, resize, extend_host_storage**

Same patterns as GCP driver (Task 11) but using Azure SDK:
- Status: `virtual_machines.instance_view()` → parse `statuses` for `PowerState/running`
- Start: `virtual_machines.begin_start()`
- Stop: `virtual_machines.begin_deallocate()` (releases billing)
- Resize: attempt `begin_update()` with new `vm_size`, fall back to deallocate→resize→start
- Extend: `disks.begin_update()` with new `disk_size_gb`

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/azure.py
git commit -m "feat: Azure driver — terminate, status, power, resize, storage extend"
```

---

### Task 21: Azure driver — console DNS (Azure DNS)

**Files:**
- Modify: `src/backend/app/services/providers/azure.py`

- [ ] **Step 1: Implement console DNS methods**

- `setup_console()`: Create Azure DNS zone via `DnsManagementClient.zones.create_or_update()`
- `create_console_record()`: `record_sets.create_or_update()` with A record
- `delete_console_record()`: `record_sets.delete()`
- `delete_console()`: `zones.delete()`

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/azure.py
git commit -m "feat: Azure driver — Azure DNS console setup and record management"
```

---

### Task 22: Azure driver — EIP management

**Files:**
- Modify: `src/backend/app/services/providers/azure.py`

- [ ] **Step 1: Implement EIP methods**

- `allocate_eip()`: `public_ip_addresses.begin_create_or_update()` — Standard SKU, static
- `associate_eip()`: Update NIC with secondary IP config + public IP association
- `release_eip()`: `public_ip_addresses.begin_delete()`
- `update_eip_ports()`: Update NSG rules for port forwarding

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/azure.py
git commit -m "feat: Azure driver — public IP allocation, association, release"
```

---

### Task 23: Azure network setup, image discovery, and marketplace terms

**Files:**
- Modify: `src/backend/app/api/providers.py`

- [ ] **Step 1: Add Azure network setup endpoint**

`POST /providers/{id}/create-network-azure`:
1. Create Resource Group (`troshka-rg`)
2. Create VNet (`10.100.0.0/16`) with subnet (`10.100.1.0/24`)
3. Create NSG with rules (SSH, console, troshkad, VXLAN)
4. Associate NSG with subnet
5. Store IDs on provider

- [ ] **Step 2: Add Azure image discovery endpoint**

`GET /providers/{id}/discover-images-azure`:
- List images from `redhat` publisher, `rhel-byos` offer for RHEL 9/10
- Fallback: `RHEL` offer for PAYG
- Return URNs

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py
git commit -m "feat: Azure network setup, image discovery, and marketplace terms"
```

---

### Task 24: Azure Files NFS pool provisioning

**Files:**
- Modify: `src/backend/app/services/storage_pool_service.py`
- Modify: `src/backend/app/api/storage_pools.py`

- [ ] **Step 1: Add Azure Files NFS creation functions**

Add to `storage_pool_service.py`:
- `create_azure_files_nfs()`: Create storage account (`kind=FileStorage`, `Premium_LRS`, NFS v3 enabled), create NFS file share, create private endpoint in VNet
- `update_azure_files_capacity()`: Update file share quota
- `provision_azure_files_pool()`: Orchestrate creation, update pool record

- [ ] **Step 2: Add Azure Files pool creation handling to storage_pools API**

Branch for `shared-azure-files` mode in the create endpoint.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/storage_pool_service.py src/backend/app/api/storage_pools.py
git commit -m "feat: Azure Files NFS pool provisioning and capacity extend"
```

---

### Task 25: Frontend — Azure Setup Network, Discover Images, Azure Files pool

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`
- Modify: `src/frontend/src/app/admin/storage-pools/page.tsx`

- [ ] **Step 1: Enable "Setup Network" for Azure providers**

Wire to `POST /providers/{id}/create-network-azure`.

- [ ] **Step 2: Add "Discover Images" for Azure providers**

Wire to `GET /providers/{id}/discover-images-azure`. Label BYOS vs PAYG.

- [ ] **Step 3: Add Azure Files NFS pool mode**

In storage pool create, when provider is Azure, show "Azure Files NFS" mode with capacity, IOPS, and throughput fields.

- [ ] **Step 4: Verify in browser**

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/
git commit -m "feat: frontend — Azure network setup, image discovery, Azure Files pool UI"
```

---

### Task 26: Final integration test

- [ ] **Step 1: Run full backend test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`

Expected: All pass.

- [ ] **Step 2: Run black and ruff**

Run: `cd /Users/prutledg/troshka/src/backend && black app/ tests/ && ruff check app/ tests/`

Expected: No issues.

- [ ] **Step 3: Verify frontend builds**

Run: `cd /Users/prutledg/troshka/src/frontend && npm run build`

Expected: Build succeeds.

- [ ] **Step 4: Final commit if any formatting fixes**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "chore: formatting cleanup"
```

---

**Phase 3 complete.** Both GCP and Azure drivers are fully implemented with all 16 ProviderDriver methods, managed NFS storage pools, console DNS, EIPs, storage auto-extend, image discovery, and frontend integration.
