"""Targeted tests to boost new-code line coverage past 80%.

Covers:
  - placement.place_project (lines 432-604)
  - ws_pubsub._collect_host_batch_for_project (lines 481-489)
  - ws_pubsub._deliver_locally when _loop is None (line 102)
"""

import uuid
from unittest.mock import MagicMock, patch

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.storage_pool import StoragePool
from app.models.user import User
from app.services.placement import place_project
from tests.conftest import TestSession

# Shared user
_setup_db = TestSession()
_test_user = User(
    email="coverage-boost@test.com",
    display_name="Coverage Boost",
    role="admin",
)
_setup_db.add(_test_user)
_setup_db.commit()
_setup_db.refresh(_test_user)
_USER_ID = _test_user.id
_setup_db.close()


def _make_provider(db):
    p = Provider(name=f"cov-prov-{uuid.uuid4().hex[:6]}", type="ec2")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_host(db, provider, vcpus=64, ram_mb=256000, **kwargs):
    h = Host(
        ip_address=f"10.0.9.{uuid.uuid4().int % 250 + 1}",
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


# ---------------------------------------------------------------------------
# place_project — error paths
# ---------------------------------------------------------------------------
class TestPlaceProjectErrors:
    def test_no_topology(self):
        db = TestSession()
        try:
            p = Project(name="no-topo", owner_id=_USER_ID, topology=None)
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p)
            assert "error" in result
            assert "no topology" in result["error"].lower()

            db.delete(p)
            db.commit()
        finally:
            db.close()

    def test_no_vms(self):
        db = TestSession()
        try:
            p = Project(
                name="no-vms",
                owner_id=_USER_ID,
                topology={
                    "nodes": [{"id": "n1", "type": "networkNode", "data": {}}],
                    "edges": [],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p)
            assert "error" in result
            assert "no VMs" in result["error"]

            db.delete(p)
            db.commit()
        finally:
            db.close()

    def test_host_not_found(self):
        db = TestSession()
        try:
            p = Project(
                name="host-missing",
                owner_id=_USER_ID,
                topology={
                    "nodes": [
                        {"id": "v1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}
                    ],
                    "edges": [],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(
                db, p, host_id="00000000-0000-0000-0000-000000000000"
            )
            assert "error" in result
            assert "not found" in result["error"]

            db.delete(p)
            db.commit()
        finally:
            db.close()

    def test_host_not_available(self):
        db = TestSession()
        try:
            prov = _make_provider(db)
            host = Host(
                ip_address="10.0.9.200",
                provider_id=prov.id,
                state="provisioning",
                agent_status="disconnected",
                total_vcpus=32,
                total_ram_mb=128000,
            )
            db.add(host)
            db.commit()
            db.refresh(host)

            p = Project(
                name="host-unavail",
                owner_id=_USER_ID,
                topology={
                    "nodes": [
                        {"id": "v1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}
                    ],
                    "edges": [],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p, host_id=host.id)
            assert "error" in result
            assert "not available" in result["error"]

            db.delete(p)
            db.delete(host)
            db.delete(prov)
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# place_project — successful single-host placement
# ---------------------------------------------------------------------------
class TestPlaceProjectSuccess:
    @patch("app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5))
    def test_single_host_admin_override(self, _mock_oc):
        db = TestSession()
        try:
            prov = _make_provider(db)
            host = _make_host(db, prov, vcpus=32, ram_mb=128000)

            p = Project(
                name="single-host-test",
                owner_id=_USER_ID,
                topology={
                    "nodes": [
                        {"id": "v1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
                        {
                            "id": "n1",
                            "type": "networkNode",
                            "data": {"cidr": "192.168.1.0/24"},
                        },
                    ],
                    "edges": [
                        {
                            "id": "e1",
                            "source": "v1",
                            "target": "n1",
                            "sourceHandle": "nic1",
                            "targetHandle": "port1",
                        }
                    ],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p, host_id=host.id)
            assert "error" not in result
            assert result["host_id"] == host.id
            assert "vni_map" in result
            assert p.host_id == host.id
            assert p.state == "deploying"

            db.delete(p)
            db.delete(host)
            db.delete(prov)
            db.commit()
        finally:
            db.close()

    @patch("app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5))
    def test_auto_select_host(self, _mock_oc):
        db = TestSession()
        try:
            prov = _make_provider(db)
            pool = StoragePool(
                name=f"cov-pool-{uuid.uuid4().hex[:6]}",
                mode="local",
                status="available",
                provider_id=prov.id,
            )
            db.add(pool)
            db.commit()
            db.refresh(pool)

            host = _make_host(
                db, prov, vcpus=32, ram_mb=128000, storage_pool_id=pool.id
            )

            p = Project(
                name="auto-select-test",
                owner_id=_USER_ID,
                provider_id=prov.id,
                topology={
                    "nodes": [
                        {"id": "v1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
                        {
                            "id": "n1",
                            "type": "networkNode",
                            "data": {"cidr": "10.0.0.0/24"},
                        },
                    ],
                    "edges": [
                        {
                            "id": "e1",
                            "source": "v1",
                            "target": "n1",
                            "sourceHandle": "nic1",
                            "targetHandle": "port1",
                        }
                    ],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p)
            assert "error" not in result
            assert result["host_id"] == host.id

            db.delete(p)
            db.delete(host)
            db.delete(pool)
            db.delete(prov)
            db.commit()
        finally:
            db.close()

    @patch("app.services.placement._get_overcommit_ratios", return_value=(4.0, 1.5))
    def test_host_override_inherits_pool(self, _mock_oc):
        """Admin override with host that has a storage_pool_id inherits it."""
        db = TestSession()
        try:
            prov = _make_provider(db)
            pool = StoragePool(
                name=f"inherit-pool-{uuid.uuid4().hex[:6]}",
                mode="local",
                status="available",
                provider_id=prov.id,
            )
            db.add(pool)
            db.commit()
            db.refresh(pool)

            host = _make_host(
                db, prov, vcpus=32, ram_mb=128000, storage_pool_id=pool.id
            )

            p = Project(
                name="inherit-pool-test",
                owner_id=_USER_ID,
                topology={
                    "nodes": [
                        {"id": "v1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
                        {
                            "id": "n1",
                            "type": "networkNode",
                            "data": {"cidr": "10.0.0.0/24"},
                        },
                    ],
                    "edges": [
                        {
                            "id": "e1",
                            "source": "v1",
                            "target": "n1",
                            "sourceHandle": "nic1",
                            "targetHandle": "port1",
                        }
                    ],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p, host_id=host.id)
            assert "error" not in result
            assert result["host_id"] == host.id

            db.delete(p)
            db.delete(host)
            db.delete(pool)
            db.delete(prov)
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# place_project — anti-affinity error (no 2-host pool)
# ---------------------------------------------------------------------------
class TestPlaceProjectAntiAffinity:
    @patch("app.services.placement._auto_select_pool", return_value=None)
    @patch("app.services.placement._get_overcommit_ratios", return_value=(1.0, 1.0))
    def test_anti_affinity_no_suitable_pool(self, _mock_oc, _mock_pool):
        """Anti-affinity with no pool having 2+ hosts returns error."""
        db = TestSession()
        try:
            # Temporarily mark all existing pools as disabled to isolate
            existing = (
                db.query(StoragePool).filter(StoragePool.status == "available").all()
            )
            for sp in existing:
                sp.status = "disabled"
            db.commit()

            prov = _make_provider(db)
            pool = StoragePool(
                name=f"aa-pool-{uuid.uuid4().hex[:6]}",
                mode="local",
                status="available",
                provider_id=prov.id,
            )
            db.add(pool)
            db.commit()
            db.refresh(pool)

            # Only one host in pool
            host = _make_host(
                db, prov, vcpus=64, ram_mb=256000, storage_pool_id=pool.id
            )

            p = Project(
                name="aa-fail",
                owner_id=_USER_ID,
                topology={
                    "nodes": [
                        {
                            "id": "v1",
                            "type": "vmNode",
                            "data": {"vcpus": 4, "ram": 8, "separateHost": "grp"},
                        },
                        {
                            "id": "v2",
                            "type": "vmNode",
                            "data": {"vcpus": 4, "ram": 8, "separateHost": "grp"},
                        },
                        {
                            "id": "n1",
                            "type": "networkNode",
                            "data": {"cidr": "10.0.0.0/24"},
                        },
                    ],
                    "edges": [],
                },
            )
            db.add(p)
            db.commit()
            db.refresh(p)

            result = place_project(db, p)
            assert "error" in result
            assert "anti-affinity" in result["error"].lower()

            db.delete(p)
            db.delete(host)
            db.delete(pool)
            db.delete(prov)
            # Restore
            for sp in existing:
                sp.status = "available"
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# ws_pubsub._collect_host_batch_for_project
# ---------------------------------------------------------------------------
class TestCollectHostBatchForProject:
    def test_multi_host_assignments(self):
        from app.services.ws_pubsub import _collect_host_batch_for_project

        proj = MagicMock()
        proj.host_assignments = {"vm1": "host-a", "vm2": "host-b", "vm3": "host-a"}
        proj.host_id = "host-a"

        batch_states = {
            "host-a": {"dom-1": "running", "dom-2": "stopped"},
            "host-b": {"dom-3": "running"},
        }

        result = _collect_host_batch_for_project(proj, batch_states)
        assert result is not None
        assert "dom-1" in result
        assert "dom-3" in result

    def test_single_host_no_assignments(self):
        from app.services.ws_pubsub import _collect_host_batch_for_project

        proj = MagicMock()
        proj.host_assignments = None
        proj.host_id = "host-a"

        batch_states = {"host-a": {"dom-1": "running"}}

        result = _collect_host_batch_for_project(proj, batch_states)
        assert result == {"dom-1": "running"}

    def test_no_host_id_no_assignments(self):
        from app.services.ws_pubsub import _collect_host_batch_for_project

        proj = MagicMock()
        proj.host_assignments = None
        proj.host_id = None

        result = _collect_host_batch_for_project(proj, {})
        assert result is None

    def test_multi_host_partial_states(self):
        from app.services.ws_pubsub import _collect_host_batch_for_project

        proj = MagicMock()
        proj.host_assignments = {"vm1": "host-a", "vm2": "host-c"}
        proj.host_id = "host-a"

        batch_states = {"host-a": {"dom-1": "running"}}
        # host-c not in batch_states — should be skipped

        result = _collect_host_batch_for_project(proj, batch_states)
        assert result == {"dom-1": "running"}


# ---------------------------------------------------------------------------
# ws_pubsub._deliver_locally — when _loop is None
# ---------------------------------------------------------------------------
class TestDeliverLocally:
    def test_no_loop_returns_immediately(self):
        import app.services.ws_pubsub as pubsub

        old_loop = pubsub._loop
        pubsub._loop = None
        try:
            # Should not raise
            pubsub._deliver_locally("proj-1", {"type": "test"})
        finally:
            pubsub._loop = old_loop

    def test_no_subscribers_returns_immediately(self):
        import asyncio

        import app.services.ws_pubsub as pubsub

        loop = asyncio.new_event_loop()
        old_loop = pubsub._loop
        pubsub._loop = loop
        try:
            pubsub._subscribers.clear()
            # Should not raise or schedule anything
            pubsub._deliver_locally("proj-nonexistent", {"type": "test"})
        finally:
            pubsub._loop = old_loop
            loop.close()
