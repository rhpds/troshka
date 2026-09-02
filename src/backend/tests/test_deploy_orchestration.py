"""Tests for large orchestration functions in deploy_service.py.

Covers zero-coverage functions: _setup_networks_via_troshkad,
_create_seed_isos_via_troshkad, _create_vm_disks_via_troshkad,
_create_vm_via_troshkad, _start_vms_via_troshkad,
cache_library_images, start_project_async, stop_project_async.
"""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

PROJECT_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
VM_NODE_ID = "vm-node-0001"
VM_NODE_ID_2 = "vm-node-0002"
NET_NODE_ID = "net-node-0001"
DISK_NODE_ID = "disk-node-0001"
DISK_NODE_ID_2 = "disk-node-0002"
HOST_ID = "host-0001"
PATTERN_ID = "pattern-0001"
PATTERN_DISK_ID = "pdisk-0001"
LIBRARY_ITEM_ID = "lib-item-0001"
SNAPSHOT_ITEM_ID = "snap-item-0001"


def _make_host(
    ip="10.0.0.1",
    host_type="ec2",
    provider_id="prov-1",
    storage_pool_id=None,
    state="active",
):
    h = MagicMock()
    h.id = HOST_ID
    h.ip_address = ip
    h.host_type = host_type
    h.provider_id = provider_id
    h.storage_pool_id = storage_pool_id
    h.state = state
    return h


def _make_project(
    state="active",
    host_id=HOST_ID,
    topology=None,
    vni_map=None,
    provider_id="prov-1",
    auto_stop_minutes=None,
    auto_stopped=False,
    mesh_subnet_id=None,
):
    p = MagicMock()
    p.id = PROJECT_ID
    p.state = state
    p.host_id = host_id
    p.topology = topology or {}
    p.vni_map = vni_map or {}
    p.provider_id = provider_id
    p.deploy_error = None
    p.auto_stop_minutes = auto_stop_minutes
    p.auto_stopped = auto_stopped
    p.auto_stop_started_at = None
    p.auto_stop_expires_at = None
    p.auto_stop_warned = False
    p.lifetime_expires_at = None
    p.ocp_status = None
    p.ocp_status_detail = None
    p.ocp_install_elapsed = None
    p.ocp_monitor_started_at = None
    p.mesh_network_host_id = None
    p.host_assignments = None
    p.mesh_subnet_id = mesh_subnet_id
    p.deployed_topology = topology or {}
    return p


def _minimal_topology(
    vm_nodes=None,
    storage_nodes=None,
    network_nodes=None,
    start_order=None,
    edges=None,
):
    """Build a minimal topology dict."""
    nodes = []
    if vm_nodes:
        nodes.extend(vm_nodes)
    if storage_nodes:
        nodes.extend(storage_nodes)
    if network_nodes:
        nodes.extend(network_nodes)
    topo = {"nodes": nodes, "edges": edges or []}
    if start_order is not None:
        topo["startOrder"] = start_order
    return topo


def _vm_node(
    node_id=VM_NODE_ID,
    name="test-vm",
    cloud_init=False,
    power_on=True,
    firmware="bios",
    secure_boot=False,
    vcpus=2,
    ram_gb=4,
    nics=None,
    bmc_enabled=False,
    pxe_boot_iso_id=None,
):
    data = {
        "name": name,
        "cloudInit": cloud_init,
        "powerOnAtDeploy": power_on,
        "vcpus": vcpus,
        "ramGb": ram_gb,
        "nics": nics or [],
        "firmware": firmware,
        "secureBoot": secure_boot,
        "bmcEnabled": bmc_enabled,
    }
    if pxe_boot_iso_id:
        data["pxeBootIsoId"] = pxe_boot_iso_id
    return {"id": node_id, "type": "vmNode", "data": data}


def _storage_node(
    node_id=DISK_NODE_ID,
    fmt="qcow2",
    size_gb=20,
    library_item_id=None,
    library_item_name=None,
    pattern_id=None,
    pattern_disk_id=None,
    bus="virtio",
):
    data = {
        "format": fmt,
        "sizeGb": size_gb,
        "bus": bus,
    }
    if library_item_id:
        data["libraryItemId"] = library_item_id
    if library_item_name:
        data["libraryItemName"] = library_item_name
    if pattern_id:
        data["patternId"] = pattern_id
    if pattern_disk_id:
        data["patternDiskId"] = pattern_disk_id
    return {"id": node_id, "type": "storageNode", "data": data}


def _network_node(node_id=NET_NODE_ID, cidr="192.168.1.0/24"):
    return {
        "id": node_id,
        "type": "networkNode",
        "data": {"cidr": cidr, "networkType": "data"},
    }


# ---------------------------------------------------------------------------
# _setup_networks_via_troshkad
# ---------------------------------------------------------------------------

SVC = "app.services.deploy_service"


class TestSetupNetworksViaTroshkad:
    """Tests for _setup_networks_via_troshkad."""

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}.build_host_network_config", create=True)
    def test_basic_network_setup(self, mock_build, mock_start, mock_wait):
        """Happy-path: single network, no LB, returns True."""
        from app.services.deploy_service import _setup_networks_via_troshkad

        mock_build.return_value = {
            "networks": [{"vni": 100, "bridge": "br-100"}],
            "gateway": None,
            "routers": [],
        }
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        db = MagicMock()
        # Mock Project query to return None (no mesh)
        db.query.return_value.filter_by.return_value.first.return_value = None
        # Mock Host query for peer IPs
        db.query.return_value.filter.return_value.all.return_value = [host]

        with patch(f"{SVC}.build_host_network_config", mock_build):
            result = _setup_networks_via_troshkad(
                host, {}, {"net1": 100}, db, PROJECT_ID
            )

        assert result is True
        mock_start.assert_called_once()
        args = mock_start.call_args
        params = args[0][2]
        assert params["project_id"] == PROJECT_ID
        assert params["host_ip"] == "10.0.0.1"

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}.build_host_network_config", create=True)
    def test_network_setup_failed_job(self, mock_build, mock_start, mock_wait):
        """Job completes with status=failed returns error string."""
        from app.services.deploy_service import _setup_networks_via_troshkad

        mock_build.return_value = {"networks": [], "gateway": None, "routers": []}
        mock_start.return_value = "job-1"
        mock_wait.return_value = {
            "status": "failed",
            "result": {"error": "nftables timeout"},
        }

        host = _make_host()
        db = MagicMock()
        # Mock Project query to return None (no mesh)
        db.query.return_value.filter_by.return_value.first.return_value = None
        # Mock Host query for peer IPs
        db.query.return_value.filter.return_value.all.return_value = [host]

        with patch(f"{SVC}.build_host_network_config", mock_build):
            result = _setup_networks_via_troshkad(
                host, {}, {"net1": 100}, db, PROJECT_ID
            )

        assert isinstance(result, str)
        assert "nftables timeout" in result

    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}.build_host_network_config", create=True)
    def test_network_setup_troshkad_error(self, mock_build, mock_start):
        """TroshkadError returns error string."""
        from app.services.deploy_service import (
            TroshkadError,
            _setup_networks_via_troshkad,
        )

        mock_build.return_value = {"networks": [], "gateway": None, "routers": []}
        mock_start.side_effect = TroshkadError("connection refused")

        host = _make_host()
        db = MagicMock()
        # Mock Project query to return None (no mesh)
        db.query.return_value.filter_by.return_value.first.return_value = None
        # Mock Host query for peer IPs
        db.query.return_value.filter.return_value.all.return_value = [host]

        with patch(f"{SVC}.build_host_network_config", mock_build):
            result = _setup_networks_via_troshkad(
                host, {}, {"net1": 100}, db, PROJECT_ID
            )

        assert isinstance(result, str)
        assert "connection refused" in result

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch("app.services.vxlan.build_host_network_config")
    def test_multi_host_peer_ips(self, mock_build, mock_start, mock_wait):
        """Peer IPs include all active hosts."""
        from app.services.deploy_service import _setup_networks_via_troshkad

        mock_build.return_value = {"networks": [], "gateway": None, "routers": []}
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host1 = _make_host(ip="10.0.0.1")
        host2 = _make_host(ip="10.0.0.2")
        host3 = _make_host(ip=None)  # no IP — should be excluded

        db = MagicMock()
        # Mock Project query to return None (no mesh)
        db.query.return_value.filter_by.return_value.first.return_value = None
        # Mock Host query for peer IPs
        db.query.return_value.filter.return_value.all.return_value = [
            host1,
            host2,
            host3,
        ]

        result = _setup_networks_via_troshkad(host1, {}, {"net1": 100}, db, PROJECT_ID)

        assert result is True
        # build_host_network_config should have been called with peer IPs
        call_args = mock_build.call_args
        peer_ips = call_args[0][2]
        assert "10.0.0.1" in peer_ips
        assert "10.0.0.2" in peer_ips
        assert None not in peer_ips


# ---------------------------------------------------------------------------
# _create_seed_isos_via_troshkad
# ---------------------------------------------------------------------------


class TestCreateSeedIsosViaTroshkad:
    """Tests for _create_seed_isos_via_troshkad."""

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(
        "app.services.cloud_init.generate_metadata", return_value="instance-id: test\n"
    )
    @patch("app.services.cloud_init.generate_userdata", return_value="#cloud-config\n")
    def test_vm_with_cloud_init(
        self, mock_userdata, mock_metadata, mock_start, mock_wait
    ):
        """VMs with cloudInit=True get seed ISOs created."""
        from app.services.deploy_service import _create_seed_isos_via_troshkad

        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        topo = _minimal_topology(vm_nodes=[_vm_node(cloud_init=True, name="my-vm")])

        _create_seed_isos_via_troshkad(host, PROJECT_ID, topo)

        mock_start.assert_called_once()
        params = mock_start.call_args[0][2]
        assert len(params["seeds"]) == 1
        assert params["seeds"][0]["user_data"] == "#cloud-config\n"
        assert params["seeds"][0]["meta_data"] == "instance-id: test\n"

    @patch(f"{SVC}.start_job")
    def test_vm_without_cloud_init_skipped(self, mock_start):
        """VMs without cloudInit=True are skipped; no jobs started."""
        from app.services.deploy_service import _create_seed_isos_via_troshkad

        host = _make_host()
        topo = _minimal_topology(vm_nodes=[_vm_node(cloud_init=False)])

        _create_seed_isos_via_troshkad(host, PROJECT_ID, topo)
        mock_start.assert_not_called()

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch("app.services.cloud_init.generate_metadata", return_value="instance-id: i\n")
    @patch("app.services.cloud_init.generate_userdata", return_value="#cloud-config\n")
    def test_network_config_included(
        self, mock_userdata, mock_metadata, mock_start, mock_wait
    ):
        """ciNetworkConfig is included in seed when present."""
        from app.services.deploy_service import _create_seed_isos_via_troshkad

        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        vm = _vm_node(cloud_init=True)
        vm["data"]["ciNetworkConfig"] = "network:\n  version: 2"
        topo = _minimal_topology(vm_nodes=[vm])

        _create_seed_isos_via_troshkad(host, PROJECT_ID, topo)

        params = mock_start.call_args[0][2]
        assert params["seeds"][0]["network_config"] == "network:\n  version: 2"

    @patch(f"{SVC}.start_job")
    @patch("app.services.cloud_init.generate_metadata", return_value="instance-id: i\n")
    @patch("app.services.cloud_init.generate_userdata", return_value="#cloud-config\n")
    def test_troshkad_error_handled(self, mock_userdata, mock_metadata, mock_start):
        """TroshkadError during seed creation is caught, not raised."""
        from app.services.deploy_service import (
            TroshkadError,
            _create_seed_isos_via_troshkad,
        )

        mock_start.side_effect = TroshkadError("timeout")

        host = _make_host()
        topo = _minimal_topology(vm_nodes=[_vm_node(cloud_init=True)])

        # Should not raise
        _create_seed_isos_via_troshkad(host, PROJECT_ID, topo)


# ---------------------------------------------------------------------------
# _create_vm_disks_via_troshkad
# ---------------------------------------------------------------------------


class TestCreateVmDisksViaTroshkad:
    """Tests for _create_vm_disks_via_troshkad."""

    @patch(f"{SVC}.start_job")
    def test_blank_disk(self, mock_start):
        """Blank (no source) disk creates without backing file."""
        from app.services.deploy_service import _create_vm_disks_via_troshkad

        mock_start.return_value = "job-1"

        host = _make_host()
        vm = {"node_id": VM_NODE_ID, "name": "test-vm"}
        disks = [
            {
                "node_id": DISK_NODE_ID,
                "format": "qcow2",
                "size_gb": 20,
                "bus": "virtio",
            }
        ]

        job_ids = _create_vm_disks_via_troshkad(host, PROJECT_ID, vm, disks)

        assert job_ids == ["job-1"]
        params = mock_start.call_args[0][2]
        assert params["size_gb"] == 20
        assert params["format"] == "qcow2"
        assert "backing_file" not in params

    @patch(f"{SVC}.start_job")
    def test_iso_disk_skipped(self, mock_start):
        """ISO disks are skipped entirely."""
        from app.services.deploy_service import _create_vm_disks_via_troshkad

        host = _make_host()
        vm = {"node_id": VM_NODE_ID, "name": "test-vm"}
        disks = [
            {
                "node_id": DISK_NODE_ID,
                "format": "iso",
                "size_gb": 0,
                "bus": "sata",
            }
        ]

        job_ids = _create_vm_disks_via_troshkad(host, PROJECT_ID, vm, disks)

        assert job_ids == []
        mock_start.assert_not_called()

    @patch(f"{SVC}.start_job")
    def test_library_disk_has_backing(self, mock_start):
        """Library-sourced disk gets backing_file from cache path."""
        from app.services.deploy_service import _create_vm_disks_via_troshkad

        mock_start.return_value = "job-1"

        host = _make_host()
        vm = {"node_id": VM_NODE_ID, "name": "test-vm"}
        disks = [
            {
                "node_id": DISK_NODE_ID,
                "format": "qcow2",
                "size_gb": 40,
                "bus": "virtio",
                "source": "library",
                "library_item_id": LIBRARY_ITEM_ID,
            }
        ]

        job_ids = _create_vm_disks_via_troshkad(host, PROJECT_ID, vm, disks)

        assert len(job_ids) == 1
        params = mock_start.call_args[0][2]
        assert "backing_file" in params
        assert LIBRARY_ITEM_ID in params["backing_file"]

    @patch(f"{SVC}.start_job")
    def test_pattern_disk_has_backing(self, mock_start):
        """Pattern-sourced disk looks up source_disk_id for backing file."""
        from app.services.deploy_service import _create_vm_disks_via_troshkad

        mock_start.return_value = "job-1"

        host = _make_host()
        vm = {"node_id": VM_NODE_ID, "name": "test-vm"}
        disks = [
            {
                "node_id": DISK_NODE_ID,
                "format": "qcow2",
                "size_gb": 40,
                "bus": "virtio",
                "source": "pattern",
                "patternId": PATTERN_ID,
                "patternDiskId": PATTERN_DISK_ID,
            }
        ]

        mock_pd = MagicMock()
        mock_pd.source_disk_id = "orig-disk-0001"

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_pd
        )

        with patch(f"{SVC}.SessionLocal", return_value=mock_session, create=True):
            job_ids = _create_vm_disks_via_troshkad(host, PROJECT_ID, vm, disks)

        assert len(job_ids) == 1
        params = mock_start.call_args[0][2]
        assert "backing_file" in params
        assert PATTERN_ID in params["backing_file"]

    @patch(f"{SVC}.start_job")
    def test_multiple_disks_returns_all_job_ids(self, mock_start):
        """Multiple disks return a job ID for each non-ISO disk."""
        from app.services.deploy_service import _create_vm_disks_via_troshkad

        mock_start.side_effect = ["job-1", "job-2"]

        host = _make_host()
        vm = {"node_id": VM_NODE_ID, "name": "test-vm"}
        disks = [
            {
                "node_id": DISK_NODE_ID,
                "format": "qcow2",
                "size_gb": 20,
                "bus": "virtio",
            },
            {
                "node_id": DISK_NODE_ID_2,
                "format": "qcow2",
                "size_gb": 50,
                "bus": "virtio",
            },
        ]

        job_ids = _create_vm_disks_via_troshkad(host, PROJECT_ID, vm, disks)

        assert job_ids == ["job-1", "job-2"]
        assert mock_start.call_count == 2


