"""Tests for gc_service.py — sync_host_capacity, discover_orphans, clean_orphans,
_find_orphaned_cache, repair_networks, recover_host_services, reconcile_host."""

import uuid
from unittest.mock import MagicMock, patch

from app.models.host import Host
from app.models.library import Library, LibraryItem
from app.models.pattern import Pattern
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession


def _make_user(db):
    u = User(
        id=str(uuid.uuid4()),
        email=f"gc-test-{uuid.uuid4().hex[:6]}@example.com",
        display_name="GC Test",
        role="user",
        auth_source="local",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_provider(db):
    p = Provider(
        id=str(uuid.uuid4()),
        name=f"gc-provider-{uuid.uuid4().hex[:6]}",
        type="ec2",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider, agent_status="connected", state="active"):
    h = Host(
        id=str(uuid.uuid4()),
        provider_id=provider.id,
        ip_address="10.0.0.1",
        agent_status=agent_status,
        state=state,
        used_vcpus=0,
        used_ram_mb=0,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _make_project(db, user, host, state="active", topology=None, vni_map=None):
    p = Project(
        id=str(uuid.uuid4()),
        name=f"gc-project-{uuid.uuid4().hex[:6]}",
        owner_id=user.id,
        host_id=host.id,
        state=state,
        topology=topology or {"nodes": [], "edges": []},
        vni_map=vni_map,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# sync_host_capacity
# ---------------------------------------------------------------------------


def test_sync_host_capacity_empty():
    """No projects on host -> 0 used resources."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        host.used_vcpus = 10
        host.used_ram_mb = 8192
        db.commit()

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)

        assert result["new"]["used_vcpus"] == 0
        assert result["new"]["used_ram_mb"] == 0
        assert result["changed"] is True
    finally:
        db.rollback()
        db.close()


def test_sync_host_capacity_with_vms():
    """VMs in active projects contribute to used resources."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        topo = {
            "nodes": [
                {
                    "type": "vmNode",
                    "data": {"vcpus": 4, "ram": 8},  # ram in GB
                },
                {
                    "type": "vmNode",
                    "data": {"vcpus": 2, "ram": 4},
                },
            ],
            "edges": [],
        }
        _make_project(db, user, host, state="active", topology=topo)

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)

        assert result["new"]["used_vcpus"] == 6
        assert result["new"]["used_ram_mb"] == 12288  # (8+4) * 1024
    finally:
        db.rollback()
        db.close()


def test_sync_host_capacity_with_containers():
    """Container nodes contribute CPU and memory."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        topo = {
            "nodes": [
                {
                    "type": "containerNode",
                    "data": {"cpus": 2, "memory": 512},  # memory in MB
                },
            ],
            "edges": [],
        }
        _make_project(db, user, host, state="active", topology=topo)

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)

        assert result["new"]["used_vcpus"] == 2
        assert result["new"]["used_ram_mb"] == 512
    finally:
        db.rollback()
        db.close()


def test_sync_host_capacity_ignores_draft():
    """Draft projects are not counted."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        topo = {"nodes": [{"type": "vmNode", "data": {"vcpus": 8, "ram": 16}}]}
        _make_project(db, user, host, state="draft", topology=topo)

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)

        assert result["new"]["used_vcpus"] == 0
        assert result["new"]["used_ram_mb"] == 0
    finally:
        db.rollback()
        db.close()


def test_sync_host_capacity_includes_stopped():
    """Stopped projects still count toward capacity."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        topo = {"nodes": [{"type": "vmNode", "data": {"vcpus": 4, "ram": 8}}]}
        _make_project(db, user, host, state="stopped", topology=topo)

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)

        assert result["new"]["used_vcpus"] == 4
        assert result["new"]["used_ram_mb"] == 8192
    finally:
        db.rollback()
        db.close()


def test_sync_host_capacity_no_change():
    """If values are already correct, changed=False."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        host.used_vcpus = 0
        host.used_ram_mb = 0
        db.commit()

        from app.services.gc_service import sync_host_capacity

        result = sync_host_capacity(db, host)
        assert result["changed"] is False
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# _find_orphaned_cache
# ---------------------------------------------------------------------------


