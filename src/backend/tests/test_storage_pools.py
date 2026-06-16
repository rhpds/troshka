from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import SharedCacheEntry, StoragePool

engine = create_engine(
    "sqlite:///./test_storage_pools.db", connect_args={"check_same_thread": False}
)
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
