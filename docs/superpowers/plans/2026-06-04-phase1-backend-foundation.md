# Phase 1: Backend Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the FastAPI backend skeleton with config, database, auth, core ORM models, and the first complete CRUD API cycle — establishing patterns that all subsequent phases follow.

**Architecture:** Monorepo with backend under `src/backend/`. Dynaconf for YAML-based config (parsec pattern). SQLAlchemy 2.0 with Mapped types. FastAPI with JWT auth + OAuth proxy headers. PostgreSQL via Podman for local dev. SQLite for tests.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, Dynaconf, PyJWT, Uvicorn, pytest, ruff

**Phasing context:** This is Phase 1 of 8. See `docs/superpowers/specs/2026-06-04-troshka-design.md` for the full design. This phase covers Tasks 1–13. Subsequent phases build on the patterns established here.

---

### Task 1: Project Scaffold

**Files:**
- Create: `.gitignore`
- Create: `src/backend/pyproject.toml`
- Create: `src/backend/app/__init__.py`
- Create: `src/frontend/.gitkeep`
- Create: `src/agent/.gitkeep`
- Create: `ansible/.gitkeep`
- Create: `collection/.gitkeep`

- [ ] **Step 1: Initialize git repository**

```bash
cd /Users/prutledg/troshka
git init
```

- [ ] **Step 2: Create .gitignore**

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
.eggs/
*.egg
.venv/
venv/

# IDE
.idea/
.vscode/
*.swp
*.swo

# Environment
.env
*.local.yaml
config.local.yaml

# Database
*.db
*.sqlite3

# OS
.DS_Store
Thumbs.db

# Testing
.coverage
htmlcov/
.pytest_cache/

# Node
node_modules/
.next/
out/

# Build
*.pyc
build/

# Logs
*.log
/tmp/

# Secrets
secrets/
*.pem
*.key
```

- [ ] **Step 3: Create backend pyproject.toml**

```toml
[project]
name = "troshka"
version = "0.1.0"
description = "Nested VM Environment Builder"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg2-binary>=2.9",
    "pydantic>=2.0",
    "dynaconf>=3.2",
    "pyjwt>=2.8",
    "passlib[bcrypt]>=1.7",
    "python-multipart>=0.0.9",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "ruff>=0.4",
    "mypy>=1.10",
]