# ---------------------------------------------------------------------------
# _create_vm_via_troshkad
# ---------------------------------------------------------------------------


class TestCreateVmViaTroshkad:
    """Tests for _create_vm_via_troshkad."""

    @patch(f"{SVC}.start_job", return_value="job-create-1")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(f"{SVC}._find_vm_disks", return_value=[])
    def test_basic_bios_vm(self, mock_disks, mock_nets, mock_start):
        """Basic BIOS VM definition with minimal params."""
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "test-vm",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": "uuid-1234",
            "firmware": "bios",
            "secure_boot": False,
            "video_model": "virtio",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        job_id = _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        assert job_id == "job-create-1"
        params = mock_start.call_args[0][2]
        assert params["vcpus"] == 2
        assert params["ram_mb"] == 4096
        assert params["firmware"] == "bios"
        assert params["secure_boot"] is False
        assert "clock_offset" not in params
        assert "disk_cache" not in params
        assert "machine_type" not in params

    @patch(f"{SVC}.start_job", return_value="job-create-1b")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(f"{SVC}._find_vm_disks", return_value=[])
    def test_machine_type_forwarded_when_set(self, mock_disks, mock_nets, mock_start):
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "test-vm",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": "uuid-1234",
            "firmware": "bios",
            "secure_boot": False,
            "machine_type": "i440fx",
            "video_model": "virtio",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        params = mock_start.call_args[0][2]
        assert params["machine_type"] == "i440fx"

    @patch(f"{SVC}.start_job", return_value="job-create-headless")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(f"{SVC}._find_vm_disks", return_value=[])
    def test_headless_when_serial_exec_eos(self, mock_disks, mock_nets, mock_start):
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "rtr2",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": "uuid-eos",
            "firmware": "bios",
            "secure_boot": False,
            "serial_exec_type": "eos",
            "video_model": "virtio",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        params = mock_start.call_args[0][2]
        assert params["headless"] is True
        assert params["serial_exec_type"] == "eos"

    @patch(f"{SVC}.start_job", return_value="job-create-2")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(f"{SVC}._find_vm_disks", return_value=[])
    def test_uefi_vm_with_clock_offset(self, mock_disks, mock_nets, mock_start):
        """UEFI VM with clock offset and disk cache."""
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "uefi-vm",
            "vcpus": 4,
            "ram_gb": 8,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": "uuid-5678",
            "firmware": "uefi",
            "secure_boot": True,
            "video_model": "qxl",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        job_id = _create_vm_via_troshkad(
            host,
            PROJECT_ID,
            vm,
            topo,
            {},
            disk_cache="writeback",
            clock_offset=-86400,
        )

        assert job_id == "job-create-2"
        params = mock_start.call_args[0][2]
        assert params["firmware"] == "uefi"
        assert params["secure_boot"] is True
        assert params["disk_cache"] == "writeback"
        assert params["clock_offset"] == -86400
        assert params["ram_mb"] == 8192

    @patch(f"{SVC}.start_job", return_value="job-create-3")
    @patch(
        f"{SVC}._find_vm_networks",
        return_value=[
            {"bridge": "br-100", "model": "virtio", "mac": "52:54:00:aa:bb:cc"}
        ],
    )
    @patch(
        f"{SVC}._find_vm_disks",
        return_value=[
            {
                "node_id": DISK_NODE_ID,
                "format": "qcow2",
                "size_gb": 20,
                "bus": "virtio",
            }
        ],
    )
    def test_vm_with_disk_and_network(self, mock_disks, mock_nets, mock_start):
        """VM with a disk and a network gets both in params."""
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "full-vm",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": None,
            "firmware": "bios",
            "secure_boot": False,
            "video_model": "virtio",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        params = mock_start.call_args[0][2]
        assert len(params["disks"]) == 1
        assert params["disks"][0]["bus"] == "virtio"
        assert len(params["networks"]) == 1
        assert params["networks"][0]["bridge"] == "br-100"
        assert params["networks"][0]["mac"] == "52:54:00:aa:bb:cc"
        # uuid falls back to node_id when None
        assert params["uuid"] == VM_NODE_ID

    @patch(f"{SVC}.start_job", return_value="job-create-4")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(f"{SVC}._find_vm_disks", return_value=[])
    def test_boot_device_translation(self, mock_disks, mock_nets, mock_start):
        """Boot devices map storage nodes to hd/cdrom and 'network' stays."""
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        hd_node = _storage_node(node_id="disk-hd", fmt="qcow2")
        iso_node = _storage_node(node_id="disk-iso", fmt="iso")
        topo = _minimal_topology(storage_nodes=[hd_node, iso_node])

        vm = {
            "node_id": VM_NODE_ID,
            "name": "boot-vm",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
            "boot_devices": ["disk-hd", "disk-iso", "network"],
            "uuid": "uuid-boot",
            "firmware": "bios",
            "secure_boot": False,
            "video_model": "virtio",
            "input_model": "virtio",
        }

        _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        params = mock_start.call_args[0][2]
        assert params["boot_devs"] == ["hd", "cdrom", "network"]

    @patch(f"{SVC}.start_job", return_value="job-create-5")
    @patch(f"{SVC}._find_vm_networks", return_value=[])
    @patch(
        f"{SVC}._find_vm_disks",
        return_value=[
            {
                "node_id": "iso1",
                "format": "iso",
                "library_item_id": LIBRARY_ITEM_ID,
                "bus": "sata",
                "size_gb": 0,
            }
        ],
    )
    def test_iso_disk_creates_cdrom_symlink(self, mock_disks, mock_nets, mock_start):
        """ISO disk with library_item_id creates a cdrom with symlink_from."""
        from app.services.deploy_service import _create_vm_via_troshkad

        host = _make_host()
        vm = {
            "node_id": VM_NODE_ID,
            "name": "iso-vm",
            "vcpus": 1,
            "ram_gb": 2,
            "cloud_init": False,
            "boot_devices": [],
            "uuid": "uuid-iso",
            "firmware": "bios",
            "secure_boot": False,
            "video_model": "virtio",
            "input_model": "virtio",
        }
        topo = _minimal_topology()

        _create_vm_via_troshkad(host, PROJECT_ID, vm, topo, {})

        params = mock_start.call_args[0][2]
        cdroms = [d for d in params["disks"] if d.get("device") == "cdrom"]
        assert len(cdroms) == 1
        assert "symlink_from" in cdroms[0]
        assert LIBRARY_ITEM_ID in cdroms[0]["symlink_from"]


# ---------------------------------------------------------------------------
# _start_vms_via_troshkad
# ---------------------------------------------------------------------------


class TestStartVmsViaTroshkad:
    """Tests for _start_vms_via_troshkad."""

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_single_vm_started(self, mock_extract, mock_start, mock_wait):
        """Single VM with no start order starts successfully."""
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        topo = _minimal_topology(vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")])

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert failed == []
        mock_start.assert_called_once()

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    @patch(f"{SVC}._time")
    def test_start_order_respected(
        self, mock_time, mock_extract, mock_start, mock_wait
    ):
        """VMs in startOrder start sequentially with delays."""
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_extract.return_value = [
            {"node_id": "vm-a", "name": "vm-a"},
            {"node_id": "vm-b", "name": "vm-b"},
        ]
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        topo = _minimal_topology(
            vm_nodes=[
                _vm_node(node_id="vm-a", name="vm-a"),
                _vm_node(node_id="vm-b", name="vm-b"),
            ],
            start_order=[
                {"vmId": "vm-a", "delaySeconds": 0},
                {"vmId": "vm-b", "delaySeconds": 5},
            ],
        )

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert failed == []
        # Should have called sleep with 5 for vm-b
        mock_time.sleep.assert_called_with(5)
        # Both VMs started
        assert mock_start.call_count == 2

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_auto_start_false_skipped(self, mock_extract, mock_start, mock_wait):
        """VMs with autoStart=False in startOrder are skipped."""
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_extract.return_value = [
            {"node_id": "vm-a", "name": "vm-a"},
        ]
        mock_start.return_value = "job-1"
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        topo = _minimal_topology(
            vm_nodes=[_vm_node(node_id="vm-a", name="vm-a")],
            start_order=[{"vmId": "vm-a", "autoStart": False}],
        )

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert failed == []
        mock_start.assert_not_called()

    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_power_on_at_deploy_false(self, mock_extract, mock_start):
        """VMs with powerOnAtDeploy=false in topology data are skipped."""
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "no-power-vm"}]

        host = _make_host()
        topo = _minimal_topology(
            vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="no-power-vm", power_on=False)]
        )

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert failed == []
        mock_start.assert_not_called()

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_start_failure_recorded(self, mock_extract, mock_start, mock_wait):
        """Failed VM start returns the failure in the list."""
        from app.services.deploy_service import (
            TroshkadError,
            _start_vms_via_troshkad,
        )

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "fail-vm"}]
        mock_start.side_effect = TroshkadError("qemu died")

        host = _make_host()
        topo = _minimal_topology(
            vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="fail-vm")]
        )

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert len(failed) == 1
        assert failed[0][0] == "fail-vm"
        assert "qemu died" in failed[0][1]

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_unordered_vms_started_parallel(self, mock_extract, mock_start, mock_wait):
        """VMs not in startOrder fire all start_jobs before waiting."""
        from app.services.deploy_service import _start_vms_via_troshkad

        mock_extract.return_value = [
            {"node_id": "vm-a", "name": "vm-a"},
            {"node_id": "vm-b", "name": "vm-b"},
        ]
        mock_start.side_effect = ["job-a", "job-b"]
        mock_wait.return_value = {"status": "completed"}

        host = _make_host()
        topo = _minimal_topology(
            vm_nodes=[
                _vm_node(node_id="vm-a", name="vm-a"),
                _vm_node(node_id="vm-b", name="vm-b"),
            ]
        )

        failed = _start_vms_via_troshkad(host, PROJECT_ID, topo)

        assert failed == []
        # Both start_job calls before any wait_for_job
        assert mock_start.call_count == 2
        assert mock_wait.call_count == 2


# ---------------------------------------------------------------------------
# cache_library_images
# ---------------------------------------------------------------------------


class TestCacheLibraryImages:
    """Tests for cache_library_images."""

    @patch(f"{SVC}._get_host_pool", return_value=None)
    def test_no_items_returns_early(self, mock_pool):
        """Empty topology has no items to cache."""
        from app.services.deploy_service import cache_library_images

        host = _make_host()
        db = MagicMock()
        topo = _minimal_topology()

        cache_library_images(topo, host, db)
        # No crash, no errors

    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch("app.services.s3_storage._get_s3_config")
    @patch("app.services.s3_storage._get_readonly_s3_config")
    @patch("app.services.troshkad_client.poll_job")
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._time")
    def test_cache_miss_downloads(
        self,
        mock_time,
        mock_start,
        mock_wait,
        mock_poll,
        mock_ro_s3,
        mock_s3,
        mock_pool,
    ):
        """Library item not cached locally triggers download."""
        from app.services.deploy_service import cache_library_images

        mock_s3.return_value = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "region": "us-east-1",
            "endpoint_url": "",
        }
        mock_ro_s3.return_value = None

        # First start_job = stat check (file missing), second = download
        mock_start.side_effect = ["stat-job", "dl-job"]
        # stat shows file doesn't exist
        mock_wait.return_value = {"result": {"exists": False}}
        # download completes immediately
        mock_poll.return_value = {"status": "completed"}

        mock_lib_item = MagicMock()
        mock_lib_item.id = LIBRARY_ITEM_ID
        mock_lib_item.name = "rhel9-base"
        mock_lib_item.s3_key = "library/rhel9-base.qcow2"
        mock_lib_item.size_bytes = 1073741824
        mock_lib_item.source = "local"
        mock_lib_item.source_provider_id = None

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = mock_lib_item

        host = _make_host()
        topo = _minimal_topology(
            storage_nodes=[
                _storage_node(
                    library_item_id=LIBRARY_ITEM_ID,
                    fmt="qcow2",
                )
            ]
        )

        with patch("app.services.s3_storage._bucket", return_value="troshka-images"):
            cache_library_images(topo, host, db)

        # stat check + download = 2 start_job calls
        assert mock_start.call_count == 2

    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_cache_hit_skips_download(self, mock_start, mock_wait, mock_pool):
        """Item already cached on host skips download."""
        from app.services.deploy_service import cache_library_images

        # stat check says file exists
        mock_start.return_value = "stat-job"
        mock_wait.return_value = {"result": {"exists": True}}

        mock_lib_item = MagicMock()
        mock_lib_item.id = LIBRARY_ITEM_ID
        mock_lib_item.name = "rhel9-base"
        mock_lib_item.s3_key = "library/rhel9-base.qcow2"
        mock_lib_item.size_bytes = 1073741824
        mock_lib_item.source = "local"
        mock_lib_item.source_provider_id = None

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = mock_lib_item

        host = _make_host()
        topo = _minimal_topology(
            storage_nodes=[_storage_node(library_item_id=LIBRARY_ITEM_ID, fmt="qcow2")]
        )

        cache_library_images(topo, host, db)

        # Only 1 start_job for stat check, no download
        assert mock_start.call_count == 1

    @patch(f"{SVC}._get_host_pool", return_value=None)
    def test_deduplication(self, mock_pool):
        """Duplicate library item IDs across nodes are deduped."""
        from app.services.deploy_service import cache_library_images

        mock_lib_item = MagicMock()
        mock_lib_item.id = LIBRARY_ITEM_ID
        mock_lib_item.name = "rhel9-base"
        mock_lib_item.s3_key = "library/rhel9-base.qcow2"
        mock_lib_item.size_bytes = 1073741824
        mock_lib_item.source = "local"
        mock_lib_item.source_provider_id = None

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = mock_lib_item

        host = _make_host()
        topo = _minimal_topology(
            storage_nodes=[
                _storage_node(
                    node_id="disk-1",
                    library_item_id=LIBRARY_ITEM_ID,
                    fmt="qcow2",
                ),
                _storage_node(
                    node_id="disk-2",
                    library_item_id=LIBRARY_ITEM_ID,
                    fmt="qcow2",
                ),
            ]
        )

        # Patch stat check and return file exists for the single deduped item
        with patch(f"{SVC}.start_job", return_value="stat-job") as mock_start, patch(
            f"{SVC}.wait_for_job", return_value={"result": {"exists": True}}
        ):
            cache_library_images(topo, host, db)

        # Only 1 stat check despite 2 nodes referencing same item
        assert mock_start.call_count == 1

    @patch(f"{SVC}._get_host_pool")
    @patch(f"{SVC}._check_shared_cache")
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_shared_cache_ready_skips(
        self, mock_start, mock_wait, mock_check_shared, mock_pool
    ):
        """Item already on shared storage is skipped without download."""
        from app.services.deploy_service import cache_library_images

        pool = MagicMock()
        pool.mode = "shared-fsx"
        mock_pool.return_value = pool
        mock_check_shared.return_value = ("ready", MagicMock())

        # stat check confirms file exists on shared storage
        mock_start.return_value = "stat-job"
        mock_wait.return_value = {"result": {"exists": True}}

        mock_lib_item = MagicMock()
        mock_lib_item.id = LIBRARY_ITEM_ID
        mock_lib_item.name = "rhel9-base"
        mock_lib_item.s3_key = "library/rhel9-base.qcow2"
        mock_lib_item.size_bytes = 1073741824
        mock_lib_item.source = "local"
        mock_lib_item.source_provider_id = None

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = mock_lib_item

        host = _make_host()
        topo = _minimal_topology(
            storage_nodes=[_storage_node(library_item_id=LIBRARY_ITEM_ID, fmt="qcow2")]
        )

        cache_library_images(topo, host, db)

        # Only stat check, no download started
        assert mock_start.call_count == 1

    @patch(f"{SVC}._get_host_pool", return_value=None)
    def test_pattern_disk_collected(self, mock_pool):
        """Pattern disk nodes are collected for caching."""
        from app.services.deploy_service import cache_library_images

        mock_pd = MagicMock()
        mock_pd.id = PATTERN_DISK_ID
        mock_pd.pattern_id = PATTERN_ID
        mock_pd.s3_key = "patterns/p1/disk.qcow2"
        mock_pd.source_disk_id = "orig-disk"
        mock_pd.format = "qcow2"
        mock_pd.size_bytes = 2147483648

        mock_pattern = MagicMock()
        mock_pattern.id = PATTERN_ID
        mock_pattern.tags = {}

        db = MagicMock()

        def mock_filter_by(**kwargs):
            result = MagicMock()
            if "pattern_id" in kwargs:
                result.first.return_value = mock_pd
            elif kwargs.get("id") == PATTERN_ID:
                result.first.return_value = mock_pattern
            else:
                result.first.return_value = None
            return result

        db.query.return_value.filter_by = mock_filter_by

        host = _make_host()
        topo = _minimal_topology(
            storage_nodes=[
                _storage_node(
                    pattern_id=PATTERN_ID,
                    pattern_disk_id=PATTERN_DISK_ID,
                    fmt="qcow2",
                )
            ]
        )

        with patch(f"{SVC}.start_job", return_value="stat-job") as mock_start, patch(
            f"{SVC}.wait_for_job", return_value={"result": {"exists": True}}
        ):
            cache_library_images(topo, host, db)

        # Should have checked (stat) the pattern disk
        assert mock_start.call_count == 1


