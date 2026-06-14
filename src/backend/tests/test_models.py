from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models.host import Host
from app.models.library import Library, LibraryItem, LibraryItemDisk
from app.models.network import Network
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.models.vm import VM

engine = create_engine(
    "sqlite:///./test_models.db", connect_args={"check_same_thread": False}
)
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_create_user():
    db = Session()
    user = User(
        email="test@example.com",
        display_name="Test User",
        role="user",
        auth_source="local",
    )
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
    project = Project(
        name="Test Project",
        owner_id=user.id,
        state="draft",
        poweroff_mode="simultaneous",
    )
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
        "users",
        "providers",
        "hosts",
        "host_assignments",
        "projects",
        "project_shares",
        "vms",
        "boot_prereqs",
        "vm_interfaces",
        "networks",
        "security_rules",
        "disks",
        "libraries",
        "library_items",
        "library_item_disks",
        "library_shares",
        "image_caches",
        "patterns",
        "pattern_disks",
        "pattern_shares",
    ]
    for name in expected:
        assert name in table_names, f"Missing table: {name}"


def test_create_pattern():
    from app.models.pattern import Pattern

    db = Session()
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
    from app.models.pattern import Pattern, PatternDisk

    db = Session()
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
    from app.models.pattern import Pattern, PatternShare

    db = Session()
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


def test_create_library_item_disk():
    db = Session()
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


def test_host_troshkad_fields():
    """Host model has agent_token, agent_cert_fingerprint, agent_version."""
    db = Session()
    provider = db.query(Provider).first()
    if not provider:
        provider = Provider(name="test-aws", type="aws", default_region="us-east-1")
        db.add(provider)
        db.commit()
    host = Host(
        provider_id=provider.id,
        state="active",
        agent_status="connected",
        agent_token="a" * 64,
        agent_cert_fingerprint="sha256:abcdef1234567890",
        agent_version="2026.06.08.1",
    )
    db.add(host)
    db.commit()
    db.refresh(host)
    assert host.agent_token == "a" * 64
    assert host.agent_cert_fingerprint == "sha256:abcdef1234567890"
    assert host.agent_version == "2026.06.08.1"
    db.close()