[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"
```

- [ ] **Step 4: Create directory structure**

```bash
mkdir -p src/backend/app/{api,core,models,schemas,services,tasks}
mkdir -p src/backend/tests
mkdir -p src/backend/config
mkdir -p src/frontend src/agent ansible collection
touch src/backend/app/__init__.py
touch src/backend/app/api/__init__.py
touch src/backend/app/core/__init__.py
touch src/backend/app/models/__init__.py
touch src/backend/app/schemas/__init__.py
touch src/backend/app/services/__init__.py
touch src/backend/app/tasks/__init__.py
touch src/backend/tests/__init__.py
touch src/frontend/.gitkeep src/agent/.gitkeep ansible/.gitkeep collection/.gitkeep
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: initial project scaffold with monorepo structure"
```

---

### Task 2: Dynaconf Configuration

**Files:**
- Create: `src/backend/app/core/config.py`
- Create: `src/backend/config/config.yaml`
- Create: `src/backend/config/config.local.yaml.example`
- Test: `src/backend/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_config.py
from app.core.config import config


def test_config_loads_defaults():
    assert config.app.name == "troshka"
    assert config.app.port == 8000


def test_config_has_database_section():
    assert hasattr(config, "database")
    assert config.database.url is not None


def test_config_has_auth_section():
    assert hasattr(config, "auth")
    assert config.auth.jwt_algorithm == "HS256"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && python3 -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Create config.yaml**

```yaml
# src/backend/config/config.yaml
app:
  name: troshka
  port: 8000
  host: "0.0.0.0"
  log_level: info
  root_path: ""

database:
  url: "postgresql+psycopg2://troshka:troshka@localhost:5432/troshka"

redis:
  url: "redis://localhost:6379/0"

auth:
  jwt_secret: "CHANGE-ME-IN-LOCAL-CONFIG"
  jwt_algorithm: "HS256"
  jwt_expiry_hours: 24
  oauth_enabled: false
  allowed_groups: ""
  admin_groups: ""
  operator_groups: ""

s3:
  bucket: "troshka-images"
  region: "us-east-1"
  endpoint_url: ""

aws:
  default_region: "us-east-1"
  default_instance_type: "r8i.4xlarge"

defaults:
  run_timer_hours: 8
  lifetime_days: 30
  max_vms_per_project: 20
  max_projects_per_user: 10
  user_library_quota_gb: 500
```

- [ ] **Step 4: Create config.local.yaml.example**

```yaml
# src/backend/config/config.local.yaml.example
# Copy to config.local.yaml and fill in real values.
# This file is gitignored.

database:
  url: "postgresql+psycopg2://troshka:troshka@localhost:5432/troshka"

auth:
  jwt_secret: "replace-with-a-real-secret"
```

- [ ] **Step 5: Write config.py**

```python
# src/backend/app/core/config.py
import os
from dynaconf import Dynaconf

_config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "config")

config = Dynaconf(
    envvar_prefix="TROSHKA",
    settings_files=[
        os.path.join(_config_dir, "config.yaml"),
        os.path.join(_config_dir, "config.local.yaml"),
    ],
    environments=False,
    load_dotenv=False,
    merge_enabled=True,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_config.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/core/config.py src/backend/config/ src/backend/tests/test_config.py
git commit -m "feat: add Dynaconf configuration with YAML config files"
```

---

### Task 3: Database Setup

**Files:**
- Create: `src/backend/app/core/database.py`
- Test: `src/backend/tests/test_database.py`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_database.py
from sqlalchemy import text

from app.core.database import Base, get_db, engine


def test_base_class_exists():
    assert hasattr(Base, "metadata")


def test_engine_connects():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_get_db_yields_session():
    gen = get_db()
    session = next(gen)
    assert session is not None
    try:
        next(gen)
    except StopIteration:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && python3 -m pytest tests/test_database.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write database.py**

```python
# src/backend/app/core/database.py
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import config


engine = create_engine(config.database.url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 4: Override database for tests — create conftest.py**

Since tests should use SQLite, not PostgreSQL:

```python
# src/backend/tests/conftest.py
import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base

test_engine = create_engine("sqlite:///./test.db", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=test_engine)
TestSession = sessionmaker(bind=test_engine)


def get_test_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_database.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/core/database.py src/backend/tests/conftest.py src/backend/tests/test_database.py
git commit -m "feat: add SQLAlchemy database setup with test infrastructure"
```

---

### Task 4: Core ORM Models

**Files:**
- Create: `src/backend/app/models/user.py`
- Create: `src/backend/app/models/provider.py`
- Create: `src/backend/app/models/host.py`
- Create: `src/backend/app/models/project.py`
- Create: `src/backend/app/models/vm.py`
- Create: `src/backend/app/models/network.py`
- Create: `src/backend/app/models/disk.py`
- Create: `src/backend/app/models/library.py`
- Test: `src/backend/tests/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_models.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.user import User
from app.models.provider import Provider
from app.models.host import Host
from app.models.project import Project, ProjectShare
from app.models.vm import VM, BootPrereq, VMInterface
from app.models.network import Network, SecurityRule
from app.models.disk import Disk
from app.models.library import Library, LibraryItem

engine = create_engine("sqlite:///./test_models.db", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_create_user():
    db = Session()
    user = User(email="test@example.com", display_name="Test User", role="user", auth_source="local")
    db.add(user)
    db.commit()
    db.refresh(user)
    assert user.id is not None
    assert user.email == "test@example.com"
    assert user.role == "user"
    db.close()


def test_create_project_with_owner():
    db = Session()
    user = db.query(User).filter_by(email="test@example.com").first()
    project = Project(name="Test Project", owner_id=user.id, state="draft", poweroff_mode="simultaneous")
    db.add(project)
    db.commit()
    db.refresh(project)
    assert project.id is not None
    assert project.owner_id == user.id
    db.close()


def test_create_vm_in_project():
    db = Session()
    project = db.query(Project).first()
    vm = VM(
        project_id=project.id,
        name="web-server-01",
        vcpus=4,
        ram_mb=8192,
        state="stopped",
        boot_method="template",
        boot_order=1,
        console_type="auto",
    )
    db.add(vm)
    db.commit()
    db.refresh(vm)
    assert vm.id is not None
    assert vm.project_id == project.id
    db.close()


def test_create_network_in_project():
    db = Session()
    project = db.query(Project).first()
    network = Network(
        project_id=project.id,
        name="lab-net",
        cidr="10.0.1.0/24",
        dhcp_enabled=True,
        dns_enabled=True,
        dns_domain="lab.local",
        dns_upstream=False,
    )
    db.add(network)
    db.commit()
    db.refresh(network)
    assert network.id is not None
    assert network.cidr == "10.0.1.0/24"
    db.close()


def test_all_tables_created():
    table_names = Base.metadata.tables.keys()
    expected = [
        "users", "providers", "hosts", "host_assignments",
        "projects", "project_shares",
        "vms", "boot_prereqs", "vm_interfaces",
        "networks", "security_rules",
        "disks",
        "libraries", "library_items", "library_shares", "image_caches",
    ]
    for name in expected:
        assert name in table_names, f"Missing table: {name}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create User model**

```python
# src/backend/app/models/user.py
import datetime
import uuid

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    auth_source: Mapped[str] = mapped_column(String(20), default="local")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    quota_overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")
    libraries: Mapped[list["Library"]] = relationship(back_populates="owner")
```

- [ ] **Step 4: Create Provider model**

```python
# src/backend/app/models/provider.py
import datetime
import uuid

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    config: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    hosts: Mapped[list["Host"]] = relationship(back_populates="provider")
```

- [ ] **Step 5: Create Host model**

```python
# src/backend/app/models/host.py
import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"))
    instance_id: Mapped[str | None] = mapped_column(String(100))
    instance_type: Mapped[str | None] = mapped_column(String(50))
    region: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="provisioning")
    host_type: Mapped[str] = mapped_column(String(20), default="shared")
    total_vcpus: Mapped[int] = mapped_column(Integer, default=0)
    total_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    used_vcpus: Mapped[int] = mapped_column(Integer, default=0)
    used_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    agent_status: Mapped[str] = mapped_column(String(20), default="disconnected")
    last_health_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider | None"] = relationship(back_populates="hosts")
    vms: Mapped[list["VM"]] = relationship(back_populates="host")


class HostAssignment(Base):
    __tablename__ = "host_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 6: Create Project model**

```python
# src/backend/app/models/project.py
import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"))
    host_type: Mapped[str] = mapped_column(String(20), default="shared")
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    state: Mapped[str] = mapped_column(String(20), default="draft")
    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    guest_permission: Mapped[str] = mapped_column(String(20), default="console_only")
    run_timer_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_max_ext_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    lifetime_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    poweroff_mode: Mapped[str] = mapped_column(String(20), default="simultaneous")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="projects")
    vms: Mapped[list["VM"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    networks: Mapped[list["Network"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    disks: Mapped[list["Disk"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    shares: Mapped[list["ProjectShare"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectShare(Base):
    __tablename__ = "project_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    permission: Mapped[str] = mapped_column(String(20), default="view")

    project: Mapped["Project"] = relationship(back_populates="shares")
```

- [ ] **Step 7: Create VM model**

```python
# src/backend/app/models/vm.py
import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class VM(Base):
    __tablename__ = "vms"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    vcpus: Mapped[int] = mapped_column(Integer, default=2)
    ram_mb: Mapped[int] = mapped_column(Integer, default=4096)
    os_template: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), default="stopped")
    boot_method: Mapped[str] = mapped_column(String(20), default="template")
    boot_iso_id: Mapped[str | None] = mapped_column(ForeignKey("library_items.id"))
    pxe_profile_id: Mapped[str | None] = mapped_column(String(36))
    boot_order: Mapped[int] = mapped_column(Integer, default=0)
    console_type: Mapped[str] = mapped_column(String(10), default="auto")
    cloud_init: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="vms")
    host: Mapped["Host | None"] = relationship(back_populates="vms")
    interfaces: Mapped[list["VMInterface"]] = relationship(back_populates="vm", cascade="all, delete-orphan")
    prereqs: Mapped[list["BootPrereq"]] = relationship(
        back_populates="vm", foreign_keys="BootPrereq.vm_id", cascade="all, delete-orphan"
    )


class BootPrereq(Base):
    __tablename__ = "boot_prereqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"))
    depends_on_vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id"))
    check_type: Mapped[str] = mapped_column(String(20), default="none")
    check_value: Mapped[str | None] = mapped_column(String(100))

    vm: Mapped["VM"] = relationship(back_populates="prereqs", foreign_keys=[vm_id])


class VMInterface(Base):
    __tablename__ = "vm_interfaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vm_id: Mapped[str] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"))
    network_id: Mapped[str] = mapped_column(ForeignKey("networks.id"))
    ip_mode: Mapped[str] = mapped_column(String(10), default="dhcp")
    ip_address: Mapped[str | None] = mapped_column(String(45))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    dns_name: Mapped[str | None] = mapped_column(String(255))

    vm: Mapped["VM"] = relationship(back_populates="interfaces")
```

- [ ] **Step 8: Create Network model**

```python
# src/backend/app/models/network.py
import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Network(Base):
    __tablename__ = "networks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    cidr: Mapped[str] = mapped_column(String(18))
    dhcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dns_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dns_domain: Mapped[str | None] = mapped_column(String(255))
    dns_upstream: Mapped[bool] = mapped_column(Boolean, default=False)
    pxe_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pxe_profile_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="networks")
    security_rules: Mapped[list["SecurityRule"]] = relationship(
        back_populates="network", cascade="all, delete-orphan"
    )


class SecurityRule(Base):
    __tablename__ = "security_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network_id: Mapped[str] = mapped_column(ForeignKey("networks.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(10))
    protocol: Mapped[str] = mapped_column(String(10), default="all")
    port_range_start: Mapped[int | None] = mapped_column(Integer)
    port_range_end: Mapped[int | None] = mapped_column(Integer)
    source_cidr: Mapped[str | None] = mapped_column(String(18))
    action: Mapped[str] = mapped_column(String(10), default="allow")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    description: Mapped[str | None] = mapped_column(String(500))

    network: Mapped["Network"] = relationship(back_populates="security_rules")