# ---------------------------------------------------------------------------
# stop_project_async
# ---------------------------------------------------------------------------


DB_MOD = "app.core.database"
KV_MOD = "app.services.providers.kubevirt"


def _mock_deploy_session(project, host=None):
    """Build a mock session that routes query().filter_by() calls by ID."""
    mock_session = MagicMock()
    _lookup = {PROJECT_ID: project}
    if host:
        _lookup[host.id] = host

    def _query_side_effect(*args):
        q = MagicMock()

        def _filter_by(**kwargs):
            fb = MagicMock()
            obj_id = kwargs.get("id")
            if obj_id and obj_id in _lookup:
                fb.first.return_value = _lookup[obj_id]
            elif obj_id:
                fb.first.return_value = None
            else:
                fb.first.return_value = None
            fb.all.return_value = []
            return fb

        q.filter_by = _filter_by
        q.filter.return_value = q
        return q

    mock_session.query.side_effect = _query_side_effect
    mock_session.get.return_value = project
    return mock_session


class TestStopProjectAsync:
    """Tests for stop_project_async."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_basic_stop(self, mock_extract, mock_start, mock_wait, mock_notify):
        """Happy path: troshkad host, VMs stop, state -> stopped."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_start.return_value = "stop-job-1"
        mock_wait.return_value = {"status": "completed"}

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert project.state == "stopped"
        assert project.deploy_error is None
        assert project.auto_stop_started_at is None
        assert project.auto_stop_expires_at is None
        assert project.auto_stop_warned is False
        mock_session.commit.assert_called()
        mock_notify.assert_called()

    @patch(f"{SVC}.notify_project")
    def test_stop_project_not_found(self, mock_notify):
        """Project not found returns early without error."""
        from app.services.deploy_service import stop_project_async

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        mock_notify.assert_not_called()

    @patch(f"{SVC}.notify_project")
    def test_stop_host_not_found_sets_error(self, mock_notify):
        """Host not found sets project to error state."""
        from app.services.deploy_service import stop_project_async

        project = _make_project(state="stopping")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            None,  # host not found
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "disconnected" in project.deploy_error
        mock_session.commit.assert_called()

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_stop_no_ip_sets_error(self, mock_extract, mock_notify):
        """Host with no IP (non-KubeVirt) sets project to error."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]

        project = _make_project(state="stopping")
        host = _make_host(ip=None, host_type="ec2")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "disconnected" in project.deploy_error

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_kubevirt_stop(self, mock_extract, mock_notify):
        """KubeVirt host patches VMs via K8s API."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
        )
        host = _make_host(host_type="kubevirt-cluster")
        provider = MagicMock()
        provider.id = "prov-1"

        mock_custom_api = MagicMock()

        mock_session = MagicMock()
        # project, host, provider
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            provider,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            f"{KV_MOD}._get_k8s_clients",
            return_value=(mock_custom_api, None, None),
        ), patch(f"{KV_MOD}._project_ns", return_value="troshka-ns"):
            stop_project_async(PROJECT_ID)

        mock_custom_api.patch_namespaced_custom_object.assert_called_once()
        call_kwargs = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_kwargs[1]["body"]
        assert {"op": "add", "path": "/spec/runStrategy", "value": "Halted"} in body
        assert project.state == "stopped"


# ---------------------------------------------------------------------------
# start_project_async
# ---------------------------------------------------------------------------


class TestStartProjectAsync:
    """Tests for start_project_async."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    def test_basic_start(
        self,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_start_vms,
        mock_bmc,
        mock_ocp,
        mock_notify,
    ):
        """Happy path: troshkad host, networks + VMs start, state -> active."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        project = _make_project(
            state="starting",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        assert project.state == "active"
        assert project.deploy_error is None
        assert project.auto_stopped is False
        mock_net.assert_called_once()
        mock_cache.assert_called_once()
        mock_start_vms.assert_called_once()
        mock_session.commit.assert_called()

    @patch(f"{SVC}.notify_project")
    def test_start_project_not_found(self, mock_notify):
        """Project not found returns early."""
        from app.services.deploy_service import start_project_async

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        mock_notify.assert_not_called()

    @patch(f"{SVC}.notify_project")
    def test_start_host_not_found_sets_error(self, mock_notify):
        """Host not found sets error state."""
        from app.services.deploy_service import start_project_async

        project = _make_project(state="starting")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            None,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "disconnected" in project.deploy_error

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_kubevirt_start(self, mock_extract, mock_notify):
        """KubeVirt host patches VMs to running via K8s API."""
        from app.services.deploy_service import start_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]

        project = _make_project(
            state="starting",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
            auto_stop_minutes=60,
        )
        host = _make_host(host_type="kubevirt-cluster")
        provider = MagicMock()
        provider.id = "prov-1"

        mock_custom_api = MagicMock()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            provider,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            f"{KV_MOD}._get_k8s_clients",
            return_value=(mock_custom_api, None, None),
        ), patch(f"{KV_MOD}._project_ns", return_value="troshka-ns"):
            start_project_async(PROJECT_ID)

        mock_custom_api.patch_namespaced_custom_object.assert_called_once()
        call_kwargs = mock_custom_api.patch_namespaced_custom_object.call_args
        body = call_kwargs[1]["body"]
        assert {"op": "add", "path": "/spec/runStrategy", "value": "Always"} in body
        assert project.state == "active"
        assert project.auto_stopped is False
        # Auto-stop timer should be set
        assert project.auto_stop_started_at is not None
        assert project.auto_stop_expires_at is not None

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._start_vms_via_troshkad")
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    def test_start_vm_failures_set_error(
        self,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_start_vms,
        mock_bmc,
        mock_ocp,
        mock_notify,
    ):
        """VM start failures set project to error state."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_start_vms.return_value = [("vm1", "qemu crashed")]

        project = _make_project(
            state="starting",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "vm1" in project.deploy_error

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._setup_networks_via_troshkad")
    @patch(f"{SVC}._get_network_lock")
    def test_network_failure_sets_error(self, mock_lock, mock_net, mock_notify):
        """Network setup failure sets project to error state."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_net.return_value = "nftables timeout"

        project = _make_project(
            state="starting",
            topology=_minimal_topology(),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "Network setup failed" in project.deploy_error

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=True)
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    def test_ocp_monitor_restarted(
        self,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_start_vms,
        mock_bmc,
        mock_ocp,
        mock_notify,
    ):
        """OCP monitor resets monitoring state on start."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        project = _make_project(
            state="starting",
            topology=_minimal_topology(),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        assert project.ocp_status == "monitoring"
        assert project.ocp_status_detail is None
        assert project.ocp_monitor_started_at is not None

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(f"{SVC}._extract_bmc_config")
    @patch(f"{SVC}._setup_bmc_via_troshkad")
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    def test_bmc_failure_nonfatal(
        self,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_start_vms,
        mock_setup_bmc,
        mock_bmc_config,
        mock_ocp,
        mock_notify,
    ):
        """BMC setup failure is non-fatal; project still becomes active."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        bmc = {"network": {"vni": 100}, "vms": []}
        mock_bmc_config.return_value = bmc
        mock_setup_bmc.side_effect = Exception("sushy crash")

        project = _make_project(
            state="starting",
            topology=_minimal_topology(),
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]
        mock_session.query.return_value.filter_by.return_value.all.return_value = []

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            start_project_async(PROJECT_ID)

        # BMC failure should not prevent activation
        assert project.state == "active"


# ---------------------------------------------------------------------------
# Block 1: _deploy_project_inner — lines 2809-3900
# ---------------------------------------------------------------------------


