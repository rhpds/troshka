# Patterns & VM Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the ability to capture entire projects as "patterns" and individual VMs as "snapshots", then stamp out new projects/VMs from them.

**Architecture:** New `Pattern`, `PatternDisk`, `PatternShare`, and `LibraryItemDisk` models with corresponding API routers, services, and frontend pages. Patterns are first-class entities with their own API (`/api/v1/patterns/`). VM snapshots extend the existing `LibraryItem` model. Both use S3 for disk image storage and qcow2 copy-on-write overlays for efficient deployment.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2 / Alembic / Pydantic (backend); Next.js 15 / PatternFly 6 / React Flow / Zustand (frontend); S3 / qemu-img (storage)

**Spec:** `docs/superpowers/specs/2026-06-07-patterns-and-snapshots-design.md`

---

## File Structure

### Backend — New Files

| File | Responsibility |
|------|---------------|
| `src/backend/app/models/pattern.py` | `Pattern`, `PatternDisk`, `PatternShare` SQLAlchemy models |
| `src/backend/app/schemas/pattern.py` | Pydantic request/response schemas for patterns |
| `src/backend/app/schemas/library.py` | Pydantic schemas for library items (snapshot create/response) |
| `src/backend/app/api/patterns.py` | FastAPI router: CRUD, share, deploy, bulk-deploy, progress |
| `src/backend/app/services/pattern_service.py` | Pattern creation (capture disks to S3), deployment (clone topology), bulk deploy |
| `src/backend/app/services/snapshot_service.py` | VM snapshot capture and import logic |
| `src/backend/alembic/versions/xxxx_add_patterns_and_snapshots.py` | Migration: new tables + LibraryItem changes |
| `src/backend/tests/test_patterns.py` | Pattern API tests |
| `src/backend/tests/test_snapshots.py` | VM snapshot API tests |

### Backend — Modified Files

| File | Change |
|------|--------|
| `src/backend/app/models/__init__.py` | Register new models |
| `src/backend/app/models/library.py` | Add `vm_config` field to `LibraryItem`, add `LibraryItemDisk` model, drop `source_project_id` |
| `src/backend/app/main.py` | Register patterns router |
| `src/backend/app/api/vms.py` | Add `POST /{vm_id}/snapshot` endpoint |
| `src/backend/app/api/projects.py` | Add `POST /{pid}/import-vm` endpoint |
| `src/backend/app/services/deploy_service.py` | Support CoW overlay creation from pattern base images |

### Frontend — New Files

| File | Responsibility |
|------|---------------|
| `src/frontend/src/app/library/patterns/page.tsx` | Patterns browser page (list, search, deploy, bulk deploy) |
| `src/frontend/src/app/library/images/page.tsx` | Images page (ISOs, templates, VM snapshots — refactored from current library page) |
| `src/frontend/src/components/canvas/SavePatternModal.tsx` | Modal for "Save as Pattern" with name/description/tags/visibility |
| `src/frontend/src/components/canvas/SnapshotVMModal.tsx` | Modal for "Save VM Snapshot" with name/description |
| `src/frontend/src/components/canvas/BulkDeployModal.tsx` | Modal for bulk deploy (count, naming convention, auto-deploy toggle) |

### Frontend — Modified Files

| File | Change |
|------|--------|
| `src/frontend/src/app/layout.tsx` | Update nav: Library → sub-items (Images, Patterns) |
| `src/frontend/src/app/library/page.tsx` | Redirect to `/library/images` |
| `src/frontend/src/components/canvas/CanvasToolbar.tsx` | Add "Save as Pattern" button |
| `src/frontend/src/components/canvas/NodeContextMenu.tsx` | Add "Save VM Snapshot" menu item |
| `src/frontend/src/components/canvas/Palette.tsx` | Add "Snapshots" section listing available VM snapshots for drag-drop |
| `src/frontend/src/components/canvas/Canvas.tsx` | Handle drop of snapshot items to create pre-configured VM+disk nodes |
| `src/frontend/src/app/projects/[id]/page.tsx` | Add "Save as Pattern" button on project page |

---

## Task 1: Pattern & PatternDisk Models

**Files:**
- Create: `src/backend/app/models/pattern.py`
- Modify: `src/backend/app/models/__init__.py`
- Test: `src/backend/tests/test_models.py` (append)

- [ ] **Step 1: Write failing test for Pattern model**

Add to the bottom of `src/backend/tests/test_models.py`:

```python
from app.models.pattern import Pattern, PatternDisk, PatternShare


def test_create_pattern():
    db = TestSession()
    user = db.query(User).first()
    pattern = Pattern(
        name="Test Pattern",
        description="A test pattern",
        owner_id=user.id,
        visibility="private",
        topology={"nodes": [], "edges": []},
        state="creating",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)
    assert pattern.id is not None
    assert len(pattern.id) == 36
    assert pattern.name == "Test Pattern"
    assert pattern.visibility == "private"
    assert pattern.state == "creating"
    assert pattern.topology == {"nodes": [], "edges": []}
    assert pattern.created_at is not None
    db.delete(pattern)
    db.commit()
    db.close()


def test_create_pattern_disk():
    db = TestSession()
    user = db.query(User).first()
    pattern = Pattern(
        name="Disk Test Pattern",
        owner_id=user.id,
        visibility="private",
        topology={"nodes": [], "edges": []},
        state="creating",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    disk = PatternDisk(
        pattern_id=pattern.id,
        source_disk_id="aaaa-bbbb-cccc",
        source_vm_id="dddd-eeee-ffff",
        s3_key="patterns/test/disk1.qcow2",
        format="qcow2",
        size_bytes=1073741824,
        virtual_size_bytes=21474836480,
        checksum_sha256="abc123",
        state="uploading",
    )
    db.add(disk)
    db.commit()
    db.refresh(disk)
    assert disk.id is not None
    assert disk.pattern_id == pattern.id
    assert len(pattern.disks) == 1
    db.delete(pattern)
    db.commit()
    db.close()


def test_create_pattern_share():
    db = TestSession()
    user = db.query(User).first()
    pattern = Pattern(
        name="Share Test Pattern",
        owner_id=user.id,
        visibility="shared",
        topology={"nodes": [], "edges": []},
        state="available",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    share = PatternShare(pattern_id=pattern.id, user_id=user.id)
    db.add(share)
    db.commit()
    db.refresh(share)
    assert share.id is not None
    assert share.pattern_id == pattern.id
    assert len(pattern.shares) == 1
    db.delete(pattern)
    db.commit()
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && python3 -m pytest tests/test_models.py::test_create_pattern -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.pattern'`

- [ ] **Step 3: Create the Pattern model file**

Create `src/backend/app/models/pattern.py`:

```python
import datetime
import uuid

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Pattern(Base):
    __tablename__ = "patterns"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    source_project_id: Mapped[str | None] = mapped_column(String(36))
    topology: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="creating")
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["User"] = relationship()
    disks: Mapped[list["PatternDisk"]] = relationship(back_populates="pattern", cascade="all, delete-orphan")
    shares: Mapped[list["PatternShare"]] = relationship(back_populates="pattern", cascade="all, delete-orphan")


class PatternDisk(Base):
    __tablename__ = "pattern_disks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    pattern_id: Mapped[str] = mapped_column(ForeignKey("patterns.id", ondelete="CASCADE"))
    source_disk_id: Mapped[str] = mapped_column(String(36))
    source_vm_id: Mapped[str] = mapped_column(String(36))
    s3_key: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    virtual_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="uploading")

    pattern: Mapped["Pattern"] = relationship(back_populates="disks")


class PatternShare(Base):
    __tablename__ = "pattern_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern_id: Mapped[str] = mapped_column(ForeignKey("patterns.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pattern: Mapped["Pattern"] = relationship(back_populates="shares")
```

- [ ] **Step 4: Register models in `__init__.py`**