```

- [ ] **Step 9: Create Disk model**

```python
# src/backend/app/models/disk.py
import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Disk(Base):
    __tablename__ = "disks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    vm_id: Mapped[str | None] = mapped_column(ForeignKey("vms.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    size_gb: Mapped[int] = mapped_column(Integer, default=20)
    format: Mapped[str] = mapped_column(String(10), default="qcow2")
    boot_order: Mapped[int] = mapped_column(Integer, default=0)
    attached: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 10: Create Library models**

```python
# src/backend/app/models/library.py
import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String(10))
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    quota_bytes: Mapped[int | None] = mapped_column(Integer)
    used_bytes: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped["User | None"] = relationship(back_populates="libraries")
    items: Mapped[list["LibraryItem"]] = relationship(back_populates="library", cascade="all, delete-orphan")


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    library_id: Mapped[str] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    type: Mapped[str] = mapped_column(String(20))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    s3_key: Mapped[str | None] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    os_variant: Mapped[str | None] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(20), default="uploading")
    source_vm_id: Mapped[str | None] = mapped_column(String(36))
    source_project_id: Mapped[str | None] = mapped_column(String(36))
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    library: Mapped["Library"] = relationship(back_populates="items")


class LibraryShare(Base):
    __tablename__ = "library_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    shared_with_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    permission: Mapped[str] = mapped_column(String(10), default="use")


class ImageCache(Base):
    __tablename__ = "image_caches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("library_items.id", ondelete="CASCADE"))
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id"))
    local_path: Mapped[str] = mapped_column(String(500))
    cached_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 11: Update models __init__.py to export all models**

```python
# src/backend/app/models/__init__.py
from app.models.user import User
from app.models.provider import Provider
from app.models.host import Host, HostAssignment
from app.models.project import Project, ProjectShare
from app.models.vm import VM, BootPrereq, VMInterface
from app.models.network import Network, SecurityRule
from app.models.disk import Disk
from app.models.library import Library, LibraryItem, LibraryShare, ImageCache

__all__ = [
    "User", "Provider", "Host", "HostAssignment",
    "Project", "ProjectShare",
    "VM", "BootPrereq", "VMInterface",
    "Network", "SecurityRule",
    "Disk",
    "Library", "LibraryItem", "LibraryShare", "ImageCache",
]
```

- [ ] **Step 12: Patch SQLite for PostgreSQL types in conftest.py**

Update `src/backend/tests/conftest.py`:

```python
# src/backend/tests/conftest.py
import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models import *  # noqa: F403 — ensure all models register with Base

test_engine = create_engine("sqlite:///./test.db", connect_args={"check_same_thread": False})
Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
TestSession = sessionmaker(bind=test_engine)


def get_test_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 13: Run tests to verify they pass**

Run: `cd src/backend && python3 -m pytest tests/test_models.py -v`
Expected: PASS (5 tests)

- [ ] **Step 14: Commit**

```bash
git add src/backend/app/models/ src/backend/tests/
git commit -m "feat: add core ORM models for all entities"
```

---

### Task 5: Alembic Migrations

**Files:**
- Create: `src/backend/alembic.ini`
- Create: `src/backend/alembic/env.py`
- Create: `src/backend/alembic/versions/` (auto-generated)

- [ ] **Step 1: Initialize Alembic**

```bash
cd src/backend
python3 -m alembic init alembic
```

- [ ] **Step 2: Edit alembic.ini**