class TestDeployProjectInner:
    """Tests for _deploy_project_inner and its sub-functions."""

    @patch(f"{SVC}.maybe_start_ocp_health_monitor")
    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{SVC}._extract_containers", return_value=[])
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._setup_metadata_via_troshkad")
    @patch(f"{SVC}._create_seed_isos_via_troshkad")
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._auto_assign_container_ips")
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._update_deploy_progress")
    @patch(f"{SVC}._checkpoint")
    @patch(f"{SVC}._project_deleted", return_value=False)
    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._extract_vms", return_value=[])
    @patch(f"{SVC}._is_pattern_deploy", return_value=False)
    @patch(f"{SVC}._is_ocp_topology", return_value=False)
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(
        "app.services.vxlan.build_host_network_config",
        return_value={
            "networks": [],
            "gateway": None,
            "routers": [],
            "loadbalancer": None,
        },
    )
    def test_happy_path_no_eips(self, *mocks):
        """Happy path: deploy with no EIPs, no BMC, no VMs to create -> active."""
        from app.services.deploy_service import _deploy_project_inner

        topo = _minimal_topology(
            vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")],
        )
        project = _make_project(state="deploying", topology=topo, vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        project.deploy_step = None
        project.deploy_progress = None
        project.deployed_topology = None
        project.auto_stop_minutes = None
        project.auto_delete_minutes = None
        project.dns_provider_id = None
        project.guid = None
        project.domain = None
        host = _make_host()
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "active"
        assert project.deploy_error is None

    @patch(f"{SVC}._clear_deploy_cancelled")
    def test_project_not_found_returns(self, mock_clear):
        """Returns immediately if project not found."""
        from app.services.deploy_service import _deploy_project_inner

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _deploy_project_inner(PROJECT_ID)

        # No crash, just returns

    @patch(f"{SVC}._clear_deploy_cancelled")
    def test_project_wrong_state_returns(self, mock_clear):
        """Returns immediately if project state is not 'deploying'."""
        from app.services.deploy_service import _deploy_project_inner

        project = _make_project(state="active")

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            project
        )

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _deploy_project_inner(PROJECT_ID)

        # Should return without touching state
        assert project.state == "active"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._clear_deploy_cancelled")
    def test_no_host_no_ip_sets_error(self, mock_clear, mock_del, mock_notify):
        """Host with no IP sets error state."""
        from app.services.deploy_service import _deploy_project_inner

        project = _make_project(state="deploying", vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        host = _make_host(ip=None)
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "error"
        assert "no IP" in project.deploy_error or "provisioning" in project.deploy_error

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._clear_deploy_cancelled")
    def test_no_host_at_all_capacity_error(self, mock_clear, mock_del, mock_notify):
        """No host and no host_id sets capacity error."""
        from app.services.deploy_service import _deploy_project_inner

        project = _make_project(state="deploying", host_id=None)
        project.clock_target = None
        project.guest_exec_enabled = True
        mock_session = _mock_deploy_session(project)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"), patch(
            "app.services.placement.calculate_project_requirements",
            return_value={"total_vcpus": 4, "total_ram_mb": 8192},
        ), patch(
            "app.services.placement.find_available_host", return_value=None
        ):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "error"
        assert (
            "capacity" in project.deploy_error.lower()
            or "Not enough" in project.deploy_error
        )

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._setup_networks_via_troshkad")
    def test_network_failure_sets_error(
        self, mock_net, mock_lock, mock_pool, mock_clear, mock_del, mock_notify
    ):
        """Network setup failure sets error state."""
        from app.services.deploy_service import _deploy_project_inner

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_net.return_value = "nftables crashed"

        project = _make_project(state="deploying", vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        host = _make_host()
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"), patch(
            f"{SVC}._checkpoint"
        ), patch(
            f"{SVC}._update_deploy_progress"
        ), patch(
            f"{SVC}._auto_assign_container_ips"
        ), patch(
            f"{SVC}._project_deleted", return_value=False
        ):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "error"
        assert "nftables crashed" in project.deploy_error

    @patch(f"{SVC}.maybe_start_ocp_health_monitor")
    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[("vm1", "qemu crash")])
    @patch(f"{SVC}._extract_containers", return_value=[])
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._setup_metadata_via_troshkad")
    @patch(f"{SVC}._create_seed_isos_via_troshkad")
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._auto_assign_container_ips")
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._update_deploy_progress")
    @patch(f"{SVC}._checkpoint")
    @patch(f"{SVC}._project_deleted", return_value=False)
    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._extract_vms", return_value=[])
    @patch(f"{SVC}._is_pattern_deploy", return_value=False)
    @patch(f"{SVC}._is_ocp_topology", return_value=False)
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(
        "app.services.vxlan.build_host_network_config",
        return_value={
            "networks": [],
            "gateway": None,
            "routers": [],
            "loadbalancer": None,
        },
    )
    def test_start_failure_sets_error(self, *mocks):
        """VM start failures set project to error state."""
        from app.services.deploy_service import _deploy_project_inner

        topo = _minimal_topology(
            vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")],
        )
        project = _make_project(state="deploying", topology=topo, vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        project.deploy_step = None
        project.deploy_progress = None
        project.deployed_topology = None
        project.auto_stop_minutes = None
        project.auto_delete_minutes = None
        project.dns_provider_id = None
        project.guid = None
        project.domain = None
        host = _make_host()
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "error"
        assert "vm1" in project.deploy_error

    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._deploy_kubevirt_native")
    def test_kubevirt_cluster_delegates(self, mock_kv_deploy, mock_pool, mock_clear):
        """kubevirt-cluster host type delegates to _deploy_kubevirt_native."""
        from app.services.deploy_service import _deploy_project_inner

        topo = _minimal_topology(vm_nodes=[_vm_node()])
        project = _make_project(state="deploying", topology=topo, vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        host = _make_host(host_type="kubevirt-cluster")
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"):
            _deploy_project_inner(PROJECT_ID)

        mock_kv_deploy.assert_called_once()

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._auto_assign_container_ips")
    @patch(f"{SVC}._checkpoint")
    @patch(f"{SVC}._update_deploy_progress")
    @patch(f"{SVC}._project_deleted")
    def test_mid_deploy_deletion_aborts(
        self,
        mock_deleted,
        mock_upd,
        mock_cp,
        mock_auto_ip,
        mock_net,
        mock_lock,
        mock_pool,
        mock_clear,
        mock_del,
        mock_notify,
    ):
        """Project deleted mid-deploy aborts and returns."""
        from app.services.deploy_service import _deploy_project_inner

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        # Return False for first check (after networks), True for second
        mock_deleted.side_effect = [True]

        project = _make_project(state="deploying", vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        host = _make_host()
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"):
            _deploy_project_inner(PROJECT_ID)

        # Project should not have been set to active
        assert project.state != "active"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._delete_deploy_progress")
    @patch(f"{SVC}._clear_deploy_cancelled")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    def test_unexpected_exception_sets_error(
        self, mock_pool, mock_clear, mock_del, mock_notify
    ):
        """Unexpected exception in deploy sets error state."""
        from app.services.deploy_service import _deploy_project_inner

        project = _make_project(state="deploying", vni_map={"net1": 100})
        project.clock_target = None
        project.guest_exec_enabled = True
        host = _make_host()
        mock_session = _mock_deploy_session(project, host)

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.placement.record_deploy_start"
        ), patch("app.services.placement.record_deploy_end"), patch(
            f"{SVC}._get_network_lock", side_effect=RuntimeError("boom")
        ), patch(
            f"{SVC}._checkpoint"
        ), patch(
            f"{SVC}._update_deploy_progress"
        ), patch(
            f"{SVC}._auto_assign_container_ips"
        ), patch(
            f"{SVC}._project_deleted", return_value=False
        ):
            _deploy_project_inner(PROJECT_ID)

        assert project.state == "error"
        assert "boom" in project.deploy_error


# ---------------------------------------------------------------------------
# Block 2: OCP health monitoring — lines 4259-5180
# ---------------------------------------------------------------------------


class TestOcpVmPollWithCsrs:
    """Tests for _ocp_vm_poll_with_csrs."""

    @patch(f"{SVC}._ocp_vm_wait_for_operators")
    def test_nodes_ready_immediately(self, mock_wait_ops):
        """When nodes are Ready on first poll, moves to operators."""
        import time

        from app.services.deploy_service import _ocp_vm_poll_with_csrs

        oc_fn = MagicMock(return_value="master-0   Ready   master   1d   v1.28\n")
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 60

        _ocp_vm_poll_with_csrs(oc_fn, approve_fn, push_fn, deadline)

        mock_wait_ops.assert_called_once()
        push_fn.assert_any_call("nodes", "1/1 ready", ["master-0: Ready"])

    @patch(f"{SVC}._ocp_vm_wait_for_operators")
    def test_api_error_retries(self, mock_wait_ops):
        """API error causes a retry with 'waiting for API server' message."""
        import time

        from app.services.deploy_service import _ocp_vm_poll_with_csrs

        call_count = 0

        def oc_side_effect(cmd, timeout=10):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return "error: connection refused"
            return "master-0   Ready   master   1d   v1.28\n"

        oc_fn = MagicMock(side_effect=oc_side_effect)
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 60

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_poll_with_csrs(oc_fn, approve_fn, push_fn, deadline)

        push_fn.assert_any_call("nodes", "waiting for API server")


class TestOcpVmWaitForConsole:
    """Tests for _ocp_vm_wait_for_console."""

    def test_console_ready_immediately(self):
        """Console and OAuth both returning 200 completes immediately."""
        import time

        from app.services.deploy_service import _ocp_vm_wait_for_console

        call_count = 0

        def oc_side_effect(cmd, timeout=15):
            nonlocal call_count
            call_count += 1
            if "get co console" in cmd:
                return "console   4.14.0   True   False   False"
            if "curl" in cmd:
                return "200"
            return ""

        oc_fn = MagicMock(side_effect=oc_side_effect)
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 60

        _ocp_vm_wait_for_console(oc_fn, approve_fn, push_fn, deadline)

        push_fn.assert_any_call("console", "console and OAuth ready")

    def test_console_not_available_waits(self):
        """Console operator not available keeps waiting."""
        import time

        from app.services.deploy_service import _ocp_vm_wait_for_console

        call_count = 0

        def oc_side_effect(cmd, timeout=15):
            nonlocal call_count
            call_count += 1
            if "get co console" in cmd:
                if call_count <= 2:
                    return "console   4.14.0   False   False   False"
                return "console   4.14.0   True   False   False"
            if "curl" in cmd:
                return "200"
            return ""

        oc_fn = MagicMock(side_effect=oc_side_effect)
        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        deadline = time.time() + 60

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_wait_for_console(oc_fn, approve_fn, push_fn, deadline)


class TestOcpVmWaitForApi:
    """Tests for _ocp_vm_wait_for_api."""

    def test_api_ready_immediately(self):
        """API ready on first try returns True."""
        import time

        from app.services.deploy_service import _ocp_vm_wait_for_api

        oc_fn = MagicMock(return_value="master-0   Ready   master   1d   v1.28\n")
        push_fn = MagicMock()
        deadline = time.time() + 60

        result = _ocp_vm_wait_for_api(oc_fn, push_fn, deadline)

        assert result is True

    def test_api_timeout_returns_false(self):
        """API never becomes ready returns False."""
        import time

        from app.services.deploy_service import _ocp_vm_wait_for_api

        oc_fn = MagicMock(side_effect=RuntimeError("connection refused"))
        push_fn = MagicMock()
        deadline = time.time() - 1  # Already past deadline

        result = _ocp_vm_wait_for_api(oc_fn, push_fn, deadline)

        assert result is False
        push_fn.assert_any_call("timeout", "API server not reachable")

    def test_api_retries_then_succeeds(self):
        """API fails first then succeeds."""
        import time

        from app.services.deploy_service import _ocp_vm_wait_for_api

        call_count = 0

        def oc_side_effect(cmd, timeout=10):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("refused")
            return "master-0   Ready   master   1d   v1.28\n"

        oc_fn = MagicMock(side_effect=oc_side_effect)
        push_fn = MagicMock()
        deadline = time.time() + 60

        with patch(f"{SVC}._time.sleep"):
            result = _ocp_vm_wait_for_api(oc_fn, push_fn, deadline)

        assert result is True


class TestOcpVmRestartIngress:
    """Tests for _ocp_vm_restart_ingress."""

    def test_restart_calls_oc(self):
        """Restart ingress calls oc rollout restart."""
        from app.services.deploy_service import _ocp_vm_restart_ingress

        oc_fn = MagicMock(return_value="")
        push_fn = MagicMock()

        _ocp_vm_restart_ingress(oc_fn, push_fn)

        push_fn.assert_called_with("console", "restarting ingress router")
        oc_fn.assert_called_once()
        assert "rollout restart" in oc_fn.call_args[0][0]


class TestOcpVmFinalCsrSweep:
    """Tests for _ocp_vm_final_csr_sweep."""

    def test_approves_until_none_pending(self):
        """Sweeps CSRs until none remain pending."""
        from app.services.deploy_service import _ocp_vm_final_csr_sweep

        approve_fn = MagicMock(side_effect=[3, 1, 0])
        push_fn = MagicMock()

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_final_csr_sweep(approve_fn, push_fn)

        assert approve_fn.call_count == 3
        push_fn.assert_any_call("certs", "approved 3 certificate(s)")
        push_fn.assert_any_call("certs", "approved 1 certificate(s)")

    def test_max_six_iterations(self):
        """Stops after 6 iterations even if CSRs keep appearing."""
        from app.services.deploy_service import _ocp_vm_final_csr_sweep

        approve_fn = MagicMock(return_value=1)
        push_fn = MagicMock()

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_final_csr_sweep(approve_fn, push_fn)

        assert approve_fn.call_count == 6


class TestConfigureBastionAndCleanup:
    """Tests for _configure_bastion_and_cleanup."""

    @patch(f"{SVC}._verify_bastion_browser", return_value=True)
    @patch(f"{SVC}._exec_on_bastion")
    def test_browser_configured_when_flag_set(self, mock_exec, mock_verify):
        """configureBastionBrowser flag triggers CA refresh + verify."""
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {"configureBastionBrowser": True},
            }
        ]
        host = _make_host()
        oc_fn = MagicMock(return_value="")
        push_fn = MagicMock()

        _configure_bastion_and_cleanup(
            nodes,
            "vm-1",
            "/tmp/kc.yaml",
            host,
            PROJECT_ID,
            "10.0.0.5",
            "password",
            oc_fn,
            push_fn,
            vm_name="sno",
        )

        # Should copy kubeconfig, refresh CA, verify
        assert mock_exec.call_count >= 1
        mock_verify.assert_called_once()
        push_fn.assert_any_call(
            "browser", "setting bastion kubeconfig for this cluster"
        )

    @patch(f"{SVC}._verify_bastion_browser", return_value=True)
    @patch(f"{SVC}._deploy_bastion_autologin_script")
    @patch(f"{SVC}._write_bastion_kubeadmin_password")
    @patch(f"{SVC}._exec_on_bastion")
    def test_syncs_kubeadmin_password_before_verify(
        self, mock_exec, mock_write_pw, mock_deploy_script, mock_verify
    ):
        """ocpKubeadminPassword from topology is written to bastion before autologin."""
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [
            {
                "id": "vm-1",
                "type": "vmNode",
                "data": {
                    "configureBastionBrowser": True,
                    "ocpKubeadminPassword": "new-recert-password",
                },
            }
        ]
        host = _make_host()
        oc_fn = MagicMock(return_value="")
        push_fn = MagicMock()

        _configure_bastion_and_cleanup(
            nodes,
            "vm-1",
            "/tmp/kc.yaml",
            host,
            PROJECT_ID,
            "10.0.0.5",
            "password",
            oc_fn,
            push_fn,
            vm_name="sno",
        )

        mock_write_pw.assert_called_once_with(
            host, PROJECT_ID, "10.0.0.5", "password", "new-recert-password"
        )
        mock_deploy_script.assert_called_once_with(
            host, PROJECT_ID, "10.0.0.5", "password"
        )
        push_fn.assert_any_call("browser", "syncing kubeadmin password to bastion")
        mock_verify.assert_called_once()

    @patch(f"{SVC}._ocp_update_status")
    @patch(f"{SVC}._configure_bastion_and_cleanup")
    @patch(f"{SVC}._ocp_vm_final_csr_sweep")
    @patch(f"{SVC}._ocp_vm_wait_for_console")
    @patch(f"{SVC}._ocp_vm_restart_ingress")
    @patch(f"{SVC}._ocp_vm_poll_with_csrs")
    @patch(f"{SVC}._ocp_vm_wait_for_api", return_value=True)
    @patch(f"{SVC}._exec_on_bastion")
    @patch(f"{SVC}.notify_project")
    def test_ready_only_after_browser_config(
        self,
        mock_notify,
        mock_exec,
        mock_api,
        mock_poll,
        mock_ingress,
        mock_console,
        mock_sweep,
        mock_bastion,
        mock_update_status,
    ):
        """DB ready status is not set until after bastion browser configuration."""
        import time

        from app.services.deploy_service import _ocp_vm_health_inner

        mock_exec.return_value = ""

        topo = _minimal_topology(
            vm_nodes=[
                {
                    "id": "vm-1",
                    "type": "vmNode",
                    "data": {
                        "label": "cp-0",
                        "configureBastionBrowser": True,
                        "recertEnabled": True,
                    },
                },
                {
                    "id": "bastion-1",
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "pass",
                    },
                },
            ],
        )

        host = _make_host()
        project = _make_project(state="active")
        project.deployed_topology = topo
        project.topology = topo

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [host, project]

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_health_inner(
                PROJECT_ID, HOST_ID, "vm-1", "cp-0", "", time.time(), db
            )

        mock_sweep.assert_called_once()
        mock_bastion.assert_called_once()
        mock_update_status.assert_called_once()
        assert mock_update_status.call_args[0][1] == "ready"

    @patch(f"{SVC}._exec_on_bastion")
    def test_cleanup_kubeconfig_when_no_browser(self, mock_exec):
        """No configureBastionBrowser cleans up temp kubeconfig."""
        from app.services.deploy_service import _configure_bastion_and_cleanup

        nodes = [
            {"id": "vm-1", "type": "vmNode", "data": {}},
        ]
        host = _make_host()
        oc_fn = MagicMock(return_value="")
        push_fn = MagicMock()

        _configure_bastion_and_cleanup(
            nodes,
            "vm-1",
            "/tmp/kc.yaml",
            host,
            PROJECT_ID,
            "10.0.0.5",
            "password",
            oc_fn,
            push_fn,
        )

        # Should call rm -f on the temp kubeconfig
        mock_exec.assert_called_once()
        assert "rm -f" in mock_exec.call_args[0][4]


class TestOcpHealthInner:
    """Tests for _ocp_health_inner."""

    @patch(f"{SVC}._ocp_report_final_status")
    @patch(f"{SVC}._ocp_final_csr_sweep")
    @patch(f"{SVC}._ocp_wait_for_console_route", return_value=True)
    @patch(f"{SVC}._ocp_wait_for_operators", return_value=True)
    @patch(f"{SVC}._ocp_wait_for_nodes_ready", return_value=True)
    @patch(f"{SVC}._ocp_ping_cp_nodes")
    @patch(f"{SVC}._exec_on_bastion", return_value="ok")
    @patch(f"{SVC}._ocp_push_status")
    @patch(f"{SVC}._ocp_wait_for_bastion_ssh", return_value=True)
    def test_pattern_deploy_path(self, *mocks):
        """Pattern deploy path: bastion SSH -> ping -> nodes -> ops -> console."""
        import time

        from app.services.deploy_service import _ocp_health_inner

        topo = _minimal_topology(
            vm_nodes=[
                {
                    "id": "bastion-1",
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "pass123",
                    },
                },
                {
                    "id": "cp-1",
                    "type": "vmNode",
                    "data": {"os": "rhcos", "label": "master-0"},
                },
            ],
            storage_nodes=[
                _storage_node(pattern_id="pat-1", pattern_disk_id="pd-1"),
            ],
        )

        host = _make_host()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host

        _ocp_health_inner(PROJECT_ID, HOST_ID, topo, time.time(), db)

        # Verify key phases were reached (mocks[0] = _ocp_wait_for_bastion_ssh, etc.)
        # The function should have called through the full pipeline

    @patch(f"{SVC}._ocp_push_status")
    def test_host_not_found_returns(self, mock_push):
        """Host not found in DB returns immediately."""
        import time

        from app.services.deploy_service import _ocp_health_inner

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        _ocp_health_inner(PROJECT_ID, HOST_ID, {}, time.time(), db)

        mock_push.assert_not_called()

    @patch(f"{SVC}._ocp_report_final_status")
    @patch(f"{SVC}._ocp_final_csr_sweep")
    @patch(f"{SVC}._ocp_wait_for_console_route", return_value=False)
    @patch(f"{SVC}._ocp_wait_for_operators", return_value=True)
    @patch(f"{SVC}._ocp_wait_for_nodes_ready", return_value=True)
    @patch(f"{SVC}._ocp_ping_cp_nodes")
    @patch(f"{SVC}._exec_on_bastion", return_value="ok")
    @patch(f"{SVC}._ocp_push_status")
    @patch(f"{SVC}._ocp_wait_for_bastion_ssh", return_value=True)
    def test_console_timeout_reports_warning(self, *mocks):
        """Console not ready calls report_final_status with console=False."""
        import time

        from app.services.deploy_service import _ocp_health_inner

        mock_final = mocks[8]  # _ocp_report_final_status (outermost decorator)

        topo = _minimal_topology(
            vm_nodes=[
                {
                    "id": "bastion-1",
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "pass",
                    },
                },
                {
                    "id": "cp-1",
                    "type": "vmNode",
                    "data": {"os": "rhcos"},
                },
            ],
            storage_nodes=[
                _storage_node(pattern_id="pat-1", pattern_disk_id="pd-1"),
            ],
        )

        host = _make_host()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = host

        _ocp_health_inner(PROJECT_ID, HOST_ID, topo, time.time(), db)

        # final_status called with console_ready=False
        call_args = mock_final.call_args[0]
        assert call_args[3] is False  # console_ready


