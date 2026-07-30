"""Integration tests for deploy_service.py and projects.py orchestration functions.

These tests use REAL DB state (SQLite via TestSession) and mock ONLY external I/O:
- troshkad_client (start_job, wait_for_job)
- notify_project (WebSocket)
- Redis helpers (progress, cancellation, locks, semaphores)
- placement helpers (record_deploy_start/end, sync_host_capacity)

All DB queries, state transitions, and topology logic execute for REAL.
"""

import copy
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.core.auth import hash_password
from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from tests.conftest import TestSession

# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------


def _base_topology(
    vm_count=1,
    with_storage=False,
    with_gateway=False,
    with_ocp_monitor=False,
):
    """Build a realistic topology with VMs, networks, and edges."""
    nodes = []
    edges = []

    # Management network
    net_id = f"net-{uuid.uuid4().hex[:8]}"
    nodes.append(
        {
            "id": net_id,
            "type": "networkNode",
            "position": {"x": 200, "y": 0},
            "data": {
                "label": "mgmt",
                "cidr": "192.168.1.0/24",
                "networkType": "management",
                "gateway": True,
            },
        }
    )

    for i in range(vm_count):
        vm_id = f"vm-{uuid.uuid4().hex[:8]}"
        nic_id = f"nic-{uuid.uuid4().hex[:8]}"
        vm_data = {
            "label": f"test-vm-{i}",
            "name": f"test-vm-{i}",
            "cpus": 2,
            "vcpus": 2,
            "ram": 4,
            "memory": 4096,
            "nics": [
                {
                    "id": nic_id,
                    "ip": f"192.168.1.{10 + i}",
                    "mac": f"52:54:00:00:00:{10 + i:02x}",
                }
            ],
            "firmware": "bios",
            "cloudInit": True,
            "ciCloudUserPassword": "testpass123",
            "disks": [],
            "diskControllers": [],
            "bootDevices": [],
        }
        if with_ocp_monitor and i == 0:
            vm_data["ocpMonitor"] = True
            vm_data["os"] = "rhcos"
        nodes.append(
            {
                "id": vm_id,
                "type": "vmNode",
                "position": {"x": 0, "y": i * 100},
                "data": vm_data,
            }
        )
        edges.append(
            {
                "id": f"e-{uuid.uuid4().hex[:8]}",
                "source": vm_id,
                "target": net_id,
                "sourceHandle": f"nic-{nic_id}",
                "targetHandle": f"port-{vm_id}",
            }
        )

        if with_storage:
            disk_id = f"disk-{uuid.uuid4().hex[:8]}"
            dp_id = f"dp-{uuid.uuid4().hex[:8]}"
            nodes.append(
                {
                    "id": disk_id,
                    "type": "storageNode",
                    "position": {"x": 400, "y": i * 100},
                    "data": {
                        "label": f"disk-{i}",
                        "name": f"disk-{i}",
                        "size": 20,
                        "format": "qcow2",
                        "source": "blank",
                    },
                }
            )
            vm_data["diskControllers"] = [{"id": dp_id, "bus": "virtio"}]
            vm_data["bootDevices"] = [disk_id]
            edges.append(
                {
                    "id": f"de-{uuid.uuid4().hex[:8]}",
                    "source": vm_id,
                    "target": disk_id,
                    "sourceHandle": dp_id,
                    "targetHandle": f"dp-in-{disk_id}",
                }
            )

    if with_gateway:
        gw_id = f"gw-{uuid.uuid4().hex[:8]}"
        nodes.append(
            {
                "id": gw_id,
                "type": "gatewayNode",
                "position": {"x": 100, "y": 0},
                "data": {"label": "gateway"},
            }
        )
        edges.append(
            {
                "id": f"ge-{uuid.uuid4().hex[:8]}",
                "source": gw_id,
                "target": net_id,
                "sourceHandle": "gw-out",
                "targetHandle": f"port-{gw_id}",
            }
        )

    return {"nodes": nodes, "edges": edges}


