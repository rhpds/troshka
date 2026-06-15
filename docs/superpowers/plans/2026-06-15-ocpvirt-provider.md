# OCP Virt Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenShift Virtualization as a second compute provider alongside AWS EC2, using nested-virt RHEL VMs running the existing troshkad agent unchanged.

**Architecture:** Extract the current monolithic provisioner into a provider-driver abstraction (`base.py`, `ec2.py`, `ocpvirt.py`), then implement the OCP Virt driver using the Kubernetes Python client to create VMs, Services, and Routes. Storage uses Ceph-NFS via the existing `shared-byo` code path. Console uses OCP edge Routes instead of Route53 + certbot. EIPs are not supported on OCP Virt.

**Tech Stack:** Python `kubernetes` client library, KubeVirt API (`kubevirt.io/v1`), OCP Route API (`route.openshift.io/v1`), Ceph-NFS storage class

**Spec:** `docs/superpowers/specs/2026-06-15-ocpvirt-provider-design.md`

---

## File Structure

### New files
- `src/backend/app/services/providers/__init__.py` — dispatcher: `get_provider_driver(provider)`
- `src/backend/app/services/providers/base.py` — abstract `ProviderDriver` interface
- `src/backend/app/services/providers/ec2.py` — extracted EC2 logic from `provisioner.py`
- `src/backend/app/services/providers/ocpvirt.py` — new OCP Virt driver
- `src/backend/tests/test_provider_driver.py` — tests for driver dispatch and interface
- `src/backend/tests/test_ocpvirt_driver.py` — tests for OCP Virt driver (mocked k8s client)
- `src/backend/alembic/versions/XXXX_add_ceph_subvolume_to_storage_pools.py` — migration

### Modified files
- `src/backend/app/api/hosts.py` — use `get_provider_driver()` instead of direct provisioner calls
- `src/backend/app/api/providers.py` — accept `type="ocpvirt"`, OCP Virt credential fields
- `src/backend/app/api/storage_pools.py` — accept `mode="shared-ceph-nfs"`
- `src/backend/app/services/health_poller.py` — use driver for auto-extend
- `src/backend/app/services/pattern_buffer_service.py` — use driver instead of `_find_ec2_provider()`
- `src/backend/app/services/console_dns.py` — add provider-type dispatch (or move into driver)
- `src/backend/app/models/storage_pool.py` — add `ceph_subvolume_group` column
- `src/backend/requirements.txt` — add `kubernetes` package
- `src/troshka-vncd/troshka-vncd.py` — add `--no-tls` flag
- `src/frontend/src/app/admin/providers/page.tsx` — OCP Virt provider type + fields
- `src/frontend/src/app/admin/hosts/page.tsx` — CPU/memory fields for OCP Virt hosts
- `src/frontend/src/app/admin/storage-pools/page.tsx` — `shared-ceph-nfs` mode option

### Unchanged (verified by keeping tests green)
- `src/backend/app/services/provisioner.py` — kept as-is during Phase 1, deprecated later
- `src/troshkad/troshkad.py` — no changes
- All canvas, topology, deploy, library, pattern code

---

## Phase 1: Provider Abstraction (Pure Refactor)

All existing functionality must continue to work identically. This phase extracts code into modules but changes no behavior.

### Task 1: Create provider driver interface

**Files:**
- Create: `src/backend/app/services/providers/__init__.py`
- Create: `src/backend/app/services/providers/base.py`
- Test: `src/backend/tests/test_provider_driver.py`

- [ ] **Step 1: Create the providers package directory**

```bash
mkdir -p src/backend/app/services/providers
```

- [ ] **Step 2: Write test for driver dispatch**

Create `src/backend/tests/test_provider_driver.py`:

```python
from unittest.mock import MagicMock

from app.services.providers import get_provider_driver
from app.services.providers.base import ProviderDriver


def test_get_ec2_driver():
    provider = MagicMock()
    provider.type = "ec2"
    driver = get_provider_driver(provider)
    assert isinstance(driver, ProviderDriver)


def test_get_unknown_driver_raises():
    provider = MagicMock()
    provider.type = "unknown_xyz"
    try:
        get_provider_driver(provider)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "unknown_xyz" in str(e)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py -v`
Expected: FAIL — module not found

- [ ] **Step 4: Create base.py with abstract interface**

Create `src/backend/app/services/providers/base.py`:

```python
"""Abstract provider driver interface.

Each provider type (EC2, OCP Virt) implements this interface
to handle infrastructure-specific operations.
"""


class ProviderDriver:
    def provision_host(self, provider, host_id, instance_type, storage_size_gb, **kwargs):
        """Provision a new host. Returns dict with:
        host_id, instance_id, instance_type, public_ip, private_ip,
        total_vcpus, total_ram_mb, private_key, key_pair_name,
        storage_size_gb, max_eips
        """
        raise NotImplementedError

    def terminate_host(self, provider, instance_id):
        """Terminate a host instance."""
        raise NotImplementedError

    def get_host_status(self, provider, instance_id):
        """Get current status. Returns dict with instance_id, state,
        public_ip, private_ip — or None if not found."""
        raise NotImplementedError

    def resize_host(self, provider, instance_id, new_instance_type):
        """Resize a host. Returns dict with new instance_type, total_vcpus, etc."""
        raise NotImplementedError

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        """Extend host storage volume. Returns dict with old_size_gb, new_size_gb."""
        raise NotImplementedError

    def setup_console(self, provider, base_domain):
        """Set up console infrastructure for a provider. Returns dict with
        console_base_domain, console_zone_id, console_nameservers, etc."""
        raise NotImplementedError

    def create_console_record(self, provider, host, hostname, ip_address):
        """Create DNS/Route record for a host's console endpoint."""
        raise NotImplementedError

    def delete_console_record(self, provider, host, hostname, ip_address):
        """Delete DNS/Route record for a host's console endpoint."""
        raise NotImplementedError

    def delete_console(self, provider):
        """Remove all console infrastructure for a provider."""
        raise NotImplementedError

    def get_host_powerstate(self, provider, instance_id):
        """Get VM power state (running, stopped, etc.)."""
        raise NotImplementedError

    def start_host(self, provider, instance_id):
        """Start a stopped host."""
        raise NotImplementedError

    def stop_host(self, provider, instance_id):
        """Stop a running host."""
        raise NotImplementedError
```