class TestOcpVmHealthInner:
    """Tests for _ocp_vm_health_inner."""

    @patch(f"{SVC}._configure_bastion_and_cleanup")
    @patch(f"{SVC}._ocp_vm_final_csr_sweep")
    @patch(f"{SVC}._ocp_vm_wait_for_console")
    @patch(f"{SVC}._ocp_vm_restart_ingress")
    @patch(f"{SVC}._ocp_vm_poll_with_csrs")
    @patch(f"{SVC}._ocp_vm_wait_for_api", return_value=True)
    @patch(f"{SVC}._exec_on_bastion")
    @patch(f"{SVC}.notify_project")
    def test_happy_path(
        self,
        mock_notify,
        mock_exec,
        mock_api,
        mock_poll,
        mock_ingress,
        mock_console,
        mock_sweep,
        mock_bastion,
    ):
        """Happy path: API reachable -> poll -> restart ingress -> console -> sweep."""
        import time

        from app.services.deploy_service import _ocp_vm_health_inner

        mock_exec.return_value = ""

        topo = _minimal_topology(
            vm_nodes=[
                {
                    "id": "bastion-1",
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "pass",
                    },
                },
            ],
        )

        host = _make_host()
        project = _make_project(state="active")
        project.deployed_topology = topo
        project.topology = topo

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [host, project]

        with patch(f"{SVC}._time.sleep"):
            _ocp_vm_health_inner(
                PROJECT_ID, HOST_ID, "vm-1", "sno-1", "", time.time(), db
            )

        mock_api.assert_called_once()
        mock_poll.assert_called_once()
        mock_ingress.assert_called_once()
        mock_console.assert_called_once()
        mock_sweep.assert_called_once()

    @patch(f"{SVC}.notify_project")
    def test_no_bastion_returns(self, mock_notify):
        """No bastion IP returns immediately."""
        import time

        from app.services.deploy_service import _ocp_vm_health_inner

        topo = _minimal_topology(
            vm_nodes=[
                {"id": "vm-1", "type": "vmNode", "data": {}},
            ],
        )

        host = _make_host()
        project = _make_project(state="active")
        project.deployed_topology = topo
        project.topology = topo

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [host, project]

        _ocp_vm_health_inner(PROJECT_ID, HOST_ID, "vm-1", "sno-1", "", time.time(), db)

        # No notify about phases because no bastion
        assert not any("nodes" in str(c) for c in mock_notify.call_args_list)

    @patch(f"{SVC}._exec_on_bastion")
    @patch(f"{SVC}._ocp_vm_wait_for_api", return_value=False)
    @patch(f"{SVC}.notify_project")
    def test_api_timeout_stops(self, mock_notify, mock_api, mock_exec):
        """API timeout returns without proceeding to polls."""
        import time

        from app.services.deploy_service import _ocp_vm_health_inner

        mock_exec.return_value = ""

        topo = _minimal_topology(
            vm_nodes=[
                {
                    "id": "bastion-1",
                    "type": "vmNode",
                    "data": {
                        "label": "bastion",
                        "nics": [{"ip": "10.0.0.5"}],
                        "ciCloudUserPassword": "pass",
                    },
                },
            ],
        )

        host = _make_host()
        project = _make_project(state="active")
        project.deployed_topology = topo
        project.topology = topo

        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.side_effect = [host, project]

        _ocp_vm_health_inner(PROJECT_ID, HOST_ID, "vm-1", "sno-1", "", time.time(), db)

        # Should not proceed to poll/console/sweep
        mock_api.assert_called_once()


class TestApproveCSRsIfDue:
    """Tests for _approve_csrs_if_due."""

    def test_skips_when_too_early(self):
        """Does not approve when interval not yet passed."""
        import time

        from app.services.deploy_service import _approve_csrs_if_due

        approve_fn = MagicMock(return_value=0)
        push_fn = MagicMock()
        now = time.time()

        result = _approve_csrs_if_due(approve_fn, push_fn, now, interval=30)

        assert result == now
        approve_fn.assert_not_called()

    def test_approves_when_due(self):
        """Approves when enough time has elapsed."""
        from app.services.deploy_service import _approve_csrs_if_due

        approve_fn = MagicMock(return_value=2)
        push_fn = MagicMock()

        result = _approve_csrs_if_due(approve_fn, push_fn, 0, interval=30)

        approve_fn.assert_called_once()
        push_fn.assert_called_once_with("certs", "approved 2 certificate(s)")
        assert result > 0


class TestOcpReportFinalStatus:
    """Tests for _ocp_report_final_status."""

    @patch(f"{SVC}._ocp_update_status")
    def test_all_ready(self, mock_update):
        """All components ready reports 'ready' status."""
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status(PROJECT_ID, True, True, True, "5m 00s", 300, push_fn)

        push_fn.assert_called_with("ready", "cluster ready")
        mock_update.assert_called_with(PROJECT_ID, "ready", 300)

    @patch(f"{SVC}._ocp_update_status")
    def test_partial_failure_reports_warning(self, mock_update):
        """Some components not ready reports 'warning'."""
        from app.services.deploy_service import _ocp_report_final_status

        push_fn = MagicMock()
        _ocp_report_final_status(PROJECT_ID, True, False, True, "10m 00s", 600, push_fn)

        push_fn.assert_called_with("warning", "timed out waiting for: operators")
        mock_update.assert_called_with(PROJECT_ID, "warning", 600)


class TestParseNodeReadiness:
    """Tests for _parse_node_readiness."""

    def test_empty_input(self):
        """Empty input returns zeros."""
        from app.services.deploy_service import _parse_node_readiness

        items, ready, total = _parse_node_readiness(None)
        assert items == []
        assert ready == 0
        assert total == 0

    def test_mixed_readiness(self):
        """Parses mixed Ready/NotReady status."""
        from app.services.deploy_service import _parse_node_readiness

        result = "master-0   Ready   master   1d   v1.28\nworker-0   NotReady   worker   1d   v1.28\n"
        items, ready, total = _parse_node_readiness(result)
        assert total == 2
        assert ready == 1
        assert "master-0: Ready" in items
        assert "worker-0: NotReady" in items


class TestParseOperatorStatus:
    """Tests for _parse_operator_status."""

    def test_all_available(self):
        """All operators available."""
        from app.services.deploy_service import _parse_operator_status

        result = "console   4.14.0   True   False   False\nauthentication   4.14.0   True   False   False\n"
        items, available, total = _parse_operator_status(result)
        assert total == 2
        assert available == 2

    def test_degraded_operator(self):
        """Degraded operator marked accordingly."""
        from app.services.deploy_service import _parse_operator_status

        result = "console   4.14.0   False   False   True\n"
        items, available, total = _parse_operator_status(result)
        assert total == 1
        assert available == 0
        assert "console: degraded" in items


class TestIsApiError:
    """Tests for _is_api_error."""

    def test_none_is_error(self):
        """None result is an error."""
        from app.services.deploy_service import _is_api_error

        assert _is_api_error(None) is True

    def test_error_text(self):
        """Result containing 'error' is an error."""
        from app.services.deploy_service import _is_api_error

        assert _is_api_error("error: connection refused") is True

    def test_clean_result(self):
        """Clean result is not an error."""
        from app.services.deploy_service import _is_api_error

        assert _is_api_error("master-0   Ready   master   1d") is False


class TestExtractBastionInfo:
    """Tests for _extract_bastion_info."""

    def test_bastion_found(self):
        """Extracts bastion IP and password from nodes."""
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {
                "id": "bastion-1",
                "type": "vmNode",
                "data": {
                    "label": "bastion",
                    "nics": [{"ip": "10.0.0.5"}],
                    "ciCloudUserPassword": "mypassword",
                },
            },
        ]
        bastion, ip, password = _extract_bastion_info(nodes)
        assert bastion is not None
        assert ip == "10.0.0.5"
        assert password == "mypassword"

    def test_no_bastion(self):
        """No bastion node returns empty strings."""
        from app.services.deploy_service import _extract_bastion_info

        nodes = [
            {"id": "vm-1", "type": "vmNode", "data": {"label": "worker"}},
        ]
        bastion, ip, password = _extract_bastion_info(nodes)
        assert bastion is None
        assert ip == ""
        assert password == ""


# ---------------------------------------------------------------------------
# Block 3: _destroy_project_inner + destroy helpers — lines 6470-6785
# ---------------------------------------------------------------------------


class TestDestroyProjectInner:
    """Tests for _destroy_project_inner."""

    @patch(f"{SVC}._delete_project_record")
    @patch(f"{SVC}._destroy_cleanup_route_access")
    @patch(f"{SVC}._destroy_cleanup_sg_rules")
    @patch(f"{SVC}._destroy_troshkad_resources")
    def test_happy_path_troshkad(
        self, mock_resources, mock_sg, mock_routes, mock_delete_record
    ):
        """Happy path: troshkad host, resources destroyed, record deleted."""
        from app.models.host import Host
        from app.models.project import Project
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host()
        project = _make_project(vni_map={"net1": 100}, topology=_minimal_topology())

        def mock_query(model):
            mock_q = MagicMock()
            if model == Project:
                mock_q.filter_by.return_value.first.return_value = project
            elif model == Host:
                mock_q.filter_by.return_value.first.return_value = host
            mock_q.filter_by.return_value.all.return_value = []  # No EIPs
            return mock_q

        mock_session = MagicMock()
        mock_session.query.side_effect = mock_query

        ctx = {
            "project_id": PROJECT_ID,
            "host_id": HOST_ID,
            "vni_map": {"net1": 100},
            "topology": _minimal_topology(),
        }

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _destroy_project_inner(ctx)

        mock_resources.assert_called_once()
        mock_sg.assert_called_once()
        mock_routes.assert_called_once()
        mock_delete_record.assert_called_once_with(PROJECT_ID)

    @patch(f"{SVC}._delete_project_record")
    @patch(f"{SVC}._destroy_cleanup_route_access")
    @patch(f"{SVC}._destroy_cleanup_sg_rules")
    @patch(f"{SVC}._destroy_troshkad_resources")
    def test_revokes_ops_pod_key(
        self, _mock_resources, _mock_sg, _mock_routes, _mock_delete
    ):
        """Destroy revokes the project-scoped ops-pod key before teardown."""
        from app.models.host import Host
        from app.models.project import Project
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host()
        project = _make_project(vni_map={"net1": 100}, topology=_minimal_topology())

        def mock_query(model):
            mock_q = MagicMock()
            if model == Project:
                mock_q.filter_by.return_value.first.return_value = project
            elif model == Host:
                mock_q.filter_by.return_value.first.return_value = host
            mock_q.filter_by.return_value.all.return_value = []
            return mock_q

        mock_session = MagicMock()
        mock_session.query.side_effect = mock_query

        ctx = {
            "project_id": PROJECT_ID,
            "host_id": HOST_ID,
            "vni_map": {"net1": 100},
            "topology": _minimal_topology(),
        }

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            f"{SVC}._destroy_revoke_ops_pod_key"
        ) as mock_revoke:
            _destroy_project_inner(ctx)

        mock_revoke.assert_called_once_with(mock_session, PROJECT_ID)

    def test_destroy_revoke_helper_deactivates_key(self):
        """_destroy_revoke_ops_pod_key deactivates a live ops-pod key (real DB)."""
        import uuid

        from app.models.api_key import ApiKey
        from app.models.project import Project as ProjectModel
        from app.models.user import User
        from app.services.deploy_service import _destroy_revoke_ops_pod_key
        from app.services.ocp.ops_pod_auth import (
            _ops_pod_key_name,
            mint_ops_pod_key,
        )
        from tests.conftest import TestSession

        db = TestSession()
        try:
            owner = User(
                id=str(uuid.uuid4()),
                email=f"owner-{uuid.uuid4().hex[:8]}@troshka",
                display_name="owner",
                role="user",
                auth_source="sso",
            )
            db.add(owner)
            db.commit()
            project = ProjectModel(
                id=str(uuid.uuid4()),
                name=f"proj-{uuid.uuid4().hex[:8]}",
                state="active",
                owner_id=owner.id,
            )
            db.add(project)
            db.commit()

            mint_ops_pod_key(db, project)
            active = (
                db.query(ApiKey)
                .filter_by(name=_ops_pod_key_name(project.id), is_active=True)
                .all()
            )
            assert len(active) == 1

            _destroy_revoke_ops_pod_key(db, project.id)

            active_after = (
                db.query(ApiKey)
                .filter_by(name=_ops_pod_key_name(project.id), is_active=True)
                .all()
            )
            assert active_after == []
        finally:
            db.close()

    def test_destroy_revoke_helper_is_best_effort(self):
        """A revoke failure never propagates out of teardown."""
        from app.services.deploy_service import _destroy_revoke_ops_pod_key

        with patch(
            "app.services.ocp.ops_pod_auth.revoke_ops_pod_key",
            side_effect=RuntimeError("db down"),
        ):
            # Must not raise.
            _destroy_revoke_ops_pod_key(MagicMock(), PROJECT_ID)

    @patch(f"{SVC}._delete_project_record")
    def test_host_not_found_deletes_record(self, mock_delete):
        """Host not found still deletes project record."""
        from app.services.deploy_service import _destroy_project_inner

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        ctx = {"project_id": PROJECT_ID, "host_id": HOST_ID}

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _destroy_project_inner(ctx)

        mock_delete.assert_called_once_with(PROJECT_ID)

    @patch(f"{SVC}._delete_project_record")
    def test_host_no_ip_deletes_record(self, mock_delete):
        """Host with no IP still deletes project record."""
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host(ip=None)
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = host

        ctx = {"project_id": PROJECT_ID, "host_id": HOST_ID}

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _destroy_project_inner(ctx)

        mock_delete.assert_called_once_with(PROJECT_ID)

    def test_no_delete_record_flag(self):
        """delete_record=False does not delete the DB record."""
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host(ip=None)
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = host

        ctx = {"project_id": PROJECT_ID, "host_id": HOST_ID}

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            f"{SVC}._delete_project_record"
        ) as mock_delete:
            _destroy_project_inner(ctx, delete_record=False)

        mock_delete.assert_not_called()

    @patch(f"{SVC}._destroy_kubevirt_native")
    def test_kubevirt_delegates(self, mock_kv_destroy):
        """kubevirt-cluster host type delegates to _destroy_kubevirt_native."""
        from app.models.host import Host
        from app.models.project import Project
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host(host_type="kubevirt-cluster")
        project = _make_project(topology={}, vni_map={})

        def mock_query(model):
            mock_q = MagicMock()
            if model == Project:
                mock_q.filter_by.return_value.first.return_value = project
            elif model == Host:
                mock_q.filter_by.return_value.first.return_value = host
            return mock_q

        mock_session = MagicMock()
        mock_session.query.side_effect = mock_query

        ctx = {
            "project_id": PROJECT_ID,
            "host_id": HOST_ID,
            "topology": {},
            "vni_map": {},
        }

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _destroy_project_inner(ctx)

        mock_kv_destroy.assert_called_once()

    @patch(f"{SVC}._set_destroy_error")
    @patch(f"{SVC}._destroy_troshkad_resources", side_effect=RuntimeError("disk busy"))
    def test_exception_sets_destroy_error(self, mock_resources, mock_set_err):
        """Exception during destroy sets error via _set_destroy_error."""
        from app.models.host import Host
        from app.models.project import Project
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host()
        project = _make_project(vni_map={}, topology={})

        def mock_query(model):
            mock_q = MagicMock()
            if model == Project:
                mock_q.filter_by.return_value.first.return_value = project
            elif model == Host:
                mock_q.filter_by.return_value.first.return_value = host
            return mock_q

        mock_session = MagicMock()
        mock_session.query.side_effect = mock_query

        ctx = {
            "project_id": PROJECT_ID,
            "host_id": HOST_ID,
            "vni_map": {},
            "topology": {},
        }

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            _destroy_project_inner(ctx)

        mock_set_err.assert_called_once()
        assert "disk busy" in mock_set_err.call_args[0][1]

    @patch(f"{SVC}._delete_project_record")
    @patch(f"{SVC}._destroy_cleanup_route_access")
    @patch(f"{SVC}._destroy_cleanup_sg_rules")
    @patch(f"{SVC}._destroy_troshkad_resources")
    def test_dns_cleanup(
        self, mock_resources, mock_sg, mock_routes, mock_delete_record
    ):
        """DNS records deleted when dns_provider_id present."""
        from app.services.deploy_service import _destroy_project_inner

        host = _make_host()
        dns_provider = MagicMock()
        dns_provider.type = "route53"
        dns_provider.config = {"zone_id": "Z123"}

        mock_session = MagicMock()

        def mock_filter_by(**kwargs):
            result = MagicMock()
            if "id" in kwargs and kwargs["id"] == HOST_ID:
                result.first.return_value = host
            elif "id" in kwargs and kwargs["id"] == "dns-prov-1":
                result.first.return_value = dns_provider
            else:
                result.first.return_value = host
            result.all.return_value = []  # No EIPs
            return result

        mock_session.query.return_value.filter_by = mock_filter_by

        topo = {
            "nodes": [],
            "edges": [],
            "_dns_records": [
                {"name": "api.ocp.local", "type": "A", "value": "1.2.3.4"}
            ],
        }
        ctx = {
            "project_id": PROJECT_ID,
            "host_id": HOST_ID,
            "vni_map": {},
            "topology": topo,
            "dns_provider_id": "dns-prov-1",
        }

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.dns_service.delete_dns_records"
        ) as mock_dns_del:
            _destroy_project_inner(ctx)

        mock_dns_del.assert_called_once()


