"""API-level tests for storage pools endpoints."""

import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.host import Host
from app.models.provider import Provider
from app.models.storage_pool import SharedCacheEntry, StoragePool
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

BASE = "/api/v1/storage-pools"


def _ensure_dev_user():
    db = TestSession()
    user = db.query(User).filter_by(email="local-dev@troshka").first()
    if not user:
        db.close()
        client.get("/api/v1/auth/me")
        db = TestSession()
        user = db.query(User).filter_by(email="local-dev@troshka").first()
    user_id = user.id
    db.close()
    return user_id


def _create_provider(name=None, provider_type="ec2"):
    db = TestSession()
    p = Provider(
        id=str(uuid.uuid4()),
        name=name or f"sp-prov-{uuid.uuid4().hex[:8]}",
        type=provider_type,
        state="active",
        created_by="local-dev@troshka",
    )
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    return pid


def _create_pool(provider_id, name=None, mode="local", **kwargs):
    db = TestSession()
    pool = StoragePool(
        id=str(uuid.uuid4()),
        name=name or f"pool-{uuid.uuid4().hex[:8]}",
        mode=mode,
        status=kwargs.pop("status", "available"),
        provider_id=provider_id,
        **kwargs,
    )
    db.add(pool)
    db.commit()
    pool_id = pool.id
    db.close()
    return pool_id


def _create_host(provider_id, storage_pool_id=None, **kwargs):
    db = TestSession()
    h = Host(
        id=str(uuid.uuid4()),
        ip_address=kwargs.pop("ip_address", "10.0.0.1"),
        state=kwargs.pop("state", "active"),
        agent_status=kwargs.pop("agent_status", "connected"),
        total_vcpus=32,
        total_ram_mb=65536,
        used_vcpus=0,
        used_ram_mb=0,
        provider_id=provider_id,
        storage_pool_id=storage_pool_id,
        **kwargs,
    )
    db.add(h)
    db.commit()
    hid = h.id
    db.close()
    return hid


def _create_cache_entry(pool_id, item_type="pattern", item_id=None):
    db = TestSession()
    entry = SharedCacheEntry(
        id=str(uuid.uuid4()),
        storage_pool_id=pool_id,
        item_type=item_type,
        item_id=item_id or str(uuid.uuid4()),
        status="ready",
        file_path=f"images/{uuid.uuid4().hex}.qcow2",
        size_bytes=1073741824,
    )
    db.add(entry)
    db.commit()
    eid = entry.id
    db.close()
    return eid


def _cleanup_pool(pool_id):
    db = TestSession()
    db.query(SharedCacheEntry).filter_by(storage_pool_id=pool_id).delete()
    db.query(Host).filter_by(storage_pool_id=pool_id).delete()
    db.query(StoragePool).filter_by(id=pool_id).delete()
    db.commit()
    db.close()


def _cleanup_host(host_id):
    db = TestSession()
    db.query(Host).filter_by(id=host_id).delete()
    db.commit()
    db.close()


def _cleanup_provider(provider_id):
    db = TestSession()
    db.query(Provider).filter_by(id=provider_id).delete()
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# 1. GET /storage-pools/ (list_pools)
# ---------------------------------------------------------------------------