- [ ] **Step 5: Create dispatcher**

Create `src/backend/app/services/providers/__init__.py`:

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
    raise ValueError(f"Unknown provider type: {provider.type}")
```

- [ ] **Step 6: Create minimal ec2.py stub (just inherits, delegates to provisioner.py)**

Create `src/backend/app/services/providers/ec2.py`:

```python
"""EC2 provider driver.

Delegates to the existing provisioner.py functions during the
refactoring transition. Methods will be inlined later.
"""

from app.services.providers.base import ProviderDriver


class EC2Driver(ProviderDriver):
    def provision_host(self, provider, host_id, instance_type, storage_size_gb, **kwargs):
        from app.services.provisioner import provision_host

        creds = provider.get_credentials()
        return provision_host(
            instance_type=instance_type,
            host_id=host_id,
            region=kwargs.get("region") or provider.default_region,
            credentials=creds,
            storage_size_gb=storage_size_gb,
            ami_id=kwargs.get("ami_id") or provider.default_ami,
            vpc_id=kwargs.get("vpc_id") or provider.vpc_id,
            subnet_id=kwargs.get("subnet_id") or provider.subnet_id,
            security_group_id=kwargs.get("security_group_id") or provider.security_group_id,
            subnet_override=kwargs.get("subnet_override"),
            console_zone_id=provider.console_zone_id,
            nfs_server=kwargs.get("nfs_server"),
            nfs_path=kwargs.get("nfs_path"),
            host_type=kwargs.get("host_type", "shared"),
        )

    def terminate_host(self, provider, instance_id):
        from app.services.provisioner import terminate_host

        creds = provider.get_credentials()
        terminate_host(instance_id, credentials=creds)

    def get_host_status(self, provider, instance_id):
        from app.services.provisioner import get_host_status

        creds = provider.get_credentials()
        return get_host_status(instance_id, credentials=creds)

    def resize_host(self, provider, instance_id, new_instance_type):
        from app.services.provisioner import resize_instance

        creds = provider.get_credentials()
        return resize_instance(instance_id, new_instance_type, credentials=creds)

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        from app.services.storage_extend import extend_host_ebs

        return extend_host_ebs(host, db, increment_gb)

    def setup_console(self, provider, base_domain):
        from app.services.console_dns import setup_console_dns

        creds = provider.get_credentials()
        return setup_console_dns(base_domain, credentials=creds)

    def create_console_record(self, provider, host, hostname, ip_address):
        from app.services.console_dns import upsert_dns_record

        creds = provider.get_credentials()
        upsert_dns_record(
            hostname, ip_address, provider.console_zone_id, credentials=creds
        )

    def delete_console_record(self, provider, host, hostname, ip_address):
        from app.services.console_dns import delete_dns_record

        creds = provider.get_credentials()
        delete_dns_record(
            hostname, ip_address, provider.console_zone_id, credentials=creds
        )

    def get_host_powerstate(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        client = _get_ec2_client(credentials=creds)
        desc = client.describe_instances(InstanceIds=[instance_id])
        inst = desc["Reservations"][0]["Instances"][0]
        return inst["State"]["Name"]

    def start_host(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        client = _get_ec2_client(credentials=creds)
        client.start_instances(InstanceIds=[instance_id])

    def stop_host(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        client = _get_ec2_client(credentials=creds)
        client.stop_instances(InstanceIds=[instance_id])
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py -v`
Expected: PASS

- [ ] **Step 8: Run full test suite to verify no regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All existing tests PASS

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/ src/backend/tests/test_provider_driver.py
git commit -m "refactor: add provider driver abstraction with EC2 delegate"
```

---

### Task 2: Update hosts.py provision/terminate to use driver

**Files:**
- Modify: `src/backend/app/api/hosts.py:190-220` (provision)
- Modify: `src/backend/app/api/hosts.py:1050-1080` (terminate)
- Modify: `src/backend/app/api/hosts.py:1088-1120` (_wait_terminated)

This task updates the two most critical call sites: provisioning and termination. Other call sites (resize, powerstate) follow in subsequent tasks.

- [ ] **Step 1: Update provision call in hosts.py**

In `src/backend/app/api/hosts.py`, find the `provision_host()` call around line 204. Replace the direct provisioner call with the driver:

Replace:
```python
    try:
        result = provision_host(
            instance_type=body.instance_type,
            ami_id=body.ami_id or provider.default_ami,
            region=region,
            credentials=creds,
            vpc_id=provider.vpc_id,
            subnet_id=subnet_override or provider.subnet_id,
            security_group_id=provider.security_group_id,
            subnet_override=subnet_override,
            console_zone_id=provider.console_zone_id,
            **nfs_kwargs,
        )
```

With:
```python
    try:
        from app.services.providers import get_provider_driver

        driver = get_provider_driver(provider)
        result = driver.provision_host(
            provider=provider,
            host_id=str(uuid.uuid4()),
            instance_type=body.instance_type,
            storage_size_gb=500,
            ami_id=body.ami_id,
            region=region,
            subnet_override=subnet_override,
            host_type="shared",
            **nfs_kwargs,
        )
```

Also add `import uuid` at the top if not already present.

- [ ] **Step 2: Update terminate call in hosts.py**

Find the `terminate_host()` call around line 1072. Replace:

```python
            terminate_host(host.instance_id, credentials=creds)
```

With:
```python
            from app.services.providers import get_provider_driver

            driver = get_provider_driver(host.provider)
            driver.terminate_host(host.provider, host.instance_id)
```

- [ ] **Step 3: Update _wait_terminated thread in hosts.py**

Find the `_wait_terminated()` inner function around line 1090. Replace the direct `_get_ec2_client` and `get_host_status` calls:

Replace:
```python
        from app.services.provisioner import _get_ec2_client, get_host_status
```

With:
```python
        from app.services.providers import get_provider_driver
```

And replace `get_host_status(instance_id, credentials=creds)` with:
```python
        driver = get_provider_driver(prov)
        # ... inside the loop:
        status = driver.get_host_status(prov, instance_id)
```

And replace `client = _get_ec2_client(credentials=creds)` / `client.delete_key_pair(KeyName=h.key_pair_name)` — keep this EC2-specific for now (key pair deletion is provider-specific cleanup, will be handled in driver later).

- [ ] **Step 4: Update console DNS calls in delete endpoint**

Find the console DNS cleanup around line 1051. Replace:
```python
            from app.services.console_dns import delete_dns_record
            ...
            delete_dns_record(host.console_domain, host.ip_address, ...)
```

With:
```python
            from app.services.providers import get_provider_driver
            driver = get_provider_driver(prov)
            driver.delete_console_record(prov, host, host.console_domain, host.ip_address)
```

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests PASS — behavior is identical, just routed through the driver

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py
git commit -m "refactor: route host provision/terminate through provider driver"
```

---

### Task 3: Update hosts.py remaining EC2 calls (resize, powerstate, extend)

**Files:**
- Modify: `src/backend/app/api/hosts.py:610-710` (powerstate endpoints)
- Modify: `src/backend/app/api/hosts.py:830-900` (resize/migrate)
- Modify: `src/backend/app/api/hosts.py:950-960` (extend-ebs)

- [ ] **Step 1: Update powerstate endpoints**

Find the poweroff/poweron endpoints that call `_get_ec2_client()` directly. Replace each `_get_ec2_client` + `stop_instances`/`start_instances` block with the driver:

```python
from app.services.providers import get_provider_driver
driver = get_provider_driver(host.provider)
driver.stop_host(host.provider, host.instance_id)   # for poweroff
driver.start_host(host.provider, host.instance_id)  # for poweron
```

- [ ] **Step 2: Update resize endpoint**

Find the resize endpoint that calls `resize_instance()`. Replace with:

```python
from app.services.providers import get_provider_driver
driver = get_provider_driver(host.provider)
result = driver.resize_host(host.provider, host.instance_id, body.instance_type)
```

- [ ] **Step 3: Update extend-ebs endpoint**

Find the extend-ebs endpoint that calls `extend_host_ebs()`. Replace with:

```python
from app.services.providers import get_provider_driver
driver = get_provider_driver(host.provider)
result = driver.extend_host_storage(host.provider, host, db, increment_gb=body.increment_gb)
```

- [ ] **Step 4: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py
git commit -m "refactor: route resize, powerstate, extend through provider driver"
```

---

### Task 4: Update health_poller.py to use driver for auto-extend

**Files:**
- Modify: `src/backend/app/services/health_poller.py:150-200`

- [ ] **Step 1: Update host auto-extend call**

Find lines 150-166 in health_poller.py. Replace:
```python
from app.services.storage_extend import extend_host_ebs, should_extend_host
if should_extend_host(host):
    logger.info("Auto-extending EBS for host %s", host.id[:8])
    extend_host_ebs(host, db)
```

With:
```python
from app.services.storage_extend import should_extend_host
if should_extend_host(host) and host.provider:
    from app.services.providers import get_provider_driver
    driver = get_provider_driver(host.provider)
    logger.info("Auto-extending storage for host %s", host.id[:8])
    driver.extend_host_storage(host.provider, host, db)
```

- [ ] **Step 2: Leave pool FSx extend as-is**

The FSx pool auto-extend (lines 174-198) stays unchanged for now — it's pool-level, not host-level, and only applies to `shared-fsx` mode. No need to abstract it yet.

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/health_poller.py
git commit -m "refactor: route health poller auto-extend through provider driver"
```

---

### Task 5: Update pattern_buffer_service.py to use driver

**Files:**
- Modify: `src/backend/app/services/pattern_buffer_service.py`

- [ ] **Step 1: Replace _find_ec2_provider() calls with pool.provider**

The `_find_ec2_provider()` function finds a provider by looking up hosts in the pool. This should instead use `pool.provider` directly (the pool already has `provider_id`). Update all 4 call sites:

At line 68, 231, 256, 292 — replace:
```python
provider = _find_ec2_provider(pool, db)
```
With:
```python
provider = pool.provider
```

Then replace each subsequent `provision_host()` call with the driver:
```python
from app.services.providers import get_provider_driver
driver = get_provider_driver(provider)
result = driver.provision_host(
    provider=provider,
    host_id=host_id,
    instance_type=instance_type,
    storage_size_gb=storage_size_gb,
    host_type="pattern_buffer",
    ...
)
```

- [ ] **Step 2: Replace terminate_host() call**

Replace the direct `terminate_host()` call with:
```python
driver = get_provider_driver(provider)
driver.terminate_host(provider, host.instance_id)
```

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/pattern_buffer_service.py
git commit -m "refactor: route pattern buffer provisioning through provider driver"
```

---

### Task 6: Update providers.py console setup to use driver

**Files:**
- Modify: `src/backend/app/api/providers.py:705+` (setup-console endpoint)

- [ ] **Step 1: Update setup-console endpoint**

The `setup-console` endpoint currently calls Route53 APIs directly. Wrap it to go through the driver's `setup_console()` method for future OCP Virt support. For now the EC2 driver delegates to the existing `console_dns.py` code.

- [ ] **Step 2: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py
git commit -m "refactor: route console setup through provider driver"
```

---

### Task 7: Verify complete Phase 1 — full regression test

**Files:** None (verification only)

- [ ] **Step 1: Run full backend test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS, zero regressions

- [ ] **Step 2: Verify imports are clean**

Run: `cd src/backend && grep -rn "from app.services.provisioner import" app/api/ app/services/ | grep -v providers/ec2.py`

This should show only `providers/ec2.py` importing from provisioner. If any API or service module still imports from provisioner directly, it needs to go through the driver.

Exceptions allowed:
- `provisioner.py` internal imports (itself)
- `providers/ec2.py` (the delegate)

- [ ] **Step 3: Commit final Phase 1 cleanup if needed**

```bash
cd /Users/prutledg/troshka && git add -A && git commit -m "refactor: complete Phase 1 provider abstraction"
```

---

## Phase 2: OCP Virt Provider Implementation

### Task 8: Add kubernetes dependency and database migration

**Files:**
- Modify: `src/backend/requirements.txt`
- Create: `src/backend/alembic/versions/XXXX_add_ceph_subvolume_to_storage_pools.py`
- Modify: `src/backend/app/models/storage_pool.py`

- [ ] **Step 1: Add kubernetes package**

Add to `src/backend/requirements.txt`:
```
kubernetes>=29.0.0
```

Install:
```bash
cd src/backend && ./venv/bin/pip install kubernetes
```

- [ ] **Step 2: Add ceph_subvolume_group column to StoragePool model**

In `src/backend/app/models/storage_pool.py`, add after `nfs_endpoint`:

```python
    ceph_subvolume_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 3: Create Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add ceph subvolume group to storage pools"
```

Edit the generated migration file. In `upgrade()`:
```python
    op.add_column("storage_pools", sa.Column("ceph_subvolume_group", sa.String(255), nullable=True))
```

In `downgrade()`:
```python
    op.drop_column("storage_pools", "ceph_subvolume_group")
```

- [ ] **Step 4: Run migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/requirements.txt src/backend/app/models/storage_pool.py src/backend/alembic/versions/
git commit -m "feat: add kubernetes dependency and ceph_subvolume_group column"
```

---

### Task 9: Implement OCP Virt driver — k8s client and provision_host

**Files:**
- Create: `src/backend/app/services/providers/ocpvirt.py`
- Create: `src/backend/tests/test_ocpvirt_driver.py`

- [ ] **Step 1: Write test for OCP Virt provision_host**

Create `src/backend/tests/test_ocpvirt_driver.py`:

```python
from unittest.mock import MagicMock, patch

import pytest


def _make_provider():
    provider = MagicMock()
    provider.type = "ocpvirt"
    provider.get_credentials.return_value = {
        "api_url": "https://api.test.example.com:6443",
        "token": "sha256~testtoken",
        "namespace": "troshka",
        "verify_ssl": False,
    }
    return provider


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_provision_host_creates_vm(mock_clients):
    from app.services.providers.ocpvirt import OCPVirtDriver

    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    # Simulate VMI reaching Running with a pod IP
    mock_custom.get_namespaced_custom_object.return_value = {
        "status": {
            "phase": "Running",
            "interfaces": [{"ipAddress": "10.128.2.50"}],
        }
    }
    # NodePort service
    mock_core.read_namespaced_service.return_value = MagicMock(
        spec=MagicMock(ports=[MagicMock(node_port=32100)])
    )
    # Node IP
    mock_core.list_node.return_value = MagicMock(
        items=[
            MagicMock(
                status=MagicMock(
                    addresses=[
                        MagicMock(type="InternalIP", address="192.168.1.10")
                    ]
                )
            )
        ]
    )

    driver = OCPVirtDriver()
    provider = _make_provider()
    result = driver.provision_host(
        provider=provider,
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        instance_type="64c-256g",
        storage_size_gb=500,
    )

    assert result["host_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert result["private_ip"] == "10.128.2.50"
    assert "private_key" in result
    assert result["total_vcpus"] == 64
    assert result["total_ram_mb"] == 256 * 1024
    mock_custom.create_namespaced_custom_object.assert_called_once()


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_terminate_host_deletes_vm(mock_clients):
    from app.services.providers.ocpvirt import OCPVirtDriver

    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.terminate_host(provider, "troshka-host-aaaaaaaa")

    mock_custom.delete_namespaced_custom_object.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocpvirt_driver.py -v`
Expected: FAIL — OCPVirtDriver not found

- [ ] **Step 3: Implement ocpvirt.py**

Create `src/backend/app/services/providers/ocpvirt.py`:

```python
"""OCP Virt (KubeVirt) provider driver.

Creates large nested-virt RHEL VMs on OpenShift Virtualization.
The VMs run troshkad identically to EC2 instances.
"""

import logging
import time
import uuid

from kubernetes import client

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CLOUD_INIT_TEMPLATE = """#cloud-config
user: ec2-user
ssh_authorized_keys:
  - {ssh_pubkey}
packages:
  - qemu-kvm
  - libvirt
  - nfs-utils
  - python3
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
  - systemctl enable --now libvirtd
  - mkdir -p /var/lib/troshka /etc/troshka-agent
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""

CLOUD_INIT_PATTERN_BUFFER = """#cloud-config
user: ec2-user
ssh_authorized_keys:
  - {ssh_pubkey}
packages:
  - qemu-img
  - nfs-utils
  - python3
write_files:
  - path: /etc/resolv.conf
    content: |
      search troshka.svc.cluster.local svc.cluster.local cluster.local
      nameserver 172.30.0.10
      options ndots:5
    permissions: '0644'
runcmd:
  - mkdir -p /var/lib/troshka /etc/troshka-agent
  - 'echo "host_id: {host_id}" > /etc/troshka-agent/host-id'
"""


def _get_k8s_clients(credentials):
    configuration = client.Configuration()
    configuration.host = credentials["api_url"]
    configuration.api_key = {"authorization": f"Bearer {credentials['token']}"}
    configuration.verify_ssl = credentials.get("verify_ssl", False)
    api_client = client.ApiClient(configuration)
    custom_api = client.CustomObjectsApi(api_client)
    core_api = client.CoreV1Api(api_client)
    return custom_api, core_api


def _parse_instance_type(instance_type):
    """Parse '64c-256g' into (cores, memory_gi).
    Also accepts 'NNc-NNNg' format."""
    if not instance_type or "-" not in instance_type:
        return 64, 256
    parts = instance_type.replace("c-", " ").replace("g", "").split()
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 64, 256


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
    public_key = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()
    return private_pem, public_key


class OCPVirtDriver(ProviderDriver):
    def provision_host(self, provider, host_id, instance_type, storage_size_gb, **kwargs):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        host_type = kwargs.get("host_type", "shared")
        cores, memory_gi = _parse_instance_type(instance_type)
        hostname = f"troshka-host-{host_id[:8]}"
        private_key, public_key = _generate_ssh_keypair()

        # Ensure namespace exists
        try:
            core_api.read_namespace(namespace)
        except client.ApiException as e:
            if e.status == 404:
                core_api.create_namespace(
                    client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
                )

        # Build cloud-init
        template = CLOUD_INIT_PATTERN_BUFFER if host_type == "pattern_buffer" else CLOUD_INIT_TEMPLATE
        nfs_server = kwargs.get("nfs_server")
        nfs_path = kwargs.get("nfs_path")
        nfs_runcmd = ""
        if nfs_server and nfs_path:
            nfs_runcmd = (
                f"\n  - mkdir -p /var/lib/troshka/shared"
                f"\n  - 'echo \"{nfs_server}:{nfs_path} /var/lib/troshka/shared nfs defaults,nconnect=16 0 0\" >> /etc/fstab'"
                f"\n  - mount /var/lib/troshka/shared"
            )

        user_data = template.format(
            ssh_pubkey=public_key,
            host_id=host_id,
        )
        if nfs_runcmd:
            user_data = user_data.rstrip() + nfs_runcmd + "\n"

        # Build VM spec
        data_volumes = [
            {
                "metadata": {"name": f"{hostname}-root"},
                "spec": {
                    "source": {"http": {"url": kwargs.get("rhel_image_url", "")}},
                    "storage": {
                        "resources": {"requests": {"storage": f"{storage_size_gb}Gi"}},
                        "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                    },
                },
            }
        ]

        disks = [
            {"disk": {"bus": "virtio"}, "name": "rootdisk"},
            {"disk": {"bus": "virtio"}, "name": "cloudinitdisk"},
        ]
        volumes = [
            {"dataVolume": {"name": f"{hostname}-root"}, "name": "rootdisk"},
            {"cloudInitNoCloud": {"userData": user_data}, "name": "cloudinitdisk"},
        ]

        if host_type == "pattern_buffer":
            data_volumes.append({
                "metadata": {"name": f"{hostname}-scratch"},
                "spec": {
                    "source": {"blank": {}},
                    "storage": {
                        "resources": {"requests": {"storage": "500Gi"}},
                        "storageClassName": "ocs-storagecluster-ceph-rbd-virtualization",
                    },
                },
            })
            disks.append({"disk": {"bus": "virtio"}, "name": "scratch"})
            volumes.append({"dataVolume": {"name": f"{hostname}-scratch"}, "name": "scratch"})

        vm_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": hostname,
                "namespace": namespace,
                "labels": {
                    "app": "troshka",
                    "troshka/host-id": host_id,
                    "troshka/host-type": host_type,
                },
            },
            "spec": {
                "running": True,
                "dataVolumeTemplates": data_volumes,
                "template": {
                    "metadata": {
                        "labels": {
                            "kubevirt.io/domain": hostname,
                            "app": "troshka",
                        }
                    },
                    "spec": {
                        "domain": {
                            "cpu": {
                                "cores": cores,
                                "sockets": 1,
                                "threads": 1,
                                "model": "host-passthrough",
                            },
                            "memory": {"guest": f"{memory_gi}Gi"},
                            "devices": {
                                "disks": disks,
                                "interfaces": [
                                    {"masquerade": {}, "model": "virtio", "name": "default"}
                                ],
                                "rng": {},
                            },
                            "features": {"kvm": {"hidden": False}},
                        },
                        "networks": [{"name": "default", "pod": {}}],
                        "volumes": volumes,
                        "terminationGracePeriodSeconds": 180,
                    },
                },
            },
        }

        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=vm_manifest,
        )
        logger.info("Created VirtualMachine %s in namespace %s", hostname, namespace)

        # Create NodePort services for SSH (temporary) and troshkad (persistent)
        for svc_name, port, target_port in [
            (f"troshka-ssh-{host_id[:8]}", 22, 22),
            (f"troshka-agent-{host_id[:8]}", 31337, 31337),
        ]:
            svc = client.V1Service(
                metadata=client.V1ObjectMeta(
                    name=svc_name,
                    namespace=namespace,
                    labels={"app": "troshka", "troshka/host-id": host_id},
                ),
                spec=client.V1ServiceSpec(
                    type="NodePort",
                    selector={"kubevirt.io/domain": hostname},
                    ports=[
                        client.V1ServicePort(port=port, target_port=target_port)
                    ],
                ),
            )
            core_api.create_namespaced_service(namespace=namespace, body=svc)

        # Wait for VMI to reach Running
        pod_ip = None
        for attempt in range(120):
            time.sleep(5)
            try:
                vmi = custom_api.get_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=hostname,
                )
                phase = vmi.get("status", {}).get("phase")
                if phase == "Running":
                    interfaces = vmi.get("status", {}).get("interfaces", [])
                    if interfaces:
                        pod_ip = interfaces[0].get("ipAddress")
                    break
            except client.ApiException:
                pass
        else:
            raise RuntimeError(f"VM {hostname} did not reach Running state within 10 minutes")

        # Get node IP for NodePort access
        ssh_svc = core_api.read_namespaced_service(
            f"troshka-ssh-{host_id[:8]}", namespace
        )
        ssh_nodeport = ssh_svc.spec.ports[0].node_port

        agent_svc = core_api.read_namespaced_service(
            f"troshka-agent-{host_id[:8]}", namespace
        )
        agent_nodeport = agent_svc.spec.ports[0].node_port

        nodes = core_api.list_node()
        node_ip = None
        for node in nodes.items:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    node_ip = addr.address
                    break
            if node_ip:
                break

        return {
            "host_id": host_id,
            "instance_id": hostname,
            "instance_type": instance_type or f"{cores}c-{memory_gi}g",
            "public_ip": f"{node_ip}:{agent_nodeport}" if node_ip else None,
            "private_ip": pod_ip,
            "total_vcpus": cores,
            "total_ram_mb": memory_gi * 1024,
            "key_pair_name": None,
            "private_key": private_key,
            "storage_size_gb": storage_size_gb,
            "max_eips": 0,
            "_ssh_host": node_ip,
            "_ssh_port": ssh_nodeport,
        }

    def terminate_host(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)

        try:
            custom_api.delete_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=instance_id,
            )
        except client.ApiException as e:
            if e.status != 404:
                raise

        # Clean up services
        host_short = instance_id.replace("troshka-host-", "")
        for prefix in ["troshka-ssh-", "troshka-agent-", "troshka-vncd-", "troshka-console-"]:
            try:
                core_api.delete_namespaced_service(f"{prefix}{host_short}", namespace)
            except client.ApiException:
                pass

        # Clean up console Route
        try:
            custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"troshka-console-{host_short}",
            )
        except client.ApiException:
            pass

        logger.info("Terminated OCP Virt host %s", instance_id)

    def get_host_status(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)

        try:
            vmi = custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=instance_id,
            )
            phase = vmi.get("status", {}).get("phase", "Unknown")
            interfaces = vmi.get("status", {}).get("interfaces", [])
            pod_ip = interfaces[0].get("ipAddress") if interfaces else None
            state_map = {
                "Running": "running",
                "Succeeded": "terminated",
                "Failed": "terminated",
                "Pending": "pending",
                "Scheduling": "pending",
            }
            return {
                "instance_id": instance_id,
                "state": state_map.get(phase, "unknown"),
                "public_ip": None,
                "private_ip": pod_ip,
            }
        except client.ApiException:
            return None

    def resize_host(self, provider, instance_id, new_instance_type):
        raise NotImplementedError("Resize is not supported for OCP Virt hosts")

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        _, core_api = _get_k8s_clients(creds)

        hostname = host.instance_id
        pvc_name = f"{hostname}-root"
        increment = increment_gb or host.auto_extend_increment_gb
        new_size = host.storage_size_gb + increment

        if host.auto_extend_max_gb:
            new_size = min(new_size, host.auto_extend_max_gb)
        if new_size <= host.storage_size_gb:
            raise ValueError(f"Cannot extend: already at max ({host.storage_size_gb} GB)")

        core_api.patch_namespaced_persistent_volume_claim(
            pvc_name,
            namespace,
            {"spec": {"resources": {"requests": {"storage": f"{new_size}Gi"}}}},
        )

        old_size = host.storage_size_gb
        host.storage_size_gb = new_size
        db.commit()
        logger.info("Extended PVC %s from %d to %d GB", pvc_name, old_size, new_size)
        return {"old_size_gb": old_size, "new_size_gb": new_size}

    def setup_console(self, provider, base_domain):
        return {
            "console_base_domain": base_domain,
            "console_zone_id": None,
            "console_nameservers": None,
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)
        host_short = host.instance_id.replace("troshka-host-", "")

        # Create vncd Service (plain WebSocket on 8080)
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=f"troshka-vncd-{host_short}",
                namespace=namespace,
            ),
            spec=client.V1ServiceSpec(
                selector={"kubevirt.io/domain": host.instance_id},
                ports=[client.V1ServicePort(port=8080, target_port=8080)],
            ),
        )
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc)
        except client.ApiException as e:
            if e.status != 409:
                raise

        # Create edge-terminated Route
        route = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": f"troshka-console-{host_short}",
                "namespace": namespace,
            },
            "spec": {
                "host": hostname,
                "to": {
                    "kind": "Service",
                    "name": f"troshka-vncd-{host_short}",
                },
                "port": {"targetPort": 8080},
                "tls": {
                    "termination": "edge",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        }
        try:
            custom_api.create_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route,
            )
        except client.ApiException as e:
            if e.status != 409:
                raise

    def delete_console_record(self, provider, host, hostname, ip_address):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, core_api = _get_k8s_clients(creds)
        host_short = host.instance_id.replace("troshka-host-", "")

        try:
            core_api.delete_namespaced_service(f"troshka-vncd-{host_short}", namespace)
        except client.ApiException:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"troshka-console-{host_short}",
            )
        except client.ApiException:
            pass

    def get_host_powerstate(self, provider, instance_id):
        status = self.get_host_status(provider, instance_id)
        return status["state"] if status else "unknown"

    def start_host(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)
        custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body={"spec": {"running": True}},
        )

    def stop_host(self, provider, instance_id):
        creds = provider.get_credentials()
        namespace = creds.get("namespace", "troshka")
        custom_api, _ = _get_k8s_clients(creds)
        custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=instance_id,
            body={"spec": {"running": False}},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocpvirt_driver.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/ocpvirt.py src/backend/tests/test_ocpvirt_driver.py
git commit -m "feat: implement OCP Virt provider driver"
```

---

### Task 10: Update provider API to accept OCP Virt type

**Files:**
- Modify: `src/backend/app/api/providers.py`

- [ ] **Step 1: Update ProviderCreate schema**

Add OCP Virt fields. The schema should accept:
- `type`: `"ec2"` or `"ocpvirt"` (existing field, just allow new value)
- `api_url`: k8s API URL (OCP Virt only)
- `token`: service account token (OCP Virt only)
- `namespace`: target namespace (OCP Virt only, default `"troshka"`)
- `verify_ssl`: bool (OCP Virt only, default `False`)

Add these as optional fields to ProviderCreate. In the create endpoint, store them in credentials JSON for OCP Virt providers.

- [ ] **Step 2: Gate AWS-specific operations on provider type**

In the VPC, AMI, and infrastructure endpoints, add guards:
```python
if provider.type != "ec2":
    raise HTTPException(status_code=400, detail="This operation is only available for EC2 providers")
```

Affected endpoints: `discover-ami`, `set-ami`, `discover-vpcs`, `create-vpc`, `setup-infra`, `validate-vpc`.

- [ ] **Step 3: Update setup-console endpoint for OCP Virt**

For OCP Virt providers, `setup-console` just stores the base domain (derived from API URL):
```python
if provider.type == "ocpvirt":
    # Derive apps domain from API URL: api.cluster.example.com → apps.cluster.example.com
    api_host = creds.get("api_url", "").replace("https://", "").split(":")[0]
    apps_domain = api_host.replace("api.", "apps.", 1)
    provider.console_base_domain = body.base_domain or apps_domain
    db.commit()
    return {"console_base_domain": provider.console_base_domain}
```

- [ ] **Step 4: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/providers.py
git commit -m "feat: accept OCP Virt provider type in API"
```

---

### Task 11: Update hosts.py for OCP Virt provisioning flow

**Files:**
- Modify: `src/backend/app/api/hosts.py`

- [ ] **Step 1: Handle OCP Virt SSH access in _auto_install**

The `_auto_install()` inner function needs to use the NodePort SSH endpoint for OCP Virt hosts. The `provision_host()` result includes `_ssh_host` and `_ssh_port` for OCP Virt. Pass these through:

```python
ssh_host = result.get("_ssh_host") or result.get("public_ip")
ssh_port = result.get("_ssh_port", 22)
```

Update the `wait_for_ssh()` and `deploy_agent()` calls to use `ssh_host:ssh_port`.

- [ ] **Step 2: Set console_domain for OCP Virt hosts**

After provisioning, if the provider is OCP Virt and has `console_base_domain`:
```python
if provider.type == "ocpvirt" and provider.console_base_domain:
    host.console_domain = f"{host_id[:8]}.{provider.console_base_domain}"
    # Create the Route + Service
    from app.services.providers import get_provider_driver
    driver = get_provider_driver(provider)
    driver.create_console_record(provider, host, host.console_domain, None)
```

- [ ] **Step 3: Delete SSH NodePort after agent connects**

After agent installation succeeds, delete the temporary SSH service:
```python
if provider.type == "ocpvirt":
    try:
        creds_val = provider_creds
        from app.services.providers.ocpvirt import _get_k8s_clients
        _, core_api = _get_k8s_clients(creds_val)
        core_api.delete_namespaced_service(
            f"troshka-ssh-{host_id[:8]}",
            creds_val.get("namespace", "troshka"),
        )
    except Exception:
        pass
```

- [ ] **Step 4: Gate EIP features on provider type**

In any endpoint that manages EIPs or external access, add:
```python
if provider.type != "ec2":
    raise HTTPException(status_code=400, detail="EIPs are not available for this provider type")
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/hosts.py
git commit -m "feat: OCP Virt host provisioning with NodePort SSH and console Routes"
```

---

### Task 12: Add shared-ceph-nfs storage pool mode

**Files:**
- Modify: `src/backend/app/api/storage_pools.py`

- [ ] **Step 1: Accept shared-ceph-nfs mode**

In the create pool endpoint, add handling for `mode="shared-ceph-nfs"`:

```python
elif body.mode == "shared-ceph-nfs":
    # Create a PVC using the Ceph-NFS storage class
    from app.services.providers.ocpvirt import _get_k8s_clients

    provider = db.query(Provider).get(body.provider_id)
    creds = provider.get_credentials()
    _, core_api = _get_k8s_clients(creds)
    namespace = creds.get("namespace", "troshka")

    pvc_name = f"troshka-pool-{pool.id[:8]}"
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=pvc_name, namespace=namespace),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            resources=client.V1ResourceRequirements(
                requests={"storage": f"{body.storage_gb or 1000}Gi"}
            ),
            storage_class_name="ocs-storagecluster-ceph-nfs",
        ),
    )
    core_api.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc)

    # Wait for PVC to bind and extract NFS server + path from PV
    # (store in nfs_endpoint for compatibility with shared-byo code path)
    pool.nfs_endpoint = f"rook-ceph-nfs-ocs-storagecluster-cephnfs-a.openshift-storage.svc.cluster.local:/{pvc_name}"
    pool.ceph_subvolume_group = pvc_name
    pool.status = "active"