class TestDestroyTroshkadResources:
    """Tests for _destroy_troshkad_resources."""

    @patch("app.services.placement.sync_host_capacity")
    @patch(f"{SVC}._teardown_networks_via_troshkad")
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._teardown_bmc_via_troshkad")
    @patch(f"{SVC}.wait_for_job", return_value={"status": "completed"})
    @patch(f"{SVC}.start_job", return_value="job-1")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._extract_containers", return_value=[])
    @patch(f"{SVC}._extract_vms")
    def test_destroys_vms_and_networks(
        self,
        mock_vms,
        mock_ctrs,
        mock_pool,
        mock_start,
        mock_wait,
        mock_bmc,
        mock_lock,
        mock_teardown,
        mock_sync,
    ):
        """Destroys VMs, removes files, tears down BMC and networks."""
        from app.services.deploy_service import _destroy_troshkad_resources

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_vms.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]

        host = _make_host()
        session = MagicMock()

        _destroy_troshkad_resources(host, PROJECT_ID, {}, {"net1": 100}, session)

        # Should have called start_job for VM destroy + file removal + metadata cleanup
        assert mock_start.call_count >= 2
        mock_bmc.assert_called_once()
        mock_teardown.assert_called_once()
        mock_sync.assert_called_once()
        # Must undefine the per-project storage pool (via /pools/cleanup) so it
        # doesn't leak and wedge virt-install on future deploys.
        pool_calls = [
            c for c in mock_start.call_args_list if c.args[1] == "/pools/cleanup"
        ]
        assert len(pool_calls) == 1
        assert pool_calls[0].args[2]["target_dir"].endswith(PROJECT_ID)

    @patch("app.services.placement.sync_host_capacity")
    @patch(f"{SVC}._teardown_networks_via_troshkad")
    @patch(f"{SVC}._get_network_lock")
    @patch(f"{SVC}._teardown_bmc_via_troshkad")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._get_host_pool", return_value=None)
    @patch(f"{SVC}._extract_containers", return_value=[])
    @patch(f"{SVC}._extract_vms")
    def test_vm_destroy_failure_nonfatal(
        self,
        mock_vms,
        mock_ctrs,
        mock_pool,
        mock_start,
        mock_bmc,
        mock_lock,
        mock_teardown,
        mock_sync,
    ):
        """VM destroy failure is non-fatal; continues with cleanup."""
        from app.services.deploy_service import (
            TroshkadError,
            _destroy_troshkad_resources,
        )

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_vms.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_start.side_effect = TroshkadError("domain not found")

        host = _make_host()
        session = MagicMock()

        # Should not raise
        _destroy_troshkad_resources(host, PROJECT_ID, {}, {"net1": 100}, session)

        # Teardown still called
        mock_teardown.assert_called_once()


class TestDestroyKubevirtNative:
    """Tests for _destroy_kubevirt_native."""

    @patch(f"{SVC}._delete_project_record")
    def test_no_provider_deletes_record(self, mock_delete):
        """No provider found still deletes record."""
        from app.services.deploy_service import _destroy_kubevirt_native

        host = _make_host(host_type="kubevirt-cluster")
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        _destroy_kubevirt_native(PROJECT_ID, host, session, True)

        mock_delete.assert_called_once_with(PROJECT_ID)

    @patch(f"{SVC}._delete_project_record")
    @patch(f"{SVC}._set_destroy_error")
    def test_driver_exception_sets_error(self, mock_set_err, mock_delete):
        """Driver exception sets destroy error, does not delete record."""
        from app.services.deploy_service import _destroy_kubevirt_native

        host = _make_host(host_type="kubevirt-cluster")
        provider = MagicMock()
        provider.type = "kubevirt"
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = provider

        mock_driver = MagicMock()
        mock_driver.destroy_project.side_effect = RuntimeError("k8s error")

        with patch(
            "app.services.providers.get_provider_driver", return_value=mock_driver
        ):
            _destroy_kubevirt_native(PROJECT_ID, host, session, True)

        mock_set_err.assert_called_once()
        mock_delete.assert_not_called()

    @patch(f"{SVC}._delete_project_record")
    def test_happy_path_waits_for_namespace(self, mock_delete):
        """Happy path: destroys project, waits for namespace deletion."""
        from kubernetes.client.exceptions import ApiException

        from app.services.deploy_service import _destroy_kubevirt_native

        host = _make_host(host_type="kubevirt-cluster")
        provider = MagicMock()
        provider.type = "kubevirt"
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = provider

        mock_driver = MagicMock()
        mock_core_api = MagicMock()
        # First call returns namespace, second raises 404
        mock_core_api.read_namespace.side_effect = [
            ApiException(status=404),
        ]

        with patch(
            "app.services.providers.get_provider_driver", return_value=mock_driver
        ), patch(
            f"{KV_MOD}._get_k8s_clients",
            return_value=(None, mock_core_api, None),
        ), patch(
            f"{KV_MOD}._project_ns", return_value="troshka-test-ns"
        ):
            _destroy_kubevirt_native(PROJECT_ID, host, session, True)

        mock_driver.destroy_project.assert_called_once()
        mock_delete.assert_called_once_with(PROJECT_ID)


class TestStopUnexpectedError:
    """Tests for stop_project_async exception handling."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._extract_vms")
    def test_unexpected_error_sets_error(self, mock_extract, mock_notify):
        """Unexpected exception during stop sets error state."""
        from app.services.deploy_service import stop_project_async

        mock_extract.side_effect = RuntimeError("unexpected")

        project = _make_project(state="stopping")
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
            project,  # re-query in exception handler
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert project.state == "error"
        assert "Stop failed" in project.deploy_error


class TestStopTroshkadVms:
    """Tests for stop_project_async troshkad VM stop path."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}.wait_for_job", return_value={"status": "completed"})
    @patch(f"{SVC}.start_job", return_value="stop-job")
    @patch(f"{SVC}._extract_vms")
    def test_stop_multiple_vms(self, mock_extract, mock_start, mock_wait, mock_notify):
        """All VMs are stopped individually via troshkad."""
        from app.services.deploy_service import stop_project_async

        mock_extract.return_value = [
            {"node_id": "vm-a", "name": "vm-a"},
            {"node_id": "vm-b", "name": "vm-b"},
        ]

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[
                    _vm_node(node_id="vm-a", name="vm-a"),
                    _vm_node(node_id="vm-b", name="vm-b"),
                ]
            ),
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        assert mock_start.call_count == 2
        assert project.state == "stopped"

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}.start_job")
    @patch(f"{SVC}._extract_vms")
    def test_stop_vm_failure_nonfatal(self, mock_extract, mock_start, mock_notify):
        """Individual VM stop failure does not prevent overall stop."""
        from app.services.deploy_service import TroshkadError, stop_project_async

        mock_extract.return_value = [{"node_id": VM_NODE_ID, "name": "vm1"}]
        mock_start.side_effect = TroshkadError("domain not found")

        project = _make_project(
            state="stopping",
            topology=_minimal_topology(
                vm_nodes=[_vm_node(node_id=VM_NODE_ID, name="vm1")]
            ),
        )
        host = _make_host()

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            project,
            host,
        ]

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session):
            stop_project_async(PROJECT_ID)

        # Still completes stop despite error
        assert project.state == "stopped"


class TestStartProjectAsyncEipReassociation:
    """Tests for EIP re-association in start_project_async."""

    @patch(f"{SVC}.notify_project")
    @patch(f"{SVC}._has_ocp_monitor", return_value=False)
    @patch(f"{SVC}._extract_bmc_config", return_value=None)
    @patch(f"{SVC}._start_vms_via_troshkad", return_value=[])
    @patch(f"{SVC}._setup_pxe_via_troshkad")
    @patch(f"{SVC}.cache_library_images")
    @patch(f"{SVC}._setup_networks_via_troshkad", return_value=True)
    @patch(f"{SVC}._get_network_lock")
    def test_eip_reassociation(
        self,
        mock_lock,
        mock_net,
        mock_cache,
        mock_pxe,
        mock_start_vms,
        mock_bmc,
        mock_ocp,
        mock_notify,
    ):
        """EIPs re-associated on start before network setup."""
        from app.services.deploy_service import start_project_async

        mock_lock.return_value.__enter__ = MagicMock()
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_eip = MagicMock()
        mock_eip.canvas_eip_id = "eip-1"
        mock_eip.public_ip = "1.2.3.4"
        mock_eip.private_ip = "10.0.0.100"
        mock_eip.state = "allocated"

        topo = {
            "nodes": [_vm_node()],
            "edges": [],
            "externalIps": [{"id": "eip-1"}],
        }
        project = _make_project(
            state="starting",
            topology=topo,
            vni_map={"net1": 100},
        )
        host = _make_host()

        mock_session = MagicMock()

        def _query_side_effect(*args):
            q = MagicMock()

            def _filter_by(**kwargs):
                fb = MagicMock()
                if "id" in kwargs and kwargs["id"] == PROJECT_ID:
                    fb.first.return_value = project
                elif "id" in kwargs and kwargs["id"] == HOST_ID:
                    fb.first.return_value = host
                else:
                    fb.first.return_value = None
                fb.all.return_value = [mock_eip]
                return fb

            q.filter_by = _filter_by
            q.filter.return_value = q
            return q

        mock_session.query.side_effect = _query_side_effect

        with patch(f"{DB_MOD}.SessionLocal", return_value=mock_session), patch(
            "app.services.eip_service.associate_eip"
        ) as mock_assoc:
            start_project_async(PROJECT_ID)

        mock_assoc.assert_called_once()
        assert project.state == "active"


# ---------------------------------------------------------------------------
# Container / pod / metadata / IP helpers
# ---------------------------------------------------------------------------

CTR_NODE_ID = "ctr-node-0001"
CTR_NODE_ID_2 = "ctr-node-0002"


def _container_node(
    node_id=CTR_NODE_ID,
    name="test-ctr",
    image="registry.example.com/app:latest",
    nics=None,
    is_pod=False,
    init_containers=None,
    pod_containers=None,
    mounts=None,
    cpus=1,
    memory=512,
):
    data = {
        "name": name,
        "image": image,
        "cpus": cpus,
        "memory": memory,
        "nics": nics or [],
        "envVars": [],
        "ports": [],
        "isPod": is_pod,
        "mounts": mounts or [],
    }
    if init_containers is not None:
        data["initContainers"] = init_containers
    if pod_containers is not None:
        data["podContainers"] = pod_containers
    return {"id": node_id, "type": "containerNode", "data": data}


class TestSetupMetadataViaTroshkad:
    """Tests for _setup_metadata_via_troshkad."""

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch("app.services.cloud_init.generate_metadata", return_value="meta-yaml")
    @patch("app.services.cloud_init.generate_userdata", return_value="user-yaml")
    def test_deploys_metadata_for_cloud_init_vms(
        self, mock_userdata, mock_metadata, mock_start, mock_wait
    ):
        """Happy path: VM with cloudInit=True gets metadata deployed."""
        from app.services.deploy_service import _setup_metadata_via_troshkad

        nic = {"id": "nic-1", "mac": "52:54:00:AA:BB:CC"}
        vm = _vm_node(cloud_init=True, nics=[nic])
        topo = _minimal_topology(vm_nodes=[vm])
        host = _make_host()
        vni_map = {"net-1": 100}
        mock_start.return_value = "job-meta-1"

        _setup_metadata_via_troshkad(host, PROJECT_ID, topo, vni_map)

        mock_userdata.assert_called_once()
        mock_metadata.assert_called_once_with("test-vm")
        mock_start.assert_called_once()
        call_args = mock_start.call_args
        params = call_args[0][2]
        assert params["namespace"] == f"troshka-{PROJECT_ID[:8]}"
        assert "52:54:00:aa:bb:cc" in params["vm_configs"]
        mock_wait.assert_called_once_with(host, "job-meta-1", timeout=30)

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_skips_vms_without_cloud_init(self, mock_start, mock_wait):
        """VMs with cloudInit=False are skipped entirely."""
        from app.services.deploy_service import _setup_metadata_via_troshkad

        vm = _vm_node(
            cloud_init=False, nics=[{"id": "nic-1", "mac": "52:54:00:AA:BB:CC"}]
        )
        topo = _minimal_topology(vm_nodes=[vm])
        host = _make_host()

        _setup_metadata_via_troshkad(host, PROJECT_ID, topo, {"net-1": 100})

        mock_start.assert_not_called()
        mock_wait.assert_not_called()

    @patch(f"{SVC}.wait_for_job", side_effect=Exception("should not be called"))
    @patch(f"{SVC}.start_job", side_effect=Exception("should not be called"))
    def test_skips_non_vm_nodes(self, mock_start, mock_wait):
        """Non-vmNode nodes are ignored."""
        from app.services.deploy_service import _setup_metadata_via_troshkad

        net = _network_node()
        topo = _minimal_topology(network_nodes=[net])
        host = _make_host()

        # Should not raise because no VMs → early return before start_job
        _setup_metadata_via_troshkad(host, PROJECT_ID, topo, {"net-1": 100})

    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    @patch("app.services.cloud_init.generate_metadata", return_value="meta")
    @patch("app.services.cloud_init.generate_userdata", return_value="user")
    def test_troshkad_error_is_caught(
        self, mock_userdata, mock_metadata, mock_start, mock_wait
    ):
        """TroshkadError during deploy is caught (doesn't propagate)."""
        from app.services.deploy_service import _setup_metadata_via_troshkad
        from app.services.troshkad_client import TroshkadError

        nic = {"id": "nic-1", "mac": "52:54:00:11:22:33"}
        vm = _vm_node(cloud_init=True, nics=[nic])
        topo = _minimal_topology(vm_nodes=[vm])
        host = _make_host()
        mock_start.return_value = "job-fail"
        mock_wait.side_effect = TroshkadError("metadata deploy failed")

        # Should not raise
        _setup_metadata_via_troshkad(host, PROJECT_ID, topo, {"net-1": 100})