def test_find_orphaned_cache_no_orphans():
    """All cache items have matching DB records."""
    db = TestSession()
    try:
        user = _make_user(db)
        lib = Library(id=str(uuid.uuid4()), type="personal", owner_id=user.id)
        db.add(lib)
        db.commit()

        item_id = str(uuid.uuid4())
        lib_item = LibraryItem(
            id=item_id,
            library_id=lib.id,
            name="test.qcow2",
            type="disk",
            format="qcow2",
        )
        db.add(lib_item)
        db.commit()

        from app.services.gc_service import _find_orphaned_cache

        cache_items = [{"path": f"/var/lib/troshka/images/{item_id}.qcow2"}]
        result = _find_orphaned_cache(db, cache_items)
        assert result == []
    finally:
        db.rollback()
        db.close()


def test_find_orphaned_cache_with_orphans():
    """Cache items with no matching DB record are returned as orphans."""
    db = TestSession()
    try:
        from app.services.gc_service import _find_orphaned_cache

        orphan_id = str(uuid.uuid4())
        cache_items = [{"path": f"/var/lib/troshka/images/{orphan_id}.qcow2"}]
        result = _find_orphaned_cache(db, cache_items)
        assert len(result) == 1
        assert orphan_id in result[0]
    finally:
        db.close()


def test_find_orphaned_cache_mixed():
    """Mix of known and orphaned items filters correctly."""
    db = TestSession()
    try:
        user = _make_user(db)

        # Create a pattern (known)
        known_id = str(uuid.uuid4())
        pattern = Pattern(
            id=known_id,
            name="known-pattern",
            owner_id=user.id,
            topology={"nodes": []},
        )
        db.add(pattern)
        db.commit()

        orphan_id = str(uuid.uuid4())

        from app.services.gc_service import _find_orphaned_cache

        cache_items = [
            {"path": f"/var/lib/troshka/cache/patterns/{known_id}/"},
            {"path": f"/var/lib/troshka/cache/patterns/{orphan_id}/"},
        ]
        result = _find_orphaned_cache(db, cache_items)
        assert len(result) == 1
        assert orphan_id in result[0]
    finally:
        db.rollback()
        db.close()


def test_find_orphaned_cache_string_items():
    """Cache items can be plain strings instead of dicts."""
    db = TestSession()
    try:
        from app.services.gc_service import _find_orphaned_cache

        orphan_id = str(uuid.uuid4())
        cache_items = [f"/var/lib/troshka/images/{orphan_id}.qcow2"]
        result = _find_orphaned_cache(db, cache_items)
        assert len(result) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# discover_orphans
# ---------------------------------------------------------------------------


def test_discover_orphans_host_not_reachable():
    """Disconnected host returns error."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider, agent_status="disconnected")

        from app.services.gc_service import discover_orphans

        result = discover_orphans(db, host)
        assert result["error"] == "Host not reachable"
    finally:
        db.rollback()
        db.close()


def test_discover_orphans_no_ip():
    """Host without IP returns error."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        host.ip_address = None
        db.commit()

        from app.services.gc_service import discover_orphans

        result = discover_orphans(db, host)
        assert result["error"] == "Host not reachable"
    finally:
        db.rollback()
        db.close()


@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_discover_orphans_success(mock_start, mock_wait):
    """Successful discovery returns orphan results."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {
                "orphan_dirs": ["/var/lib/troshka/vms/old-project"],
                "orphan_domains": ["troshka-old1234"],
                "orphan_bridges": [],
                "orphan_containers": [],
                "orphan_namespaces": [],
                "cache_items": [],
                "stale_temps": [],
            },
        }

        from app.services.gc_service import discover_orphans

        result = discover_orphans(db, host)

        assert "error" not in result
        assert len(result["orphan_dirs"]) == 1
        assert len(result["orphan_domains"]) == 1
        mock_start.assert_called_once()
    finally:
        db.rollback()
        db.close()


@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_discover_orphans_failed_job(mock_start, mock_wait):
    """Failed discovery job returns error."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "timeout"},
        }

        from app.services.gc_service import discover_orphans

        result = discover_orphans(db, host)
        assert "error" in result
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# clean_orphans
# ---------------------------------------------------------------------------