```

- [ ] **Step 2: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/storage_pools.py
git commit -m "feat: add shared-ceph-nfs storage pool mode"
```

---

### Task 13: Add --no-tls flag to troshka-vncd

**Files:**
- Modify: `src/troshka-vncd/troshka-vncd.py`

- [ ] **Step 1: Add --no-tls argument**

In the argparse section of vncd, add:
```python
parser.add_argument("--no-tls", action="store_true", help="Listen for plain WebSocket (TLS handled externally)")
parser.add_argument("--plain-port", type=int, default=8080, help="Port for plain WebSocket when --no-tls is set")
```

- [ ] **Step 2: Update server startup**

In the main server startup, check the flag:
```python
if args.no_tls:
    server = await websockets.serve(handler, "0.0.0.0", args.plain_port)
    logger.info("vncd listening on ws://0.0.0.0:%d (plain, TLS handled externally)", args.plain_port)
else:
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(args.cert, args.key)
    server = await websockets.serve(handler, "0.0.0.0", args.port, ssl=ssl_context)
    logger.info("vncd listening on wss://0.0.0.0:%d", args.port)
```

- [ ] **Step 3: Update agent deployer to pass --no-tls for OCP Virt**

In `agent_deployer.py`, the vncd systemd unit creation should check the host's provider type and add `--no-tls` to the ExecStart if on OCP Virt. This can be passed as a parameter to `deploy_agent()`:

Add `vncd_no_tls: bool = False` parameter. When True, the vncd systemd unit includes `--no-tls` in ExecStart.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshka-vncd/troshka-vncd.py src/backend/app/services/agent_deployer.py
git commit -m "feat: add --no-tls flag to vncd for OCP Virt console access"
```

---

### Task 14: Frontend — provider type selector and OCP Virt fields

**Files:**
- Modify: `src/frontend/src/app/admin/providers/page.tsx`

- [ ] **Step 1: Add OCP Virt to provider type dropdown**

In the provider create form, add `"ocpvirt"` as an option with label "OCP Virt":
```tsx
<FormSelectOption value="ocpvirt" label="OCP Virt (OpenShift Virtualization)" />
```

- [ ] **Step 2: Show conditional fields based on type**

When `type === "ocpvirt"`, show:
- API URL (text input)
- Token (password input)
- Namespace (text input, default "troshka")
- Verify SSL (checkbox, default false)

When `type === "ec2"`, show existing AWS fields (region, access key, secret key).

- [ ] **Step 3: Hide AWS-specific buttons for OCP Virt providers**

Conditionally hide: "Discover AMI", "Setup VPC", "Setup Infrastructure" buttons when `provider.type === "ocpvirt"`.

Show "Setup Console" for both types (it works differently but same UX).

- [ ] **Step 4: Test in browser**

Start dev server, navigate to `/admin/providers`, verify:
- Can create OCP Virt provider with API URL + token
- AWS-specific buttons are hidden
- Console setup works

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/providers/page.tsx
git commit -m "feat: OCP Virt provider type in admin UI"
```

