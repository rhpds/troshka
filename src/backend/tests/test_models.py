from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects import sqlite

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

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
Base.metadata.drop_all(bind=engine)
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