Set `sqlalchemy.url` to empty — we'll read it from Dynaconf in env.py:

In `src/backend/alembic.ini`, change the `sqlalchemy.url` line to:
```
sqlalchemy.url =
```

- [ ] **Step 3: Edit alembic/env.py**

Replace the generated `alembic/env.py` with:

```python
# src/backend/alembic/env.py
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import config as app_config
from app.core.database import Base
from app.models import *  # noqa: F403 — register all models

config = context.config
config.set_main_option("sqlalchemy.url", app_config.database.url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Generate initial migration**

```bash
cd src/backend
python3 -m alembic revision --autogenerate -m "initial schema"
```

- [ ] **Step 5: Commit**

```bash
git add src/backend/alembic.ini src/backend/alembic/
git commit -m "feat: add Alembic migration infrastructure with initial schema"
```

---

### Task 6: Authentication

**Files:**
- Create: `src/backend/app/core/auth.py`
- Create: `src/backend/app/schemas/auth.py`
- Test: `src/backend/tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_auth.py
from app.core.auth import hash_password, verify_password, create_jwt, decode_jwt


def test_password_hashing():
    hashed = hash_password("secret123")
    assert hashed != "secret123"
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_jwt_roundtrip():
    token = create_jwt(user_id="abc-123", email="test@example.com", role="user")
    payload = decode_jwt(token)
    assert payload["sub"] == "abc-123"
    assert payload["email"] == "test@example.com"
    assert payload["role"] == "user"


def test_jwt_invalid_token():
    payload = decode_jwt("garbage.token.here")
    assert payload is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_auth.py -v`
Expected: FAIL

- [ ] **Step 3: Write auth.py**

```python
# src/backend/app/core/auth.py
import datetime
import logging

import jwt
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import config
from app.core.database import get_db

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_group_cache: dict[str, dict] = {}
GROUP_CACHE_TTL = 60


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_jwt(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=config.auth.jwt_expiry_hours),
    }
    return jwt.encode(payload, config.auth.jwt_secret, algorithm=config.auth.jwt_algorithm)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.auth.jwt_secret, algorithms=[config.auth.jwt_algorithm])
    except jwt.PyJWTError:
        return None


def _get_user_from_oauth_headers(request: Request) -> dict | None:
    email = request.headers.get("X-Forwarded-Email")
    if not email:
        return None
    return {"email": email, "user": request.headers.get("X-Forwarded-User", email)}


def _get_user_from_jwt(request: Request) -> dict | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    return decode_jwt(token)


def get_current_user(request: Request, db: Session = Depends(get_db)):
    from app.models.user import User

    user_info = None

    if config.auth.oauth_enabled:
        user_info = _get_user_from_oauth_headers(request)

    if user_info is None:
        user_info = _get_user_from_jwt(request)

    if user_info is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = user_info.get("email") or user_info.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="No user identity found")

    user = db.query(User).filter_by(email=email).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def require_role(min_role: str):
    role_levels = {"user": 0, "operator": 1, "admin": 2}
    required_level = role_levels.get(min_role, 0)

    def dependency(user=Depends(get_current_user)):
        user_level = role_levels.get(user.role, 0)
        if user_level < required_level:
            raise HTTPException(status_code=403, detail=f"Requires {min_role} role")
        return user

    return dependency
```

- [ ] **Step 4: Write auth schemas**

```python
# src/backend/app/schemas/auth.py
from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: str
    email: str
    role: str


class UserIdentity(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str

    model_config = {"from_attributes": True}
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && python3 -m pytest tests/test_auth.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/core/auth.py src/backend/app/schemas/auth.py src/backend/tests/test_auth.py
git commit -m "feat: add JWT + OAuth proxy authentication with RBAC"
```

---

### Task 7: FastAPI Application Shell

**Files:**
- Create: `src/backend/app/main.py`
- Create: `src/backend/app/api/auth.py`
- Test: `src/backend/tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_app.py
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.main import app
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)


def test_health_check():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["app"] == "troshka"