def test_clean_orphans_host_not_reachable():
    """Disconnected host returns error."""
    host = MagicMock()
    host.ip_address = None
    host.agent_status = "disconnected"

    from app.services.gc_service import clean_orphans

    result = clean_orphans(host, {})
    assert result["error"] == "Host not reachable"
    assert result["cleaned"] == 0


@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_clean_orphans_success(mock_start, mock_wait):
    """Successful cleanup returns cleaned count."""
    host = MagicMock()
    host.ip_address = "10.0.0.1"
    host.agent_status = "connected"

    mock_start.return_value = "job-1"
    mock_wait.return_value = {
        "status": "completed",
        "output": ["Cleaned 3 items"],
    }

    orphans = {
        "orphan_dirs": ["/dir1", "/dir2"],
        "orphan_domains": ["dom1"],
        "orphan_containers": [],
        "orphan_bridges": [],
        "orphan_namespaces": [],
        "orphaned_bmc_project_ids": [],
    }

    from app.services.gc_service import clean_orphans

    result = clean_orphans(host, orphans)
    assert result["success"] is True
    assert result["cleaned"] == 3  # 2 dirs + 1 domain


@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_clean_orphans_with_cache(mock_start, mock_wait):
    """Cache items from DB filtering are included in cleanup."""
    db = TestSession()
    try:
        host = MagicMock()
        host.ip_address = "10.0.0.1"
        host.agent_status = "connected"

        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed", "output": []}

        orphan_id = str(uuid.uuid4())
        orphans = {
            "orphan_dirs": [],
            "orphan_domains": [],
            "orphan_containers": [],
            "orphan_bridges": [],
            "orphan_namespaces": [],
            "orphaned_bmc_project_ids": [],
            "cache_items": [{"path": f"/var/lib/troshka/images/{orphan_id}.qcow2"}],
            "stale_temps": ["/var/lib/troshka/tmp/old-tmp-dir"],
        }

        from app.services.gc_service import clean_orphans

        result = clean_orphans(host, orphans, db)
        assert result["cache_cleaned"] >= 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# repair_networks
# ---------------------------------------------------------------------------


def test_repair_networks_host_not_reachable():
    """Disconnected host returns error."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider, agent_status="disconnected")

        from app.services.gc_service import repair_networks

        result = repair_networks(db, host)
        assert result["error"] == "Host not reachable"
    finally:
        db.rollback()
        db.close()


def test_repair_networks_no_projects():
    """No active projects returns repaired=0."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)

        from app.services.gc_service import repair_networks

        result = repair_networks(db, host)
        assert result["repaired"] == 0
    finally:
        db.rollback()
        db.close()