def test_list_pools_empty():
    _ensure_dev_user()
    resp = client.get(f"{BASE}/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_pools_returns_created_pool():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.get(f"{BASE}/")
        assert resp.status_code == 200
        pools = resp.json()
        assert any(p["id"] == pool_id for p in pools)
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 2. GET /storage-pools/{id} (get_pool)
# ---------------------------------------------------------------------------


def test_get_pool_success():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, name="get-test-pool")
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pool_id
        assert data["name"] == "get-test-pool"
        assert data["host_count"] == 0
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_get_pool_not_found():
    _ensure_dev_user()
    resp = client.get(f"{BASE}/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_pool_with_host_count():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, storage_pool_id=pool_id)
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        assert resp.json()["host_count"] == 1
    finally:
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_get_pool_excludes_pattern_buffer_from_count():
    """Pattern buffer hosts should not count toward host_count."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, storage_pool_id=pool_id, host_type="pattern_buffer")
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        assert resp.json()["host_count"] == 0
    finally:
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_get_pool_with_worker_host():
    """Pool with a worker_host_id should resolve worker status."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, agent_status="connected", state="active")
    # Link worker host
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_host_id = hid
    db.commit()
    db.close()
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_status"] == "connected"
        assert data["worker_ip"] == "10.0.0.1"
    finally:
        # Unlink before cleanup
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        pool.worker_host_id = None
        db.commit()
        db.close()
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 3. POST /storage-pools/ (create_pool)
# ---------------------------------------------------------------------------


@patch(
    "app.services.storage_pool_service.generate_pool_ca",
    return_value=("cert", "key"),
)
def test_create_local_pool(mock_ca):
    _ensure_dev_user()
    pid = _create_provider()
    name = f"local-pool-{uuid.uuid4().hex[:8]}"
    try:
        resp = client.post(
            f"{BASE}/",
            json={"name": name, "mode": "local", "provider_id": pid},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == name
        assert data["mode"] == "local"
        assert data["status"] == "available"
    finally:
        # Clean up pool created via API
        db = TestSession()
        db.query(StoragePool).filter_by(name=name).delete()
        db.commit()
        db.close()
        _cleanup_provider(pid)


def test_create_pool_invalid_mode():
    _ensure_dev_user()
    pid = _create_provider()
    try:
        resp = client.post(
            f"{BASE}/",
            json={
                "name": f"bad-mode-{uuid.uuid4().hex[:8]}",
                "mode": "invalid-mode",
                "provider_id": pid,
            },
        )
        assert resp.status_code == 400
        assert "Invalid mode" in resp.json()["detail"]
    finally:
        _cleanup_provider(pid)


@patch(
    "app.services.storage_pool_service.generate_pool_ca",
    return_value=("cert", "key"),
)
def test_create_pool_duplicate_name(mock_ca):
    _ensure_dev_user()
    pid = _create_provider()
    name = f"dup-pool-{uuid.uuid4().hex[:8]}"
    pool_id = _create_pool(pid, name=name)
    try:
        resp = client.post(
            f"{BASE}/",
            json={"name": name, "mode": "local", "provider_id": pid},
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_create_pool_provider_not_found():
    _ensure_dev_user()
    resp = client.post(
        f"{BASE}/",
        json={
            "name": f"orphan-{uuid.uuid4().hex[:8]}",
            "mode": "local",
            "provider_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 404
    assert "Provider not found" in resp.json()["detail"]


def test_create_fsx_pool_missing_az():
    _ensure_dev_user()
    pid = _create_provider()
    try:
        resp = client.post(
            f"{BASE}/",
            json={
                "name": f"fsx-noaz-{uuid.uuid4().hex[:8]}",
                "mode": "shared-fsx",
                "provider_id": pid,
                "fsx_throughput_mbps": 128,
                "fsx_storage_gb": 64,
            },
        )
        assert resp.status_code == 400
        assert "AZ is required" in resp.json()["detail"]
    finally:
        _cleanup_provider(pid)


def test_create_fsx_pool_missing_throughput():
    _ensure_dev_user()
    pid = _create_provider()
    try:
        resp = client.post(
            f"{BASE}/",
            json={
                "name": f"fsx-nothr-{uuid.uuid4().hex[:8]}",
                "mode": "shared-fsx",
                "provider_id": pid,
                "az": "us-east-1a",
            },
        )
        assert resp.status_code == 400
        assert "fsx_throughput_mbps" in resp.json()["detail"]
    finally:
        _cleanup_provider(pid)


def test_create_byo_pool_missing_nfs_endpoint():
    _ensure_dev_user()
    pid = _create_provider()
    try:
        resp = client.post(
            f"{BASE}/",
            json={
                "name": f"byo-nonfs-{uuid.uuid4().hex[:8]}",
                "mode": "shared-byo",
                "provider_id": pid,
            },
        )
        assert resp.status_code == 400
        assert "nfs_endpoint" in resp.json()["detail"]
    finally:
        _cleanup_provider(pid)


@patch(
    "app.services.storage_pool_service.generate_pool_ca",
    return_value=("cert", "key"),
)
@patch(
    "app.services.storage_pool_service.add_sg_rules_for_shared_storage",
)
def test_create_byo_pool_success(mock_sg, mock_ca):
    _ensure_dev_user()
    pid = _create_provider()
    # BYO provisioning requires provider with region + SG
    db = TestSession()
    prov = db.get(Provider, pid)
    prov.default_region = "us-east-1"
    prov.security_group_id = "sg-test"
    db.commit()
    db.close()

    name = f"byo-ok-{uuid.uuid4().hex[:8]}"
    try:
        resp = client.post(
            f"{BASE}/",
            json={
                "name": name,
                "mode": "shared-byo",
                "provider_id": pid,
                "nfs_endpoint": "10.0.1.50:/exports/troshka",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["mode"] == "shared-byo"
        assert data["status"] == "available"
        assert data["nfs_endpoint"] == "10.0.1.50:/exports/troshka"
    finally:
        db = TestSession()
        db.query(StoragePool).filter_by(name=name).delete()
        db.commit()
        db.close()
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 4. PATCH /storage-pools/{id} (update_pool)
# ---------------------------------------------------------------------------


def test_update_pool_not_found():
    _ensure_dev_user()
    resp = client.patch(
        f"{BASE}/{uuid.uuid4()}",
        json={"auto_extend_enabled": True},
    )
    assert resp.status_code == 404


def test_update_byo_nfs_endpoint():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(
        pid,
        mode="shared-byo",
        nfs_endpoint="10.0.1.50:/old",
    )
    try:
        resp = client.patch(
            f"{BASE}/{pool_id}",
            json={"nfs_endpoint": "10.0.2.100:/new-share"},
        )
        assert resp.status_code == 200
        assert resp.json()["nfs_endpoint"] == "10.0.2.100:/new-share"
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_update_auto_extend_fields():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.patch(
            f"{BASE}/{pool_id}",
            json={
                "auto_extend_enabled": True,
                "auto_extend_threshold_pct": 90,
                "auto_extend_increment_gb": 128,
                "auto_extend_max_gb": 2048,
                "pb_auto_sleep_minutes": 60,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_extend_enabled"] is True
        assert data["auto_extend_threshold_pct"] == 90
        assert data["auto_extend_increment_gb"] == 128
        assert data["auto_extend_max_gb"] == 2048
        assert data["pb_auto_sleep_minutes"] == 60
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_update_nfs_ignored_for_local():
    """nfs_endpoint update is only applied for shared-byo pools."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="local")
    try:
        resp = client.patch(
            f"{BASE}/{pool_id}",
            json={"nfs_endpoint": "should-be-ignored"},
        )
        assert resp.status_code == 200
        assert resp.json()["nfs_endpoint"] is None
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 5. DELETE /storage-pools/{id} (delete_pool)
# ---------------------------------------------------------------------------


def test_delete_empty_pool():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    resp = client.delete(f"{BASE}/{pool_id}")
    assert resp.status_code == 204

    # Verify gone
    resp2 = client.get(f"{BASE}/{pool_id}")
    assert resp2.status_code == 404

    _cleanup_provider(pid)


def test_delete_pool_with_hosts():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, storage_pool_id=pool_id)
    try:
        resp = client.delete(f"{BASE}/{pool_id}")
        assert resp.status_code == 400
        assert "hosts assigned" in resp.json()["detail"]
    finally:
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_delete_pool_not_found():
    _ensure_dev_user()
    resp = client.delete(f"{BASE}/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_pool_pattern_buffer_excluded():
    """Pattern buffer hosts should not block pool deletion."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, storage_pool_id=pool_id, host_type="pattern_buffer")
    try:
        resp = client.delete(f"{BASE}/{pool_id}")
        assert resp.status_code == 204
    finally:
        _cleanup_host(hid)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 6. GET /storage-pools/{id}/cache (list_cache)
# ---------------------------------------------------------------------------


def test_list_cache_empty():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.get(f"{BASE}/{pool_id}/cache")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_list_cache_with_entries():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    entry_id = _create_cache_entry(pool_id)
    try:
        resp = client.get(f"{BASE}/{pool_id}/cache")
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert any(e["id"] == entry_id for e in entries)
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_list_cache_not_found():
    _ensure_dev_user()
    resp = client.get(f"{BASE}/{uuid.uuid4()}/cache")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 7. DELETE /storage-pools/{id}/cache/{entry_id} (evict_cache_entry)
# ---------------------------------------------------------------------------


def test_evict_cache_entry_success():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    entry_id = _create_cache_entry(pool_id)
    try:
        resp = client.delete(f"{BASE}/{pool_id}/cache/{entry_id}")
        assert resp.status_code == 204

        # Verify entry is gone
        resp2 = client.get(f"{BASE}/{pool_id}/cache")
        assert not any(e["id"] == entry_id for e in resp2.json())
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_evict_cache_entry_not_found():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.delete(f"{BASE}/{pool_id}/cache/{uuid.uuid4()}")
        assert resp.status_code == 404
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_evict_cache_entry_pool_not_found():
    _ensure_dev_user()
    resp = client.delete(f"{BASE}/{uuid.uuid4()}/cache/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 8. POST /storage-pools/{id}/extend (extend_pool)
# ---------------------------------------------------------------------------


def test_extend_pool_not_found():
    _ensure_dev_user()
    resp = client.post(f"{BASE}/{uuid.uuid4()}/extend")
    assert resp.status_code == 404


def test_extend_non_fsx_pool():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="local")
    try:
        resp = client.post(f"{BASE}/{pool_id}/extend")
        assert resp.status_code == 400
        assert "FSx" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


def test_extend_byo_pool_rejected():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="shared-byo", nfs_endpoint="10.0.0.1:/nfs")
    try:
        resp = client.post(f"{BASE}/{pool_id}/extend")
        assert resp.status_code == 400
        assert "FSx" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.storage_extend.extend_pool_fsx")
def test_extend_fsx_pool_success(mock_extend):
    mock_extend.return_value = {"status": "extending", "new_size_gb": 256}
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(
        pid,
        mode="shared-fsx",
        fsx_filesystem_id="fs-test",
        fsx_storage_gb=128,
    )
    try:
        resp = client.post(f"{BASE}/{pool_id}/extend")
        assert resp.status_code == 200
        mock_extend.assert_called_once()
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.storage_extend.extend_pool_fsx")
def test_extend_fsx_pool_with_increment(mock_extend):
    mock_extend.return_value = {"status": "extending", "new_size_gb": 256}
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(
        pid,
        mode="shared-fsx",
        fsx_filesystem_id="fs-test",
        fsx_storage_gb=128,
    )
    try:
        resp = client.post(f"{BASE}/{pool_id}/extend", json={"increment_gb": 128})
        assert resp.status_code == 200
        mock_extend.assert_called_once()
        # Verify increment_gb was passed through
        call_kwargs = mock_extend.call_args
        assert call_kwargs[1]["increment_gb"] == 128
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.storage_extend.extend_pool_fsx")
def test_extend_fsx_pool_value_error(mock_extend):
    mock_extend.side_effect = ValueError("Cooldown period active")
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(
        pid,
        mode="shared-fsx",
        fsx_filesystem_id="fs-test",
        fsx_storage_gb=128,
    )
    try:
        resp = client.post(f"{BASE}/{pool_id}/extend")
        assert resp.status_code == 400
        assert "Cooldown" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 9. POST /storage-pools/{id}/gc (run_pool_gc)
# ---------------------------------------------------------------------------


def test_gc_pool_not_found():
    _ensure_dev_user()
    resp = client.post(f"{BASE}/{uuid.uuid4()}/gc")
    assert resp.status_code == 404


def test_gc_local_pool_rejected():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="local")
    try:
        resp = client.post(f"{BASE}/{pool_id}/gc")
        assert resp.status_code == 400
        assert "shared storage" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.gc_service.reconcile_pool")
def test_gc_shared_pool_success(mock_gc):
    mock_gc.return_value = {"cleaned": 0, "errors": []}
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="shared-byo", nfs_endpoint="10.0.0.1:/nfs")
    try:
        resp = client.post(f"{BASE}/{pool_id}/gc")
        assert resp.status_code == 200
        mock_gc.assert_called_once_with(pool_id, dry_run=False)
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.gc_service.reconcile_pool")
def test_gc_dry_run(mock_gc):
    mock_gc.return_value = {"would_clean": 3}
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid, mode="shared-byo", nfs_endpoint="10.0.0.1:/nfs")
    try:
        resp = client.post(f"{BASE}/{pool_id}/gc?dry_run=true")
        assert resp.status_code == 200
        mock_gc.assert_called_once_with(pool_id, dry_run=True)
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 10. POST /storage-pools/{id}/pattern-buffer (provision_or_replace)
# ---------------------------------------------------------------------------


def test_pattern_buffer_provision_not_found():
    _ensure_dev_user()
    resp = client.post(f"{BASE}/{uuid.uuid4()}/pattern-buffer")
    assert resp.status_code == 404


@patch("app.services.pattern_buffer_service.replace_pattern_buffer")
def test_pattern_buffer_provision_success(mock_replace):
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "provisioning"
        assert data["pool_id"] == pool_id
        mock_replace.assert_called_once()
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.replace_pattern_buffer")
def test_pattern_buffer_provision_with_instance_type(mock_replace):
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(
            f"{BASE}/{pool_id}/pattern-buffer",
            json={"instance_type": "i4i.xlarge"},
        )
        assert resp.status_code == 200
        # Verify instance_type was saved
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        assert pool.worker_instance_type == "i4i.xlarge"
        db.close()
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.replace_pattern_buffer")
def test_pattern_buffer_provision_conflict(mock_replace):
    mock_replace.side_effect = RuntimeError("Already provisioning")
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer")
        assert resp.status_code == 409
        assert "Already provisioning" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 11. DELETE /storage-pools/{id}/pattern-buffer (delete_pattern_buffer)
# ---------------------------------------------------------------------------


def test_delete_pattern_buffer_pool_not_found():
    _ensure_dev_user()
    resp = client.delete(f"{BASE}/{uuid.uuid4()}/pattern-buffer")
    assert resp.status_code == 404


def test_delete_pattern_buffer_no_worker():
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.delete(f"{BASE}/{pool_id}/pattern-buffer")
        assert resp.status_code == 404
        assert "No pattern buffer" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.providers.get_provider_driver")
def test_delete_pattern_buffer_success(mock_driver):
    mock_drv = MagicMock()
    mock_driver.return_value = mock_drv
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, instance_id="i-worker123", host_type="pattern_buffer")
    # Link as worker
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_host_id = hid
    db.commit()
    db.close()
    try:
        resp = client.delete(f"{BASE}/{pool_id}/pattern-buffer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        mock_drv.terminate_host.assert_called_once()
        # Verify worker_host_id is cleared
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        assert pool.worker_host_id is None
        db.close()
    finally:
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.providers.get_provider_driver")
def test_delete_pattern_buffer_terminate_fails_gracefully(mock_driver):
    """Even if terminate throws, the worker is still unlinked."""
    mock_drv = MagicMock()
    mock_drv.terminate_host.side_effect = Exception("AWS error")
    mock_driver.return_value = mock_drv
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    hid = _create_host(pid, instance_id="i-worker-bad", host_type="pattern_buffer")
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_host_id = hid
    db.commit()
    db.close()
    try:
        resp = client.delete(f"{BASE}/{pool_id}/pattern-buffer")
        assert resp.status_code == 200
        # Worker should still be unlinked despite error
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        assert pool.worker_host_id is None
        db.close()
    finally:
        _cleanup_host(hid)
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 12. POST /storage-pools/{id}/pattern-buffer/stop
# ---------------------------------------------------------------------------


def test_stop_pattern_buffer_not_found():
    _ensure_dev_user()
    resp = client.post(f"{BASE}/{uuid.uuid4()}/pattern-buffer/stop")
    assert resp.status_code == 404


@patch("app.services.pattern_buffer_service.stop_pattern_buffer")
def test_stop_pattern_buffer_success(mock_stop):
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_stop.assert_called_once()
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.stop_pattern_buffer")
def test_stop_pattern_buffer_conflict(mock_stop):
    mock_stop.side_effect = RuntimeError("Buffer busy")
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer/stop")
        assert resp.status_code == 409
        assert "Buffer busy" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 13. POST /storage-pools/{id}/pattern-buffer/wake
# ---------------------------------------------------------------------------


def test_wake_pattern_buffer_not_found():
    _ensure_dev_user()
    resp = client.post(f"{BASE}/{uuid.uuid4()}/pattern-buffer/wake")
    assert resp.status_code == 404


@patch("app.services.pattern_buffer_service.wake_pattern_buffer")
def test_wake_pattern_buffer_success(mock_wake):
    mock_wake.return_value = True
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer/wake")
        assert resp.status_code == 200
        assert resp.json()["status"] == "connected"
        mock_wake.assert_called_once()
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.wake_pattern_buffer")
def test_wake_pattern_buffer_failure(mock_wake):
    mock_wake.return_value = False
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    try:
        resp = client.post(f"{BASE}/{pool_id}/pattern-buffer/wake")
        assert resp.status_code == 503
        assert "failed to wake" in resp.json()["detail"]
    finally:
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


# ---------------------------------------------------------------------------
# 14. Helper function unit tests
# ---------------------------------------------------------------------------


def test_resolve_worker_status_connected():
    from app.api.storage_pools import _resolve_worker_status

    worker = MagicMock()
    worker.agent_status = "connected"
    worker.state = "active"
    assert _resolve_worker_status(worker) == "connected"


def test_resolve_worker_status_installing():
    from app.api.storage_pools import _resolve_worker_status

    worker = MagicMock()
    worker.agent_status = "disconnected"
    worker.state = "active"
    assert _resolve_worker_status(worker) == "installing"


def test_resolve_worker_status_other():
    from app.api.storage_pools import _resolve_worker_status

    worker = MagicMock()
    worker.agent_status = "disconnected"
    worker.state = "provisioning"
    assert _resolve_worker_status(worker) == "provisioning"


def test_resolve_worker_status_terminated():
    from app.api.storage_pools import _resolve_worker_status

    worker = MagicMock()
    worker.agent_status = "disconnected"
    worker.state = "terminated"
    assert _resolve_worker_status(worker) == "terminated"


def test_validate_fsx_missing_az():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_fsx

    body = MagicMock()
    body.az = None
    body.fsx_throughput_mbps = 128
    body.fsx_storage_gb = 64
    with pytest.raises(HTTPException) as exc:
        _validate_fsx(body)
    assert exc.value.status_code == 400
    assert "AZ is required" in str(exc.value.detail)


def test_validate_fsx_missing_throughput():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_fsx

    body = MagicMock()
    body.az = "us-east-1a"
    body.fsx_throughput_mbps = None
    body.fsx_storage_gb = None
    with pytest.raises(HTTPException) as exc:
        _validate_fsx(body)
    assert exc.value.status_code == 400
    assert "fsx_throughput_mbps" in str(exc.value.detail)


def test_validate_byo_missing_nfs():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_byo

    body = MagicMock()
    body.nfs_endpoint = None
    with pytest.raises(HTTPException) as exc:
        _validate_byo(body)
    assert exc.value.status_code == 400
    assert "nfs_endpoint" in str(exc.value.detail)


def test_validate_ceph_nfs_wrong_provider():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_ceph_nfs

    provider = MagicMock()
    provider.type = "ec2"
    with pytest.raises(HTTPException) as exc:
        _validate_ceph_nfs(provider)
    assert exc.value.status_code == 400
    assert "OCP Virt" in str(exc.value.detail)


def test_validate_ceph_nfs_correct_provider():
    from app.api.storage_pools import _validate_ceph_nfs

    provider = MagicMock()
    provider.type = "ocpvirt"
    # Should not raise
    _validate_ceph_nfs(provider)


def test_validate_netapp_wrong_provider():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_netapp

    body = MagicMock()
    body.netapp_capacity_gb = 1024
    provider = MagicMock()
    provider.type = "ec2"
    with pytest.raises(HTTPException) as exc:
        _validate_netapp(body, provider)
    assert exc.value.status_code == 400
    assert "GCP" in str(exc.value.detail)


def test_validate_netapp_missing_capacity():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_netapp

    body = MagicMock()
    body.netapp_capacity_gb = None
    provider = MagicMock()
    provider.type = "gcp"
    with pytest.raises(HTTPException) as exc:
        _validate_netapp(body, provider)
    assert exc.value.status_code == 400
    assert "netapp_capacity_gb" in str(exc.value.detail)


def test_validate_azure_files_wrong_provider():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_azure_files

    body = MagicMock()
    body.azure_files_capacity_gb = 256
    provider = MagicMock()
    provider.type = "ec2"
    with pytest.raises(HTTPException) as exc:
        _validate_azure_files(body, provider)
    assert exc.value.status_code == 400
    assert "Azure" in str(exc.value.detail)


def test_validate_azure_files_missing_capacity():
    import pytest
    from fastapi import HTTPException

    from app.api.storage_pools import _validate_azure_files

    body = MagicMock()
    body.azure_files_capacity_gb = None
    provider = MagicMock()
    provider.type = "azure"
    with pytest.raises(HTTPException) as exc:
        _validate_azure_files(body, provider)
    assert exc.value.status_code == 400
    assert "azure_files_capacity_gb" in str(exc.value.detail)


def test_validate_pool_mode_local_no_validation():
    """Local mode has no extra validation requirements."""
    from app.api.storage_pools import _validate_pool_mode

    body = MagicMock()
    body.mode = "local"
    provider = MagicMock()
    # Should not raise
    _validate_pool_mode(body, provider)


def test_pool_response_worker_missing_host():
    """When worker_host_id points to a deleted host, status should be error."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_host_id = str(uuid.uuid4())  # non-existent host
    db.commit()
    db.close()
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        assert resp.json()["worker_status"] == "error"
    finally:
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        pool.worker_host_id = None
        db.commit()
        db.close()
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.is_provisioning", return_value=True)
def test_pool_response_worker_provisioning(mock_is_prov):
    """When no worker_host_id but worker_instance_type set and provisioning."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_instance_type = "i4i.xlarge"
    db.commit()
    db.close()
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        assert resp.json()["worker_status"] == "provisioning"
    finally:
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        pool.worker_instance_type = None
        db.commit()
        db.close()
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)


@patch("app.services.pattern_buffer_service.is_provisioning", return_value=False)
@patch(
    "app.services.pattern_buffer_service.get_provision_error",
    return_value="Failed to launch",
)
def test_pool_response_worker_provision_error(mock_err, mock_prov):
    """When provisioning failed, worker_status should be error with message."""
    _ensure_dev_user()
    pid = _create_provider()
    pool_id = _create_pool(pid)
    db = TestSession()
    pool = db.get(StoragePool, pool_id)
    pool.worker_instance_type = "i4i.xlarge"
    db.commit()
    db.close()
    try:
        resp = client.get(f"{BASE}/{pool_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_status"] == "error"
        assert data["worker_error"] == "Failed to launch"
    finally:
        db = TestSession()
        pool = db.get(StoragePool, pool_id)
        pool.worker_instance_type = None
        db.commit()
        db.close()
        _cleanup_pool(pool_id)
        _cleanup_provider(pid)