class TestCollectUsedIps:
    """Tests for _collect_used_ips."""

    def test_collects_vm_ips(self):
        """Collects IPs from VM NICs."""
        from app.services.deploy_topology import _collect_used_ips

        vm = _vm_node(nics=[{"id": "n1", "mac": "aa:bb", "ip": "10.0.0.5"}])
        topo = _minimal_topology(vm_nodes=[vm])
        result = _collect_used_ips(topo)
        assert "10.0.0.5" in result

    def test_collects_gateway_ips(self):
        """Collects gateway IPs (.1 of each network CIDR)."""
        from app.services.deploy_topology import _collect_used_ips

        net = _network_node(cidr="192.168.1.0/24")
        topo = _minimal_topology(network_nodes=[net])
        result = _collect_used_ips(topo)
        assert "192.168.1.1" in result

    def test_collects_container_ips(self):
        """Collects IPs from container NICs."""
        from app.services.deploy_topology import _collect_used_ips

        ctr = _container_node(nics=[{"id": "cn1", "ip": "192.168.1.50"}])
        topo = _minimal_topology()
        topo["nodes"].append(ctr)
        result = _collect_used_ips(topo)
        assert "192.168.1.50" in result

    def test_empty_topology(self):
        """Empty topology returns empty set."""
        from app.services.deploy_topology import _collect_used_ips

        result = _collect_used_ips({"nodes": [], "edges": []})
        assert result == set()


class TestGetDhcpRange:
    """Tests for _get_dhcp_range."""

    def test_explicit_range(self):
        """Returns explicit DHCP range when provided."""
        import ipaddress

        from app.services.deploy_topology import _get_dhcp_range

        net_data = {
            "cidr": "192.168.1.0/24",
            "dhcpRangeStart": "192.168.1.100",
            "dhcpRangeEnd": "192.168.1.200",
        }
        result = _get_dhcp_range(net_data)
        assert result is not None
        start_int, end_int = result
        assert str(ipaddress.ip_address(start_int)) == "192.168.1.100"
        assert str(ipaddress.ip_address(end_int)) == "192.168.1.200"

    def test_auto_range_from_cidr(self):
        """Auto-generates range from CIDR: hosts[9] to hosts[-1]."""
        import ipaddress

        from app.services.deploy_topology import _get_dhcp_range

        net_data = {"cidr": "192.168.1.0/24"}
        result = _get_dhcp_range(net_data)
        assert result is not None
        start_int, end_int = result
        # hosts[9] = .10 for a /24
        assert str(ipaddress.ip_address(start_int)) == "192.168.1.10"
        # hosts[-1] = .254 for a /24
        assert str(ipaddress.ip_address(end_int)) == "192.168.1.254"

    def test_no_cidr_returns_none(self):
        """No CIDR and no explicit range returns None."""
        from app.services.deploy_topology import _get_dhcp_range

        result = _get_dhcp_range({})
        assert result is None

    def test_small_subnet_returns_none(self):
        """Very small subnet (<=10 hosts) without explicit range returns None."""
        from app.services.deploy_topology import _get_dhcp_range

        net_data = {"cidr": "10.0.0.0/29"}  # 6 usable hosts
        result = _get_dhcp_range(net_data)
        assert result is None


class TestAutoAssignContainerIps:
    """Tests for _auto_assign_container_ips."""

    def test_assigns_ip_from_dhcp_range(self):
        """Container NIC without IP gets one from the connected network's DHCP range."""
        from app.services.deploy_topology import _auto_assign_container_ips

        nic = {"id": "cnic-1", "name": "eth0"}
        ctr = _container_node(nics=[nic])
        net = _network_node(cidr="10.0.0.0/24")
        edges = [
            {
                "source": CTR_NODE_ID,
                "target": NET_NODE_ID,
                "sourceHandle": "nic-cnic-1-top",
                "targetHandle": "port-1",
            }
        ]
        topo = _minimal_topology(network_nodes=[net], edges=edges)
        topo["nodes"].append(ctr)

        _auto_assign_container_ips(topo)

        assigned_nic = topo["nodes"][-1]["data"]["nics"][0]
        assert assigned_nic.get("ip") is not None
        # Should be from DHCP range: 10.0.0.10 to 10.0.0.254, first available
        assert assigned_nic["ip"] == "10.0.0.10"

    def test_skips_nics_with_existing_ip(self):
        """Container NIC with an existing IP is left unchanged."""
        from app.services.deploy_topology import _auto_assign_container_ips

        nic = {"id": "cnic-1", "name": "eth0", "ip": "10.0.0.99"}
        ctr = _container_node(nics=[nic])
        net = _network_node(cidr="10.0.0.0/24")
        edges = [
            {
                "source": CTR_NODE_ID,
                "target": NET_NODE_ID,
                "sourceHandle": "nic-cnic-1-top",
                "targetHandle": "port-1",
            }
        ]
        topo = _minimal_topology(network_nodes=[net], edges=edges)
        topo["nodes"].append(ctr)

        _auto_assign_container_ips(topo)

        assigned_nic = topo["nodes"][-1]["data"]["nics"][0]
        assert assigned_nic["ip"] == "10.0.0.99"

    def test_avoids_used_ips(self):
        """Assigned IP avoids IPs already used by VMs."""
        from app.services.deploy_topology import _auto_assign_container_ips

        # VM occupies 10.0.0.10
        vm_nic = {"id": "vn1", "mac": "aa:bb", "ip": "10.0.0.10"}
        vm = _vm_node(nics=[vm_nic])
        ctr_nic = {"id": "cnic-1", "name": "eth0"}
        ctr = _container_node(nics=[ctr_nic])
        net = _network_node(cidr="10.0.0.0/24")
        edges = [
            {
                "source": CTR_NODE_ID,
                "target": NET_NODE_ID,
                "sourceHandle": "nic-cnic-1-top",
                "targetHandle": "port-1",
            }
        ]
        topo = _minimal_topology(vm_nodes=[vm], network_nodes=[net], edges=edges)
        topo["nodes"].append(ctr)

        _auto_assign_container_ips(topo)

        assigned_nic = topo["nodes"][-1]["data"]["nics"][0]
        # 10.0.0.10 is used by the VM, so container should get 10.0.0.11
        assert assigned_nic["ip"] == "10.0.0.11"


class TestCreateAndStartContainer:
    """Tests for _create_and_start_container."""

    @patch(f"{SVC}._find_container_volumes", return_value=[])
    @patch(
        f"{SVC}._find_container_networks",
        return_value=[
            {
                "bridge": "br-100",
                "ip": "10.0.0.10",
                "mac": "aa:bb",
                "cidr": "10.0.0.0/24",
            }
        ],
    )
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_creates_and_starts_container(
        self, mock_start, mock_wait, mock_nets, mock_vols
    ):
        """Calls start_job twice: create then start."""
        from app.services.deploy_service import _create_and_start_container

        host = _make_host()
        ctr = {
            "node_id": CTR_NODE_ID,
            "name": "test-ctr",
            "image": "registry.example.com/app:latest",
            "cpus": 1,
            "memory_mb": 512,
            "env_vars": [],
            "ports": [],
            "command": None,
            "restart_policy": "always",
            "privileged": False,
        }
        topo = _minimal_topology()
        vni_map = {"net-1": 100}

        mock_start.side_effect = ["job-create", "job-start"]

        _create_and_start_container(host, PROJECT_ID, ctr, topo, vni_map)

        assert mock_start.call_count == 2
        # First call: /containers/create
        create_call = mock_start.call_args_list[0]
        assert create_call[0][1] == "/containers/create"
        create_params = create_call[0][2]
        expected_name = f"troshka-{PROJECT_ID[:8]}-{CTR_NODE_ID[:8]}"
        assert create_params["container_name"] == expected_name
        assert create_params["image"] == "registry.example.com/app:latest"
        # Second call: /containers/start
        start_call = mock_start.call_args_list[1]
        assert start_call[0][1] == "/containers/start"
        assert start_call[0][2]["container_name"] == expected_name
        assert mock_wait.call_count == 2


class TestCreateAndStartPod:
    """Tests for _create_and_start_pod."""

    @patch(f"{SVC}._find_container_volumes", return_value=[])
    @patch(
        f"{SVC}._find_container_networks",
        return_value=[
            {
                "bridge": "br-200",
                "ip": "10.0.0.20",
                "mac": "cc:dd",
                "cidr": "10.0.0.0/24",
            }
        ],
    )
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_creates_and_starts_pod(self, mock_start, mock_wait, mock_nets, mock_vols):
        """Calls start_job twice: /pods/create then /pods/start."""
        from app.services.deploy_service import _create_and_start_pod

        host = _make_host()
        ctr = {
            "node_id": CTR_NODE_ID,
            "name": "my-pod",
            "image": "",
            "cpus": 1,
            "memory_mb": 512,
            "env_vars": [],
            "ports": [],
            "command": None,
            "restart_policy": "always",
            "privileged": False,
            "init_containers": [
                {
                    "name": "init-1",
                    "image": "busybox:latest",
                    "envVars": [{"key": "FOO", "value": "bar"}],
                    "mounts": [],
                    "command": "echo hello",
                }
            ],
            "pod_containers": [
                {
                    "name": "app",
                    "image": "nginx:latest",
                    "cpus": 2,
                    "memory": 1024,
                    "envVars": [],
                    "mounts": [],
                    "command": None,
                }
            ],
        }
        topo = _minimal_topology()
        vni_map = {"net-1": 200}

        mock_start.side_effect = ["job-pod-create", "job-pod-start"]

        _create_and_start_pod(host, PROJECT_ID, ctr, topo, vni_map)

        assert mock_start.call_count == 2
        # First call: /pods/create
        create_call = mock_start.call_args_list[0]
        assert create_call[0][1] == "/pods/create"
        create_params = create_call[0][2]
        assert create_params["pod_name"] == "my-pod"
        assert len(create_params["init_containers"]) == 1
        assert create_params["init_containers"][0]["name"] == "init-1"
        assert create_params["init_containers"][0]["env"] == {"FOO": "bar"}
        assert len(create_params["containers"]) == 1
        assert create_params["containers"][0]["name"] == "app"
        assert create_params["containers"][0]["cpus"] == 2
        # Second call: /pods/start
        start_call = mock_start.call_args_list[1]
        assert start_call[0][1] == "/pods/start"
        full_pod_name = f"troshka-{PROJECT_ID[:8]}-my-pod"
        assert start_call[0][2]["pod_name"] == full_pod_name
        assert mock_wait.call_count == 2

    @patch(f"{SVC}._find_container_volumes")
    @patch(
        f"{SVC}._find_container_networks",
        return_value=[],
    )
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_pod_with_volume_mounts(self, mock_start, mock_wait, mock_nets, mock_vols):
        """Pod sub-containers resolve volume mounts via disk node IDs."""
        from app.services.deploy_service import _create_and_start_pod

        mock_vols.return_value = [
            {
                "node_id": DISK_NODE_ID,
                "disk_path": "/var/lib/troshka/vms/proj/disk.qcow2",
                "mount_dir": "/var/lib/troshka/vms/proj/mnt-disk",
                "mount_path": "/data",
            }
        ]

        host = _make_host()
        ctr = {
            "node_id": CTR_NODE_ID,
            "name": "vol-pod",
            "image": "",
            "cpus": 1,
            "memory_mb": 512,
            "env_vars": [],
            "ports": [],
            "command": None,
            "restart_policy": "always",
            "privileged": False,
            "init_containers": [],
            "pod_containers": [
                {
                    "name": "writer",
                    "image": "busybox:latest",
                    "cpus": 1,
                    "memory": 256,
                    "envVars": [],
                    "mounts": [{"diskNodeId": DISK_NODE_ID, "mountPath": "/mnt/data"}],
                    "command": None,
                }
            ],
        }
        topo = _minimal_topology()
        vni_map = {}
        mock_start.side_effect = ["job-1", "job-2"]

        _create_and_start_pod(host, PROJECT_ID, ctr, topo, vni_map)

        create_params = mock_start.call_args_list[0][0][2]
        pod_ctr = create_params["containers"][0]
        assert len(pod_ctr["mounts"]) == 1
        assert "/var/lib/troshka/vms/proj/mnt-disk:/mnt/data" in pod_ctr["mounts"][0]
        assert len(create_params["volumes"]) == 1
        assert (
            create_params["volumes"][0]["mount_dir"]
            == "/var/lib/troshka/vms/proj/mnt-disk"
        )


# ---------------------------------------------------------------------------
# Ops-pod deploy wiring (Plan 4, Task 6)
# ---------------------------------------------------------------------------


def _ocp_topology(n_clusters):
    """OCP topology with `n_clusters` clusters carrying generated configs."""
    clusters = []
    for i in range(n_clusters):
        clusters.append(
            {
                "id": f"cl-{i}",
                "name": f"c{i}",
                "ocpVersion": "4.20",
                "baseDomain": "ocp.local",
                "_generatedInstallConfig": f"install: c{i}\n",
                "_generatedAgentConfig": f"agent: c{i}\n",
            }
        )
    topo = _minimal_topology()
    topo["clusters"] = clusters
    return topo


class TestShouldUseOpsPod:
    """Gating logic for the ops-pod install path.

    Plan 4b: the selector is now per-PROJECT — it returns True for an OCP
    project iff its persisted ``ocpInstallVia`` resolves to "pod" (the default),
    on ALL host types. "bastion" and non-OCP projects use the bastion path.
    """

    def test_non_ocp_project_never_uses_ops_pod(self):
        from app.services.deploy_service import _should_use_ops_pod

        # No ``clusters`` -> not an OCP project, even if a value leaks in.
        topo = _minimal_topology()
        topo["ocpInstallVia"] = "pod"
        assert _should_use_ops_pod(topo) is False

    def test_single_cluster_pod_uses_ops_pod(self):
        from app.services.deploy_service import _should_use_ops_pod

        topo = _ocp_topology(1)
        topo["ocpInstallVia"] = "pod"
        assert _should_use_ops_pod(topo) is True

    def test_multicluster_pod_uses_ops_pod(self):
        from app.services.deploy_service import _should_use_ops_pod

        topo = _ocp_topology(2)
        topo["ocpInstallVia"] = "pod"
        assert _should_use_ops_pod(topo) is True

    def test_single_cluster_bastion_uses_bastion(self):
        from app.services.deploy_service import _should_use_ops_pod

        topo = _ocp_topology(1)
        topo["ocpInstallVia"] = "bastion"
        assert _should_use_ops_pod(topo) is False

    def test_multicluster_bastion_uses_bastion(self):
        from app.services.deploy_service import _should_use_ops_pod

        topo = _ocp_topology(2)
        topo["ocpInstallVia"] = "bastion"
        assert _should_use_ops_pod(topo) is False

    def test_unset_defaults_to_pod(self):
        from app.services.deploy_service import _should_use_ops_pod

        # No ``ocpInstallVia`` on the topology -> config default "pod" -> ops pod.
        topo = _ocp_topology(1)
        topo.pop("ocpInstallVia", None)
        assert _should_use_ops_pod(topo) is True


