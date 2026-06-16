# OCP Virt EIPs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable external IP allocation for nested VMs on OCP Virt hosts via MetalLB LoadBalancer Services, with the same UX as EC2 EIPs.

**Architecture:** Refactor `eip_service.py` to dispatch through the `ProviderDriver` interface instead of calling boto3 directly. EC2 logic moves into `EC2Driver`. OCP Virt gets `OCPVirtDriver` EIP methods that create/delete LoadBalancer Services. A "transit port" scheme (40000-49999) maps user-facing ports to unique high ports so troshkad can DNAT without needing per-EIP private IPs. troshkad stays provider-agnostic — it checks `_transit_port` (OCP Virt) or `_private_ip` (EC2) on each port forward.

**Tech Stack:** Python 3.11, SQLAlchemy 2, kubernetes Python client, nftables, MetalLB

**Spec:** `docs/superpowers/specs/2026-06-16-ocpvirt-eips-design.md`

---

### Task 1: Alembic migration — add `port_map` and `max_eips` columns

**Files:**
- Create: `src/backend/alembic/versions/<auto>_add_eip_port_map_and_provider_max_eips.py`
- Modify: `src/backend/app/models/elastic_ip.py:1-34`
- Modify: `src/backend/app/models/provider.py:12-44`

- [ ] **Step 1: Add `port_map` column to ElasticIp model**

In `src/backend/app/models/elastic_ip.py`, add after the `tags` column (line 30):

```python
port_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

- [ ] **Step 2: Add `max_eips` column to Provider model**

In `src/backend/app/models/provider.py`, add after the `state` column (line 29). Requires importing `Integer`:

```python
from sqlalchemy import DateTime, Integer, String, Text, func
```

Then add the column:

```python
max_eips: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 3: Generate Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add eip port_map and provider max_eips"
```

Open the generated file and fill in:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

def upgrade():
    op.add_column("elastic_ips", sa.Column("port_map", postgresql.JSONB(), nullable=True))
    op.add_column("providers", sa.Column("max_eips", sa.Integer(), nullable=True))

def downgrade():
    op.drop_column("providers", "max_eips")
    op.drop_column("elastic_ips", "port_map")
```

- [ ] **Step 4: Run the migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 5: Run tests to verify models still work**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_models.py tests/test_eip_service.py -v
```

Expected: all pass (existing tests don't touch the new columns).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/elastic_ip.py src/backend/app/models/provider.py src/backend/alembic/versions/
git commit -m "feat: add port_map to ElasticIp and max_eips to Provider models"
```

---

### Task 2: Add EIP methods to ProviderDriver base class

**Files:**
- Modify: `src/backend/app/services/providers/base.py:1-67`

- [ ] **Step 1: Add the four EIP method stubs to `ProviderDriver`**

In `src/backend/app/services/providers/base.py`, add after the `delete_key_pair` method (line 66):

```python
def allocate_eip(self, provider, host, eip_id):
    """Allocate an external IP.
    Returns dict with keys: public_ip, allocation_id."""
    raise NotImplementedError

def associate_eip(self, provider, host, allocation_id):
    """Associate an EIP with a host.
    Returns dict with optional keys: private_ip, association_id.
    Empty dict if no association step needed (e.g. OCP Virt)."""
    raise NotImplementedError

def release_eip(self, provider, allocation_id, namespace=None):
    """Release an external IP and clean up infra resources.
    namespace is provider-specific context (k8s namespace for OCP Virt)."""
    raise NotImplementedError

def update_eip_ports(self, provider, host, allocation_id, ports):
    """Update port mappings on an EIP infra resource.
    ports is a list of dicts: [{port, targetPort, name}].
    No-op for providers that don't need it."""
    pass
```

- [ ] **Step 2: Run provider driver tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py -v
```

Expected: all pass (stubs don't break existing interface).

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/base.py
git commit -m "feat: add EIP lifecycle methods to ProviderDriver interface"
```

---

### Task 3: Implement EC2Driver EIP methods (extract from eip_service)

**Files:**
- Modify: `src/backend/app/services/providers/ec2.py:1-106`
- Test: `src/backend/tests/test_provider_driver.py`

- [ ] **Step 1: Write tests for EC2Driver EIP methods**

Add to `src/backend/tests/test_provider_driver.py`:

```python
from unittest.mock import patch

@patch("app.services.provisioner._get_ec2_client")
def test_ec2_allocate_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-test123",
        "PublicIp": "54.1.2.3",
    }
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.id = "prov-123"
    provider.default_region = "us-east-1"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    host = MagicMock()
    host.instance_id = "i-abc123"

    driver = EC2Driver()
    result = driver.allocate_eip(provider, host, "eip-uuid-1234")

    assert result["public_ip"] == "54.1.2.3"
    assert result["allocation_id"] == "eipalloc-test123"
    mock_ec2.allocate_address.assert_called_once_with(Domain="vpc")
    mock_ec2.create_tags.assert_called_once()


@patch("app.services.provisioner._get_ec2_client")
def test_ec2_associate_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_instances.return_value = {
        "Reservations": [{
            "Instances": [{
                "NetworkInterfaces": [{
                    "NetworkInterfaceId": "eni-primary",
                    "Attachment": {"DeviceIndex": 0},
                }]
            }]
        }]
    }
    mock_ec2.assign_private_ip_addresses.return_value = {
        "AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.50"}]
    }
    mock_ec2.associate_address.return_value = {"AssociationId": "eipassoc-xyz"}
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    host = MagicMock()
    host.instance_id = "i-abc123"

    driver = EC2Driver()
    result = driver.associate_eip(provider, host, "eipalloc-test123")

    assert result["private_ip"] == "10.0.1.50"
    assert result["association_id"] == "eipassoc-xyz"


@patch("app.services.provisioner._get_ec2_client")
def test_ec2_release_eip(mock_get_client):
    mock_ec2 = MagicMock()
    mock_get_client.return_value = mock_ec2

    provider = MagicMock()
    provider.type = "ec2"
    provider.get_credentials.return_value = {
        "access_key_id": "fake",
        "secret_access_key": "fake",
    }

    driver = EC2Driver()
    driver.release_eip(provider, "eipalloc-test123")

    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-test123")
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py::test_ec2_allocate_eip tests/test_provider_driver.py::test_ec2_associate_eip tests/test_provider_driver.py::test_ec2_release_eip -v
```

Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement EC2Driver EIP methods**

In `src/backend/app/services/providers/ec2.py`, add after the `delete_key_pair` method (line 105):

```python
def allocate_eip(self, provider, host, eip_id):
    from app.services.provisioner import _get_ec2_client

    creds = provider.get_credentials()
    ec2 = _get_ec2_client(credentials=creds)

    response = ec2.allocate_address(Domain="vpc")
    allocation_id = response["AllocationId"]
    public_ip = response["PublicIp"]

    tags = [
        {"Key": "ManagedBy", "Value": "troshka"},
        {"Key": "troshka-provider-id", "Value": provider.id},
        {"Key": "troshka-eip-id", "Value": eip_id},
    ]
    ec2.create_tags(Resources=[allocation_id], Tags=tags)

    return {"public_ip": public_ip, "allocation_id": allocation_id}

def associate_eip(self, provider, host, allocation_id):
    from app.services.provisioner import _get_ec2_client

    creds = provider.get_credentials()
    ec2 = _get_ec2_client(credentials=creds)

    desc = ec2.describe_instances(InstanceIds=[host.instance_id])
    eni_id = None
    for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
        if eni["Attachment"]["DeviceIndex"] == 0:
            eni_id = eni["NetworkInterfaceId"]
            break
    if not eni_id:
        raise ValueError(f"No primary ENI found for {host.instance_id}")

    assign_resp = ec2.assign_private_ip_addresses(
        NetworkInterfaceId=eni_id, SecondaryPrivateIpAddressCount=1
    )
    private_ip = assign_resp["AssignedPrivateIpAddresses"][0]["PrivateIpAddress"]

    assoc_resp = ec2.associate_address(
        AllocationId=allocation_id,
        NetworkInterfaceId=eni_id,
        PrivateIpAddress=private_ip,
    )

    return {
        "private_ip": private_ip,
        "association_id": assoc_resp["AssociationId"],
    }

def release_eip(self, provider, allocation_id, namespace=None):
    from app.services.provisioner import _get_ec2_client

    creds = provider.get_credentials()
    ec2 = _get_ec2_client(credentials=creds)
    ec2.release_address(AllocationId=allocation_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_provider_driver.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/ec2.py src/backend/tests/test_provider_driver.py
git commit -m "feat: implement EC2Driver EIP methods (allocate, associate, release)"
```

---

### Task 4: Implement OCPVirtDriver EIP methods

**Files:**
- Modify: `src/backend/app/services/providers/ocpvirt.py:1-694`
- Test: `src/backend/tests/test_ocpvirt_driver.py`

- [ ] **Step 1: Write tests for OCPVirtDriver EIP methods**

Add to `src/backend/tests/test_ocpvirt_driver.py`:

```python
@patch("app.services.providers.ocpvirt._get_k8s_clients")
@patch("app.services.providers.ocpvirt.time")
def test_allocate_eip_creates_lb_service(mock_time, mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    lb_svc = MagicMock()
    lb_ingress = MagicMock()
    lb_ingress.ip = "67.228.103.10"
    lb_svc.status.load_balancer.ingress = [lb_ingress]
    mock_core.read_namespaced_service.return_value = lb_svc

    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    host.instance_id = "troshka-host-aaaaaaaa"

    result = driver.allocate_eip(provider, host, "eip-uuid-1234")

    assert result["public_ip"] == "67.228.103.10"
    assert result["allocation_id"] == "troshka-eip-eip-uuid"
    mock_core.create_namespaced_service.assert_called_once()

    svc_call = mock_core.create_namespaced_service.call_args
    svc_body = svc_call[1]["body"]
    assert svc_body.spec.type == "LoadBalancer"
    assert svc_body.spec.selector == {"kubevirt.io/domain": "troshka-host-aaaaaaaa"}


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_release_eip_deletes_lb_service(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    driver.release_eip(provider, "troshka-eip-abcdefgh", namespace="troshka")

    mock_core.delete_namespaced_service.assert_called_once_with(
        "troshka-eip-abcdefgh", "troshka"
    )


@patch("app.services.providers.ocpvirt._get_k8s_clients")
def test_update_eip_ports_patches_service(mock_clients):
    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_clients.return_value = (mock_custom, mock_core)

    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    host.instance_id = "troshka-host-aaaaaaaa"

    ports = [
        {"port": 443, "targetPort": 40001, "name": "pf-0"},
        {"port": 8080, "targetPort": 40002, "name": "pf-1"},
    ]
    driver.update_eip_ports(provider, host, "troshka-eip-abcdefgh", ports)

    mock_core.patch_namespaced_service.assert_called_once()
    patch_call = mock_core.patch_namespaced_service.call_args
    assert patch_call[0][0] == "troshka-eip-abcdefgh"
    patch_body = patch_call[0][2] if len(patch_call[0]) > 2 else patch_call[1].get("body")
    spec_ports = patch_body["spec"]["ports"]
    assert len(spec_ports) == 2
    assert spec_ports[0]["port"] == 443
    assert spec_ports[0]["targetPort"] == 40001


def test_associate_eip_is_noop():
    driver = OCPVirtDriver()
    provider = _make_provider()
    host = MagicMock()
    result = driver.associate_eip(provider, host, "troshka-eip-abcdefgh")
    assert result == {}
```

- [ ] **Step 2: Run new tests to verify they fail**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocpvirt_driver.py::test_allocate_eip_creates_lb_service tests/test_ocpvirt_driver.py::test_release_eip_deletes_lb_service tests/test_ocpvirt_driver.py::test_update_eip_ports_patches_service tests/test_ocpvirt_driver.py::test_associate_eip_is_noop -v
```

Expected: FAIL with `AttributeError` (methods don't exist yet).

- [ ] **Step 3: Implement OCPVirtDriver EIP methods**

In `src/backend/app/services/providers/ocpvirt.py`, add after the `stop_host` method (line 694):

```python
def allocate_eip(self, provider, host, eip_id):
    from kubernetes import client

    creds = provider.get_credentials()
    namespace = creds.get("namespace", "troshka")
    _, core_api = _get_k8s_clients(creds)

    svc_name = f"troshka-eip-{eip_id[:8]}"
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=svc_name,
            namespace=namespace,
            labels={
                "app": "troshka",
                "troshka/eip-id": eip_id,
                "troshka/host-id": host.instance_id.replace("troshka-host-", ""),
            },
        ),
        spec=client.V1ServiceSpec(
            type="LoadBalancer",
            selector={"kubevirt.io/domain": host.instance_id},
            ports=[
                client.V1ServicePort(
                    name="placeholder", port=1, target_port=1, protocol="TCP"
                )
            ],
        ),
    )
    core_api.create_namespaced_service(namespace=namespace, body=svc)

    external_ip = None
    for _ in range(60):
        time.sleep(2)
        lb_svc = core_api.read_namespaced_service(svc_name, namespace)
        ingress = (lb_svc.status.load_balancer or {}).ingress
        if ingress and ingress[0].ip:
            external_ip = ingress[0].ip
            break
    if not external_ip:
        raise RuntimeError(f"MetalLB did not assign IP for {svc_name}")

    logger.info("Allocated EIP %s (%s) for host %s", external_ip, svc_name, host.instance_id)
    return {"public_ip": external_ip, "allocation_id": svc_name}

def associate_eip(self, provider, host, allocation_id):
    return {}

def release_eip(self, provider, allocation_id, namespace=None):
    from kubernetes import client

    creds = provider.get_credentials()
    ns = namespace or creds.get("namespace", "troshka")
    _, core_api = _get_k8s_clients(creds)

    try:
        core_api.delete_namespaced_service(allocation_id, ns)
        logger.info("Deleted EIP LB Service %s", allocation_id)
    except client.ApiException as e:
        if e.status != 404:
            raise

def update_eip_ports(self, provider, host, allocation_id, ports):
    from kubernetes import client

    creds = provider.get_credentials()
    namespace = creds.get("namespace", "troshka")
    _, core_api = _get_k8s_clients(creds)

    svc_ports = [
        {"port": p["port"], "targetPort": p["targetPort"], "name": p["name"], "protocol": "TCP"}
        for p in ports
    ]
    core_api.patch_namespaced_service(
        allocation_id, namespace, {"spec": {"ports": svc_ports}}
    )
    logger.info("Updated EIP %s ports: %s", allocation_id, [p["port"] for p in ports])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocpvirt_driver.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/ocpvirt.py src/backend/tests/test_ocpvirt_driver.py