def test_login_creates_user_and_returns_jwt():
    resp = client.post("/api/v1/auth/register", json={
        "email": "admin@example.com",
        "password": "secret123",
        "display_name": "Admin User",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "token" in data
    assert data["email"] == "admin@example.com"


def test_login_with_credentials():
    resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "secret123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data


def test_login_wrong_password():
    resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


def test_auth_me_without_token():
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_auth_me_with_token():
    login_resp = client.post("/api/v1/auth/login", json={
        "email": "admin@example.com",
        "password": "secret123",
    })
    token = login_resp.json()["token"]
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "admin@example.com"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_app.py -v`
Expected: FAIL

- [ ] **Step 3: Write main.py**

```python
# src/backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import config

app = FastAPI(
    title=config.app.name,
    description="Nested VM Environment Builder",
    version="0.1.0",
    root_path=config.app.root_path,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import auth as auth_routes  # noqa: E402

app.include_router(auth_routes.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "app": config.app.name, "version": "0.1.0"}
```

- [ ] **Step 4: Write auth routes**

```python
# src/backend/app/api/auth.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import create_jwt, get_current_user, hash_password, verify_password
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, UserIdentity

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(LoginRequest):
    display_name: str | None = None


@router.post("/register", response_model=LoginResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter_by(email=body.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role="user",
        auth_source="local",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(token=token, user_id=user.id, email=user.email, role=user.role)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email).first()
    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_jwt(user_id=user.id, email=user.email, role=user.role)
    return LoginResponse(token=token, user_id=user.id, email=user.email, role=user.role)


@router.get("/me", response_model=UserIdentity)
def auth_me(user: User = Depends(get_current_user)):
    return UserIdentity.model_validate(user)
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && python3 -m pytest tests/test_app.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/main.py src/backend/app/api/auth.py src/backend/tests/test_app.py
git commit -m "feat: add FastAPI app shell with auth endpoints"
```

---

### Task 8: Pydantic Schemas for Core Entities

**Files:**
- Create: `src/backend/app/schemas/user.py`
- Create: `src/backend/app/schemas/project.py`
- Create: `src/backend/app/schemas/vm.py`
- Create: `src/backend/app/schemas/network.py`
- Create: `src/backend/app/schemas/disk.py`
- Create: `src/backend/app/schemas/provider.py`
- Create: `src/backend/app/schemas/host.py`

- [ ] **Step 1: Create user schemas**

```python
# src/backend/app/schemas/user.py
import datetime

from pydantic import BaseModel


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "user"
    password: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    quota_overrides: dict | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str
    auth_source: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Create project schemas**

```python
# src/backend/app/schemas/project.py
import datetime

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    provider_id: str | None = None
    host_type: str = "shared"
    run_timer_hours: int | None = None
    lifetime_expires_at: datetime.datetime | None = None
    poweroff_mode: str = "simultaneous"


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    host_type: str | None = None
    run_timer_hours: int | None = None
    run_timer_max_ext_hours: int | None = None
    lifetime_expires_at: datetime.datetime | None = None
    poweroff_mode: str | None = None
    guest_permission: str | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    provider_id: str | None = None
    host_type: str
    state: str
    public_token: str | None = None
    guest_permission: str
    run_timer_hours: int | None = None
    poweroff_mode: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class ProjectShareRequest(BaseModel):
    user_id: str
    permission: str = "view"
```

- [ ] **Step 3: Create VM schemas**

```python
# src/backend/app/schemas/vm.py
import datetime

from pydantic import BaseModel


class VMCreate(BaseModel):
    name: str
    description: str | None = None
    vcpus: int = 2
    ram_mb: int = 4096
    os_template: str | None = None
    boot_method: str = "template"
    boot_iso_id: str | None = None
    boot_order: int = 0
    console_type: str = "auto"
    cloud_init: str | None = None


class VMUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    vcpus: int | None = None
    ram_mb: int | None = None
    os_template: str | None = None
    boot_method: str | None = None
    boot_order: int | None = None
    console_type: str | None = None
    cloud_init: str | None = None


class VMResponse(BaseModel):
    id: str
    project_id: str
    host_id: str | None = None
    name: str
    description: str | None = None
    vcpus: int
    ram_mb: int
    os_template: str | None = None
    state: str
    boot_method: str
    boot_order: int
    console_type: str
    ip_address: str | None = None
    mac_address: str | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Create network schemas**

```python
# src/backend/app/schemas/network.py
import datetime

from pydantic import BaseModel


class NetworkCreate(BaseModel):
    name: str
    cidr: str
    dhcp_enabled: bool = False
    dns_enabled: bool = False
    dns_domain: str | None = None
    dns_upstream: bool = False


class NetworkUpdate(BaseModel):
    name: str | None = None
    cidr: str | None = None
    dhcp_enabled: bool | None = None
    dns_enabled: bool | None = None
    dns_domain: str | None = None
    dns_upstream: bool | None = None


class NetworkResponse(BaseModel):
    id: str
    project_id: str
    name: str
    cidr: str
    dhcp_enabled: bool
    dns_enabled: bool
    dns_domain: str | None = None
    dns_upstream: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class SecurityRuleCreate(BaseModel):
    direction: str
    protocol: str = "all"
    port_range_start: int | None = None
    port_range_end: int | None = None
    source_cidr: str | None = None
    action: str = "allow"
    priority: int = 100
    description: str | None = None


class SecurityRuleResponse(BaseModel):
    id: int
    network_id: str
    direction: str
    protocol: str
    port_range_start: int | None = None
    port_range_end: int | None = None
    source_cidr: str | None = None
    action: str
    priority: int
    description: str | None = None

    model_config = {"from_attributes": True}
```

- [ ] **Step 5: Create disk schemas**

```python
# src/backend/app/schemas/disk.py
import datetime

from pydantic import BaseModel


class DiskCreate(BaseModel):
    name: str
    size_gb: int = 20
    format: str = "qcow2"
    boot_order: int = 0


class DiskUpdate(BaseModel):
    name: str | None = None
    size_gb: int | None = None


class DiskResponse(BaseModel):
    id: str
    project_id: str
    vm_id: str | None = None
    name: str
    size_gb: int
    format: str
    boot_order: int
    attached: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 6: Create provider schemas**

```python
# src/backend/app/schemas/provider.py
import datetime

from pydantic import BaseModel


class ProviderCreate(BaseModel):
    name: str
    type: str
    config: str | None = None
    region: str | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    config: str | None = None
    region: str | None = None
    state: str | None = None


class ProviderResponse(BaseModel):
    id: str
    name: str
    type: str
    region: str | None = None
    state: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 7: Create host schemas**

```python
# src/backend/app/schemas/host.py
import datetime

from pydantic import BaseModel


class HostCreate(BaseModel):
    provider_id: str
    instance_type: str = "r8i.4xlarge"
    region: str | None = None
    host_type: str = "shared"


class HostResponse(BaseModel):
    id: str
    provider_id: str | None = None
    instance_id: str | None = None
    instance_type: str | None = None
    region: str | None = None
    state: str
    host_type: str
    total_vcpus: int
    total_ram_mb: int
    used_vcpus: int
    used_ram_mb: int
    ip_address: str | None = None
    agent_status: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 8: Commit**

```bash
git add src/backend/app/schemas/
git commit -m "feat: add Pydantic schemas for all core entities"
```

---

### Task 9: Project CRUD API

**Files:**
- Create: `src/backend/app/api/projects.py`
- Test: `src/backend/tests/test_projects.py`

This is the first full CRUD route — establishes the pattern for all subsequent entity routes.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_projects.py
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)

# Seed a user and get a token
_db = TestSession()
_user = User(email="proj-test@example.com", display_name="Test", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
USER_ID = _user.id
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_create_project():
    resp = client.post("/api/v1/projects", json={
        "name": "My Lab",
        "description": "Test project",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Lab"
    assert data["owner_id"] == USER_ID
    assert data["state"] == "draft"


def test_list_projects():
    resp = client.get("/api/v1/projects", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["name"] == "My Lab"


def test_get_project():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "My Lab"


def test_update_project():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(f"/api/v1/projects/{project_id}", json={
        "name": "Renamed Lab",
        "poweroff_mode": "ordered",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Lab"
    assert resp.json()["poweroff_mode"] == "ordered"


def test_delete_project():
    create_resp = client.post("/api/v1/projects", json={"name": "To Delete"}, headers=HEADERS)
    project_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 204

    get_resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert get_resp.status_code == 404


def test_unauthorized_access():
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_projects.py -v`
Expected: FAIL

- [ ] **Step 3: Write projects route**

```python
# src/backend/app/api/projects.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/", response_model=list[ProjectResponse])
def list_projects(
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Project).filter(Project.owner_id == user.id)
    return query.offset(skip).limit(limit).all()


@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = Project(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        provider_id=body.provider_id,
        host_type=body.host_type,
        run_timer_hours=body.run_timer_hours,
        lifetime_expires_at=body.lifetime_expires_at,
        poweroff_mode=body.poweroff_mode,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    db.delete(project)
    db.commit()
```

- [ ] **Step 4: Register route in main.py**

Add to `src/backend/app/main.py` after the auth router:

```python
from app.api import projects as project_routes  # noqa: E402

app.include_router(project_routes.router, prefix="/api/v1")
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && python3 -m pytest tests/test_projects.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/api/projects.py src/backend/app/main.py src/backend/tests/test_projects.py
git commit -m "feat: add project CRUD API with ownership checks"
```

---

### Task 10: VM CRUD API

**Files:**
- Create: `src/backend/app/api/vms.py`
- Test: `src/backend/tests/test_vms.py`

Follows the pattern from Task 9. VMs are nested under projects.

- [ ] **Step 1: Write the failing test**

```python
# src/backend/tests/test_vms.py
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)

_db = TestSession()
_user = User(email="vm-test@example.com", display_name="Test", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

PROJECT_ID = None


def test_setup_project():
    global PROJECT_ID
    resp = client.post("/api/v1/projects", json={"name": "VM Test Project"}, headers=HEADERS)
    assert resp.status_code == 201
    PROJECT_ID = resp.json()["id"]


def test_create_vm():
    resp = client.post(f"/api/v1/projects/{PROJECT_ID}/vms", json={
        "name": "web-server-01",
        "vcpus": 4,
        "ram_mb": 8192,
        "os_template": "rhel9",
    }, headers=HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "web-server-01"
    assert data["vcpus"] == 4
    assert data["state"] == "stopped"


def test_list_vms():
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_vm():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    vm_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == "web-server-01"


def test_update_vm():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/vms", headers=HEADERS)
    vm_id = list_resp.json()[0]["id"]
    resp = client.patch(f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}", json={
        "vcpus": 8,
        "name": "web-server-updated",
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["vcpus"] == 8
    assert resp.json()["name"] == "web-server-updated"


def test_delete_vm():
    create_resp = client.post(f"/api/v1/projects/{PROJECT_ID}/vms", json={
        "name": "to-delete",
    }, headers=HEADERS)
    vm_id = create_resp.json()["id"]
    resp = client.delete(f"/api/v1/projects/{PROJECT_ID}/vms/{vm_id}", headers=HEADERS)
    assert resp.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && python3 -m pytest tests/test_vms.py -v`
Expected: FAIL

- [ ] **Step 3: Write vms route**

```python
# src/backend/app/api/vms.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.project import Project
from app.models.user import User
from app.models.vm import VM
from app.schemas.vm import VMCreate, VMResponse, VMUpdate

router = APIRouter(prefix="/projects/{project_id}/vms", tags=["vms"])


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[VMResponse])
def list_vms(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    return db.query(VM).filter_by(project_id=project_id).all()


@router.post("/", response_model=VMResponse, status_code=201)
def create_vm(
    project_id: str,
    body: VMCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    vm = VM(project_id=project_id, **body.model_dump())
    db.add(vm)
    db.commit()
    db.refresh(vm)
    return vm


@router.get("/{vm_id}", response_model=VMResponse)
def get_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


@router.patch("/{vm_id}", response_model=VMResponse)
def update_vm(
    project_id: str,
    vm_id: str,
    body: VMUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(vm, field, value)
    db.commit()
    db.refresh(vm)
    return vm


@router.delete("/{vm_id}", status_code=204)
def delete_vm(
    project_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    vm = db.query(VM).filter_by(id=vm_id, project_id=project_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    db.delete(vm)
    db.commit()
```

- [ ] **Step 4: Register route in main.py**

Add to `src/backend/app/main.py`:

```python
from app.api import vms as vm_routes  # noqa: E402

app.include_router(vm_routes.router, prefix="/api/v1")
```

- [ ] **Step 5: Run tests**

Run: `cd src/backend && python3 -m pytest tests/test_vms.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/backend/app/api/vms.py src/backend/app/main.py src/backend/tests/test_vms.py
git commit -m "feat: add VM CRUD API nested under projects"
```

---

### Task 11: Network and Disk CRUD APIs

**Files:**
- Create: `src/backend/app/api/networks.py`
- Create: `src/backend/app/api/disks.py`
- Test: `src/backend/tests/test_networks.py`
- Test: `src/backend/tests/test_disks.py`

These follow the exact same pattern as Task 10 (nested under projects). The `_get_project_or_403` helper pattern is reused.

- [ ] **Step 1: Write network route**

```python
# src/backend/app/api/networks.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.network import Network
from app.models.project import Project
from app.models.user import User
from app.schemas.network import NetworkCreate, NetworkResponse, NetworkUpdate

router = APIRouter(prefix="/projects/{project_id}/networks", tags=["networks"])


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[NetworkResponse])
def list_networks(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    return db.query(Network).filter_by(project_id=project_id).all()


@router.post("/", response_model=NetworkResponse, status_code=201)
def create_network(
    project_id: str, body: NetworkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    network = Network(project_id=project_id, **body.model_dump())
    db.add(network)
    db.commit()
    db.refresh(network)
    return network


@router.get("/{network_id}", response_model=NetworkResponse)
def get_network(
    project_id: str, network_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.patch("/{network_id}", response_model=NetworkResponse)
def update_network(
    project_id: str,
    network_id: str,
    body: NetworkUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(network, field, value)
    db.commit()
    db.refresh(network)
    return network


@router.delete("/{network_id}", status_code=204)
def delete_network(
    project_id: str, network_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    network = db.query(Network).filter_by(id=network_id, project_id=project_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    db.delete(network)
    db.commit()
```

- [ ] **Step 2: Write disk route**

```python
# src/backend/app/api/disks.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.disk import Disk
from app.models.project import Project
from app.models.user import User
from app.schemas.disk import DiskCreate, DiskResponse, DiskUpdate

router = APIRouter(prefix="/projects/{project_id}/disks", tags=["disks"])


def _get_project_or_403(project_id: str, user: User, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")
    return project


@router.get("/", response_model=list[DiskResponse])
def list_disks(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_project_or_403(project_id, user, db)
    return db.query(Disk).filter_by(project_id=project_id).all()


@router.post("/", response_model=DiskResponse, status_code=201)
def create_disk(
    project_id: str, body: DiskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    disk = Disk(project_id=project_id, **body.model_dump())
    db.add(disk)
    db.commit()
    db.refresh(disk)
    return disk


@router.get("/{disk_id}", response_model=DiskResponse)
def get_disk(
    project_id: str, disk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    return disk


@router.patch("/{disk_id}", response_model=DiskResponse)
def update_disk(
    project_id: str,
    disk_id: str,
    body: DiskUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(disk, field, value)
    db.commit()
    db.refresh(disk)
    return disk


@router.delete("/{disk_id}", status_code=204)
def delete_disk(
    project_id: str, disk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    db.delete(disk)
    db.commit()


@router.post("/{disk_id}/attach/{vm_id}", response_model=DiskResponse)
def attach_disk(
    project_id: str,
    disk_id: str,
    vm_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    disk.vm_id = vm_id
    disk.attached = True
    db.commit()
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/detach", response_model=DiskResponse)
def detach_disk(
    project_id: str,
    disk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_project_or_403(project_id, user, db)
    disk = db.query(Disk).filter_by(id=disk_id, project_id=project_id).first()
    if not disk:
        raise HTTPException(status_code=404, detail="Disk not found")
    disk.vm_id = None
    disk.attached = False
    db.commit()
    db.refresh(disk)
    return disk
```

- [ ] **Step 3: Register routes in main.py**

Add to `src/backend/app/main.py`:

```python
from app.api import networks as network_routes  # noqa: E402
from app.api import disks as disk_routes  # noqa: E402

app.include_router(network_routes.router, prefix="/api/v1")
app.include_router(disk_routes.router, prefix="/api/v1")
```

- [ ] **Step 4: Write network tests**

```python
# src/backend/tests/test_networks.py
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)

_db = TestSession()
_user = User(email="net-test@example.com", display_name="Test", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ID = None


def test_setup():
    global PROJECT_ID
    resp = client.post("/api/v1/projects", json={"name": "Net Test"}, headers=HEADERS)
    PROJECT_ID = resp.json()["id"]


def test_create_network():
    resp = client.post(f"/api/v1/projects/{PROJECT_ID}/networks", json={
        "name": "lab-net", "cidr": "10.0.1.0/24", "dhcp_enabled": True, "dns_enabled": True,
        "dns_domain": "lab.local",
    }, headers=HEADERS)
    assert resp.status_code == 201
    assert resp.json()["cidr"] == "10.0.1.0/24"
    assert resp.json()["dhcp_enabled"] is True


def test_list_networks():
    resp = client.get(f"/api/v1/projects/{PROJECT_ID}/networks", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_delete_network():
    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/networks", headers=HEADERS)
    net_id = list_resp.json()[0]["id"]
    resp = client.delete(f"/api/v1/projects/{PROJECT_ID}/networks/{net_id}", headers=HEADERS)
    assert resp.status_code == 204
```

- [ ] **Step 5: Write disk tests**

```python
# src/backend/tests/test_disks.py
from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db, test_engine

Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)
app.dependency_overrides[get_db] = get_test_db

client = TestClient(app)

_db = TestSession()
_user = User(email="disk-test@example.com", display_name="Test", role="user",
             auth_source="local", password_hash=hash_password("pass"))
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
_db.close()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ID = None


def test_setup():
    global PROJECT_ID
    resp = client.post("/api/v1/projects", json={"name": "Disk Test"}, headers=HEADERS)
    PROJECT_ID = resp.json()["id"]


def test_create_disk():
    resp = client.post(f"/api/v1/projects/{PROJECT_ID}/disks", json={
        "name": "db-data", "size_gb": 500, "format": "raw",
    }, headers=HEADERS)
    assert resp.status_code == 201
    assert resp.json()["size_gb"] == 500
    assert resp.json()["attached"] is False


def test_attach_and_detach_disk():
    # Create a VM to attach to
    vm_resp = client.post(f"/api/v1/projects/{PROJECT_ID}/vms", json={"name": "test-vm"}, headers=HEADERS)
    vm_id = vm_resp.json()["id"]

    list_resp = client.get(f"/api/v1/projects/{PROJECT_ID}/disks", headers=HEADERS)
    disk_id = list_resp.json()[0]["id"]

    attach_resp = client.post(f"/api/v1/projects/{PROJECT_ID}/disks/{disk_id}/attach/{vm_id}", headers=HEADERS)
    assert attach_resp.status_code == 200
    assert attach_resp.json()["attached"] is True
    assert attach_resp.json()["vm_id"] == vm_id

    detach_resp = client.post(f"/api/v1/projects/{PROJECT_ID}/disks/{disk_id}/detach", headers=HEADERS)
    assert detach_resp.status_code == 200
    assert detach_resp.json()["attached"] is False
    assert detach_resp.json()["vm_id"] is None
```

- [ ] **Step 6: Run all tests**

Run: `cd src/backend && python3 -m pytest tests/test_networks.py tests/test_disks.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/api/networks.py src/backend/app/api/disks.py src/backend/app/main.py
git add src/backend/tests/test_networks.py src/backend/tests/test_disks.py
git commit -m "feat: add network and disk CRUD APIs with attach/detach"
```

---

### Task 12: Dev Services Script

**Files:**
- Create: `dev-services.sh`

- [ ] **Step 1: Create dev-services.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/src/backend"
DB_CONTAINER="troshka-postgres"
DB_PORT=5432
DB_USER="troshka"
DB_PASS="troshka"
DB_NAME="troshka"
BACKEND_PORT=8000
PID_DIR="/tmp/troshka"

mkdir -p "$PID_DIR"

start_db() {
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        echo "PostgreSQL already running"
        return
    fi
    if podman ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        podman start "$DB_CONTAINER"
    else
        podman run -d --name "$DB_CONTAINER" \
            -e POSTGRES_USER="$DB_USER" \
            -e POSTGRES_PASSWORD="$DB_PASS" \
            -e POSTGRES_DB="$DB_NAME" \
            -p "${DB_PORT}:5432" \
            docker.io/library/postgres:16
    fi
    echo "Waiting for PostgreSQL..."
    for i in $(seq 1 30); do
        if podman exec "$DB_CONTAINER" pg_isready -U "$DB_USER" &>/dev/null; then
            echo "PostgreSQL ready"
            return
        fi
        sleep 1
    done
    echo "ERROR: PostgreSQL failed to start" >&2
    exit 1
}

stop_db() {
    podman stop "$DB_CONTAINER" 2>/dev/null || true
    echo "PostgreSQL stopped"
}

start_backend() {
    cd "$BACKEND_DIR"
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        venv/bin/pip install -e ".[dev]"
    fi
    source venv/bin/activate
    alembic upgrade head 2>/dev/null || echo "No migrations to run"
    uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload &
    echo $! > "$PID_DIR/backend.pid"
    echo "Backend started on port $BACKEND_PORT (PID: $(cat "$PID_DIR/backend.pid"))"
}

stop_backend() {
    if [ -f "$PID_DIR/backend.pid" ]; then
        kill "$(cat "$PID_DIR/backend.pid")" 2>/dev/null || true
        rm "$PID_DIR/backend.pid"
    fi
    echo "Backend stopped"
}

status() {
    echo "=== Troshka Dev Services ==="
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^${DB_CONTAINER}$"; then
        echo "  PostgreSQL: RUNNING (port $DB_PORT)"
    else
        echo "  PostgreSQL: STOPPED"
    fi
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        echo "  Backend:    RUNNING (port $BACKEND_PORT, PID $(cat "$PID_DIR/backend.pid"))"
    else
        echo "  Backend:    STOPPED"
    fi
}

case "${1:-status}" in
    start)
        start_db
        start_backend
        ;;
    stop)
        stop_backend
        stop_db
        ;;
    restart)
        stop_backend
        stop_db
        start_db
        start_backend
        ;;
    db)
        case "${2:-start}" in
            start) start_db ;;
            stop) stop_db ;;
        esac
        ;;
    backend)
        case "${2:-start}" in
            start) start_backend ;;
            stop) stop_backend ;;
        esac
        ;;
    status) status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|db start|db stop|backend start|backend stop}"
        exit 1
        ;;
esac
```

- [ ] **Step 2: Make executable**

```bash
chmod +x dev-services.sh
```

- [ ] **Step 3: Commit**

```bash
git add dev-services.sh
git commit -m "feat: add dev-services.sh for local development"
```

---

### Task 13: CI Configuration

**Files:**
- Create: `.github/workflows/ci-backend.yml`

- [ ] **Step 1: Create CI workflow**

```yaml
# .github/workflows/ci-backend.yml
name: Backend CI

on:
  push:
    branches: [main]
    paths: ['src/backend/**']
  pull_request:
    branches: [main]
    paths: ['src/backend/**']

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: src/backend

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Lint with ruff
        run: ruff check .

      - name: Type check with mypy
        run: mypy app/ --ignore-missing-imports

      - name: Run tests
        run: pytest tests/ -v --tb=short
```

- [ ] **Step 2: Commit**

```bash
mkdir -p .github/workflows
git add .github/workflows/ci-backend.yml
git commit -m "feat: add GitHub Actions CI for backend"
```

---

### Task 14: Run Full Test Suite

- [ ] **Step 1: Install dependencies and run all tests**

```bash
cd src/backend
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

Expected: All tests pass (config: 3, database: 3, models: 5, auth: 3, app: 6, projects: 6, vms: 6, networks: 4, disks: 3 = ~39 tests)

- [ ] **Step 2: Run linter**

```bash
cd src/backend
ruff check .
```

Expected: No errors (or minor fixable ones)

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address linting issues from full test run"
```

---

## What Phase 1 Produces

After completing all 14 tasks, you have:
- A working FastAPI backend with health check, auth, and CRUD for projects, VMs, networks, and disks
- Dynaconf-based config (YAML + env var overrides, no hardcoded secrets)
- SQLAlchemy 2.0 ORM with all core models (User, Provider, Host, Project, VM, Network, Disk, Library)
- Alembic migration infrastructure
- JWT + OAuth proxy authentication with role-based access control
- ~39 passing tests with SQLite test database
- Dev services script for local PostgreSQL via Podman
- GitHub Actions CI pipeline

## Next Phases

- **Phase 2:** Complete remaining API endpoints (users, providers, hosts, security rules, tunnels, libraries, PXE, events, WebSocket infrastructure)
- **Phase 3:** Frontend foundation (Next.js + PatternFly shell, auth, routing)
- **Phase 4:** Canvas editor (React Flow, custom nodes, drag-and-drop)
- **Phase 5:** Host agent (WebSocket client, libvirt, VM lifecycle)
- **Phase 6:** Console & power management (noVNC/SPICE, boot order, timers)
- **Phase 7:** Library, events & sharing (S3, templates, bulk deployment)
- **Phase 8:** Deployment & Ansible collection (OCP manifests, troshka.core)