---

### Task 15: Frontend — host form for OCP Virt

**Files:**
- Modify: `src/frontend/src/app/admin/hosts/page.tsx`

- [ ] **Step 1: Detect provider type for host form**

When the selected provider is OCP Virt, show CPU + Memory fields instead of instance type dropdown:

```tsx
{selectedProvider?.type === "ocpvirt" ? (
  <>
    <FormGroup label="CPU Cores" fieldId="cpu-cores">
      <TextInput id="cpu-cores" type="number" value={cpuCores} onChange={setCpuCores} />
    </FormGroup>
    <FormGroup label="Memory (GB)" fieldId="memory-gb">
      <TextInput id="memory-gb" type="number" value={memoryGb} onChange={setMemoryGb} />
    </FormGroup>
  </>
) : (
  <FormGroup label="Instance Type" fieldId="instance-type">
    {/* existing EC2 instance type dropdown */}
  </FormGroup>
)}
```

- [ ] **Step 2: Map CPU/memory to instance_type string**

When submitting the form for OCP Virt, construct `instance_type` as `"{cpu}c-{memory}g"`:
```tsx
const instanceType = selectedProvider?.type === "ocpvirt"
  ? `${cpuCores}c-${memoryGb}g`
  : selectedInstanceType;
```

- [ ] **Step 3: Add preset buttons**