def _seed_deploy_env(
    project_state="deploying",
    topology=None,
    with_storage=False,
    with_gateway=False,
    host_type="shared",
    deployed_topology=None,
    auto_stop_minutes=None,
    host_ip="10.0.0.1",
):
    """Create a complete deploy environment in the test DB."""
    db = TestSession()
    try:
        user = User(
            id=str(uuid.uuid4()),
            email=f"int-{uuid.uuid4().hex[:6]}@test.com",
            role="admin",
            auth_source="local",
            password_hash=hash_password("testpass"),
        )
        db.add(user)

        provider = Provider(
            id=str(uuid.uuid4()),
            name=f"test-prov-{uuid.uuid4().hex[:6]}",
            type="ec2",
            credentials='{"access_key_id":"x","secret_access_key":"x"}',
            default_region="us-east-1",
            state="active",
        )
        db.add(provider)
        db.flush()

        host = Host(
            id=str(uuid.uuid4()),
            provider_id=provider.id,
            state="running",
            host_type=host_type,
            ip_address=host_ip,
            private_ip="10.0.0.1",
            agent_status="connected",
            agent_token="test-token-abc",
            private_key="test-private-key",
            total_vcpus=64,
            total_ram_mb=256000,
            storage_size_gb=1000,
            used_vcpus=0,
            used_ram_mb=0,
        )
        db.add(host)
        db.flush()

        if topology is None:
            topology = _base_topology(
                with_storage=with_storage, with_gateway=with_gateway
            )

        project = Project(
            id=str(uuid.uuid4()),
            name=f"int-test-{uuid.uuid4().hex[:6]}",
            state=project_state,
            owner_id=user.id,
            host_id=host.id,
            topology=topology,
            vni_map={"net-001": 100},
            deployed_topology=deployed_topology,
            auto_stop_minutes=auto_stop_minutes,
        )
        db.add(project)
        db.commit()

        ids = {
            "user": user.id,
            "provider": provider.id,
            "host": host.id,
            "project": project.id,
        }
    finally:
        db.close()
    return ids


# ---------------------------------------------------------------------------
# Patch context managers
# ---------------------------------------------------------------------------

DS = "app.services.deploy_service"