git commit -m "feat: implement OCPVirtDriver EIP methods (MetalLB LB Services)"
```

---

### Task 5: Refactor eip_service.py to dispatch through provider driver

**Files:**
- Modify: `src/backend/app/services/eip_service.py:1-338`
- Modify: `src/backend/tests/test_eip_service.py:1-301`

This is the core refactoring. `eip_service.py` stops importing boto3 and dispatches through `get_provider_driver()`.

- [ ] **Step 1: Update tests to mock the driver instead of `_get_ec2_client`**

Rewrite `src/backend/tests/test_eip_service.py`. The existing tests mock `_get_ec2_client` (boto3). After the refactor, eip_service calls `get_provider_driver(provider)` which returns the driver, and the driver does the cloud API calls. The tests should mock the driver.

Replace the entire file with:

```python
"""Tests for EIP service lifecycle operations."""

import uuid
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.services import eip_service
from tests.conftest import TestSession

# Test data setup
_db = TestSession()
_user = User(
    email="eip-test@example.com",
    display_name="EIP Tester",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)

_provider = Provider(
    name="test-provider-eip",
    type="ec2",
    default_region="us-east-1",
)
_provider.set_credentials(
    {"access_key_id": "fake-key", "secret_access_key": "fake-secret"}
)
_db.add(_provider)
_db.commit()
_db.refresh(_provider)

_ocpvirt_provider = Provider(
    name="test-provider-ocpvirt",
    type="ocpvirt",
)
_ocpvirt_provider.set_credentials(
    {"api_url": "https://api.test:6443", "token": "fake", "namespace": "troshka"}
)
_db.add(_ocpvirt_provider)
_db.commit()
_db.refresh(_ocpvirt_provider)

_host = Host(
    provider_id=_provider.id,
    instance_id="i-test123",
    ip_address="10.0.1.100",
    private_key="test-key-not-real",
    state="running",
    max_eips=14,
)
_db.add(_host)
_db.commit()
_db.refresh(_host)