Add quick-select buttons: "64c / 256G", "128c / 512G" that fill in the fields.

- [ ] **Step 4: Hide EIP/resize options for OCP Virt hosts**

Check `host.provider?.type` and hide resize button, EIP allocation, and external access toggles for OCP Virt hosts.

- [ ] **Step 5: Test in browser**

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/hosts/page.tsx
git commit -m "feat: CPU/memory input for OCP Virt hosts in admin UI"
```

---

### Task 16: Frontend — storage pool Ceph-NFS mode

**Files:**
- Modify: `src/frontend/src/app/admin/storage-pools/page.tsx`

- [ ] **Step 1: Add shared-ceph-nfs mode option**

In the pool create form mode dropdown:
```tsx
<FormSelectOption value="shared-ceph-nfs" label="Shared Ceph-NFS (OCP Virt)" />
```

- [ ] **Step 2: Show conditional fields**

When `mode === "shared-ceph-nfs"`:
- Storage quota (GB) — number input
- Provider dropdown (filter to OCP Virt providers only)

Hide FSx-specific fields (throughput, AZ).

- [ ] **Step 3: Test in browser**

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/admin/storage-pools/page.tsx
git commit -m "feat: shared-ceph-nfs mode in storage pool admin UI"
```

---

## Phase 3: Testing & Hardening