@contextmanager
def _deploy_patches(extra_patches=None):
    """Context manager that patches all external I/O for deploy functions.

    Yields a dict of mock objects keyed by name.
    """
    TestSession()

    patches = {
        # SessionLocal -> TestSession
        "session_local": patch("app.core.database.SessionLocal", lambda: TestSession()),
        # troshkad calls — patch BOTH module-level AND source (for lazy imports)
        "start_job": patch(f"{DS}.start_job"),
        "wait_for_job": patch(f"{DS}.wait_for_job"),
        "start_job_src": patch("app.services.troshkad_client.start_job"),
        "wait_for_job_src": patch("app.services.troshkad_client.wait_for_job"),
        "poll_job_src": patch("app.services.troshkad_client.poll_job"),
        # WebSocket
        "notify": patch(f"{DS}.notify_project"),
        # Redis progress/cancellation
        "set_progress": patch(f"{DS}.set_progress"),
        "get_progress": patch(f"{DS}.get_progress", return_value=None),
        "delete_progress": patch(f"{DS}.delete_progress"),
        "is_cancelled": patch(f"{DS}._redis_is_cancelled", return_value=False),
        "clear_cancelled": patch(f"{DS}.clear_cancelled"),
        "get_lock": patch(f"{DS}.get_lock"),
        "add_to_set": patch(f"{DS}.add_to_set"),
        "remove_from_set": patch(f"{DS}.remove_from_set"),
        "is_in_set": patch(f"{DS}.is_in_set", return_value=False),
        # Placement
        "record_start": patch("app.services.placement.record_deploy_start"),
        "record_end": patch("app.services.placement.record_deploy_end"),
        "sync_capacity": patch("app.services.placement.sync_host_capacity"),
    }
    if extra_patches:
        patches.update(extra_patches)

    mocks = {}
    try:
        for name, p in patches.items():
            mocks[name] = p.start()
        # get_lock must return a context manager
        lock_mock = MagicMock()
        lock_mock.__enter__ = MagicMock(return_value=lock_mock)
        lock_mock.__exit__ = MagicMock(return_value=False)
        mocks["get_lock"].return_value = lock_mock
        # Wire source-level mocks to delegate to module-level mocks
        # so lazy imports inside sub-functions get the same mock behavior
        mocks["start_job_src"].side_effect = lambda *a, **kw: mocks["start_job"](
            *a, **kw
        )
        mocks["wait_for_job_src"].side_effect = lambda *a, **kw: mocks["wait_for_job"](
            *a, **kw
        )
        mocks["poll_job_src"].side_effect = lambda *a, **kw: mocks.get(
            "poll_job", MagicMock()
        )(*a, **kw)
        yield mocks
    finally:
        for p in patches.values():
            p.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeployProjectInner:
    """Integration tests for _deploy_project_inner."""

    def test_deploy_full_flow_sets_active(self):
        """Full deploy with real DB records. Verify state -> active."""
        ids = _seed_deploy_env(
            project_state="deploying",
            topology=_base_topology(vm_count=1, with_storage=True),
        )

        with _deploy_patches() as m:
            m["start_job"].return_value = "job-001"
            m["wait_for_job"].return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-uuid-001"},
            }

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert (
                project.state == "active"
            ), f"Expected 'active', got '{project.state}': {project.deploy_error}"
            assert project.deploy_error is None
            assert project.deployed_topology is not None
        finally:
            db.close()

    def test_deploy_no_auto_start_sets_stopped(self):
        """Deploy with auto_start=False should set state to stopped."""
        ids = _seed_deploy_env(project_state="deploying")

        with _deploy_patches() as m:
            m["start_job"].return_value = "job-002"
            m["wait_for_job"].return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-uuid-002"},
            }

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=False)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert (
                project.state == "stopped"
            ), f"Expected 'stopped', got '{project.state}': {project.deploy_error}"
        finally:
            db.close()

    def test_deploy_network_failure_sets_error(self):
        """Network setup failure should set project to error state."""
        ids = _seed_deploy_env(project_state="deploying")

        with _deploy_patches() as m:
            from app.services.troshkad_client import TroshkadError

            m["start_job"].side_effect = TroshkadError("Network setup timeout")

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "error", f"Expected 'error', got '{project.state}'"
            assert project.deploy_error is not None
            assert "Network setup timeout" in project.deploy_error
        finally:
            db.close()

    def test_deploy_vm_start_failure_sets_error(self):
        """VM start failure should set error state after networks/disks succeed."""
        ids = _seed_deploy_env(
            project_state="deploying",
            topology=_base_topology(vm_count=1, with_storage=True),
        )

        with _deploy_patches() as m:
            from app.services.troshkad_client import TroshkadError

            call_count = {"n": 0}

            def _start_job(host, endpoint, params=None):
                call_count["n"] += 1
                return f"job-{call_count['n']:03d}"

            m["start_job"].side_effect = _start_job

            endpoints_seen = []

            def _wait(host, job_id, timeout=None):
                # Track the last endpoint passed to start_job
                if m["start_job"].call_args:
                    ep = (
                        m["start_job"].call_args[0][1]
                        if len(m["start_job"].call_args[0]) > 1
                        else ""
                    )
                    endpoints_seen.append(ep)

                    if ep == "/vms/start":
                        raise TroshkadError("VM start failed: libvirt error")
                return {
                    "status": "completed",
                    "result": {"domain_uuid": "dom-uuid-003"},
                }

            m["wait_for_job"].side_effect = _wait

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "error", f"Expected 'error', got '{project.state}'"
            assert project.deploy_error is not None
        finally:
            db.close()

    def test_deploy_non_deploying_state_exits_early(self):
        """If project is not in 'deploying' state, deploy should exit immediately."""
        ids = _seed_deploy_env(project_state="active")

        with _deploy_patches() as m:
            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

            m["start_job"].assert_not_called()

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "active"
        finally:
            db.close()

    def test_deploy_no_host_sets_error(self):
        """Deploy with no host and no capacity should set error state."""
        db = TestSession()
        try:
            user = User(
                id=str(uuid.uuid4()),
                email=f"nohost-{uuid.uuid4().hex[:6]}@test.com",
                role="admin",
                auth_source="local",
                password_hash=hash_password("p"),
            )
            db.add(user)
            db.flush()

            project = Project(
                id=str(uuid.uuid4()),
                name="no-host-test",
                state="deploying",
                owner_id=user.id,
                host_id=None,
                topology=_base_topology(vm_count=1),
                vni_map={"net-001": 100},
            )
            db.add(project)
            db.commit()
            project_id = project.id
        finally:
            db.close()

        extra = {
            "find_host": patch(
                "app.services.placement.find_available_host", return_value=None
            ),
            "calc_reqs": patch(
                "app.services.placement.calculate_project_requirements",
                return_value={"total_vcpus": 4, "total_ram_mb": 8192},
            ),
        }
        with _deploy_patches(extra_patches=extra):
            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(project_id, auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=project_id).first()
            assert project is not None
            assert project.state == "error"
            assert (
                "capacity" in project.deploy_error.lower()
                or "room" in project.deploy_error.lower()
            )
        finally:
            db.close()

    def test_deploy_with_gateway_injects_ntp_ip(self):
        """Deploy with a gateway node should inject gateway_ip for NTP."""
        topo = _base_topology(vm_count=1, with_gateway=True)
        ids = _seed_deploy_env(project_state="deploying", topology=topo)

        with _deploy_patches() as m:
            m["start_job"].return_value = "job-gw-001"
            m["wait_for_job"].return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-uuid-gw"},
            }

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "active"
            for node in (project.topology or {}).get("nodes", []):
                if node.get("type") == "vmNode" and node.get("data", {}).get(
                    "cloudInit"
                ):
                    assert node["data"].get("gateway_ip") == "192.168.1.1"
        finally:
            db.close()

    def test_deploy_sets_deployed_topology_snapshot(self):
        """After deploy, deployed_topology should be a copy of topology."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(project_state="deploying", topology=topo)

        with _deploy_patches() as m:
            m["start_job"].return_value = "job-snap-001"
            m["wait_for_job"].return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-snap"},
            }

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.deployed_topology is not None
            # deployed_topology should have same VM nodes
            dt_vms = [
                n
                for n in project.deployed_topology.get("nodes", [])
                if n.get("type") == "vmNode"
            ]
            t_vms = [
                n
                for n in project.topology.get("nodes", [])
                if n.get("type") == "vmNode"
            ]
            assert len(dt_vms) == len(t_vms)
        finally:
            db.close()


class TestStopProjectAsync:
    """Integration tests for stop_project_async."""

    def test_stop_full_flow_sets_stopped(self):
        """Stop a running project via stop_project_async."""
        topo = _base_topology(vm_count=2)
        ids = _seed_deploy_env(project_state="stopping", topology=topo)

        with _deploy_patches() as m:
            m["start_job"].return_value = "stop-job-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import stop_project_async

            stop_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "stopped"
            assert project.deploy_error is None
            assert project.auto_stop_started_at is None
            assert project.auto_stop_expires_at is None
        finally:
            db.close()

    def test_stop_no_host_sets_error(self):
        """Stop should error when host is not found."""
        db = TestSession()
        try:
            user = User(
                id=str(uuid.uuid4()),
                email=f"stop-nohost-{uuid.uuid4().hex[:6]}@test.com",
                role="admin",
                auth_source="local",
                password_hash=hash_password("p"),
            )
            db.add(user)
            db.flush()

            project = Project(
                id=str(uuid.uuid4()),
                name="stop-nohost-test",
                state="stopping",
                owner_id=user.id,
                host_id=str(uuid.uuid4()),
                topology=_base_topology(vm_count=1),
                vni_map={"net-001": 100},
            )
            db.add(project)
            db.commit()
            project_id = project.id
        finally:
            db.close()

        with _deploy_patches():
            from app.services.deploy_service import stop_project_async

            stop_project_async(project_id)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=project_id).first()
            assert project is not None
            assert project.state == "error"
            assert (
                "unavailable" in project.deploy_error.lower()
                or "host" in project.deploy_error.lower()
            )
        finally:
            db.close()

    def test_stop_vm_failure_still_marks_stopped(self):
        """Individual VM stop failures should not prevent project from being stopped."""
        topo = _base_topology(vm_count=2)
        ids = _seed_deploy_env(project_state="stopping", topology=topo)

        with _deploy_patches() as m:
            from app.services.troshkad_client import TroshkadError

            m["start_job"].return_value = "stop-job-002"
            m["wait_for_job"].side_effect = TroshkadError("VM stop failed")

            from app.services.deploy_service import stop_project_async

            stop_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "stopped"
        finally:
            db.close()

    def test_stop_clears_auto_stop_timer(self):
        """Stop should clear auto-stop timer fields."""
        import datetime

        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="stopping",
            topology=topo,
            auto_stop_minutes=30,
        )
        # Set timer fields that stop should clear
        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            now = datetime.datetime.now(datetime.UTC)
            project.auto_stop_started_at = now
            project.auto_stop_expires_at = now + datetime.timedelta(minutes=30)
            project.auto_stop_warned = True
            db.commit()
        finally:
            db.close()

        with _deploy_patches() as m:
            m["start_job"].return_value = "stop-timer-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import stop_project_async

            stop_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project.state == "stopped"
            assert project.auto_stop_started_at is None
            assert project.auto_stop_expires_at is None
            assert project.auto_stop_warned is False
        finally:
            db.close()


class TestStartProjectAsync:
    """Integration tests for start_project_async."""

    def test_start_full_flow_sets_active(self):
        """Start a stopped project via start_project_async."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="starting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
        )

        with _deploy_patches() as m:
            m["start_job"].return_value = "start-job-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import start_project_async

            start_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "active"
            assert project.deploy_error is None
            assert project.auto_stopped is False
        finally:
            db.close()

    def test_start_with_auto_stop_timer(self):
        """Start should restart auto-stop timer when configured."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="starting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
            auto_stop_minutes=60,
        )

        with _deploy_patches() as m:
            m["start_job"].return_value = "start-job-002"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import start_project_async

            start_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "active"
            assert project.auto_stop_started_at is not None
            assert project.auto_stop_expires_at is not None
            assert project.auto_stop_warned is False
        finally:
            db.close()

    def test_start_no_host_sets_error(self):
        """Start should set error state when host not found."""
        db = TestSession()
        try:
            user = User(
                id=str(uuid.uuid4()),
                email=f"start-nohost-{uuid.uuid4().hex[:6]}@test.com",
                role="admin",
                auth_source="local",
                password_hash=hash_password("p"),
            )
            db.add(user)
            db.flush()

            project = Project(
                id=str(uuid.uuid4()),
                name="start-nohost-test",
                state="starting",
                owner_id=user.id,
                host_id=str(uuid.uuid4()),
                topology=_base_topology(vm_count=1),
                vni_map={"net-001": 100},
            )
            db.add(project)
            db.commit()
            project_id = project.id
        finally:
            db.close()

        with _deploy_patches():
            from app.services.deploy_service import start_project_async

            start_project_async(project_id)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=project_id).first()
            assert project is not None
            assert project.state == "error"
            assert (
                "unavailable" in project.deploy_error.lower()
                or "host" in project.deploy_error.lower()
            )
        finally:
            db.close()

    def test_start_no_ip_sets_error(self):
        """Start should error when host has no IP address."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="starting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
            host_ip=None,
        )

        with _deploy_patches():
            from app.services.deploy_service import start_project_async

            start_project_async(ids["project"])

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "error"
            assert (
                "unavailable" in project.deploy_error.lower()
                or "host" in project.deploy_error.lower()
            )
        finally:
            db.close()


