# Network Mesh Phase 1: Multi-Host Within a Pool

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable a single project's VMs to span multiple troshkad hosts within the same storage pool, connected via WireGuard-encrypted VXLAN tunnels providing full L2 adjacency.

**Architecture:** Backend generates per-project WireGuard mesh between participating hosts. VXLAN runs inside the WireGuard tunnels. One host is designated the "network host" running dnsmasq/nftables/chronyd; remote hosts have VXLAN+bridge only. Placement uses first-fit-decreasing bin packing with optional affinity groups.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, Alembic, FastAPI, WireGuard (kernel module + `wg` CLI), VXLAN, Linux network namespaces, nftables

**Spec:** `docs/superpowers/specs/2026-08-04-network-mesh-design.md`

## Global Constraints

- UUID columns: `UUID(as_uuid=False)`, `default=lambda: str(uuid.uuid4())`
- JSONB columns: `from sqlalchemy.dialects.postgresql import JSONB`
- FK columns in migrations: `sa.dialects.postgresql.UUID(as_uuid=False)`
- Tests use SQLite with JSONB→JSON and UUID→VARCHAR(36) compiler overrides
- Fernet encryption via `app.core.encryption.encrypt()` / `decrypt()`
- Troshkad is single-file stdlib Python — no pip dependencies
- Troshkad async commands: `COMMAND_HANDLERS["path"] = handler_func`
- Troshkad sync endpoints: `@route("GET", "/path")`
- Backend calls troshkad via `start_job(host, "/path", params)` + `wait_for_job(host, job_id)`
- Current Alembic head: `e66ef9d238cf`
- Run `black` before committing
- Run tests: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/backend/app/models/mesh_peer.py` | `ProjectMeshPeer` SQLAlchemy model |
| `src/backend/app/services/mesh_service.py` | WireGuard key generation, subnet/port allocation, peer management |
| `src/backend/alembic/versions/xxxx_add_network_mesh.py` | Migration for new model + project columns |
| `src/backend/tests/test_mesh_service.py` | Tests for mesh service |
| `src/backend/tests/test_placement_multihost.py` | Tests for multi-host placement |

### Modified Files

| File | Changes |
|------|---------|
| `src/backend/app/models/project.py` | Add `mesh_subnet_id`, `mesh_network_host_id`, `host_assignments` columns |
| `src/backend/app/models/__init__.py` | Register `ProjectMeshPeer` |
| `src/backend/app/services/placement.py` | Multi-host bin packing with affinity groups |
| `src/backend/app/services/deploy_service.py` | Multi-host deploy orchestration |
| `src/backend/app/services/ws_pubsub.py` | Multi-host VM state aggregation |
| `src/backend/app/services/health_poller.py` | WireGuard health checks |
| `src/backend/app/services/gc_service.py` | WireGuard recovery on host reconnect |
| `src/backend/app/api/projects.py` | Console routing by VM's host |
| `src/backend/app/services/provisioner.py` | WireGuard UDP security group rule |
| `src/backend/app/services/storage_pool_service.py` | WireGuard UDP rule in pool SG |
| `src/backend/app/services/agent_deployer.py` | Add `wireguard-tools` to install |
| `src/backend/app/services/template_loader.py` | `affinity_group` import/export |
| `src/troshkad/troshkad.py` | Mesh endpoints: setup, join-network, teardown, status |

---

### Task 1: ProjectMeshPeer Model & Migration

**Files:**
- Create: `src/backend/app/models/mesh_peer.py`
- Modify: `src/backend/app/models/project.py:28-60`
- Modify: `src/backend/app/models/__init__.py`
- Create: `src/backend/alembic/versions/xxxx_add_network_mesh.py`
- Test: `src/backend/tests/test_mesh_model.py`

**Interfaces:**
- Consumes: `Base` from `app.core.database`, `Project` model
- Produces: `ProjectMeshPeer` model with columns: `id`, `project_id`, `host_id`, `provider_id`, `peer_type`, `wg_public_key`, `wg_private_key`, `wg_endpoint`, `wg_address`, `wg_port`, `is_network_host`, `created_at`. Project gains `mesh_subnet_id: int | None`, `mesh_network_host_id: str | None`, `host_assignments: dict | None`.

- [ ] **Step 1: Write the failing test for ProjectMeshPeer model**

Create `src/backend/tests/test_mesh_model.py`:

```python
import uuid

from app.models.mesh_peer import ProjectMeshPeer
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession


def _make_user(db):
    u = User(
        email=f"mesh-{uuid.uuid4().hex[:6]}@test.com",
        display_name="Mesh Test",
        role="admin",
        auth_source="local",
        password_hash="x",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_create_mesh_peer():
    db = TestSession()
    try:
        user = _make_user(db)
        project = Project(
            name="mesh-test",
            owner_id=user.id,
            state="deploying",
            mesh_subnet_id=1,
            mesh_network_host_id="host-abc",
            host_assignments={"vm1": "host-a", "vm2": "host-b"},
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        peer = ProjectMeshPeer(
            project_id=project.id,
            host_id="host-abc",
            peer_type="troshkad",
            wg_public_key="pubkey123",
            wg_private_key="encrypted-privkey",
            wg_endpoint="10.0.1.50:51820",
            wg_address="10.252.1.1/24",
            wg_port=51820,
            is_network_host=True,
        )
        db.add(peer)
        db.commit()
        db.refresh(peer)

        assert peer.id is not None
        assert peer.project_id == project.id
        assert peer.is_network_host is True
        assert project.mesh_subnet_id == 1
        assert project.host_assignments == {"vm1": "host-a", "vm2": "host-b"}

        fetched = (
            db.query(ProjectMeshPeer)
            .filter_by(project_id=project.id)
            .all()
        )
        assert len(fetched) == 1

        db.delete(peer)
        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_mesh_peer_cascade_delete():
    db = TestSession()
    try:
        user = _make_user(db)
        project = Project(name="mesh-cascade", owner_id=user.id, state="draft")
        db.add(project)
        db.commit()
        db.refresh(project)

        peer = ProjectMeshPeer(
            project_id=project.id,
            host_id="host-xyz",
            peer_type="troshkad",
            wg_public_key="pub",
            wg_private_key="priv",
            wg_endpoint="1.2.3.4:51820",
            wg_address="10.252.2.1/24",
            wg_port=51820,
            is_network_host=False,
        )
        db.add(peer)
        db.commit()

        db.delete(project)
        db.commit()

        remaining = db.query(ProjectMeshPeer).filter_by(project_id=project.id).all()
        assert len(remaining) == 0

        db.delete(user)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_mesh_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.mesh_peer'`

- [ ] **Step 3: Create ProjectMeshPeer model**

Create `src/backend/app/models/mesh_peer.py`:

```python
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProjectMeshPeer(Base):
    __tablename__ = "project_mesh_peers"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    host_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosts.id"), nullable=True
    )
    provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("providers.id"), nullable=True
    )
    peer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    wg_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    wg_private_key: Mapped[str] = mapped_column(String(256), nullable=False)
    wg_endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    wg_address: Mapped[str] = mapped_column(String(32), nullable=False)
    wg_port: Mapped[int] = mapped_column(Integer, nullable=False)
    is_network_host: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Add Project model columns**

In `src/backend/app/models/project.py`, add after the existing JSONB columns (near `vni_map`):

```python
mesh_subnet_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
mesh_network_host_id: Mapped[str | None] = mapped_column(
    ForeignKey("hosts.id"), nullable=True
)
host_assignments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

Import `Integer` if not already imported.

- [ ] **Step 5: Register model in `__init__.py`**

In `src/backend/app/models/__init__.py`, add:

```python
from app.models.mesh_peer import ProjectMeshPeer
```

And add `"ProjectMeshPeer"` to the `__all__` list.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_mesh_model.py -v`
Expected: PASS

- [ ] **Step 7: Create Alembic migration**

Run: `cd src/backend && ./venv/bin/python3 -m alembic revision -m "add network mesh"`

Edit the generated file:

```python
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "<generated>"
down_revision: str | Sequence[str] | None = "e66ef9d238cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_mesh_peers",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "host_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("hosts.id"),
            nullable=True,
        ),
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("providers.id"),
            nullable=True,
        ),
        sa.Column("peer_type", sa.String(20), nullable=False),
        sa.Column("wg_public_key", sa.String(64), nullable=False),
        sa.Column("wg_private_key", sa.String(256), nullable=False),
        sa.Column("wg_endpoint", sa.String(64), nullable=False),
        sa.Column("wg_address", sa.String(32), nullable=False),
        sa.Column("wg_port", sa.Integer, nullable=False),
        sa.Column("is_network_host", sa.Boolean, default=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "projects",
        sa.Column("mesh_subnet_id", sa.Integer, nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "mesh_network_host_id",
            sa.dialects.postgresql.UUID(as_uuid=False),
            sa.ForeignKey("hosts.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "host_assignments",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "host_assignments")
    op.drop_column("projects", "mesh_network_host_id")
    op.drop_column("projects", "mesh_subnet_id")
    op.drop_table("project_mesh_peers")
```

- [ ] **Step 8: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass including new mesh model tests.

- [ ] **Step 9: Commit**

```bash
git add src/backend/app/models/mesh_peer.py src/backend/app/models/project.py \
  src/backend/app/models/__init__.py src/backend/alembic/versions/*add_network_mesh* \
  src/backend/tests/test_mesh_model.py
git commit -m "feat: add ProjectMeshPeer model and project mesh columns"
```

---

### Task 2: Mesh Service — Key Generation & Peer Management

**Files:**
- Create: `src/backend/app/services/mesh_service.py`
- Create: `src/backend/tests/test_mesh_service.py`

**Interfaces:**
- Consumes: `ProjectMeshPeer` model, `encrypt()`/`decrypt()` from `app.core.encryption`, `Session` from SQLAlchemy
- Produces:
  - `generate_wireguard_keypair() -> tuple[str, str]` — returns `(private_key, public_key)`
  - `allocate_mesh_subnet(db: Session) -> int` — returns next available `mesh_subnet_id`
  - `allocate_wg_port(db: Session, host_id: str) -> int` — returns next available WireGuard port for a host
  - `create_mesh_peers(db: Session, project_id: str, host_assignments: dict[str, list[str]], network_host_id: str) -> list[ProjectMeshPeer]` — generates keypairs, allocates subnet/ports, creates all peer records
  - `get_peer_config_for_host(db: Session, project_id: str, host_id: str) -> dict` — returns WireGuard config dict ready to push to troshkad
  - `delete_mesh_peers(db: Session, project_id: str) -> None` — cleanup

- [ ] **Step 1: Write failing tests for key generation and subnet allocation**

Create `src/backend/tests/test_mesh_service.py`:

```python
import uuid

from unittest.mock import patch

from app.models.mesh_peer import ProjectMeshPeer
from app.models.project import Project
from app.models.user import User
from app.services.mesh_service import (
    allocate_mesh_subnet,
    allocate_wg_port,
    create_mesh_peers,
    delete_mesh_peers,
    generate_wireguard_keypair,
    get_peer_config_for_host,
)
from tests.conftest import TestSession


def _make_user(db):
    u = User(
        email=f"mesh-svc-{uuid.uuid4().hex[:6]}@test.com",
        display_name="Test",
        role="admin",
        auth_source="local",
        password_hash="x",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_project(db, user, **kwargs):
    p = Project(name=f"proj-{uuid.uuid4().hex[:6]}", owner_id=user.id, state="deploying", **kwargs)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_generate_wireguard_keypair():
    private_key, public_key = generate_wireguard_keypair()
    assert len(private_key) == 44  # base64-encoded 32 bytes
    assert len(public_key) == 44
    assert private_key != public_key


def test_allocate_mesh_subnet_sequential():
    db = TestSession()
    try:
        subnet1 = allocate_mesh_subnet(db)
        subnet2 = allocate_mesh_subnet(db)
        assert subnet1 >= 1
        assert subnet2 == subnet1 + 1
    finally:
        db.rollback()
        db.close()


def test_allocate_wg_port_starts_at_base():
    db = TestSession()
    try:
        port = allocate_wg_port(db, "host-new")
        assert port == 51820
    finally:
        db.rollback()
        db.close()


def test_allocate_wg_port_avoids_conflicts():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)
        peer = ProjectMeshPeer(
            project_id=project.id,
            host_id="host-conflict",
            peer_type="troshkad",
            wg_public_key="pub",
            wg_private_key="priv",
            wg_endpoint="1.2.3.4:51820",
            wg_address="10.252.1.1/24",
            wg_port=51820,
            is_network_host=False,
        )
        db.add(peer)
        db.commit()

        port = allocate_wg_port(db, "host-conflict")
        assert port == 51821

        db.delete(peer)
        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_create_mesh_peers():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        host_assignments = {
            "host-a": ["vm1", "vm2"],
            "host-b": ["vm3", "vm4"],
        }
        host_ips = {"host-a": "10.0.1.50", "host-b": "10.0.1.51"}

        peers = create_mesh_peers(
            db, project.id, host_assignments, "host-a", host_ips
        )

        assert len(peers) == 2
        network_hosts = [p for p in peers if p.is_network_host]
        assert len(network_hosts) == 1
        assert network_hosts[0].host_id == "host-a"

        for p in peers:
            assert p.wg_address.startswith("10.252.")
            assert p.wg_port >= 51820

        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_get_peer_config_for_host():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        host_assignments = {
            "host-a": ["vm1"],
            "host-b": ["vm2"],
        }
        host_ips = {"host-a": "10.0.1.50", "host-b": "10.0.1.51"}

        create_mesh_peers(db, project.id, host_assignments, "host-a", host_ips)

        config = get_peer_config_for_host(db, project.id, "host-a")
        assert config["project_id"] == project.id
        assert config["wg_address"].startswith("10.252.")
        assert config["wg_port"] >= 51820
        assert "wg_private_key" in config
        assert len(config["peers"]) == 1
        assert config["peers"][0]["endpoint"] == "10.0.1.51:" + str(config["peers"][0]["endpoint"].split(":")[1])

        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()


def test_delete_mesh_peers():
    db = TestSession()
    try:
        user = _make_user(db)
        project = _make_project(db, user)

        host_assignments = {"host-a": ["vm1"], "host-b": ["vm2"]}
        host_ips = {"host-a": "10.0.1.50", "host-b": "10.0.1.51"}

        create_mesh_peers(db, project.id, host_assignments, "host-a", host_ips)
        assert db.query(ProjectMeshPeer).filter_by(project_id=project.id).count() == 2

        delete_mesh_peers(db, project.id)
        assert db.query(ProjectMeshPeer).filter_by(project_id=project.id).count() == 0

        db.delete(project)
        db.delete(user)
        db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_mesh_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.mesh_service'`

- [ ] **Step 3: Implement mesh_service.py**

Create `src/backend/app/services/mesh_service.py`:

```python
from __future__ import annotations

import base64
import logging
import threading

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.encryption import decrypt, encrypt
from app.models.mesh_peer import ProjectMeshPeer
from app.models.project import Project

logger = logging.getLogger(__name__)

_WG_PORT_BASE = 51820
_WG_PORT_MAX = 51850
_MESH_SUBNET_PREFIX = "10.252"

_subnet_lock = threading.Lock()


def generate_wireguard_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    private_bytes = private.private_bytes_raw()
    public_bytes = private.public_key().public_bytes_raw()
    return (
        base64.b64encode(private_bytes).decode(),
        base64.b64encode(public_bytes).decode(),
    )


def allocate_mesh_subnet(db: Session) -> int:
    with _subnet_lock:
        max_id = (
            db.query(sa_func.max(Project.mesh_subnet_id)).scalar() or 0
        )
        return max_id + 1


def allocate_wg_port(db: Session, host_id: str) -> int:
    used_ports = {
        row[0]
        for row in db.query(ProjectMeshPeer.wg_port)
        .filter(ProjectMeshPeer.host_id == host_id)
        .all()
    }
    for port in range(_WG_PORT_BASE, _WG_PORT_MAX + 1):
        if port not in used_ports:
            return port
    raise RuntimeError(
        f"No available WireGuard ports on host {host_id} "
        f"(range {_WG_PORT_BASE}-{_WG_PORT_MAX})"
    )


def create_mesh_peers(
    db: Session,
    project_id: str,
    host_assignments: dict[str, list[str]],
    network_host_id: str,
    host_ips: dict[str, str],
) -> list[ProjectMeshPeer]:
    subnet_id = allocate_mesh_subnet(db)

    project = db.query(Project).filter_by(id=project_id).first()
    if project:
        project.mesh_subnet_id = subnet_id
        project.mesh_network_host_id = network_host_id
        flat_assignments = {}
        for hid, vm_ids in host_assignments.items():
            for vm_id in vm_ids:
                flat_assignments[vm_id] = hid
        project.host_assignments = flat_assignments

    peers = []
    for idx, host_id in enumerate(sorted(host_assignments.keys()), start=1):
        private_key, public_key = generate_wireguard_keypair()
        wg_port = allocate_wg_port(db, host_id)
        host_ip = host_ips[host_id]

        peer = ProjectMeshPeer(
            project_id=project_id,
            host_id=host_id,
            peer_type="troshkad",
            wg_public_key=public_key,
            wg_private_key=encrypt(private_key),
            wg_endpoint=f"{host_ip}:{wg_port}",
            wg_address=f"{_MESH_SUBNET_PREFIX}.{subnet_id}.{idx}/24",
            wg_port=wg_port,
            is_network_host=(host_id == network_host_id),
        )
        db.add(peer)
        peers.append(peer)

    db.commit()
    for p in peers:
        db.refresh(p)
    return peers


def get_peer_config_for_host(
    db: Session, project_id: str, host_id: str
) -> dict:
    all_peers = (
        db.query(ProjectMeshPeer)
        .filter_by(project_id=project_id)
        .all()
    )
    this_peer = next(p for p in all_peers if p.host_id == host_id)
    other_peers = [p for p in all_peers if p.host_id != host_id]

    return {
        "project_id": project_id,
        "wg_private_key": decrypt(this_peer.wg_private_key),
        "wg_address": this_peer.wg_address,
        "wg_port": this_peer.wg_port,
        "peers": [
            {
                "public_key": p.wg_public_key,
                "endpoint": p.wg_endpoint,
                "allowed_ips": p.wg_address.replace("/24", "/32"),
            }
            for p in other_peers
        ],
    }


def delete_mesh_peers(db: Session, project_id: str) -> None:
    db.query(ProjectMeshPeer).filter_by(project_id=project_id).delete()
    project = db.query(Project).filter_by(id=project_id).first()
    if project:
        project.mesh_subnet_id = None
        project.mesh_network_host_id = None
        project.host_assignments = None
    db.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_mesh_service.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite for regressions**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/mesh_service.py src/backend/tests/test_mesh_service.py
git commit -m "feat: mesh service for WireGuard key generation and peer management"
```

---

### Task 3: Multi-Host Placement

**Files:**
- Modify: `src/backend/app/services/placement.py:22-80,157-240,241-340`
- Create: `src/backend/tests/test_placement_multihost.py`

**Interfaces:**
- Consumes: `Host` model, `Project` model, `calculate_project_requirements(topology)` (existing), `find_available_host()` (existing)
- Produces:
  - `find_multihost_placement(db: Session, topology: dict, pool_id: str | None, provider_id: str | None) -> dict[str, list[str]] | None` — returns `{host_id: [vm_node_ids]}` or `None` if can't fit. Respects `affinityGroup` on VM nodes.
  - `select_network_host(host_assignments: dict[str, list[str]], topology: dict) -> str` — picks the host with the most VMs, preferring the one with a gateway VM.
  - `place_project()` updated to attempt multi-host when single-host fails.

- [ ] **Step 1: Write failing tests for multi-host placement**

Create `src/backend/tests/test_placement_multihost.py`:

```python
import uuid

from app.models.host import Host
from app.models.provider import Provider
from app.services.placement import (
    find_multihost_placement,
    select_network_host,
)
from tests.conftest import TestSession


def _make_provider(db):
    p = Provider(name=f"prov-{uuid.uuid4().hex[:6]}", type="ec2")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider, vcpus=64, ram_mb=256000, **kwargs):
    h = Host(
        ip_address=f"10.0.1.{uuid.uuid4().int % 250 + 1}",
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        total_vcpus=vcpus,
        total_ram_mb=ram_mb,
        used_vcpus=0,
        used_ram_mb=0,
        **kwargs,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _topology_with_vms(vm_specs):
    """vm_specs: list of (name, vcpus, ram_mb, affinity_group|None)"""
    nodes = []
    for name, vcpus, ram_mb, affinity in vm_specs:
        node_id = str(uuid.uuid4())
        nodes.append({
            "id": node_id,
            "type": "vmNode",
            "data": {
                "label": name,
                "name": name,
                "vcpus": vcpus,
                "memoryMb": ram_mb,
                **({"affinityGroup": affinity} if affinity else {}),
            },
        })
    return {"nodes": nodes, "edges": []}


def test_multihost_placement_basic():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=16, ram_mb=64000)
        host_b = _make_host(db, prov, vcpus=16, ram_mb=64000)

        topo = _topology_with_vms([
            ("vm1", 8, 32000, None),
            ("vm2", 8, 32000, None),
            ("vm3", 8, 32000, None),
        ])

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None
        all_vms = []
        for vms in result.values():
            all_vms.extend(vms)
        assert len(all_vms) == 3

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_placement_respects_affinity():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=16, ram_mb=64000)
        host_b = _make_host(db, prov, vcpus=16, ram_mb=64000)

        topo = _topology_with_vms([
            ("worker1", 4, 16000, "workers"),
            ("worker2", 4, 16000, "workers"),
            ("hub", 8, 48000, "hub"),
        ])

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is not None

        vm_names_by_node_id = {
            n["id"]: n["data"]["name"] for n in topo["nodes"]
        }
        host_for_vm = {}
        for hid, vm_ids in result.items():
            for vid in vm_ids:
                host_for_vm[vm_names_by_node_id[vid]] = hid

        assert host_for_vm["worker1"] == host_for_vm["worker2"]

        db.delete(host_a)
        db.delete(host_b)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_multihost_placement_returns_none_when_impossible():
    db = TestSession()
    try:
        prov = _make_provider(db)
        host_a = _make_host(db, prov, vcpus=4, ram_mb=16000)

        topo = _topology_with_vms([
            ("huge-vm", 64, 512000, None),
        ])

        result = find_multihost_placement(db, topo, None, prov.id)
        assert result is None

        db.delete(host_a)
        db.delete(prov)
        db.commit()
    finally:
        db.close()


def test_select_network_host_picks_most_vms():
    assignments = {
        "host-a": ["vm1"],
        "host-b": ["vm2", "vm3", "vm4"],
    }
    topo = {"nodes": [], "edges": []}
    result = select_network_host(assignments, topo)
    assert result == "host-b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_placement_multihost.py -v`
Expected: FAIL — `ImportError: cannot import name 'find_multihost_placement'`

- [ ] **Step 3: Implement multi-host placement functions**

Add to `src/backend/app/services/placement.py`:

```python
def find_multihost_placement(
    db: Session,
    topology: dict,
    pool_id: str | None,
    provider_id: str | None,
) -> dict[str, list[str]] | None:
    """Bin-pack VMs across multiple hosts. Returns {host_id: [vm_node_ids]} or None."""
    vm_nodes = [
        n for n in topology.get("nodes", [])
        if n.get("type") in ("vmNode", "containerNode")
    ]
    if not vm_nodes:
        return None

    affinity_groups: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for node in vm_nodes:
        ag = node.get("data", {}).get("affinityGroup")
        if ag:
            affinity_groups.setdefault(ag, []).append(node)
        else:
            ungrouped.append(node)

    def _group_ram(nodes):
        return sum(n.get("data", {}).get("memoryMb", 4096) for n in nodes)

    def _group_vcpus(nodes):
        return sum(n.get("data", {}).get("vcpus", 2) for n in nodes)

    units = []
    for ag_name, ag_nodes in affinity_groups.items():
        units.append({
            "vm_ids": [n["id"] for n in ag_nodes],
            "ram_mb": _group_ram(ag_nodes),
            "vcpus": _group_vcpus(ag_nodes),
        })
    for node in ungrouped:
        units.append({
            "vm_ids": [node["id"]],
            "ram_mb": node.get("data", {}).get("memoryMb", 4096),
            "vcpus": node.get("data", {}).get("vcpus", 2),
        })

    units.sort(key=lambda u: u["ram_mb"], reverse=True)

    hosts_query = db.query(Host).filter(
        Host.state == "active",
        Host.agent_status == "connected",
    )
    if pool_id:
        hosts_query = hosts_query.filter(Host.storage_pool_id == pool_id)
    if provider_id:
        hosts_query = hosts_query.filter(Host.provider_id == provider_id)

    available_hosts = hosts_query.all()
    if not available_hosts:
        return None

    overcommit = 2.0
    host_remaining = {
        h.id: {
            "ram_mb": (h.total_ram_mb or 0) - (h.used_ram_mb or 0),
            "vcpus": int((h.total_vcpus or 0) * overcommit) - (h.used_vcpus or 0),
        }
        for h in available_hosts
    }

    assignments: dict[str, list[str]] = {h.id: [] for h in available_hosts}

    for unit in units:
        placed = False
        sorted_hosts = sorted(
            host_remaining.keys(),
            key=lambda hid: host_remaining[hid]["ram_mb"],
            reverse=True,
        )
        for hid in sorted_hosts:
            remaining = host_remaining[hid]
            if (
                remaining["ram_mb"] >= unit["ram_mb"]
                and remaining["vcpus"] >= unit["vcpus"]
            ):
                assignments[hid].extend(unit["vm_ids"])
                remaining["ram_mb"] -= unit["ram_mb"]
                remaining["vcpus"] -= unit["vcpus"]
                placed = True
                break
        if not placed:
            return None

    return {hid: vms for hid, vms in assignments.items() if vms}


def select_network_host(
    host_assignments: dict[str, list[str]], topology: dict
) -> str:
    """Pick the network host: prefer host with gateway VM, else most VMs."""
    gateway_vm_ids = set()
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("isGateway"):
            gateway_vm_ids.add(node["id"])

    if gateway_vm_ids:
        for hid, vm_ids in host_assignments.items():
            if gateway_vm_ids & set(vm_ids):
                return hid

    return max(host_assignments, key=lambda hid: len(host_assignments[hid]))
```

Add required imports at the top of the file: `from app.models.host import Host` (likely already imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_placement_multihost.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/placement.py src/backend/tests/test_placement_multihost.py
git commit -m "feat: multi-host bin-packing placement with affinity groups"
```

---

### Task 4: Troshkad Mesh Endpoints

**Files:**
- Modify: `src/troshkad/troshkad.py`
- Modify: `src/backend/app/services/agent_deployer.py:67`

**Interfaces:**
- Consumes: WireGuard config dict from `get_peer_config_for_host()` (Task 2)
- Produces (troshkad endpoints):
  - `POST /commands/mesh/setup` — create WireGuard interface, write config, verify peers
  - `POST /commands/mesh/join-network` — create namespace + VXLAN + bridge on remote host (no dnsmasq/nftables)
  - `DELETE /mesh/teardown?project_id=X` — remove WireGuard interface and config
  - `GET /mesh/status` — return per-project WireGuard handshake status
- Produces (agent deployer): `wireguard-tools` added to `dnf install` line

- [ ] **Step 1: Add `wireguard-tools` to agent install script**

In `src/backend/app/services/agent_deployer.py`, find the `dnf install` line (around line 67) that installs `qemu-kvm libvirt ...`. Append `wireguard-tools` to the package list.

Before:
```
python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass || true
```
After:
```
python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass wireguard-tools || true
```

- [ ] **Step 2: Add mesh endpoints to troshkad**

In `src/troshkad/troshkad.py`, add the following handler functions and registrations. Place them after the existing network handlers (after `_handle_network_full_teardown`).

Add to `_SKIP_DRAIN` set (around line 6803):
```python
"mesh/setup",
"mesh/join-network",
```

Add the WireGuard mesh handlers:

```python
def _handle_mesh_setup(job, params):
    """Set up WireGuard interface for a project mesh.
    Params:
        project_id: str
        wg_private_key: str (base64)
        wg_address: str (e.g. "10.252.1.1/24")
        wg_port: int
        peers: list of {public_key, endpoint, allowed_ips}
    """
    project_id = params["project_id"]
    pid = project_id[:8]
    wg_iface = f"wg-{pid}"

    os.makedirs("/var/lib/troshka/mesh", exist_ok=True)
    conf_path = f"/var/lib/troshka/mesh/{project_id}.conf"

    conf_lines = [
        "[Interface]",
        f"PrivateKey = {params['wg_private_key']}",
        f"ListenPort = {params['wg_port']}",
        "",
    ]
    for peer in params["peers"]:
        conf_lines.extend([
            "[Peer]",
            f"PublicKey = {peer['public_key']}",
            f"Endpoint = {peer['endpoint']}",
            f"AllowedIPs = {peer['allowed_ips']}",
            "PersistentKeepalive = 25",
            "",
        ])

    with open(conf_path, "w") as f:
        f.write("\n".join(conf_lines))
    os.chmod(conf_path, 0o600)

    _run(["ip", "link", "del", wg_iface], check=False)
    _run(["ip", "link", "add", wg_iface, "type", "wireguard"])
    _run(["wg", "setconf", wg_iface, conf_path])
    _run(["ip", "addr", "add", params["wg_address"], "dev", wg_iface])
    _run(["ip", "link", "set", wg_iface, "up"])

    for peer in params["peers"]:
        peer_ip = peer["allowed_ips"].split("/")[0]
        rc = _run(
            ["ping", "-c", "3", "-W", "2", peer_ip], check=False
        ).returncode
        if rc != 0:
            logger.warning("Mesh peer %s not yet reachable (may connect later)", peer_ip)

    return {"status": "ok", "interface": wg_iface}


COMMAND_HANDLERS["mesh/setup"] = _handle_mesh_setup


def _handle_mesh_join_network(job, params):
    """Set up VXLAN + bridge on a remote (non-network) host.
    Params:
        project_id: str
        wg_local_ip: str -- this host's WireGuard tunnel IP (e.g. "10.252.1.2")
        networks: list of {vni, bridge_name, wg_peer_ips}
    """
    project_id = params["project_id"]
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    wg_local_ip = params["wg_local_ip"]

    _run(["ip", "netns", "add", ns], check=False)
    _run(["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])

    for net in params["networks"]:
        vni = net["vni"]
        bridge = net["bridge_name"]
        vxlan_if = f"vxlan-{vni}"
        peers = net["wg_peer_ips"]

        _run([
            "ip", "link", "add", vxlan_if, "type", "vxlan",
            "id", str(vni), "local", wg_local_ip,
            "dstport", "4789", "nolearning",
        ])

        for peer_ip in peers:
            if peer_ip != wg_local_ip:
                _run([
                    "bridge", "fdb", "append", "00:00:00:00:00:00",
                    "dev", vxlan_if, "dst", peer_ip,
                ])

        _run(["ip", "link", "set", vxlan_if, "netns", ns])

        _run(["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"])
        _run(["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "master", bridge])
        _run(["ip", "netns", "exec", ns, "ip", "link", "set", vxlan_if, "up"])
        _run(["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"])

        _run(["ip", "link", "add", bridge, "type", "bridge"], check=False)
        _run(["ip", "link", "set", bridge, "type", "bridge",
              "forward_delay", "99", "ageing_time", "0"], check=False)
        _run(["ip", "link", "set", bridge, "up"], check=False)

    return {"status": "ok", "namespace": ns}


COMMAND_HANDLERS["mesh/join-network"] = _handle_mesh_join_network


@route("DELETE", "/mesh/teardown")
def handle_mesh_teardown(handler, params):
    project_id = params.get("project_id")
    if not project_id:
        handler._send_json(400, {"error": "project_id required"})
        return

    pid = project_id[:8]
    wg_iface = f"wg-{pid}"
    ns = f"troshka-{pid}"
    conf_path = f"/var/lib/troshka/mesh/{project_id}.conf"

    _run(["ip", "link", "del", wg_iface], check=False)

    if os.path.exists(conf_path):
        os.remove(conf_path)

    handler._send_json(200, {"status": "ok"})


@route("GET", "/mesh/status")
def handle_mesh_status(handler, params):
    result = {}
    mesh_dir = "/var/lib/troshka/mesh"
    if not os.path.isdir(mesh_dir):
        handler._send_json(200, {"projects": {}})
        return

    for fname in os.listdir(mesh_dir):
        if not fname.endswith(".conf"):
            continue
        project_id = fname[:-5]
        pid = project_id[:8]
        wg_iface = f"wg-{pid}"

        try:
            out = subprocess.check_output(
                ["wg", "show", wg_iface, "latest-handshakes"],
                text=True, timeout=5,
            )
            peers = {}
            for line in out.strip().split("\n"):
                if "\t" in line:
                    pubkey, ts = line.split("\t", 1)
                    peers[pubkey] = int(ts)
            result[project_id] = {"interface": wg_iface, "peers": peers}
        except Exception:
            result[project_id] = {"interface": wg_iface, "error": "not running"}

    handler._send_json(200, {"projects": result})
```

- [ ] **Step 3: Verify troshkad syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/troshkad/troshkad.py', doraise=True)"`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add src/troshkad/troshkad.py src/backend/app/services/agent_deployer.py
git commit -m "feat: troshkad mesh endpoints for WireGuard setup/teardown/status"
```

---

### Task 5: Deploy Orchestration — Multi-Host Flow

**Files:**
- Modify: `src/backend/app/services/deploy_service.py:2994-3020,1290-1367,3118-3800`
- Modify: `src/backend/app/services/placement.py:241-340`

**Interfaces:**
- Consumes: `find_multihost_placement()`, `select_network_host()` (Task 3), `create_mesh_peers()`, `get_peer_config_for_host()` (Task 2), `start_job()`/`wait_for_job()` from `troshkad_client`
- Produces:
  - Updated `place_project()` that falls back to multi-host when single-host fails
  - `_setup_mesh(db, project, host_assignments, host_ips) -> bool` — pushes WireGuard configs to all hosts
  - `_setup_remote_networks(db, project, host_assignments, vni_map) -> bool` — calls `/mesh/join-network` on non-network hosts
  - Updated `_deploy_project_inner()` with multi-host deploy path
  - Updated `_setup_networks_via_troshkad()` to use WireGuard tunnel IPs as VXLAN peers when mesh is active

- [ ] **Step 1: Update `place_project()` to try multi-host**

In `src/backend/app/services/placement.py`, modify `place_project()`. After the single-host attempt fails (where it would normally try auto-provisioning or raise), add a multi-host fallback:

```python
# After find_available_host() returns None and before auto-provisioning:
host_assignments = find_multihost_placement(db, project.topology, storage_pool_id, project.provider_id)
if host_assignments:
    network_host_id = select_network_host(host_assignments, project.topology)
    host_ips = {}
    for hid in host_assignments:
        h = db.query(Host).filter_by(id=hid).first()
        host_ips[hid] = h.ip_address
    
    vni_map = allocate_vnis_for_project(db, project.topology)
    
    return {
        "multi_host": True,
        "host_assignments": host_assignments,
        "network_host_id": network_host_id,
        "host_ips": host_ips,
        "vni_map": vni_map,
    }
```

- [ ] **Step 2: Add mesh setup function to deploy service**

In `src/backend/app/services/deploy_service.py`, add:

```python
from app.services.mesh_service import (
    create_mesh_peers,
    get_peer_config_for_host,
    delete_mesh_peers,
)


def _setup_mesh(db, project, host_assignments, host_ips):
    """Push WireGuard configs to all hosts. Returns True on success."""
    peers = create_mesh_peers(
        db, project.id, host_assignments,
        project.mesh_network_host_id, host_ips,
    )

    errors = []
    for peer in peers:
        host = db.query(Host).filter_by(id=peer.host_id).first()
        config = get_peer_config_for_host(db, project.id, peer.host_id)
        try:
            job_id = start_job(host, "/mesh/setup", config)
            job = wait_for_job(host, job_id, timeout=60)
            if job["status"] == "failed":
                errors.append(f"Host {host.id}: {job.get('result', {}).get('error', 'unknown')}")
        except Exception as e:
            errors.append(f"Host {host.id}: {e}")

    if errors:
        logger.error("Mesh setup failed: %s", errors)
        for peer in peers:
            host = db.query(Host).filter_by(id=peer.host_id).first()
            try:
                troshkad_request(host, "DELETE", f"/mesh/teardown?project_id={project.id}")
            except Exception:
                pass
        delete_mesh_peers(db, project.id)
        return False
    return True


def _setup_remote_networks(db, project, host_assignments, vni_map, topology):
    """Set up VXLAN + bridge on remote (non-network) hosts."""
    network_host_id = project.mesh_network_host_id
    all_peers = (
        db.query(ProjectMeshPeer).filter_by(project_id=project.id).all()
    )
    wg_ip_map = {p.host_id: p.wg_address.split("/")[0] for p in all_peers}
    all_wg_ips = list(wg_ip_map.values())

    network_nodes = [
        n for n in topology.get("nodes", [])
        if n.get("type") == "networkNode"
        and n.get("data", {}).get("networkType") != "bmc"
    ]

    errors = []
    for host_id, vm_ids in host_assignments.items():
        if host_id == network_host_id:
            continue

        host = db.query(Host).filter_by(id=host_id).first()
        networks = []
        for node in network_nodes:
            vni = vni_map.get(node["id"])
            if vni:
                networks.append({
                    "vni": vni,
                    "bridge_name": f"br-{vni}",
                    "wg_peer_ips": all_wg_ips,
                })

        params = {
            "project_id": project.id,
            "wg_local_ip": wg_ip_map[host_id],
            "networks": networks,
        }
        try:
            job_id = start_job(host, "/mesh/join-network", params)
            job = wait_for_job(host, job_id, timeout=120)
            if job["status"] == "failed":
                errors.append(f"Host {host_id}: {job.get('result', {}).get('error')}")
        except Exception as e:
            errors.append(f"Host {host_id}: {e}")

    if errors:
        logger.error("Remote network setup failed: %s", errors)
        return False
    return True
```

- [ ] **Step 3: Update `_setup_networks_via_troshkad` to use WireGuard IPs**

In the existing `_setup_networks_via_troshkad()`, modify the peer IP collection to use WireGuard tunnel IPs when the project has a mesh:

```python
def _setup_networks_via_troshkad(host, topology, vni_map, db_session, project_id):
    project = db_session.query(Project).filter_by(id=project_id).first()
    
    if project and project.mesh_subnet_id:
        mesh_peers = (
            db_session.query(ProjectMeshPeer)
            .filter_by(project_id=project_id)
            .all()
        )
        peer_ips = [p.wg_address.split("/")[0] for p in mesh_peers]
    else:
        all_hosts = db_session.query(Host).filter(Host.state == "active").all()
        peer_ips = [h.ip_address for h in all_hosts if h.ip_address]
    
    # ... rest of existing function unchanged, using peer_ips ...
```

Also, when the project is multi-host, pass the network host's WireGuard IP as `host_ip` instead of the real IP:

```python
    if project and project.mesh_subnet_id:
        this_peer = next(
            (p for p in mesh_peers if p.host_id == host.id), None
        )
        host_ip = this_peer.wg_address.split("/")[0] if this_peer else host.ip_address
    else:
        host_ip = host.ip_address
```

- [ ] **Step 4: Update `_deploy_project_inner` for multi-host path**

In `_deploy_project_inner()`, after the placement result is obtained, add a branch for multi-host:

```python
placement_result = place_project(db, project, ...)

if placement_result.get("multi_host"):
    host_assignments = placement_result["host_assignments"]
    network_host_id = placement_result["network_host_id"]
    vni_map = placement_result["vni_map"]
    
    project.mesh_network_host_id = network_host_id
    project.host_id = network_host_id  # primary host for backward compat
    project.vni_map = vni_map
    project.state = "deploying"
    db.commit()
    
    # Step: mesh setup
    if not _setup_mesh(db, project, host_assignments, placement_result["host_ips"]):
        project.state = "error"
        db.commit()
        return
    
    # Step: networks on network host
    network_host = db.query(Host).filter_by(id=network_host_id).first()
    net_result = _setup_networks_via_troshkad(
        network_host, project.topology, vni_map, db, project.id
    )
    if net_result is not True:
        project.state = "error"
        db.commit()
        return
    
    # Step: VXLAN on remote hosts
    if not _setup_remote_networks(db, project, host_assignments, vni_map, project.topology):
        project.state = "error"
        db.commit()
        return
    
    # Step: deploy VMs per host
    for host_id, vm_node_ids in host_assignments.items():
        host = db.query(Host).filter_by(id=host_id).first()
        # Filter topology to only this host's VMs for disk/VM creation
        # ... (existing per-VM deploy logic, filtered by vm_node_ids)
```

The per-host VM deploy logic filters the topology nodes to only include VMs assigned to that host, then runs the existing disk creation → VM definition → VM start pipeline.

- [ ] **Step 5: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All existing tests pass (multi-host path is only triggered when single-host fails)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/deploy_service.py src/backend/app/services/placement.py
git commit -m "feat: multi-host deploy orchestration with WireGuard mesh"
```

---

### Task 6: Multi-Host Teardown

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` (destroy/teardown functions)

**Interfaces:**
- Consumes: `delete_mesh_peers()` (Task 2), `troshkad_request()`, `start_job()`/`wait_for_job()`
- Produces:
  - Updated destroy flow: stop VMs on all hosts → tear down remote VXLAN → tear down network host → tear down WireGuard → clean up DB

- [ ] **Step 1: Update project destroy for multi-host**

Find the destroy function (likely `destroy_project_async` or similar in `deploy_service.py`). Add multi-host awareness:

```python
def _destroy_multihost(db, project):
    """Destroy a multi-host project: VMs, networks, mesh."""
    host_assignments = project.host_assignments or {}
    network_host_id = project.mesh_network_host_id
    
    unique_host_ids = set(host_assignments.values()) if host_assignments else set()
    if project.host_id:
        unique_host_ids.add(project.host_id)
    
    # 1. Stop VMs on all hosts
    for host_id in unique_host_ids:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host or host.agent_status != "connected":
            continue
        try:
            job_id = start_job(host, "/vms/stop-all", {"project_id": project.id})
            wait_for_job(host, job_id, timeout=120)
        except Exception as e:
            logger.warning("Failed to stop VMs on host %s: %s", host_id, e)
    
    # 2. Tear down VXLAN on remote hosts
    for host_id in unique_host_ids:
        if host_id == network_host_id:
            continue
        host = db.query(Host).filter_by(id=host_id).first()
        if not host or host.agent_status != "connected":
            continue
        vni_list = list((project.vni_map or {}).values())
        try:
            job_id = start_job(host, "/networks/full-teardown", {
                "project_id": project.id,
                "vni_list": vni_list,
            })
            wait_for_job(host, job_id, timeout=120)
        except Exception as e:
            logger.warning("Failed to tear down remote network on %s: %s", host_id, e)
    
    # 3. Tear down network host (existing path)
    if network_host_id:
        network_host = db.query(Host).filter_by(id=network_host_id).first()
        if network_host and network_host.agent_status == "connected":
            # Use existing single-host teardown for the network host
            _teardown_networks_via_troshkad(network_host, project, db)
    
    # 4. Tear down WireGuard on all hosts
    for host_id in unique_host_ids:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host or host.agent_status != "connected":
            continue
        try:
            troshkad_request(host, "DELETE", f"/mesh/teardown?project_id={project.id}")
        except Exception as e:
            logger.warning("Failed to teardown mesh on %s: %s", host_id, e)
    
    # 5. Clean up DB
    delete_mesh_peers(db, project.id)
```

- [ ] **Step 2: Wire multi-host destroy into the existing destroy path**

In the existing `destroy_project_async` (or equivalent), add a check:

```python
if project.mesh_subnet_id:
    _destroy_multihost(db, project)
else:
    # existing single-host destroy path
    ...
```

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/deploy_service.py
git commit -m "feat: multi-host project teardown"
```

---

### Task 7: State Polling & Console Routing

**Files:**
- Modify: `src/backend/app/services/ws_pubsub.py:432-480`
- Modify: `src/backend/app/api/projects.py:1777-1810`

**Interfaces:**
- Consumes: `Project.host_assignments` (JSONB), `Host` model
- Produces:
  - Updated `_poll_active_projects()` that queries all hosts in `host_assignments`
  - Updated console token endpoint that routes by VM to the correct host

- [ ] **Step 1: Update state poller for multi-host**

In `src/backend/app/services/ws_pubsub.py`, modify `_map_vm_states_for_project()` (or equivalent) to check multiple hosts:

```python
# Where the poller maps VM states for a project:
if project.host_assignments:
    # Multi-host: gather states from all hosts
    vm_states = {}
    host_ids = set(project.host_assignments.values())
    for hid in host_ids:
        host_batch = host_batch_states.get(hid, {})
        vm_states.update(host_batch)
else:
    # Single-host: existing behavior
    vm_states = host_batch_states.get(project.host_id, {})
```

Also update the host collection in `_poll_active_projects()` to include all hosts from multi-host projects:

```python
# When building the set of hosts to query:
for project in active_projects:
    if project.host_assignments:
        hosts_to_query.update(set(project.host_assignments.values()))
    elif project.host_id:
        hosts_to_query.add(project.host_id)
```

- [ ] **Step 2: Update console token endpoint**

In `src/backend/app/api/projects.py`, update the console endpoint to look up the VM's host:

```python
@router.get("/{project_id}/vms/{vm_id}/console")
def get_vm_console(project_id, vm_id, ...):
    project, _ = _get_project_and_host(project_id, user, db)
    
    # Determine which host this VM is on
    if project.host_assignments and vm_id in project.host_assignments:
        target_host_id = project.host_assignments[vm_id]
        host = db.query(Host).filter_by(id=target_host_id).first()
    else:
        host = db.query(Host).filter_by(id=project.host_id).first()
    
    if not host:
        raise HTTPException(status_code=404, detail="Host not found for VM")
    
    # ... existing JWT signing and URL generation using `host` ...
```

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/ws_pubsub.py src/backend/app/api/projects.py
git commit -m "feat: multi-host state polling and console routing"
```

---

### Task 8: Health Monitoring & Recovery

**Files:**
- Modify: `src/backend/app/services/health_poller.py:110-260`
- Modify: `src/backend/app/services/gc_service.py:291-360`

**Interfaces:**
- Consumes: `troshkad_request()` for `GET /mesh/status`, `ProjectMeshPeer` model
- Produces:
  - Health poller checks WireGuard handshake freshness, stores warnings
  - Recovery restores WireGuard interfaces on reconnect

- [ ] **Step 1: Add WireGuard health check to health poller**

In `src/backend/app/services/health_poller.py`, add a mesh health check in `_poll_hosts()` after the existing capacity sync:

```python
def _check_mesh_health(host, db):
    """Check WireGuard handshake status for any mesh peers on this host."""
    peer_count = (
        db.query(ProjectMeshPeer)
        .filter_by(host_id=host.id)
        .count()
    )
    if peer_count == 0:
        return

    try:
        resp = troshkad_request(host, "GET", "/mesh/status")
        mesh_warnings = []
        import time
        now = int(time.time())
        for project_id, info in resp.get("projects", {}).items():
            if "error" in info:
                mesh_warnings.append(f"WireGuard {project_id[:8]}: {info['error']}")
                continue
            for pubkey, last_handshake in info.get("peers", {}).items():
                if last_handshake > 0 and (now - last_handshake) > 180:
                    mesh_warnings.append(
                        f"WireGuard {project_id[:8]}: peer {pubkey[:8]}… stale ({now - last_handshake}s)"
                    )

        existing_warnings = host.storage_warnings or []
        non_mesh = [w for w in existing_warnings if not w.startswith("WireGuard")]
        host.storage_warnings = non_mesh + mesh_warnings
        db.commit()
    except Exception as e:
        logger.debug("Mesh health check failed for host %s: %s", host.id, e)
```

Add the import at the top: `from app.models.mesh_peer import ProjectMeshPeer`

Call `_check_mesh_health(host, db)` in the per-host poll loop after capacity sync.

- [ ] **Step 2: Add mesh recovery to `recover_host_services()`**

In `src/backend/app/services/gc_service.py`, add mesh recovery at the beginning of `recover_host_services()`:

```python
# After loading host and checking guard conditions:

# Recover WireGuard mesh interfaces
mesh_peers = (
    db.query(ProjectMeshPeer)
    .filter_by(host_id=host_id)
    .all()
)
for peer in mesh_peers:
    try:
        config = get_peer_config_for_host(db, peer.project_id, host_id)
        job_id = start_job(host, "/mesh/setup", config)
        wait_for_job(host, job_id, timeout=60)
        logger.info("Recovered mesh for project %s on host %s", peer.project_id, host_id)
    except Exception as e:
        logger.warning("Failed to recover mesh for %s: %s", peer.project_id, e)
```

Add imports: `from app.models.mesh_peer import ProjectMeshPeer` and `from app.services.mesh_service import get_peer_config_for_host`.

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/health_poller.py src/backend/app/services/gc_service.py
git commit -m "feat: WireGuard health monitoring and host disconnect recovery"
```

---

### Task 9: Security Group Rules

**Files:**
- Modify: `src/backend/app/services/provisioner.py:139-168`
- Modify: `src/backend/app/services/storage_pool_service.py:374-432`

**Interfaces:**
- Consumes: Existing AWS SG patterns
- Produces: WireGuard UDP 51820-51850 rule added to host security groups

- [ ] **Step 1: Add WireGuard rule to host security group (AWS)**

In `src/backend/app/services/provisioner.py`, in the `ensure_security_group()` function, add a WireGuard rule alongside the existing VXLAN UDP 4789 rule:

```python
# After the existing UDP 4789 VXLAN rule:
{
    "IpProtocol": "udp",
    "FromPort": 51820,
    "ToPort": 51850,
    "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "WireGuard mesh"}],
},
```

- [ ] **Step 2: Add WireGuard rule to pool security group (AWS)**

In `src/backend/app/services/storage_pool_service.py`, in `add_sg_rules_for_shared_storage()`, add WireGuard alongside the NFS/libvirt rules:

```python
# In the rules list, after NBD 10809-10829:
if 51820 not in existing_ports:
    rules.append({
        "IpProtocol": "udp",
        "FromPort": 51820,
        "ToPort": 51850,
        "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "WireGuard mesh"}],
    })
```

- [ ] **Step 3: Run full test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/provisioner.py src/backend/app/services/storage_pool_service.py
git commit -m "feat: WireGuard UDP security group rules"
```

---

### Task 10: Template YAML Support for Affinity Groups

**Files:**
- Modify: `src/backend/app/services/template_loader.py:407-625,906-1415`

**Interfaces:**
- Consumes: Template YAML `vms:` section with optional `affinity_group` per VM
- Produces:
  - Import: `affinity_group` on VM spec → `affinityGroup` on topology node data
  - Export: `affinityGroup` on topology node → `affinity_group` in template YAML

- [ ] **Step 1: Write failing test for affinity group import**

Add to an existing template test file or create `src/backend/tests/test_affinity_group.py`:

```python
from app.services.template_loader import resolve_inline_template, generate_topology_from_template


def test_affinity_group_import():
    template = {
        "networks": {
            "net1": {"cidr": "192.168.1.0/24", "dhcp": True},
        },
        "vms": {
            "hub": {
                "cpus": 4,
                "memory_gb": 16,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 50}],
                "affinity_group": "control-plane",
            },
            "worker1": {
                "cpus": 2,
                "memory_gb": 8,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 30}],
                "affinity_group": "workers",
            },
            "worker2": {
                "cpus": 2,
                "memory_gb": 8,
                "nics": [{"network": "net1"}],
                "disks": [{"size_gb": 30}],
                "affinity_group": "workers",
            },
        },
    }

    resolved = resolve_inline_template(template)
    topo = generate_topology_from_template(resolved)

    vm_nodes = [n for n in topo["nodes"] if n["type"] == "vmNode"]
    hub = next(n for n in vm_nodes if n["data"]["name"] == "hub")
    w1 = next(n for n in vm_nodes if n["data"]["name"] == "worker1")
    w2 = next(n for n in vm_nodes if n["data"]["name"] == "worker2")

    assert hub["data"]["affinityGroup"] == "control-plane"
    assert w1["data"]["affinityGroup"] == "workers"
    assert w2["data"]["affinityGroup"] == "workers"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_affinity_group.py -v`
Expected: FAIL — `affinityGroup` not present in node data

- [ ] **Step 3: Add affinity_group to template import**

In `src/backend/app/services/template_loader.py`, in `_generate_topology_from_vms()`, inside the VM processing loop (around where other VM properties are set on the node data dict), add:

```python
# After setting other VM node data fields (cpus, memoryMb, etc.):
if vm_spec.get("affinity_group"):
    node_data["affinityGroup"] = vm_spec["affinity_group"]
```

- [ ] **Step 4: Add affinity_group to template export**

In `export_topology_to_template()`, inside the VM export loop, add:

```python
# After other VM fields are exported:
if node_data.get("affinityGroup"):
    vm_entry["affinity_group"] = node_data["affinityGroup"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_affinity_group.py tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/services/template_loader.py src/backend/tests/test_affinity_group.py
git commit -m "feat: affinity_group support in template YAML import/export"
```

---

### Not In Plan (Deferred)

- **Canvas UI affinity group dropdown** — frontend polish for setting `affinityGroup` on VM nodes via the palette UI. The feature works through template YAML and raw topology JSONB without this. Can be added as a follow-up.
- **Live migration interaction testing** — migration already works between hosts with VXLAN+WireGuard, but needs integration testing on real multi-host clusters.