Add to `src/backend/app/models/__init__.py`:

```python
from app.models.pattern import Pattern, PatternDisk, PatternShare
```

And add `"Pattern", "PatternDisk", "PatternShare"` to the `__all__` list.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_models.py::test_create_pattern tests/test_models.py::test_create_pattern_disk tests/test_models.py::test_create_pattern_share -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/models/pattern.py src/backend/app/models/__init__.py src/backend/tests/test_models.py
git commit -m "feat: add Pattern, PatternDisk, PatternShare models"
```

---

## Task 2: LibraryItem Changes — vm_config + LibraryItemDisk

**Files:**
- Modify: `src/backend/app/models/library.py`
- Modify: `src/backend/app/models/__init__.py`
- Test: `src/backend/tests/test_models.py` (append)

- [ ] **Step 1: Write failing test for LibraryItemDisk**

Add to the bottom of `src/backend/tests/test_models.py`:

```python
from app.models.library import LibraryItemDisk


def test_create_library_item_disk():
    db = TestSession()
    user = db.query(User).first()
    lib = db.query(Library).filter_by(owner_id=user.id).first()
    if not lib:
        lib = Library(type="user", owner_id=user.id)
        db.add(lib)
        db.commit()
        db.refresh(lib)

    item = LibraryItem(
        library_id=lib.id,
        name="Snapshot VM",
        type="snapshot",
        format="qcow2",
        state="uploading",
        vm_config={"vcpus": 4, "ram": 8192, "nics": []},
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    assert item.vm_config["vcpus"] == 4

    disk = LibraryItemDisk(
        library_item_id=item.id,
        s3_key="snapshots/test/disk1.qcow2",
        format="qcow2",
        size_bytes=1073741824,
        virtual_size_bytes=21474836480,
        boot_order=0,
        checksum_sha256="def456",
        state="uploading",
    )
    db.add(disk)
    db.commit()
    db.refresh(disk)
    assert disk.id is not None
    assert disk.library_item_id == item.id
    assert len(item.item_disks) == 1
    db.delete(item)
    db.commit()
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && python3 -m pytest tests/test_models.py::test_create_library_item_disk -v`
Expected: FAIL — `LibraryItemDisk` not defined, `vm_config` not a column

- [ ] **Step 3: Add vm_config and LibraryItemDisk to library model**

Edit `src/backend/app/models/library.py`:

Add `vm_config` field to `LibraryItem` (after `source_project_id`):

```python
    vm_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

Remove `source_project_id` line from `LibraryItem`.

Add `item_disks` relationship to `LibraryItem` (after the `library` relationship):

```python
    item_disks: Mapped[list["LibraryItemDisk"]] = relationship(back_populates="library_item", cascade="all, delete-orphan")
```

Add `LibraryItemDisk` class after `LibraryShare`:

```python
class LibraryItemDisk(Base):
    __tablename__ = "library_item_disks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    library_item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    s3_key: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    virtual_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    boot_order: Mapped[int] = mapped_column(Integer, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="uploading")

    library_item: Mapped["LibraryItem"] = relationship(back_populates="item_disks")
```

- [ ] **Step 4: Register LibraryItemDisk in `__init__.py`**

Add `LibraryItemDisk` to the import from `app.models.library` and to `__all__` in `src/backend/app/models/__init__.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src/backend && python3 -m pytest tests/test_models.py::test_create_library_item_disk -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/models/library.py src/backend/app/models/__init__.py src/backend/tests/test_models.py
git commit -m "feat: add vm_config to LibraryItem, add LibraryItemDisk model"
```

---

## Task 3: Alembic Migration

**Files:**
- Create: `src/backend/alembic/versions/xxxx_add_patterns_and_snapshots.py`

- [ ] **Step 1: Generate the migration**

Run: `cd src/backend && python3 -m alembic revision -m "add patterns snapshots tables"`

- [ ] **Step 2: Write the migration**

Edit the generated file with the following upgrade/downgrade:

```python
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '<generated>'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patterns',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('visibility', sa.String(20), nullable=False, server_default='private'),
        sa.Column('source_project_id', sa.String(36), nullable=True),
        sa.Column('topology', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('state', sa.String(20), nullable=False, server_default='creating'),
        sa.Column('total_size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'pattern_disks',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('pattern_id', sa.String(36), sa.ForeignKey('patterns.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_disk_id', sa.String(36), nullable=False),
        sa.Column('source_vm_id', sa.String(36), nullable=False),
        sa.Column('s3_key', sa.String(500), nullable=False),
        sa.Column('format', sa.String(10), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('virtual_size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('checksum_sha256', sa.String(64), nullable=True),
        sa.Column('state', sa.String(20), nullable=False, server_default='uploading'),
    )

    op.create_table(
        'pattern_shares',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('pattern_id', sa.String(36), sa.ForeignKey('patterns.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'library_item_disks',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column('library_item_id', sa.String(36), sa.ForeignKey('library_items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('s3_key', sa.String(500), nullable=False),
        sa.Column('format', sa.String(10), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('virtual_size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('boot_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('checksum_sha256', sa.String(64), nullable=True),
        sa.Column('state', sa.String(20), nullable=False, server_default='uploading'),
    )

    op.add_column('library_items', sa.Column('vm_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_column('library_items', 'source_project_id')


def downgrade() -> None:
    op.add_column('library_items', sa.Column('source_project_id', sa.String(36), nullable=True))
    op.drop_column('library_items', 'vm_config')
    op.drop_table('library_item_disks')
    op.drop_table('pattern_shares')
    op.drop_table('pattern_disks')
    op.drop_table('patterns')
```

- [ ] **Step 3: Run migration against dev database**

Run: `cd src/backend && python3 -m alembic upgrade head`
Expected: Tables created successfully

- [ ] **Step 4: Commit**

```bash
git add src/backend/alembic/versions/
git commit -m "migration: add patterns and snapshots tables"
```

---

## Task 4: Pattern Pydantic Schemas

**Files:**
- Create: `src/backend/app/schemas/pattern.py`

- [ ] **Step 1: Create pattern schemas**

Create `src/backend/app/schemas/pattern.py`:

```python
import datetime

from pydantic import BaseModel


class PatternDiskResponse(BaseModel):
    id: str
    source_disk_id: str
    source_vm_id: str
    s3_key: str
    format: str
    size_bytes: int
    virtual_size_bytes: int
    checksum_sha256: str | None = None
    state: str

    model_config = {"from_attributes": True}


class PatternCreate(BaseModel):
    name: str
    description: str | None = None
    visibility: str = "private"
    tags: dict | None = None
    source_project_id: str | None = None
    topology: dict | None = None
    disk_mappings: list[dict] | None = None


class PatternUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    visibility: str | None = None
    tags: dict | None = None


class PatternResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    visibility: str
    source_project_id: str | None = None
    topology: dict
    state: str
    total_size_bytes: int
    tags: dict | None = None
    created_at: datetime.datetime
    disks: list[PatternDiskResponse] = []

    model_config = {"from_attributes": True}


class PatternListResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    visibility: str
    state: str
    total_size_bytes: int
    tags: dict | None = None
    created_at: datetime.datetime
    disk_count: int = 0
    vm_count: int = 0

    model_config = {"from_attributes": True}


class PatternShareRequest(BaseModel):
    user_email: str


class PatternDeployRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class PatternBulkDeployRequest(BaseModel):
    count: int
    name_template: str
    auto_deploy: bool = False
```

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/schemas/pattern.py
git commit -m "feat: add Pattern pydantic schemas"
```

---

## Task 5: Pattern API — CRUD Endpoints

**Files:**
- Create: `src/backend/app/api/patterns.py`
- Modify: `src/backend/app/main.py`
- Test: `src/backend/tests/test_patterns.py`

- [ ] **Step 1: Write failing tests for pattern CRUD**

Create `src/backend/tests/test_patterns.py`:

```python
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="pattern-test@example.com", display_name="Pattern Tester", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

SAMPLE_TOPOLOGY = {
    "nodes": [
        {"id": "vm-1", "type": "vmNode", "position": {"x": 0, "y": 0},
         "data": {"name": "web", "vcpus": 2, "ram": 4096}},
        {"id": "net-1", "type": "networkNode", "position": {"x": 200, "y": 0},
         "data": {"name": "mgmt", "cidr": "10.0.1.0/24"}},
    ],
    "edges": [
        {"source": "vm-1", "target": "net-1"},
    ],
}


def test_create_pattern_from_payload():
    resp = client.post("/api/v1/patterns", json={
        "name": "Test Pattern",
        "description": "A test",
        "topology": SAMPLE_TOPOLOGY,
        "visibility": "private",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Pattern"
    assert data["owner_id"] == USER_ID
    assert data["state"] == "available"
    assert data["visibility"] == "private"
    assert data["topology"]["nodes"][0]["id"] == "vm-1"


def test_list_patterns():
    resp = client.get("/api/v1/patterns", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["name"] == "Test Pattern"


def test_get_pattern():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Pattern"
    assert "topology" in resp.json()


def test_update_pattern():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]
    resp = client.patch(f"/api/v1/patterns/{pattern_id}", json={
        "name": "Renamed Pattern",
        "visibility": "public",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Pattern"
    assert resp.json()["visibility"] == "public"


def test_delete_pattern():
    create_resp = client.post("/api/v1/patterns", json={
        "name": "To Delete",
        "topology": SAMPLE_TOPOLOGY,
    }, headers=HEADERS)
    pattern_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert resp.status_code == 204
    get_resp = client.get(f"/api/v1/patterns/{pattern_id}", headers=HEADERS)
    assert get_resp.status_code == 404


def test_unauthorized_access():
    resp = client.get("/api/v1/patterns")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py -v`
Expected: FAIL — 404s because the router doesn't exist yet

- [ ] **Step 3: Create the patterns API router**

Create `src/backend/app/api/patterns.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.pattern import Pattern, PatternShare
from app.models.user import User
from app.schemas.pattern import (
    PatternCreate,
    PatternDeployRequest,
    PatternBulkDeployRequest,
    PatternResponse,
    PatternShareRequest,
    PatternUpdate,
)

router = APIRouter(prefix="/patterns", tags=["patterns"])


def _check_pattern_access(pattern: Pattern, user: User) -> bool:
    if pattern.owner_id == user.id:
        return True
    if user.role == "admin":
        return True
    if pattern.visibility == "public":
        return True
    if pattern.visibility == "shared":
        for share in pattern.shares:
            if share.user_id == user.id:
                return True
    return False


@router.post("/", response_model=PatternResponse, status_code=201)
def create_pattern(
    body: PatternCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.source_project_id and not body.topology:
        raise HTTPException(status_code=400, detail="Provide source_project_id or topology")

    if body.source_project_id:
        from app.models.project import Project
        project = db.query(Project).filter_by(id=body.source_project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Source project not found")
        if project.owner_id != user.id and user.role != "admin":
            raise HTTPException(status_code=403, detail="Access denied")
        topology = project.topology or {"nodes": [], "edges": []}
    else:
        topology = body.topology

    pattern = Pattern(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        visibility=body.visibility,
        source_project_id=body.source_project_id,
        topology=topology,
        tags=body.tags,
        state="available" if not body.source_project_id else "creating",
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    if body.source_project_id:
        import threading
        from app.services.pattern_service import capture_pattern_disks
        threading.Thread(
            target=capture_pattern_disks,
            args=(pattern.id, body.source_project_id),
            daemon=True,
        ).start()

    return pattern


@router.get("/", response_model=list[PatternResponse])
def list_patterns(
    visibility: str | None = None,
    q: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Pattern)

    if user.role != "admin":
        query = query.filter(
            (Pattern.owner_id == user.id)
            | (Pattern.visibility == "public")
            | (
                (Pattern.visibility == "shared")
                & (Pattern.id.in_(
                    db.query(PatternShare.pattern_id).filter_by(user_id=user.id)
                ))
            )
        )

    if visibility:
        query = query.filter_by(visibility=visibility)
    if q:
        query = query.filter(Pattern.name.ilike(f"%{q}%"))

    return query.order_by(Pattern.created_at.desc()).all()


@router.get("/{pattern_id}", response_model=PatternResponse)
def get_pattern(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if not _check_pattern_access(pattern, user):
        raise HTTPException(status_code=403, detail="Access denied")
    return pattern


@router.patch("/{pattern_id}", response_model=PatternResponse)
def update_pattern(
    pattern_id: str,
    body: PatternUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the owner can update")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(pattern, field, value)
    db.commit()
    db.refresh(pattern)
    return pattern


@router.delete("/{pattern_id}", status_code=204)
def delete_pattern(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the owner can delete")

    from app.services import s3_storage
    for disk in pattern.disks:
        try:
            s3_storage.delete_file(disk.s3_key)
        except Exception:
            pass

    db.delete(pattern)
    db.commit()


@router.post("/{pattern_id}/share")
def share_pattern(
    pattern_id: str,
    body: PatternShareRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the owner can share")

    target_user = db.query(User).filter_by(email=body.user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User {body.user_email} not found")
    if target_user.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot share with yourself")

    existing = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=target_user.id).first()
    if existing:
        return {"shared_with": body.user_email, "status": "already shared"}

    db.add(PatternShare(pattern_id=pattern_id, user_id=target_user.id))
    if pattern.visibility == "private":
        pattern.visibility = "shared"
    db.commit()
    return {"shared_with": body.user_email, "status": "shared"}


@router.delete("/{pattern_id}/share/{user_email}")
def unshare_pattern(
    pattern_id: str,
    user_email: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if pattern.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the owner can unshare")

    target_user = db.query(User).filter_by(email=user_email).first()
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User {user_email} not found")

    share = db.query(PatternShare).filter_by(pattern_id=pattern_id, user_id=target_user.id).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")

    db.delete(share)
    db.commit()
    return {"unshared": user_email}


@router.get("/{pattern_id}/progress")
def get_pattern_progress(
    pattern_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if not _check_pattern_access(pattern, user):
        raise HTTPException(status_code=403, detail="Access denied")

    from app.services.pattern_service import get_capture_progress
    return {
        "state": pattern.state,
        "progress": get_capture_progress(pattern_id),
    }
```

- [ ] **Step 4: Register the router in main.py**

Add to `src/backend/app/main.py` after the library import:

```python
from app.api import patterns as pattern_routes  # noqa: E402
```

And after the library router registration:

```python
app.include_router(pattern_routes.router, prefix="/api/v1")
```

- [ ] **Step 5: Create stub pattern_service**

Create `src/backend/app/services/pattern_service.py`:

```python
_capture_progress: dict[str, dict] = {}


def capture_pattern_disks(pattern_id: str, project_id: str) -> None:
    pass


def get_capture_progress(pattern_id: str) -> dict | None:
    return _capture_progress.get(pattern_id)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py -v`
Expected: 6 PASSED

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/api/patterns.py src/backend/app/schemas/pattern.py src/backend/app/services/pattern_service.py src/backend/app/main.py src/backend/tests/test_patterns.py
git commit -m "feat: add Pattern CRUD API with sharing and progress endpoints"
```

---

## Task 6: Pattern Deploy Endpoint (Single Project from Pattern)

**Files:**
- Modify: `src/backend/app/api/patterns.py`
- Test: `src/backend/tests/test_patterns.py` (append)

- [ ] **Step 1: Write failing test for deploy**

Add to `src/backend/tests/test_patterns.py`:

```python
def test_deploy_pattern_creates_project():
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Deploy Test",
        "topology": SAMPLE_TOPOLOGY,
    }, headers=HEADERS)
    pattern_id = create_resp.json()["id"]

    resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={
        "name": "My Lab Instance",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Lab Instance"
    assert data["state"] == "draft"
    assert data["topology"] is not None
    nodes = data["topology"]["nodes"]
    assert len(nodes) == 2
    assert nodes[0]["id"] != "vm-1"


def test_deploy_pattern_default_name():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    patterns = [p for p in list_resp.json() if p["name"] == "Deploy Test"]
    pattern_id = patterns[0]["id"]

    resp = client.post(f"/api/v1/patterns/{pattern_id}/deploy", json={}, headers=HEADERS)
    assert resp.status_code == 201
    assert "Deploy Test" in resp.json()["name"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py::test_deploy_pattern_creates_project tests/test_patterns.py::test_deploy_pattern_default_name -v`
Expected: FAIL — 404 or 405

- [ ] **Step 3: Add deploy endpoint to patterns router**

Add to `src/backend/app/api/patterns.py`:

```python
import uuid

from app.models.project import Project
from app.schemas.project import ProjectResponse


def _remap_topology(topology: dict) -> dict:
    id_map = {}
    new_nodes = []
    for node in topology.get("nodes", []):
        old_id = node["id"]
        new_id = str(uuid.uuid4())
        id_map[old_id] = new_id
        new_node = {**node, "id": new_id}
        data = dict(new_node.get("data", {}))
        if "nics" in data:
            data["nics"] = [
                {**nic, "id": f"nic-{uuid.uuid4()}", "mac": _generate_mac()}
                for nic in data["nics"]
            ]
        if "diskControllers" in data:
            data["diskControllers"] = [
                {**dc, "id": f"dp-{uuid.uuid4()}"}
                for dc in data["diskControllers"]
            ]
        new_node["data"] = data
        new_nodes.append(new_node)

    new_edges = []
    for edge in topology.get("edges", []):
        new_edge = {**edge}
        if edge.get("source") in id_map:
            new_edge["source"] = id_map[edge["source"]]
        if edge.get("target") in id_map:
            new_edge["target"] = id_map[edge["target"]]
        new_edges.append(new_edge)

    return {"nodes": new_nodes, "edges": new_edges}


def _generate_mac() -> str:
    import random
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


@router.post("/{pattern_id}/deploy", response_model=ProjectResponse, status_code=201)
def deploy_pattern(
    pattern_id: str,
    body: PatternDeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if not _check_pattern_access(pattern, user):
        raise HTTPException(status_code=403, detail="Access denied")
    if pattern.state != "available":
        raise HTTPException(status_code=400, detail="Pattern is not ready")

    name = body.name or f"{pattern.name} - copy"
    new_topology = _remap_topology(pattern.topology)

    project = Project(
        name=name,
        description=body.description or pattern.description,
        owner_id=user.id,
        topology=new_topology,
        state="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py::test_deploy_pattern_creates_project tests/test_patterns.py::test_deploy_pattern_default_name -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/api/patterns.py src/backend/tests/test_patterns.py
git commit -m "feat: add pattern deploy endpoint with topology remapping"
```

---

## Task 7: Bulk Deploy Endpoint

**Files:**
- Modify: `src/backend/app/api/patterns.py`
- Test: `src/backend/tests/test_patterns.py` (append)

- [ ] **Step 1: Write failing test for bulk deploy**

Add to `src/backend/tests/test_patterns.py`:

```python
def test_bulk_deploy_pattern():
    create_resp = client.post("/api/v1/patterns", json={
        "name": "Bulk Test",
        "topology": SAMPLE_TOPOLOGY,
    }, headers=HEADERS)
    pattern_id = create_resp.json()["id"]

    resp = client.post(f"/api/v1/patterns/{pattern_id}/bulk-deploy", json={
        "count": 3,
        "name_template": "lab-{n}",
        "auto_deploy": False,
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["projects"]) == 3
    names = [p["name"] for p in data["projects"]]
    assert "lab-001" in names
    assert "lab-002" in names
    assert "lab-003" in names
    for p in data["projects"]:
        assert p["state"] == "draft"


def test_bulk_deploy_validates_count():
    list_resp = client.get("/api/v1/patterns", headers=HEADERS)
    pattern_id = list_resp.json()[0]["id"]

    resp = client.post(f"/api/v1/patterns/{pattern_id}/bulk-deploy", json={
        "count": 0,
        "name_template": "lab-{n}",
    }, headers=HEADERS)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py::test_bulk_deploy_pattern tests/test_patterns.py::test_bulk_deploy_validates_count -v`
Expected: FAIL — 404 or 405

- [ ] **Step 3: Add bulk-deploy endpoint**

Add to `src/backend/app/api/patterns.py`:

```python
@router.post("/{pattern_id}/bulk-deploy", status_code=201)
def bulk_deploy_pattern(
    pattern_id: str,
    body: PatternBulkDeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.count < 1 or body.count > 500:
        raise HTTPException(status_code=400, detail="Count must be between 1 and 500")

    pattern = db.query(Pattern).filter_by(id=pattern_id).first()
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if not _check_pattern_access(pattern, user):
        raise HTTPException(status_code=403, detail="Access denied")
    if pattern.state != "available":
        raise HTTPException(status_code=400, detail="Pattern is not ready")

    projects = []
    for i in range(1, body.count + 1):
        name = body.name_template.replace("{n}", str(i).zfill(3))
        new_topology = _remap_topology(pattern.topology)
        project = Project(
            name=name,
            description=pattern.description,
            owner_id=user.id,
            topology=new_topology,
            state="draft",
        )
        db.add(project)
        projects.append(project)

    db.commit()
    for p in projects:
        db.refresh(p)

    return {"projects": [{"id": p.id, "name": p.name, "state": p.state} for p in projects]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_patterns.py::test_bulk_deploy_pattern tests/test_patterns.py::test_bulk_deploy_validates_count -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/api/patterns.py src/backend/tests/test_patterns.py
git commit -m "feat: add bulk deploy endpoint for patterns"
```

---

## Task 8: VM Snapshot Endpoint

**Files:**
- Modify: `src/backend/app/api/vms.py`
- Create: `src/backend/app/schemas/library.py`
- Create: `src/backend/app/services/snapshot_service.py`
- Test: `src/backend/tests/test_snapshots.py`

- [ ] **Step 1: Write failing tests**

Create `src/backend/tests/test_snapshots.py`:

```python
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from app.models.library import Library
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

_db = TestSession()
_user = User(email="snap-test@example.com", display_name="Snap Tester", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_lib = Library(type="user", owner_id=_user.id)
_db.add(_user)
_db.add(_lib)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _create_project_with_vm():
    topology = {
        "nodes": [
            {"id": "vm-1", "type": "vmNode", "position": {"x": 0, "y": 0},
             "data": {"name": "webserver", "vcpus": 4, "ram": 8192, "os": "rhel10",
                      "nics": [{"id": "nic-1", "name": "eth0", "mac": "52:54:00:aa:bb:cc", "model": "virtio"}],
                      "diskControllers": [{"id": "dp-1", "name": "disk0", "bus": "virtio"}]}},
            {"id": "disk-1", "type": "storageNode", "position": {"x": 100, "y": 100},
             "data": {"name": "root", "size": 40, "format": "qcow2"}},
        ],
        "edges": [
            {"source": "vm-1", "target": "disk-1"},
        ],
    }
    resp = client.post("/api/v1/projects", json={
        "name": "Snap Project",
        "description": "For snapshot testing",
    }, headers=HEADERS)
    project_id = resp.json()["id"]
    client.patch(f"/api/v1/projects/{project_id}", json={
        "topology": topology,
    }, headers=HEADERS)
    return project_id


def test_snapshot_vm():
    project_id = _create_project_with_vm()
    resp = client.post(f"/api/v1/projects/{project_id}/vms/vm-1/snapshot", json={
        "name": "webserver snapshot",
        "description": "Pre-configured web server",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "webserver snapshot"
    assert data["type"] == "snapshot"
    assert data["vm_config"]["vcpus"] == 4
    assert data["vm_config"]["ram"] == 8192


def test_snapshot_vm_not_found():
    project_id = _create_project_with_vm()
    resp = client.post(f"/api/v1/projects/{project_id}/vms/nonexistent/snapshot", json={
        "name": "nope",
    }, headers=HEADERS)
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_snapshots.py -v`
Expected: FAIL — endpoint doesn't exist

- [ ] **Step 3: Create library schemas**

Create `src/backend/app/schemas/library.py`:

```python
import datetime

from pydantic import BaseModel


class SnapshotCreate(BaseModel):
    name: str
    description: str | None = None


class LibraryItemDiskResponse(BaseModel):
    id: str
    s3_key: str
    format: str
    size_bytes: int
    virtual_size_bytes: int
    boot_order: int
    checksum_sha256: str | None = None
    state: str

    model_config = {"from_attributes": True}


class SnapshotResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    type: str
    format: str
    state: str
    vm_config: dict | None = None
    source_vm_id: str | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Create snapshot service stub**

Create `src/backend/app/services/snapshot_service.py`:

```python
def capture_vm_disks(library_item_id: str, project_id: str, vm_node_id: str) -> None:
    pass
```

- [ ] **Step 5: Add snapshot endpoint to vms router**

Add to the bottom of `src/backend/app/api/vms.py`:

```python
from app.models.library import Library, LibraryItem
from app.schemas.library import SnapshotCreate, SnapshotResponse


@router.post("/{vm_id}/snapshot", response_model=SnapshotResponse, status_code=201)
def snapshot_vm(
    project_id: str,
    vm_id: str,
    body: SnapshotCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    topology = project.topology or {"nodes": [], "edges": []}
    vm_node = None
    for node in topology.get("nodes", []):
        if node["id"] == vm_id and node.get("type") == "vmNode":
            vm_node = node
            break
    if not vm_node:
        raise HTTPException(status_code=404, detail="VM not found in topology")

    vm_data = vm_node.get("data", {})
    vm_config = {
        "vcpus": vm_data.get("vcpus"),
        "ram": vm_data.get("ram"),
        "os": vm_data.get("os"),
        "nics": vm_data.get("nics", []),
        "diskControllers": vm_data.get("diskControllers", []),
        "bootMethod": vm_data.get("bootMethod"),
        "cloudInit": vm_data.get("cloudInit"),
        "consoleType": vm_data.get("consoleType"),
        "autoStart": vm_data.get("autoStart"),
    }

    lib = db.query(Library).filter_by(owner_id=user.id, type="user").first()
    if not lib:
        lib = Library(type="user", owner_id=user.id)
        db.add(lib)
        db.commit()
        db.refresh(lib)

    item = LibraryItem(
        library_id=lib.id,
        name=body.name,
        description=body.description,
        type="snapshot",
        format="qcow2",
        state="uploading",
        source_vm_id=vm_id,
        vm_config=vm_config,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    if project.state in ("active", "stopped"):
        import threading
        from app.services.snapshot_service import capture_vm_disks
        threading.Thread(
            target=capture_vm_disks,
            args=(item.id, project.id, vm_id),
            daemon=True,
        ).start()
    else:
        item.state = "available"
        db.commit()

    return item
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_snapshots.py -v`
Expected: 2 PASSED

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/api/vms.py src/backend/app/schemas/library.py src/backend/app/services/snapshot_service.py src/backend/tests/test_snapshots.py
git commit -m "feat: add VM snapshot endpoint"
```

---

## Task 9: Import VM Snapshot into Project

**Files:**
- Modify: `src/backend/app/api/projects.py`
- Test: `src/backend/tests/test_snapshots.py` (append)

- [ ] **Step 1: Write failing test**

Add to `src/backend/tests/test_snapshots.py`:

```python
def test_import_vm_snapshot():
    project_id = _create_project_with_vm()
    snap_resp = client.post(f"/api/v1/projects/{project_id}/vms/vm-1/snapshot", json={
        "name": "import test snap",
    }, headers=HEADERS)
    snapshot_id = snap_resp.json()["id"]

    new_project_resp = client.post("/api/v1/projects", json={
        "name": "Import Target",
    }, headers=HEADERS)
    target_id = new_project_resp.json()["id"]

    resp = client.post(f"/api/v1/projects/{target_id}/import-vm", json={
        "snapshot_id": snapshot_id,
    }, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    topology = data["topology"]
    vm_nodes = [n for n in topology["nodes"] if n["type"] == "vmNode"]
    assert len(vm_nodes) == 1
    assert vm_nodes[0]["data"]["vcpus"] == 4
    assert vm_nodes[0]["data"]["ram"] == 8192
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && python3 -m pytest tests/test_snapshots.py::test_import_vm_snapshot -v`
Expected: FAIL — 404 or 405

- [ ] **Step 3: Add import-vm endpoint to projects router**

Add to `src/backend/app/api/projects.py`:

```python
import uuid as uuid_mod

from pydantic import BaseModel as PydanticBaseModel


class ImportVMRequest(PydanticBaseModel):
    snapshot_id: str
    position_x: float = 100.0
    position_y: float = 100.0


@router.post("/{project_id}/import-vm", response_model=ProjectResponse)
def import_vm_from_snapshot(
    project_id: str,
    body: ImportVMRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    from app.models.library import LibraryItem
    item = db.query(LibraryItem).filter_by(id=body.snapshot_id, type="snapshot").first()
    if not item:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    vm_config = item.vm_config or {}
    vm_id = str(uuid_mod.uuid4())
    vm_node = {
        "id": vm_id,
        "type": "vmNode",
        "position": {"x": body.position_x, "y": body.position_y},
        "data": {
            "label": item.name,
            "name": item.name,
            "vcpus": vm_config.get("vcpus", 2),
            "ram": vm_config.get("ram", 4096),
            "os": vm_config.get("os", ""),
            "status": "stopped",
            "icon": "\U0001f5a5",
            "nics": [
                {**nic, "id": f"nic-{uuid_mod.uuid4()}", "mac": _generate_import_mac()}
                for nic in vm_config.get("nics", [])
            ],
            "diskControllers": [
                {**dc, "id": f"dp-{uuid_mod.uuid4()}"}
                for dc in vm_config.get("diskControllers", [])
            ],
            "bootMethod": vm_config.get("bootMethod"),
            "cloudInit": vm_config.get("cloudInit"),
            "consoleType": vm_config.get("consoleType"),
            "autoStart": vm_config.get("autoStart"),
            "snapshotItemId": item.id,
        },
    }

    topology = project.topology or {"nodes": [], "edges": []}
    topology["nodes"].append(vm_node)

    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy import cast
    db.query(Project).filter_by(id=project_id).update(
        {"topology": topology}, synchronize_session="fetch"
    )
    db.commit()
    db.refresh(project)
    return project


def _generate_import_mac() -> str:
    import random
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && python3 -m pytest tests/test_snapshots.py::test_import_vm_snapshot -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/api/projects.py src/backend/tests/test_snapshots.py
git commit -m "feat: add import-vm endpoint for importing snapshots into projects"
```

---

## Task 10: Pattern Capture Service (Disk Upload to S3)

**Files:**
- Modify: `src/backend/app/services/pattern_service.py`

- [ ] **Step 1: Implement capture_pattern_disks**

Replace the contents of `src/backend/app/services/pattern_service.py`:

```python
import logging

from app.core.database import SessionLocal
from app.models.pattern import Pattern, PatternDisk

log = logging.getLogger(__name__)

_capture_progress: dict[str, dict] = {}


def get_capture_progress(pattern_id: str) -> dict | None:
    return _capture_progress.get(pattern_id)


def capture_pattern_disks(pattern_id: str, project_id: str) -> None:
    from app.models.project import Project
    from app.models.host import Host
    from app.services import s3_storage
    from app.services.deploy_service import run_ssh_script

    db = SessionLocal()
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        project = db.query(Project).filter_by(id=project_id).first()
        if not pattern or not project:
            log.error("Pattern or project not found: %s / %s", pattern_id, project_id)
            return

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            pattern.state = "error"
            db.commit()
            log.error("No host found for project %s", project_id)
            return

        topology = project.deployed_topology or project.topology or {"nodes": [], "edges": []}
        disk_nodes = [n for n in topology.get("nodes", []) if n.get("type") == "storageNode"]
        vm_nodes = {n["id"]: n for n in topology.get("nodes", []) if n.get("type") == "vmNode"}

        edges = topology.get("edges", [])
        disk_to_vm = {}
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            if src in vm_nodes and tgt in [d["id"] for d in disk_nodes]:
                disk_to_vm[tgt] = src
            elif tgt in vm_nodes and src in [d["id"] for d in disk_nodes]:
                disk_to_vm[src] = tgt

        total = len(disk_nodes)
        for idx, disk_node in enumerate(disk_nodes):
            disk_id = disk_node["id"]
            vm_id = disk_to_vm.get(disk_id, "unknown")
            fmt = disk_node.get("data", {}).get("format", "qcow2")
            s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"

            _capture_progress[pattern_id] = {
                "step": "uploading",
                "detail": f"disk {idx + 1}/{total}",
                "disk_id": disk_id,
            }

            disk_path = f"/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{fmt}"

            presigned = s3_storage.generate_presigned_upload_url(s3_key, expires=7200)

            script = f"""
set -e
DISK_PATH="{disk_path}"
UPLOAD_URL='{presigned}'

if [ ! -f "$DISK_PATH" ]; then
    echo "ERROR: disk not found at $DISK_PATH"
    exit 1
fi

curl -X PUT -T "$DISK_PATH" "$UPLOAD_URL"
echo "UPLOAD_COMPLETE"
"""
            result = run_ssh_script(host.ip_address, host.private_key, script, timeout=3600)

            pd = PatternDisk(
                pattern_id=pattern_id,
                source_disk_id=disk_id,
                source_vm_id=vm_id,
                s3_key=s3_key,
                format=fmt,
                size_bytes=0,
                virtual_size_bytes=int(disk_node.get("data", {}).get("size", 0)) * 1073741824,
                state="available" if result["success"] else "error",
            )
            db.add(pd)
            db.commit()

            if not result["success"]:
                log.error("Failed to upload disk %s: %s", disk_id, result.get("output", ""))
                pattern.state = "error"
                db.commit()
                return

        pattern.state = "available"
        pattern.total_size_bytes = sum(d.size_bytes for d in pattern.disks)
        db.commit()
        log.info("Pattern %s capture complete", pattern_id)

    except Exception as e:
        log.exception("Pattern capture failed for %s: %s", pattern_id, e)
        try:
            pattern = db.query(Pattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.state = "error"
                db.commit()
        except Exception:
            pass
    finally:
        _capture_progress.pop(pattern_id, None)
        db.close()
```

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/services/pattern_service.py
git commit -m "feat: implement pattern disk capture service with S3 upload"
```

---

## Task 11: Deploy Service — CoW Overlay Support for Patterns

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

- [ ] **Step 1: Add pattern disk handling to deploy flow**

In `src/backend/app/services/deploy_service.py`, find the `generate_vm_script()` function where disks are created. After the existing library source check (`if disk.get("source") == "library"`), add a pattern source check:

```python
elif disk.get("source") == "pattern" and disk.get("patternDiskS3Key"):
    cache_path = f"/var/lib/troshka/cache/patterns/{disk['patternId']}/{disk['patternDiskId']}.{disk['format']}"
    lines.append(f"qemu-img create -f {disk['format']} -b {cache_path} -F {disk['format']} {dp} {disk['size_gb']}G")
```

Also add pattern image caching to the `cache_library_images()` function (or create a parallel `cache_pattern_images()` function) that downloads pattern disk images from S3 to the host cache path before VM creation.

The exact line numbers and surrounding context will vary — the implementer should read the current state of `deploy_service.py` and find the appropriate insertion points. The pattern follows the existing library image caching flow: generate presigned URL, download to cache path via SSH, poll for completion.

- [ ] **Step 2: Commit**

```bash
git add src/backend/app/services/deploy_service.py
git commit -m "feat: support CoW overlay disks from pattern base images"
```

---

## Task 12: Frontend — Library Navigation Redesign

**Files:**
- Modify: `src/frontend/src/app/layout.tsx`
- Create: `src/frontend/src/app/library/page.tsx` (redirect)
- Create: `src/frontend/src/app/library/images/page.tsx`
- Create: `src/frontend/src/app/library/patterns/page.tsx`

- [ ] **Step 1: Update sidebar navigation**

In `src/frontend/src/app/layout.tsx`, replace the Library nav item with expandable sub-items. Change the `navItems` array:

```typescript
const navItems = [
  { label: "Projects", path: "/projects" },
  { label: "Images", path: "/library/images" },
  { label: "Patterns", path: "/library/patterns" },
  { label: "Settings", path: "/settings" },
];
```

- [ ] **Step 2: Make /library redirect to /library/images**

Replace `src/frontend/src/app/library/page.tsx` with a redirect:

```typescript
"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function LibraryRedirect() {
  const router = useRouter();
  useEffect(() => { router.replace("/library/images"); }, [router]);
  return null;
}
```

- [ ] **Step 3: Move existing library page to /library/images**

Copy the current `src/frontend/src/app/library/page.tsx` content to `src/frontend/src/app/library/images/page.tsx`. This is the existing library page with ISOs, templates, and will also show VM snapshots.

- [ ] **Step 4: Create patterns page stub**

Create `src/frontend/src/app/library/patterns/page.tsx`:

```typescript
"use client";

import React, { useState, useEffect } from "react";
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  EmptyState,
  EmptyStateBody,
  EmptyStateHeader,
  Label,
  PageSection,
  SearchInput,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";

interface PatternSummary {
  id: string;
  name: string;
  description: string | null;
  visibility: string;
  state: string;
  total_size_bytes: number;
  tags: Record<string, string> | null;
  created_at: string;
  disks: { id: string }[];
}

export default function PatternsPage() {
  const [patterns, setPatterns] = useState<PatternSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  const loadPatterns = () => {
    const params = new URLSearchParams();
    if (search) params.set("q", search);
    const url = "/api/v1/patterns/" + (params.toString() ? `?${params}` : "");
    fetch(url)
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => { setPatterns(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadPatterns(); }, [search]);

  const formatSize = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  };

  if (loading) return <PageSection><Title headingLevel="h1">Loading...</Title></PageSection>;

  return (
    <>
      <PageSection>
        <Toolbar>
          <ToolbarContent>
            <ToolbarItem><Title headingLevel="h1">Patterns</Title></ToolbarItem>
            <ToolbarItem>
              <SearchInput
                placeholder="Search patterns..."
                value={search}
                onChange={(_e, v) => setSearch(v)}
                onClear={() => setSearch("")}
              />
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </PageSection>
      <PageSection>
        {patterns.length === 0 ? (
          <EmptyState>
            <EmptyStateHeader titleText="No patterns yet" headingLevel="h2" />
            <EmptyStateBody>
              Create a pattern from a project to stamp out reusable environments.
            </EmptyStateBody>
          </EmptyState>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
            {patterns.map((p) => (
              <Card key={p.id} isCompact>
                <CardTitle>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span>{p.name}</span>
                    <Label color={p.visibility === "public" ? "green" : p.visibility === "shared" ? "blue" : "grey"}>
                      {p.visibility}
                    </Label>
                  </div>
                </CardTitle>
                <CardBody>
                  {p.description && <p style={{ fontSize: 13, opacity: 0.8, marginBottom: 8 }}>{p.description}</p>}
                  <div style={{ fontSize: 12, opacity: 0.6 }}>
                    {p.disks.length} disk(s) · {formatSize(p.total_size_bytes)}
                    {" · "}{new Date(p.created_at).toLocaleDateString()}
                  </div>
                  <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
                    <Button variant="primary" size="sm"
                      onClick={() => {
                        fetch(`/api/v1/patterns/${p.id}/deploy`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({}),
                        })
                          .then((r) => r.json())
                          .then((data) => {
                            if (data.id) window.location.href = `/projects/${data.id}`;
                          });
                      }}
                    >
                      Create Project
                    </Button>
                    <Button variant="secondary" size="sm">Bulk Deploy</Button>
                  </div>
                </CardBody>
              </Card>
            ))}
          </div>
        )}
      </PageSection>
    </>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add src/frontend/src/app/layout.tsx src/frontend/src/app/library/page.tsx src/frontend/src/app/library/images/page.tsx src/frontend/src/app/library/patterns/page.tsx
git commit -m "feat: redesign library navigation with Images and Patterns sub-pages"
```

---

## Task 13: Frontend — Save as Pattern Modal + Canvas Integration

**Files:**
- Create: `src/frontend/src/components/canvas/SavePatternModal.tsx`
- Modify: `src/frontend/src/components/canvas/CanvasToolbar.tsx`
- Modify: `src/frontend/src/app/projects/[id]/page.tsx`

- [ ] **Step 1: Create SavePatternModal**

Create `src/frontend/src/components/canvas/SavePatternModal.tsx`:

```typescript
"use client";

import React, { useState } from "react";
import {
  Button,
  Form,
  FormGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextArea,
  TextInput,
} from "@patternfly/react-core";

interface SavePatternModalProps {
  projectId: string;
  projectName: string;
  hasRunningVMs: boolean;
  onClose: () => void;
  onSaved: (patternId: string) => void;
}

export default function SavePatternModal({ projectId, projectName, hasRunningVMs, onClose, onSaved }: SavePatternModalProps) {
  const [name, setName] = useState(projectName);
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState("private");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      const resp = await fetch("/api/v1/patterns", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          description: description || undefined,
          visibility,
          source_project_id: projectId,
        }),
      });
      if (resp.ok) {
        const data = await resp.json();
        onSaved(data.id);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal variant="small" isOpen onClose={onClose}>
      <ModalHeader title="Save as Pattern" />
      <ModalBody>
        {hasRunningVMs && (
          <div style={{ padding: "8px 12px", marginBottom: 16, background: "rgba(251,191,36,0.15)", borderRadius: 6, fontSize: 13 }}>
            Some VMs are running. For best results, stop all VMs before creating a pattern. Running VMs may have inconsistent disk state.
          </div>
        )}
        <Form>
          <FormGroup label="Name" isRequired fieldId="pattern-name">
            <TextInput id="pattern-name" value={name} onChange={(_e, v) => setName(v)} />
          </FormGroup>
          <FormGroup label="Description" fieldId="pattern-desc">
            <TextArea id="pattern-desc" value={description} onChange={(_e, v) => setDescription(v)} rows={3} />
          </FormGroup>
          <FormGroup label="Visibility" fieldId="pattern-vis">
            <select value={visibility} onChange={(e) => setVisibility(e.target.value)}
              style={{ padding: "6px 10px", borderRadius: 4, border: "1px solid var(--pf-t--global--border--color--default)" }}>
              <option value="private">Private</option>
              <option value="public">Public</option>
            </select>
          </FormGroup>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={handleSave} isLoading={saving} isDisabled={!name.trim()}>
          Save Pattern
        </Button>
        <Button variant="link" onClick={onClose}>Cancel</Button>
      </ModalFooter>
    </Modal>
  );
}
```

- [ ] **Step 2: Add Save as Pattern button to CanvasToolbar**

In `src/frontend/src/components/canvas/CanvasToolbar.tsx`, add a "Save as Pattern" button. The button should call a callback prop `onSavePattern` that the parent page handles by opening the `SavePatternModal`.

Add to the toolbar (after existing buttons):

```typescript
<Button variant="secondary" size="sm" onClick={onSavePattern}>
  Save as Pattern
</Button>
```

Add the prop to the component interface and pass it from the parent.

- [ ] **Step 3: Add Save as Pattern button to project page**

In `src/frontend/src/app/projects/[id]/page.tsx`, add state for the modal and a button in the project header:

```typescript
const [showPatternModal, setShowPatternModal] = useState(false);
```

Add button alongside the existing deploy/start/stop buttons:

```typescript
<Button variant="secondary" onClick={() => setShowPatternModal(true)}>
  Save as Pattern
</Button>
```

Add the modal render:

```typescript
{showPatternModal && (
  <SavePatternModal
    projectId={projectId}
    projectName={projectName}
    hasRunningVMs={/* check if any VMs are running */}
    onClose={() => setShowPatternModal(false)}
    onSaved={(id) => {
      setShowPatternModal(false);
      showToast("Pattern saved!");
    }}
  />
)}
```

- [ ] **Step 4: Commit**

```bash
git add src/frontend/src/components/canvas/SavePatternModal.tsx src/frontend/src/components/canvas/CanvasToolbar.tsx src/frontend/src/app/projects/\\[id\\]/page.tsx
git commit -m "feat: add Save as Pattern modal and buttons on canvas and project page"
```

---

## Task 14: Frontend — VM Snapshot Context Menu + Drag Import

**Files:**
- Create: `src/frontend/src/components/canvas/SnapshotVMModal.tsx`
- Modify: `src/frontend/src/components/canvas/NodeContextMenu.tsx`
- Modify: `src/frontend/src/components/canvas/Palette.tsx`
- Modify: `src/frontend/src/components/canvas/Canvas.tsx`

- [ ] **Step 1: Create SnapshotVMModal**

Create `src/frontend/src/components/canvas/SnapshotVMModal.tsx`:

```typescript
"use client";

import React, { useState } from "react";
import {
  Button,
  Form,
  FormGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextArea,
  TextInput,
} from "@patternfly/react-core";

interface SnapshotVMModalProps {
  projectId: string;
  vmId: string;
  vmName: string;
  isRunning: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export default function SnapshotVMModal({ projectId, vmId, vmName, isRunning, onClose, onSaved }: SnapshotVMModalProps) {
  const [name, setName] = useState(`${vmName} snapshot`);
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description: description || undefined }),
      });
      if (resp.ok) onSaved();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal variant="small" isOpen onClose={onClose}>
      <ModalHeader title="Save VM Snapshot" />
      <ModalBody>
        {isRunning && (
          <div style={{ padding: "8px 12px", marginBottom: 16, background: "rgba(251,191,36,0.15)", borderRadius: 6, fontSize: 13 }}>
            This VM is running. For best results, stop the VM before taking a snapshot.
          </div>
        )}
        <Form>
          <FormGroup label="Name" isRequired fieldId="snap-name">
            <TextInput id="snap-name" value={name} onChange={(_e, v) => setName(v)} />
          </FormGroup>
          <FormGroup label="Description" fieldId="snap-desc">
            <TextArea id="snap-desc" value={description} onChange={(_e, v) => setDescription(v)} rows={3} />
          </FormGroup>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={handleSave} isLoading={saving} isDisabled={!name.trim()}>
          Save Snapshot
        </Button>
        <Button variant="link" onClick={onClose}>Cancel</Button>
      </ModalFooter>
    </Modal>
  );
}
```

- [ ] **Step 2: Add "Save VM Snapshot" to NodeContextMenu**

In `src/frontend/src/components/canvas/NodeContextMenu.tsx`, add a "Save VM Snapshot" menu item for VM nodes. It should call a callback prop that opens the `SnapshotVMModal` with the VM's ID and name.

Add after existing menu items (e.g., after "Duplicate"):

```typescript
{node.type === "vmNode" && (
  <button onClick={() => { onClose(); onSnapshotVM(nodeId, node.data.name, node.data.status === "running"); }}>
    Save VM Snapshot
  </button>
)}
```

- [ ] **Step 3: Add Snapshots section to Palette**

In `src/frontend/src/components/canvas/Palette.tsx`, add a new section that fetches and displays available VM snapshots from `/api/v1/library/?type=snapshot`. Each snapshot is draggable with type `"snapshot"` in the drag data:

```typescript
event.dataTransfer.setData("application/troshka-node", JSON.stringify({
  type: "snapshot",
  label: snapshot.name,
  desc: "VM Snapshot",
  icon: "📸",
  iconClass: "palette-icon-snapshot",
  defaults: { snapshotId: snapshot.id },
}));
```

- [ ] **Step 4: Handle snapshot drop on Canvas**

In `src/frontend/src/components/canvas/Canvas.tsx`, in the `onDrop` handler, add a case for `item.type === "snapshot"`:

```typescript
if (item.type === "snapshot") {
  const snapshotId = item.defaults?.snapshotId;
  fetch(`/api/v1/projects/${projectId}/import-vm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      snapshot_id: snapshotId,
      position_x: position.x,
      position_y: position.y,
    }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data.topology) {
        useCanvasStore.setState({ nodes: data.topology.nodes, edges: data.topology.edges });
      }
    });
  return;
}
```

- [ ] **Step 5: Commit**

```bash
git add src/frontend/src/components/canvas/SnapshotVMModal.tsx src/frontend/src/components/canvas/NodeContextMenu.tsx src/frontend/src/components/canvas/Palette.tsx src/frontend/src/components/canvas/Canvas.tsx
git commit -m "feat: add VM snapshot context menu, palette section, and canvas drag-drop import"
```

---

## Task 15: Frontend — Bulk Deploy Modal

**Files:**
- Create: `src/frontend/src/components/canvas/BulkDeployModal.tsx`
- Modify: `src/frontend/src/app/library/patterns/page.tsx`

- [ ] **Step 1: Create BulkDeployModal**

Create `src/frontend/src/components/canvas/BulkDeployModal.tsx`:

```typescript
"use client";

import React, { useState } from "react";
import {
  Button,
  Checkbox,
  Form,
  FormGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  NumberInput,
  TextInput,
} from "@patternfly/react-core";

interface BulkDeployModalProps {
  patternId: string;
  patternName: string;
  onClose: () => void;
  onDeployed: (count: number) => void;
}

export default function BulkDeployModal({ patternId, patternName, onClose, onDeployed }: BulkDeployModalProps) {
  const [count, setCount] = useState(10);
  const [nameTemplate, setNameTemplate] = useState(`${patternName.toLowerCase().replace(/\s+/g, "-")}-{n}`);
  const [autoDeploy, setAutoDeploy] = useState(false);
  const [deploying, setDeploying] = useState(false);

  const handleDeploy = async () => {
    setDeploying(true);
    try {
      const resp = await fetch(`/api/v1/patterns/${patternId}/bulk-deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          count,
          name_template: nameTemplate,
          auto_deploy: autoDeploy,
        }),
      });
      if (resp.ok) {
        const data = await resp.json();
        onDeployed(data.projects.length);
      }
    } finally {
      setDeploying(false);
    }
  };

  const preview = Array.from({ length: Math.min(count, 3) }, (_, i) =>
    nameTemplate.replace("{n}", String(i + 1).padStart(3, "0"))
  ).join(", ") + (count > 3 ? `, ... (${count} total)` : "");

  return (
    <Modal variant="small" isOpen onClose={onClose}>
      <ModalHeader title="Bulk Deploy" />
      <ModalBody>
        <Form>
          <FormGroup label="Number of projects" isRequired fieldId="bulk-count">
            <NumberInput
              id="bulk-count"
              value={count}
              min={1}
              max={500}
              onMinus={() => setCount(Math.max(1, count - 1))}
              onPlus={() => setCount(Math.min(500, count + 1))}
              onChange={(e) => setCount(Math.max(1, Math.min(500, Number((e.target as HTMLInputElement).value) || 1)))}
            />
          </FormGroup>
          <FormGroup label="Naming template" isRequired fieldId="bulk-name"
            helperText="Use {n} for the number (e.g., lab-{n} → lab-001, lab-002, ...)">
            <TextInput id="bulk-name" value={nameTemplate} onChange={(_e, v) => setNameTemplate(v)} />
          </FormGroup>
          <FormGroup fieldId="bulk-preview" label="Preview">
            <div style={{ fontSize: 13, opacity: 0.7, fontFamily: "monospace" }}>{preview}</div>
          </FormGroup>
          <FormGroup fieldId="bulk-auto">
            <Checkbox id="bulk-auto" label="Auto-deploy immediately" isChecked={autoDeploy}
              onChange={(_e, v) => setAutoDeploy(v)} />
          </FormGroup>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={handleDeploy} isLoading={deploying}
          isDisabled={!nameTemplate.includes("{n}")}>
          Deploy {count} Projects
        </Button>
        <Button variant="link" onClick={onClose}>Cancel</Button>
      </ModalFooter>
    </Modal>
  );
}
```

- [ ] **Step 2: Wire BulkDeployModal into patterns page**

In `src/frontend/src/app/library/patterns/page.tsx`, add state for the modal:

```typescript
const [bulkPattern, setBulkPattern] = useState<PatternSummary | null>(null);
```

Wire the "Bulk Deploy" button's `onClick`:

```typescript
<Button variant="secondary" size="sm" onClick={() => setBulkPattern(p)}>Bulk Deploy</Button>
```

Render the modal:

```typescript
{bulkPattern && (
  <BulkDeployModal
    patternId={bulkPattern.id}
    patternName={bulkPattern.name}
    onClose={() => setBulkPattern(null)}
    onDeployed={(count) => {
      setBulkPattern(null);
      alert(`Created ${count} projects! View them on the Projects page.`);
    }}
  />
)}
```

- [ ] **Step 3: Commit**

```bash
git add src/frontend/src/components/canvas/BulkDeployModal.tsx src/frontend/src/app/library/patterns/page.tsx
git commit -m "feat: add bulk deploy modal for patterns"
```

---

## Task 16: Run All Tests

- [ ] **Step 1: Run the full backend test suite**

Run: `cd src/backend && python3 -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Fix any failures**

If tests fail, fix them. Common issues:
- Import ordering in `__init__.py`
- SQLite test DB missing JSONB support (already handled in conftest.py)
- Missing schema fields in response models

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "fix: resolve any test failures from patterns integration"
```

---

## Summary

| Task | What it builds |
|------|---------------|
| 1 | Pattern, PatternDisk, PatternShare models |
| 2 | LibraryItem vm_config + LibraryItemDisk model |
| 3 | Alembic migration for all new tables |
| 4 | Pydantic schemas for pattern API |
| 5 | Pattern CRUD + sharing + progress API |
| 6 | Deploy single project from pattern |
| 7 | Bulk deploy from pattern |
| 8 | VM snapshot endpoint |
| 9 | Import VM snapshot into project |
| 10 | Pattern disk capture service (S3 upload) |
| 11 | CoW overlay support in deploy service |
| 12 | Frontend library navigation redesign |
| 13 | Save as Pattern modal + buttons |
| 14 | VM snapshot context menu + drag import |
| 15 | Bulk deploy modal |
| 16 | Full test suite verification |