class TestDestroyProjectInner:
    """Integration tests for _destroy_project_inner."""

    def test_destroy_full_flow_deletes_record(self):
        """Full destroy should delete the project record from DB."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="deleting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
        )

        ctx = {
            "project_id": ids["project"],
            "host_id": ids["host"],
            "vni_map": {"net-001": 100},
            "topology": copy.deepcopy(topo),
            "dns_provider_id": None,
            "domain": None,
        }

        with _deploy_patches() as m:
            m["start_job"].return_value = "destroy-job-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import _destroy_project_inner

            _destroy_project_inner(ctx, delete_record=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is None, "Project should be deleted from DB"
        finally:
            db.close()

    def test_destroy_no_delete_record(self):
        """Destroy with delete_record=False should keep the project in DB."""
        topo = _base_topology(vm_count=1)
        ids = _seed_deploy_env(
            project_state="deleting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
        )

        ctx = {
            "project_id": ids["project"],
            "host_id": ids["host"],
            "vni_map": {"net-001": 100},
            "topology": copy.deepcopy(topo),
            "dns_provider_id": None,
            "domain": None,
        }

        with _deploy_patches() as m:
            m["start_job"].return_value = "destroy-job-002"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import _destroy_project_inner

            _destroy_project_inner(ctx, delete_record=False)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None, "Project should still exist in DB"
        finally:
            db.close()

    def test_destroy_missing_host_still_deletes(self):
        """Destroy when host is gone should still delete the project record."""
        db = TestSession()
        try:
            user = User(
                id=str(uuid.uuid4()),
                email=f"destroy-nohost-{uuid.uuid4().hex[:6]}@test.com",
                role="admin",
                auth_source="local",
                password_hash=hash_password("p"),
            )
            db.add(user)
            db.flush()

            project = Project(
                id=str(uuid.uuid4()),
                name="destroy-nohost-test",
                state="deleting",
                owner_id=user.id,
                host_id=str(uuid.uuid4()),
                topology=_base_topology(vm_count=1),
                vni_map={"net-001": 100},
            )
            db.add(project)
            db.commit()
            project_id = project.id
        finally:
            db.close()

        ctx = {
            "project_id": project_id,
            "host_id": str(uuid.uuid4()),
            "vni_map": {"net-001": 100},
            "topology": _base_topology(vm_count=1),
            "dns_provider_id": None,
            "domain": None,
        }

        with _deploy_patches():
            from app.services.deploy_service import _destroy_project_inner

            _destroy_project_inner(ctx, delete_record=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=project_id).first()
            assert project is None, "Project should be deleted even with missing host"
        finally:
            db.close()

    def test_destroy_calls_troshkad_for_each_vm(self):
        """Destroy should call troshkad to destroy each VM in the topology."""
        topo = _base_topology(vm_count=3)
        ids = _seed_deploy_env(
            project_state="deleting",
            topology=topo,
            deployed_topology=copy.deepcopy(topo),
        )

        ctx = {
            "project_id": ids["project"],
            "host_id": ids["host"],
            "vni_map": {"net-001": 100},
            "topology": copy.deepcopy(topo),
            "dns_provider_id": None,
            "domain": None,
        }

        with _deploy_patches() as m:
            m["start_job"].return_value = "destroy-multi-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import _destroy_project_inner

            _destroy_project_inner(ctx, delete_record=True)

            # Verify troshkad was called to destroy VMs
            vm_destroy_calls = [
                c
                for c in m["start_job"].call_args_list
                if len(c[0]) > 1 and c[0][1] == "/vms/destroy"
            ]
            assert len(vm_destroy_calls) == 3


class TestDeployHelpers:
    """Test pure topology functions with real data."""

    def test_extract_vms_from_real_topology(self):
        """_extract_vms should find all VM nodes in a topology."""
        from app.services.deploy_service import _extract_vms

        topo = _base_topology(vm_count=3)
        vms = _extract_vms(topo)
        assert len(vms) == 3
        for vm in vms:
            assert "node_id" in vm
            assert vm["vcpus"] == 2
            assert vm["ram_gb"] == 4
            assert vm["firmware"] == "bios"

    def test_extract_containers_empty(self):
        """_extract_containers returns empty for VM-only topology."""
        from app.services.deploy_service import _extract_containers

        topo = _base_topology(vm_count=2)
        containers = _extract_containers(topo)
        assert containers == []

    def test_vm_domain_name_format(self):
        """_vm_domain_name should produce correct format."""
        from app.services.deploy_service import _vm_domain_name

        pid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        nid = "vm-12345678-abcd"
        name = _vm_domain_name(pid, nid)
        assert name == f"troshka-{pid[:8]}-{nid[:8]}"

    def test_find_vm_disks_with_storage(self):
        """_find_vm_disks should find storage nodes connected to a VM."""
        from app.services.deploy_service import _find_vm_disks

        topo = _base_topology(vm_count=1, with_storage=True)
        vm_node = next(n for n in topo["nodes"] if n.get("type") == "vmNode")
        disks = _find_vm_disks(vm_node["id"], topo)
        assert len(disks) == 1
        assert disks[0]["size_gb"] == 20
        assert disks[0]["format"] == "qcow2"
        assert disks[0]["source"] == "blank"

    def test_is_ocp_topology_false(self):
        """_is_ocp_topology returns False for non-OCP topologies."""
        from app.services.deploy_service import _is_ocp_topology

        assert _is_ocp_topology(_base_topology(vm_count=1)) is False

    def test_has_ocp_monitor_true(self):
        """_has_ocp_monitor detects ocpMonitor flag."""
        from app.services.deploy_service import _has_ocp_monitor

        assert (
            _has_ocp_monitor(_base_topology(vm_count=1, with_ocp_monitor=True)) is True
        )

    def test_has_ocp_monitor_false(self):
        """_has_ocp_monitor returns False when no VM has ocpMonitor."""
        from app.services.deploy_service import _has_ocp_monitor

        assert _has_ocp_monitor(_base_topology(vm_count=1)) is False

    def test_auto_assign_container_ips_no_containers(self):
        """_auto_assign_container_ips should be a no-op for VM-only topology."""
        from app.services.deploy_service import _auto_assign_container_ips

        topo = _base_topology(vm_count=1)
        original = copy.deepcopy(topo)
        _auto_assign_container_ips(topo)
        assert len(topo["nodes"]) == len(original["nodes"])


class TestDeployWithMultipleVMs:
    """Test deploy with multiple VMs to cover iteration logic."""

    def test_deploy_two_vms_both_start(self):
        """Deploy with 2 VMs should start both and set active."""
        topo = _base_topology(vm_count=2, with_storage=True)
        ids = _seed_deploy_env(project_state="deploying", topology=topo)

        with _deploy_patches() as m:
            job_counter = {"n": 0}

            def _start_job(host, endpoint, params=None, **kwargs):
                job_counter["n"] += 1
                return f"multi-{job_counter['n']:03d}"

            m["start_job"].side_effect = _start_job
            m["wait_for_job"].return_value = {
                "status": "completed",
                "result": {"domain_uuid": "dom-multi-uuid"},
            }

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert (
                project.state == "active"
            ), f"Expected 'active', got '{project.state}': {project.deploy_error}"
            assert project.deployed_topology is not None
            vm_nodes = [
                n
                for n in project.deployed_topology.get("nodes", [])
                if n.get("type") == "vmNode"
            ]
            assert len(vm_nodes) == 2
        finally:
            db.close()

    def test_deploy_project_deleted_mid_deploy(self):
        """Deploy should abort when project is deleted mid-deploy."""
        ids = _seed_deploy_env(project_state="deploying")

        extra = {
            "project_deleted": patch(f"{DS}._project_deleted", return_value=True),
        }
        with _deploy_patches(extra_patches=extra) as m:
            m["start_job"].return_value = "job-del-001"
            m["wait_for_job"].return_value = {"status": "completed", "result": {}}

            from app.services.deploy_service import _deploy_project_inner

            _deploy_project_inner(ids["project"], auto_start=True)

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            if project:
                assert (
                    project.state != "active"
                ), "Project should not be active after mid-deploy deletion"
        finally:
            db.close()


class TestDeleteProjectRecord:
    """Integration tests for _delete_project_record helper."""

    def test_delete_project_record_removes_from_db(self):
        """_delete_project_record should remove the project and notify."""
        ids = _seed_deploy_env(project_state="deleting")

        with _deploy_patches() as m:
            from app.services.deploy_service import _delete_project_record

            _delete_project_record(ids["project"])

            # Verify notification was sent
            m["notify"].assert_called()
            call_args = m["notify"].call_args[0]
            assert call_args[0] == ids["project"]
            assert call_args[1]["type"] == "project-deleted"

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is None
        finally:
            db.close()

    def test_delete_nonexistent_project_no_error(self):
        """_delete_project_record should handle non-existent project gracefully."""
        with _deploy_patches():
            from app.services.deploy_service import _delete_project_record

            # Should not raise
            _delete_project_record(str(uuid.uuid4()))


class TestSetDestroyError:
    """Integration tests for _set_destroy_error helper."""

    def test_set_destroy_error_updates_state(self):
        """_set_destroy_error should set state to error with message."""
        ids = _seed_deploy_env(project_state="deleting")

        with _deploy_patches():
            from app.services.deploy_service import _set_destroy_error

            _set_destroy_error(ids["project"], "Something went wrong")

        db = TestSession()
        try:
            project = db.query(Project).filter_by(id=ids["project"]).first()
            assert project is not None
            assert project.state == "error"
            assert "Something went wrong" in project.deploy_error
            assert "Delete failed:" in project.deploy_error
        finally:
            db.close()