### Task 17: End-to-end test on ocpvdev01 cluster

**Files:** None (manual testing)

- [ ] **Step 1: Create OCP Virt provider via admin UI**

Navigate to `/admin/providers`, create:
- Name: `ocpvdev01`
- Type: OCP Virt
- API URL: `https://api.ocpvdev01.dal13.infra.demo.redhat.com:6443`
- Token: (service account token)
- Namespace: `troshka`

- [ ] **Step 2: Setup console**

Click "Setup Console" on the provider. Verify `console_base_domain` is set to `apps.ocpvdev01.dal13.infra.demo.redhat.com`.

- [ ] **Step 3: Create shared-ceph-nfs storage pool**

Create a storage pool:
- Mode: Shared Ceph-NFS
- Provider: ocpvdev01
- Quota: 500 GB

Verify PVC is created in `troshka` namespace.

- [ ] **Step 4: Provision a test host**

Add a host:
- Provider: ocpvdev01
- CPU: 8 (small for testing)
- Memory: 16 GB
- Storage pool: the one created above

Watch the VM come up, agent install, and connection.

- [ ] **Step 5: Deploy a small project**

Create a project with 1-2 small VMs, deploy to the OCP Virt host. Verify:
- VMs create and boot
- Console access works through the OCP Route
- NFS shared storage is mounted

- [ ] **Step 6: Test pattern buffer**

Provision a pattern buffer for the pool. Capture a pattern from the deployed project. Verify S3 upload works.

- [ ] **Step 7: Terminate and clean up**

Delete the project, terminate the host, verify all k8s resources are cleaned up (VMs, Services, Routes, PVCs).

---

### Task 18: Gate EIP features on provider type

**Files:**
- Modify: `src/backend/app/api/eips.py`
- Modify: `src/backend/app/services/deploy_service.py`

- [ ] **Step 1: Skip EIP allocation for non-EC2 providers**

In the deploy pipeline, skip EIP provisioning when the host's provider is not EC2. The deploy service should check:

```python
if host.provider and host.provider.type == "ec2":
    # existing EIP allocation logic
```

- [ ] **Step 2: Frontend: hide external access toggle for OCP Virt**

In the canvas topology editor, disable or hide the `externalAccess` toggle on gateway nodes when the project's target host is on an OCP Virt provider.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/eips.py src/backend/app/services/deploy_service.py src/frontend/
git commit -m "feat: gate EIP features on EC2 provider type"
```