_provider_id = _provider.id
_ocpvirt_provider_id = _ocpvirt_provider.id
_host_id = _host.id
_db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_allocate_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_driver.allocate_eip.return_value = {
        "allocation_id": "eipalloc-abc123",
        "public_ip": "54.123.45.67",
    }
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    provider = db.query(Provider).filter_by(id=_provider_id).first()
    host = db.query(Host).filter_by(id=_host_id).first()
    project_id = str(uuid.uuid4())
    canvas_eip_id = f"eip-{uuid.uuid4()}"

    eip = eip_service.allocate_eip(db, provider, project_id, canvas_eip_id, host)

    mock_driver.allocate_eip.assert_called_once()
    assert eip.allocation_id == "eipalloc-abc123"
    assert eip.public_ip == "54.123.45.67"
    assert eip.state == "allocated"
    assert eip.host_id is None
    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_associate_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_driver.associate_eip.return_value = {
        "private_ip": "10.0.1.50",
        "association_id": "eipassoc-abc456",
    }
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    host = db.query(Host).filter_by(id=_host_id).first()
    provider = db.query(Provider).filter_by(id=_provider_id).first()
    eip = ElasticIp(
        provider_id=_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="eipalloc-assoc123",
        public_ip="54.11.22.33",
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    eip_service.associate_eip(db, eip, host)

    mock_driver.associate_eip.assert_called_once()
    db.refresh(eip)
    assert eip.state == "associated"
    assert eip.private_ip == "10.0.1.50"
    assert eip.host_id == _host_id
    assert eip.association_id == "eipassoc-abc456"
    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_release_eip(mock_get_driver):
    mock_driver = MagicMock()
    mock_get_driver.return_value = mock_driver

    db = TestSession()
    eip = ElasticIp(
        provider_id=_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="eipalloc-release123",
        public_ip="54.99.88.77",
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)
    eip_id = eip.id

    eip_service.release_eip(db, eip)

    mock_driver.release_eip.assert_called_once()
    assert db.query(ElasticIp).filter_by(id=eip_id).first() is None
    db.close()


def test_allocate_transit_ports():
    db = TestSession()
    host = db.query(Host).filter_by(id=_host_id).first()

    eip = ElasticIp(
        provider_id=_ocpvirt_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="troshka-eip-test1234",
        public_ip="67.228.103.10",
        state="associated",
        host_id=_host_id,
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    port_forwards = [
        {"extPort": 443},
        {"extPort": 8080},
    ]
    port_map = eip_service.allocate_transit_ports(db, eip, host, port_forwards)

    assert port_map["443"] == 40000
    assert port_map["8080"] == 40001

    db.refresh(eip)
    assert eip.port_map == port_map

    # Second EIP on same host should get non-overlapping ports
    eip2 = ElasticIp(
        provider_id=_ocpvirt_provider_id,
        project_id=str(uuid.uuid4()),
        canvas_eip_id=f"eip-{uuid.uuid4()}",
        allocation_id="troshka-eip-test5678",
        public_ip="67.228.103.11",
        state="associated",
        host_id=_host_id,
    )
    db.add(eip2)
    db.commit()
    db.refresh(eip2)

    port_map2 = eip_service.allocate_transit_ports(
        db, eip2, host, [{"extPort": 443}]
    )
    assert port_map2["443"] == 40002  # Skips 40000, 40001

    db.close()


@patch("app.services.eip_service.get_provider_driver")
def test_sync_sg_rules_noop_for_ocpvirt(mock_get_driver):
    db = TestSession()
    provider = db.query(Provider).filter_by(id=_ocpvirt_provider_id).first()
    result = eip_service.sync_security_group_rules(db, provider, [])
    assert result == {"added": 0, "removed": 0}
    mock_get_driver.assert_not_called()
    db.close()
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py -v
```

Expected: FAIL — `allocate_eip()` still has old signature (no `host` param), no `allocate_transit_ports` function, etc.

- [ ] **Step 3: Rewrite `eip_service.py`**

Replace the entire contents of `src/backend/app/services/eip_service.py`:

```python
"""EIP lifecycle management — allocate, associate, disassociate, release.

Dispatches cloud-specific operations through the ProviderDriver interface.
No cloud SDK imports in this module.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from app.services.providers import get_provider_driver

logger = logging.getLogger(__name__)

TRANSIT_PORT_START = 40000
TRANSIT_PORT_END = 49999


def allocate_eip(
    db: Session, provider: Provider, project_id: str, canvas_eip_id: str, host
) -> ElasticIp:
    """Allocate a new EIP via the provider driver."""
    import uuid

    eip_id = str(uuid.uuid4())
    driver = get_provider_driver(provider)
    result = driver.allocate_eip(provider, host, eip_id)

    eip = ElasticIp(
        id=eip_id,
        provider_id=provider.id,
        project_id=project_id,
        canvas_eip_id=canvas_eip_id,
        allocation_id=result["allocation_id"],
        public_ip=result["public_ip"],
        state="allocated",
    )
    db.add(eip)
    db.commit()
    db.refresh(eip)

    logger.info(
        "Allocated EIP %s (%s) for project %s",
        eip.public_ip, eip.allocation_id, project_id[:8],
    )
    return eip


def associate_eip(db: Session, eip: ElasticIp, host) -> None:
    """Associate an EIP with a host via the provider driver."""
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    driver = get_provider_driver(provider)
    result = driver.associate_eip(provider, host, eip.allocation_id)

    eip.private_ip = result.get("private_ip")
    eip.association_id = result.get("association_id")
    eip.host_id = host.id
    eip.state = "associated"
    db.commit()

    logger.info(
        "EIP %s associated to host %s", eip.public_ip, host.id[:8],
    )


def disassociate_eip(db: Session, eip: ElasticIp, host) -> None:
    """Disassociate an EIP from a host.

    For EC2: disassociates address and unassigns private IP via driver.
    For OCP Virt: no-op at infra level (LB Service stays, just DB update).
    """
    if eip.state != "associated":
        logger.warning("EIP %s is not associated, skipping", eip.id)
        return

    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    if provider.type == "ec2":
        driver = get_provider_driver(provider)
        if eip.association_id:
            from app.services.provisioner import _get_ec2_client

            creds = provider.get_credentials()
            ec2 = _get_ec2_client(credentials=creds)
            ec2.disassociate_address(AssociationId=eip.association_id)

            if eip.private_ip:
                desc = ec2.describe_instances(InstanceIds=[host.instance_id])
                for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
                    if eni["Attachment"]["DeviceIndex"] == 0:
                        ec2.unassign_private_ip_addresses(
                            NetworkInterfaceId=eni["NetworkInterfaceId"],
                            PrivateIpAddresses=[eip.private_ip],
                        )
                        break

    eip.private_ip = None
    eip.host_id = None
    eip.association_id = None
    eip.port_map = None
    eip.state = "allocated"
    db.commit()

    logger.info("EIP %s disassociated", eip.public_ip)


def release_eip(db: Session, eip: ElasticIp) -> None:
    """Release an EIP back to the provider."""
    if eip.state == "associated" and eip.host_id:
        from app.models.host import Host

        host = db.query(Host).filter_by(id=eip.host_id).first()
        if host:
            disassociate_eip(db, eip, host)

    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    if not provider:
        raise ValueError(f"Provider {eip.provider_id} not found")

    driver = get_provider_driver(provider)
    ns = None
    if provider.type == "ocpvirt":
        ns = provider.get_credentials().get("namespace", "troshka")
    driver.release_eip(provider, eip.allocation_id, namespace=ns)

    logger.info("Released EIP %s (%s)", eip.public_ip, eip.allocation_id)
    db.delete(eip)
    db.commit()


def migrate_eip(db: Session, eip: ElasticIp, from_host, to_host) -> None:
    """Migrate an EIP from one host to another."""
    logger.info(
        "Migrating EIP %s from host %s to host %s",
        eip.public_ip, from_host.id[:8], to_host.id[:8],
    )
    disassociate_eip(db, eip, from_host)
    associate_eip(db, eip, to_host)
    logger.info("EIP %s migration complete", eip.public_ip)


def get_host_eip_usage(db: Session, host_id: str) -> int:
    """Get count of EIPs associated with a host."""
    return (
        db.query(func.count(ElasticIp.id))
        .filter(ElasticIp.host_id == host_id, ElasticIp.state == "associated")
        .scalar()
    )


def allocate_transit_ports(
    db: Session, eip: ElasticIp, host, port_forwards: list[dict]
) -> dict:
    """Allocate transit ports for OCP Virt EIP port forwards.

    Scans existing port_map values on the same host to avoid collisions.
    Returns dict mapping ext_port (str) → transit_port (int).
    """
    used = set()
    for other in db.query(ElasticIp).filter(
        ElasticIp.host_id == host.id, ElasticIp.port_map.isnot(None)
    ):
        used.update(other.port_map.values())

    port_map = {}
    next_port = TRANSIT_PORT_START
    for pf in port_forwards:
        while next_port in used:
            next_port += 1
        if next_port > TRANSIT_PORT_END:
            raise RuntimeError("Transit port range exhausted")
        port_map[str(pf["extPort"])] = next_port
        used.add(next_port)
        next_port += 1

    eip.port_map = port_map
    db.commit()
    return port_map


def sync_security_group_rules(
    db: Session, provider, desired_rules: list[dict]
) -> dict:
    """Reconcile SG ingress rules. EC2 only — no-op for other providers."""
    if provider.type != "ec2":
        return {"added": 0, "removed": 0}

    if not provider.security_group_id:
        return {"added": 0, "removed": 0, "error": "No security group configured"}

    from app.services.provisioner import _get_ec2_client

    creds = provider.get_credentials()
    ec2 = _get_ec2_client(credentials=creds)
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
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": r["protocol"],
                        "FromPort": r["port"],
                        "ToPort": r["port"],
                        "IpRanges": [
                            {"CidrIp": "0.0.0.0/0", "Description": r["description"]}
                        ],
                    }
                    for r in to_add.values()
                ],
            )
        except Exception as e:
            if "InvalidPermission.Duplicate" not in str(e):
                raise

    if to_remove:
        ec2.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": r["protocol"],
                    "FromPort": r["port"],
                    "ToPort": r["port"],
                    "IpRanges": [
                        {"CidrIp": "0.0.0.0/0", "Description": r["description"]}
                    ],
                }
                for r in to_remove.values()
            ],
        )

    added = len(to_add)
    removed = len(to_remove)
    if added or removed:
        logger.info("SG %s sync: +%d -%d rules", sg_id, added, removed)
    return {"added": added, "removed": removed}
```

- [ ] **Step 4: Run all eip tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_eip_service.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30
```

Expected: all pass (deploy_service tests don't call eip_service directly).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/eip_service.py src/backend/tests/test_eip_service.py
git commit -m "refactor: eip_service dispatches through ProviderDriver, no direct cloud SDK imports"
```

---

### Task 6: Update deploy_service.py to pass host and propagate transit ports

**Files:**
- Modify: `src/backend/app/services/deploy_service.py:1282-1330`

- [ ] **Step 1: Update EIP allocation call (line ~1321)**

In `src/backend/app/services/deploy_service.py`, find the EIP allocation block (~line 1282-1330). Change the `allocate_eip` call to pass `host`:

Replace lines 1321:
```python
                    eip = allocate_eip(s, provider, project_id, canvas_id)
```
With:
```python
                    eip = allocate_eip(s, provider, project_id, canvas_id, host)
```

- [ ] **Step 2: Add transit port allocation and port_map propagation**

After the existing `ext_ip["_private_ip"] = eip.private_ip` (line 1327), add transit port handling:

Replace lines 1326-1327:
```python
                ext_ip["ip"] = eip.public_ip
                ext_ip["_private_ip"] = eip.private_ip
```
With:
```python
                ext_ip["ip"] = eip.public_ip
                ext_ip["_private_ip"] = eip.private_ip

                if provider.type != "ec2" and not eip.port_map:
                    pf_for_eip = [
                        pf for pf in topology.get("portForwards", [])
                        if pf.get("extIpId") == canvas_id
                    ]
                    if not pf_for_eip:
                        for node in topology.get("nodes", []):
                            and = node.get("data", {})
                            if and.get("subtype") == "gateway":
                                pf_for_eip = [
                                    pf for pf in and.get("portForwards", [])
                                    if pf.get("extIpId") == canvas_id
                                ]
                                break
                    if pf_for_eip:
                        from app.services.eip_service import allocate_transit_ports
                        port_map = allocate_transit_ports(s, eip, host, pf_for_eip)
                        driver = get_provider_driver(provider)
                        driver.update_eip_ports(
                            provider, host, eip.allocation_id,
                            [
                                {"port": int(ep), "targetPort": tp, "name": f"pf-{i}"}
                                for i, (ep, tp) in enumerate(port_map.items())
                            ],
                        )

                if eip.port_map:
                    ext_ip["_transit_port_map"] = eip.port_map
```

Add the import near the top of the EIP block (after line 1294):
```python
            from app.services.providers import get_provider_driver
```

- [ ] **Step 3: Run the full test suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py
git commit -m "feat: deploy_service passes host to allocate_eip, allocates transit ports for OCP Virt"
```

---

### Task 7: Update vxlan.py to propagate `_transit_port` per port forward

**Files:**
- Modify: `src/backend/app/services/vxlan.py:274-279`

- [ ] **Step 1: Add `_transit_port` to port forward entries**

In `src/backend/app/services/vxlan.py`, in the gateway config builder (~line 274-279), add transit port lookup:

Replace lines 274-279:
```python
            port_forwards = []
            for pf in data.get("portForwards", []):
                pf_entry = dict(pf)
                ext_ip = eip_map.get(pf.get("extIpId", ""), {})
                pf_entry["_private_ip"] = ext_ip.get("_private_ip", "")
                port_forwards.append(pf_entry)
```
With:
```python
            port_forwards = []
            for pf in data.get("portForwards", []):
                pf_entry = dict(pf)
                ext_ip = eip_map.get(pf.get("extIpId", ""), {})
                pf_entry["_private_ip"] = ext_ip.get("_private_ip", "")
                transit_map = ext_ip.get("_transit_port_map")
                if transit_map:
                    ext_port_str = str(pf.get("extPort", ""))
                    pf_entry["_transit_port"] = transit_map.get(ext_port_str)
                port_forwards.append(pf_entry)
```

- [ ] **Step 2: Run tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_networks.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/vxlan.py
git commit -m "feat: vxlan.py propagates _transit_port from EIP port_map to port forwards"
```

---

### Task 8: Update troshkad nftables to handle `_transit_port`

**Files:**
- Modify: `src/troshkad/troshkad.py:2756-2990`

- [ ] **Step 1: Update namespace-level DNAT (lines 2756-2805)**

In `src/troshkad/troshkad.py`, find lines 2756-2759:

```python
        for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
            ext_port = pf.get("extPort", "")
            int_ip = pf.get("intIp", "")
            int_port = pf.get("intPort", "")
```

Replace with:

```python
        for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
            ext_port = pf.get("extPort", "")
            int_ip = pf.get("intIp", "")
            int_port = pf.get("intPort", "")
            transit_port = pf.get("_transit_port")
            effective_port = str(transit_port) if transit_port else str(ext_port)
```

Then on line 2800, change `str(ext_port)` to `effective_port` in the namespace prerouting DNAT:

```python
                        "dport",
                        str(ext_port),   # ← change this
```
to:
```python
                        "dport",
                        effective_port,
```

The DNAT target on line 2804 (`f"{int_ip}:{int_port}"`) stays unchanged — it forwards to the VM's actual IP:port.

- [ ] **Step 2: Update host-level DNAT (lines 2956-2990)**

Find lines 2956-2960:

```python
            for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                priv_ip = pf.get("_private_ip", "")
```

Replace with:

```python
            for pf_idx, pf in enumerate(gateway.get("port_forwards", [])):
                ext_port = pf.get("extPort", "")
                int_ip = pf.get("intIp", "")
                int_port = pf.get("intPort", "")
                priv_ip = pf.get("_private_ip", "")
                transit_port = pf.get("_transit_port")
                effective_port = str(transit_port) if transit_port else str(ext_port)
```

Then find lines 2986-2990 (the `else` branch after the `if priv_ip:` block):

```python
                    else:
                        _job_log(
                            job,
                            f"Skipping port forward :{ext_port} — no EIP private IP yet",
                        )
```

Replace with:

```python
                    elif transit_port:
                        _run_cmd(
                            job,
                            [
                                "nft",
                                "add",
                                "rule",
                                "inet",
                                "nat",
                                pre_chain,
                                "tcp",
                                "dport",
                                str(transit_port),
                                "dnat",
                                "ip",
                                "to",
                                f"{pf_transit_ip}:{effective_port}",
                            ],
                            timeout=10,
                        )
                    else:
                        _job_log(
                            job,
                            f"Skipping port forward :{ext_port} — no EIP private IP or transit port",
                        )
```

- [ ] **Step 3: Verify troshkad syntax**

```bash
python3 -c "import ast; ast.parse(open('src/troshkad/troshkad.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad DNAT rules support _transit_port for OCP Virt EIPs"
```

---

### Task 9: Update OCPVirtDriver — remove masquerade ports, bump max_eips

**Files:**
- Modify: `src/backend/app/services/providers/ocpvirt.py:320-330, 427`
- Modify: `src/backend/tests/test_ocpvirt_driver.py:69`

- [ ] **Step 1: Remove explicit masquerade ports from VM spec**

In `src/backend/app/services/providers/ocpvirt.py`, find the interfaces section (~line 320-330):

```python
                                "interfaces": [
                                    {
                                        "masquerade": {},
                                        "model": "virtio",
                                        "name": "default",
                                        "ports": [
                                            {"port": 22},
                                            {"port": 31337},
                                            {"port": 443},
                                        ],
                                    }
                                ],
```

Replace with:
```python
                                "interfaces": [
                                    {
                                        "masquerade": {},
                                        "model": "virtio",
                                        "name": "default",
                                    }
                                ],
```

- [ ] **Step 2: Change max_eips from 0 to 100**

Find line ~427:
```python
            "max_eips": 0,
```
Replace with:
```python
            "max_eips": 100,
```

- [ ] **Step 3: Add EIP service cleanup to terminate_host**

In `terminate_host()`, after the existing service cleanup loop (~line 482-489), add cleanup for EIP services by label selector:

After the existing `for prefix in ["troshka-lb-", "troshka-vncd-"]:` cleanup block, add:

```python
        # Clean up EIP LB services by label
        try:
            host_short = instance_id.replace("troshka-host-", "")
            eip_svcs = core_api.list_namespaced_service(
                namespace,
                label_selector=f"troshka/host-id={host_short}",
            )
            for svc in eip_svcs.items:
                if svc.metadata.name.startswith("troshka-eip-"):
                    try:
                        core_api.delete_namespaced_service(svc.metadata.name, namespace)
                    except client.ApiException:
                        pass
        except client.ApiException:
            pass
```

- [ ] **Step 4: Update test assertion for max_eips**

In `src/backend/tests/test_ocpvirt_driver.py`, line 69:
```python
    assert result["max_eips"] == 0
```
Change to:
```python
    assert result["max_eips"] == 100
```

- [ ] **Step 5: Run OCP Virt driver tests**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_ocpvirt_driver.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/providers/ocpvirt.py src/backend/tests/test_ocpvirt_driver.py
git commit -m "feat: OCPVirt removes masquerade ports, bumps max_eips to 100, cleans EIP services on terminate"
```

---

### Task 10: Add provider-level max_eips check to placement service

**Files:**
- Modify: `src/backend/app/services/placement.py:120-126`

- [ ] **Step 1: Add provider-level EIP cap check**

In `src/backend/app/services/placement.py`, after the existing host-level EIP check (~line 120-125), add a provider-level check. The function needs the provider passed in or looked up.

Find the existing block:
```python
            if required_eips > 0:
                from app.services.eip_service import get_host_eip_usage

                eip_used = get_host_eip_usage(db, host.id)
                if host.max_eips - eip_used < required_eips:
                    continue
```

Add after `continue` (still inside the `if required_eips > 0:` block, but before `candidates.append`):

```python
                if host.provider_id:
                    from app.models.provider import Provider as _Prov

                    prov = db.query(_Prov).filter_by(id=host.provider_id).first()
                    if prov and prov.max_eips is not None:
                        total_provider_eips = (
                            db.query(func.count(ElasticIp.id))
                            .filter(
                                ElasticIp.provider_id == prov.id,
                                ElasticIp.state != "released",
                            )
                            .scalar()
                        )
                        if total_provider_eips + required_eips > prov.max_eips:
                            continue
```

Add `func` import at line 10 of `placement.py` (currently only `from sqlalchemy.orm import Session`):

```python
from sqlalchemy import func
from sqlalchemy.orm import Session
```

The `ElasticIp` import goes inside the `if` block (lazy import pattern used throughout placement.py):
```python
                from app.models.elastic_ip import ElasticIp
```

- [ ] **Step 2: Run placement tests (if any) and full suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/placement.py
git commit -m "feat: placement checks provider-level max_eips cap for OCP Virt"
```

---

### Task 11: Run full test suite and verify

**Files:**
- None (verification only)

- [ ] **Step 1: Run full backend test suite**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30
```

Expected: all tests pass.

- [ ] **Step 2: Verify no boto3 imports remain in eip_service.py**

```bash
grep -n "import boto3\|from boto3" src/backend/app/services/eip_service.py
```

Expected: no output (no boto3 imports).

- [ ] **Step 3: Verify provider driver interface completeness**

```bash
grep -n "def allocate_eip\|def associate_eip\|def release_eip\|def update_eip_ports" src/backend/app/services/providers/base.py src/backend/app/services/providers/ec2.py src/backend/app/services/providers/ocpvirt.py
```

Expected: each method appears in all three files.

- [ ] **Step 4: Remind to restart backend**

Tell the user: "Python changes require a backend restart: `./dev-services.sh restart backend`"

- [ ] **Step 5: Commit (if any fixups needed)**

Only if previous steps required fixes. Otherwise skip.