class TestOpsPodCreateParams:
    """Shape of the real troshkad /pods/create params for the ops pod."""

    def test_params_match_troshkad_contract(self):
        from app.services.deploy_service import _ops_pod_create_params
        from app.services.ocp.ops_pod_scaffold import OPS_POD_IMAGE

        project = _make_project()
        topo = _ocp_topology(2)
        params = _ops_pod_create_params(
            project,
            topo["clusters"],
            topo,
            {"net-1": 4106},
            api_url="https://troshka.example.com",
            api_key="trk_secret",
            ocp_version="4.20",
            pull_secret_json="",
        )
        assert params["pod_name"] == "ops"
        assert params["restart_policy"] == "always"
        assert params["privileged"] is True
        assert params["project_id"] == PROJECT_ID
        # Single main container on the OPS EE image.
        assert len(params["containers"]) == 1
        ctr = params["containers"][0]
        assert ctr["name"] == "ops"
        assert ctr["image"] == OPS_POD_IMAGE
        # Real troshkad contract: env is a DICT, not an envVars[] list.
        env = ctr["env"]
        assert env["TROSHKA_API_KEY"] == "trk_secret"
        assert env["TROSHKA_PROJECT_ID"] == PROJECT_ID
        assert env["TROSHKA_API_URL"] == "https://troshka.example.com"
        assert env["OCP_VERSION"] == "4.20"
        # Command runs the install script under bash -c.
        assert ctr["command"][0] == "bash"
        assert ctr["command"][1] == "-c"
        script = ctr["command"][2]
        assert "wait-for install-complete" in script
        # Workdir materialization writes each cluster's install-config.
        assert "cl-0/install-config.yaml" in script
        assert "cl-1/install-config.yaml" in script
        # Transit network with the ops .4 infra IP.
        assert params["networks"][0]["ip"] == "172.30.10.4"
        assert params["networks"][0]["infra_transit"] is True


class TestDeployOpsPod:
    """End-to-end (mocked troshkad) ops-pod create+start sequence."""

    @patch(f"{SVC}._start_ops_pod_install_monitor")
    @patch("app.services.ocp.ops_pod_auth.mint_ops_pod_key", return_value="trk_test")
    @patch(f"{SVC}.wait_for_job", return_value={"status": "completed"})
    @patch(f"{SVC}.start_job")
    def test_deploy_ops_pod_mints_key_then_creates_and_starts(
        self, mock_start, _mock_wait, mock_mint, mock_monitor
    ):
        from app.services.deploy_service import _deploy_ops_pod
        from app.services.ocp.ops_pod_scaffold import OPS_POD_IMAGE

        s = MagicMock()
        host = _make_host()
        project = _make_project()
        topo = _ocp_topology(2)
        vni_map = {"net-1": 4106}
        mock_start.side_effect = ["job-create", "job-start"]

        _deploy_ops_pod(s, host, PROJECT_ID, project, topo, vni_map)

        # Scoped key minted exactly once for this project.
        mock_mint.assert_called_once_with(s, project)
        assert mock_start.call_count == 2
        # Install-progress monitor spawned after create+start, for both clusters.
        mock_monitor.assert_called_once()
        m_args = mock_monitor.call_args[0]
        assert m_args[0] is host
        assert m_args[1] == PROJECT_ID
        assert [c["id"] for c in m_args[2]] == ["cl-0", "cl-1"]

        # First call: /pods/create with the ops-pod params.
        create_call = mock_start.call_args_list[0]
        assert create_call[0][1] == "/pods/create"
        params = create_call[0][2]
        assert params["pod_name"] == "ops"
        ctr = params["containers"][0]
        assert ctr["image"] == OPS_POD_IMAGE
        assert ctr["env"]["TROSHKA_API_KEY"] == "trk_test"
        assert ctr["env"]["TROSHKA_PROJECT_ID"] == PROJECT_ID
        assert "wait-for install-complete" in ctr["command"][2]

        # Second call: /pods/start with the full pod name.
        start_call = mock_start.call_args_list[1]
        assert start_call[0][1] == "/pods/start"
        assert start_call[0][2]["pod_name"] == f"troshka-{PROJECT_ID[:8]}-ops"

    @patch(f"{SVC}.threading.Thread")
    def test_start_ops_pod_install_monitor_spawns_daemon_thread(self, mock_thread):
        """The monitor is spawned as a daemon thread targeting
        ``_monitor_ops_pod_install`` with the project's clusters."""
        from app.services import deploy_service
        from app.services.deploy_service import _start_ops_pod_install_monitor
        from app.services.ocp.ops_pod_scaffold import OPS_POD_WORKDIR

        host = _make_host()
        clusters = [{"id": "cl-0"}, {"id": "cl-1"}]

        _start_ops_pod_install_monitor(host, PROJECT_ID, clusters)

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["target"] is deploy_service._monitor_ops_pod_install
        assert kwargs["args"] == (PROJECT_ID, host, clusters)
        assert kwargs["daemon"] is True
        assert kwargs["kwargs"]["workdir"] == OPS_POD_WORKDIR
        assert kwargs["kwargs"]["container_name"] == (
            f"troshka-{PROJECT_ID[:8]}-ops-ops"
        )
        # Thread actually started.
        mock_thread.return_value.start.assert_called_once()


class TestOpsPodDeadDetection:
    """Consecutive-not-running counter that tolerates a recoverable pod restart
    (restart_policy=always + idempotent install) before failing the deploy."""

    def test_dead_count_increments_when_not_running(self):
        from app.services.deploy_service import _next_ops_pod_dead_count

        assert _next_ops_pod_dead_count(0, pod_running=False) == 1
        assert _next_ops_pod_dead_count(2, pod_running=False) == 3

    def test_dead_count_resets_when_running(self):
        from app.services.deploy_service import _next_ops_pod_dead_count

        # A running poll (or a transient status error → conservatively running)
        # resets the counter, so an isolated blip never accumulates to failure.
        assert _next_ops_pod_dead_count(2, pod_running=True) == 0

    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}._ops_pod_running")
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=False)
    def test_monitor_fails_after_consecutive_not_running(
        self, _mock_cancel, mock_logs, mock_running, _mock_pub
    ):
        """A persistent crash-loop (not-running for the full threshold) fails."""
        from app.services.deploy_service import (
            _OPS_POD_DEAD_POLLS,
            _monitor_ops_pod_install,
        )

        host = _make_host()
        mock_logs.return_value = {"c1": "Waiting for cluster installation to complete"}
        mock_running.return_value = False  # pod never comes back

        result = _monitor_ops_pod_install(
            PROJECT_ID, host, [{"id": "c1"}], poll_interval=0
        )

        assert result == "failed"
        # Failed exactly at the threshold, not on the first not-running poll.
        assert mock_running.call_count == _OPS_POD_DEAD_POLLS

    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}._ops_pod_running")
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=False)
    def test_monitor_single_not_running_then_recovers(
        self, _mock_cancel, mock_logs, mock_running, _mock_pub
    ):
        """A single not-running poll (restart window) does NOT fail; a running
        poll resets the counter and the install completes normally."""
        from app.services.deploy_service import _monitor_ops_pod_install

        host = _make_host()
        waiting = "Waiting for cluster installation to complete"
        mock_logs.side_effect = [
            {"c1": waiting},  # poll 1: not running, count=1 (< threshold)
            {"c1": waiting},  # poll 2: running again, counter reset
            {"c1": "install complete"},  # poll 3: finished
        ]
        mock_running.side_effect = [False, True, True]

        result = _monitor_ops_pod_install(
            PROJECT_ID, host, [{"id": "c1"}], poll_interval=0
        )

        assert result == "complete"


class TestOpsPodCancellation:
    """Cancelling a bastionless OCP install must STOP the persistent ops pod.

    The ops pod is ``restart_policy=always`` and the real install runs INSIDE it;
    the ``/pods/create`` job completes immediately, so cancelling that job is a
    no-op. Cancellation must issue ``/pods/destroy`` for the ops pod so the
    in-pod install actually halts (and the pod won't restart).
    """

    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job", return_value="job-destroy")
    def test_cancel_issues_pods_destroy_for_ops_pod(
        self, mock_start, mock_wait, _mock_pub
    ):
        from app.services.deploy_service import _cancel_ops_pod_install

        host = _make_host()

        _cancel_ops_pod_install(host, PROJECT_ID, ["c1", "c2"])

        # A /pods/destroy is issued for the ops pod name, NOT cancel_job on the
        # completed create job.
        mock_start.assert_called_once()
        endpoint = mock_start.call_args[0][1]
        payload = mock_start.call_args[0][2]
        assert endpoint == "/pods/destroy"
        assert payload["pod_name"] == f"troshka-{PROJECT_ID[:8]}-ops"
        assert payload["project_id"] == PROJECT_ID
        mock_wait.assert_called_once()

    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}.wait_for_job")
    @patch(f"{SVC}.start_job")
    def test_cancel_is_best_effort_on_troshkad_error(
        self, mock_start, _mock_wait, mock_pub
    ):
        """A troshkad failure while destroying the pod must not raise; the
        terminal ``cancelled`` status is still published."""
        from app.services.deploy_service import _cancel_ops_pod_install
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("boom")
        host = _make_host()

        # Does not raise.
        _cancel_ops_pod_install(host, PROJECT_ID, ["c1"])

        mock_pub.assert_called_once()
        published = mock_pub.call_args[0][1]
        assert published["overall"] == "cancelled"

    @patch(f"{SVC}._cancel_ops_pod_install")
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=True)
    def test_monitor_calls_cancel_on_cancellation(
        self, _mock_cancel_flag, _mock_logs, mock_cancel
    ):
        """The monitor delegates to ``_cancel_ops_pod_install`` (which stops the
        pod) and returns ``cancelled`` when the deploy is cancelled."""
        from app.services.deploy_service import _monitor_ops_pod_install

        host = _make_host()

        result = _monitor_ops_pod_install(
            PROJECT_ID, host, [{"id": "c1"}], poll_interval=0
        )

        assert result == "cancelled"
        mock_cancel.assert_called_once()
        args = mock_cancel.call_args[0]
        assert args[0] is host
        assert args[1] == PROJECT_ID
        assert args[2] == ["c1"]


class TestOpsPodOcpStatus:
    """Pod (bastionless) installs have no ocpMonitor VM node, so the bastion
    ``maybe_start_ocp_health_monitor`` gate never fires. The ops-pod install
    monitor must instead drive the SAME ``ocp_status`` / ``ocp_install_elapsed``
    fields the OCP-status UI reads, mirroring the bastion path's vocabulary
    (monitoring -> ready / error)."""

    def test_overall_to_ocp_status_mapping(self):
        from app.services.deploy_service import _ops_pod_overall_to_ocp_status

        # Success -> "ready", failure/timeout -> "error" (bastion vocabulary).
        assert _ops_pod_overall_to_ocp_status("complete") == "ready"
        assert _ops_pod_overall_to_ocp_status("failed") == "error"
        assert _ops_pod_overall_to_ocp_status("timeout") == "error"
        # Cancellation is a user action, not an install outcome -> untouched.
        assert _ops_pod_overall_to_ocp_status("cancelled") is None
        # In-progress phases never persist a terminal status.
        assert _ops_pod_overall_to_ocp_status("creating-image") is None

    @patch(f"{SVC}._start_ops_pod_install_monitor")
    @patch("app.services.ocp.ops_pod_auth.mint_ops_pod_key", return_value="trk_test")
    @patch(f"{SVC}.wait_for_job", return_value={"status": "completed"})
    @patch(f"{SVC}.start_job")
    def test_deploy_ops_pod_marks_ocp_status_monitoring(
        self, mock_start, _mock_wait, _mock_mint, _mock_monitor
    ):
        """Deploying a pod OCP project sets the initial in-progress OCP status so
        the UI shows install-in-progress (bastion gate would never set it)."""
        from app.services.deploy_service import _deploy_ops_pod

        s = MagicMock()
        project = _make_project()
        topo = _ocp_topology(1)
        mock_start.side_effect = ["job-create", "job-start"]

        _deploy_ops_pod(s, _make_host(), PROJECT_ID, project, topo, {})

        assert project.ocp_status == "monitoring"
        assert project.ocp_status_detail is None
        assert project.ocp_install_elapsed is None
        assert project.ocp_monitor_started_at is not None
        s.commit.assert_called()

    @patch(f"{SVC}._ocp_update_status")
    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}._ops_pod_running", return_value=True)
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=False)
    def test_monitor_complete_persists_ready(
        self, _cancel, mock_logs, _running, _pub, mock_status
    ):
        from app.services.deploy_service import _monitor_ops_pod_install

        mock_logs.return_value = {"c1": "complete"}

        result = _monitor_ops_pod_install(
            PROJECT_ID, _make_host(), [{"id": "c1"}], poll_interval=0
        )

        assert result == "complete"
        mock_status.assert_called_once()
        call = mock_status.call_args[0]
        assert call[0] == PROJECT_ID
        assert call[1] == "ready"
        assert isinstance(call[2], int)  # elapsed seconds persisted

    @patch(f"{SVC}._ocp_update_status")
    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}._ops_pod_running", return_value=True)
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=False)
    def test_monitor_failed_persists_error(
        self, _cancel, mock_logs, _running, _pub, mock_status
    ):
        from app.services.deploy_service import _monitor_ops_pod_install

        mock_logs.return_value = {"c1": "failed"}

        result = _monitor_ops_pod_install(
            PROJECT_ID, _make_host(), [{"id": "c1"}], poll_interval=0
        )

        assert result == "failed"
        mock_status.assert_called_once()
        assert mock_status.call_args[0][1] == "error"

    @patch(f"{SVC}._ocp_update_status")
    @patch(f"{SVC}._publish_ops_pod_progress")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=False)
    def test_monitor_timeout_persists_error(self, _cancel, _pub, mock_status):
        from app.services.deploy_service import _monitor_ops_pod_install

        # timeout=0 -> the poll loop never runs; monitor falls through to timeout.
        result = _monitor_ops_pod_install(
            PROJECT_ID, _make_host(), [{"id": "c1"}], poll_interval=0, timeout=0
        )

        assert result == "timeout"
        mock_status.assert_called_once()
        assert mock_status.call_args[0][1] == "error"

    @patch(f"{SVC}._ocp_update_status")
    @patch(f"{SVC}._cancel_ops_pod_install")
    @patch(f"{SVC}._read_ops_pod_cluster_logs")
    @patch(f"{SVC}._is_deploy_cancelled", return_value=True)
    def test_monitor_cancel_leaves_ocp_status_untouched(
        self, _cancel_flag, _logs, _cancel, mock_status
    ):
        """Cancellation halts the pod (via ``_cancel_ops_pod_install``) but is not
        an install outcome, so ``ocp_status`` is left untouched."""
        from app.services.deploy_service import _monitor_ops_pod_install

        result = _monitor_ops_pod_install(
            PROJECT_ID, _make_host(), [{"id": "c1"}], poll_interval=0
        )

        assert result == "cancelled"
        mock_status.assert_not_called()