@patch("app.services.deploy_service._setup_networks_via_troshkad")
@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_repair_networks_missing_bridges(mock_start, mock_wait, mock_setup):
    """Missing bridges for active projects are repaired."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        _make_project(
            db,
            user,
            host,
            state="active",
            topology={"nodes": [], "edges": []},
            vni_map={"net-1": 1001},
        )

        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"bridges": []},  # no bridges exist
        }
        mock_setup.return_value = True

        from app.services.gc_service import repair_networks

        result = repair_networks(db, host)
        assert result["repaired"] == 1
        mock_setup.assert_called_once()
    finally:
        db.rollback()
        db.close()


@patch("app.services.troshkad_client.wait_for_job")
@patch("app.services.troshkad_client.start_job")
def test_repair_networks_all_bridges_present(mock_start, mock_wait):
    """All bridges already exist -> repaired=0."""
    db = TestSession()
    try:
        provider = _make_provider(db)
        host = _make_host(db, provider)
        user = _make_user(db)

        _make_project(
            db,
            user,
            host,
            state="active",
            vni_map={"net-1": 1001},
        )

        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "completed",
            "result": {"bridges": ["br-1001"]},
        }

        from app.services.gc_service import repair_networks

        result = repair_networks(db, host)
        assert result["repaired"] == 0
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# recover_host_services
# ---------------------------------------------------------------------------


@patch("app.services.gc_service.repair_networks")
@patch("app.core.database.SessionLocal")
def test_recover_host_services_disconnected(mock_session_local, mock_repair):
    """Recovery skips if host is disconnected."""
    db = MagicMock()
    mock_session_local.return_value = db
    host = MagicMock()
    host.agent_status = "disconnected"
    db.query.return_value.filter_by.return_value.first.return_value = host

    from app.services.gc_service import _recovering_hosts, recover_host_services

    host_id = str(uuid.uuid4())
    _recovering_hosts.discard(host_id)  # Ensure clean state

    recover_host_services(host_id)

    mock_repair.assert_not_called()
    db.close.assert_called_once()


def test_recover_host_services_dedup():
    """Concurrent recovery calls are deduplicated."""
    from app.services.gc_service import _recovering_hosts, recover_host_services

    host_id = str(uuid.uuid4())
    _recovering_hosts.add(host_id)

    # Should return immediately without doing anything
    recover_host_services(host_id)

    # Clean up
    _recovering_hosts.discard(host_id)


# ---------------------------------------------------------------------------
# reconcile_host
# ---------------------------------------------------------------------------


@patch("app.core.database.SessionLocal")
def test_reconcile_host_not_found(mock_session_local):
    """Nonexistent host returns error."""
    db = MagicMock()
    mock_session_local.return_value = db
    db.query.return_value.filter_by.return_value.first.return_value = None

    from app.services.gc_service import reconcile_host

    result = reconcile_host(str(uuid.uuid4()))
    assert result["error"] == "Host not found"


@patch("app.services.gc_service.clean_s3_orphans", return_value={"deleted": 0})
@patch(
    "app.services.gc_service.clean_orphans",
    return_value={"success": True, "cleaned": 0, "cache_cleaned": 0},
)
@patch("app.services.gc_service.discover_orphans")
@patch("app.services.gc_service.repair_networks", return_value={"repaired": 0})
@patch("app.services.gc_service.sync_host_capacity")
@patch("app.core.database.SessionLocal")
def test_reconcile_host_skips_deploying(
    mock_sl, mock_sync, mock_repair, mock_discover, mock_clean, mock_s3
):
    """GC is skipped when projects are deploying."""
    db = MagicMock()
    mock_sl.return_value = db

    host = MagicMock()
    host.id = str(uuid.uuid4())
    host.ip_address = "10.0.0.1"
    host.agent_status = "connected"
    host.provider_id = None
    host.storage_pool_id = None
    db.query.return_value.filter_by.return_value.first.return_value = host

    # Simulate deploying count > 0
    db.query.return_value.filter.return_value.count.return_value = 1

    from app.services.gc_service import reconcile_host

    result = reconcile_host(host.id)
    assert "skipped" in result
    mock_discover.assert_not_called()


@patch("app.services.gc_service.clean_s3_orphans", return_value={"deleted": 0})
@patch("app.services.gc_service.repair_networks", return_value={"repaired": 0})
@patch("app.services.gc_service.discover_orphans")
@patch(
    "app.services.gc_service.sync_host_capacity",
    return_value={"old": {}, "new": {}, "changed": False},
)
@patch("app.core.database.SessionLocal")
def test_reconcile_host_unreachable(
    mock_sl, mock_sync, mock_discover, mock_repair, mock_s3
):
    """Unreachable host skips orphan scan."""
    db = MagicMock()
    mock_sl.return_value = db

    host = MagicMock()
    host.id = str(uuid.uuid4())
    host.ip_address = None
    host.agent_status = "disconnected"
    host.provider_id = None
    host.storage_pool_id = None
    db.query.return_value.filter_by.return_value.first.return_value = host
    db.query.return_value.filter.return_value.count.return_value = 0

    from app.services.gc_service import reconcile_host

    result = reconcile_host(host.id)
    assert "not reachable" in result.get("orphans", {}).get("error", "")
    mock_discover.assert_not_called()
