"""Tests for uncovered lines in app.api.projects — reconfigure helpers,
disk-change detection, deploy-related helpers, and kubevirt reconfigure functions.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
client = TestClient(app)

# Module-level test user
_db = TestSession()
_user = User(
    email="proj-cov-test@example.com",
    display_name="CovTest",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
TOKEN = create_jwt(user_id=_user.id, email=_user.email, role=_user.role)
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
_db.close()


# ---------------------------------------------------------------------------
# _find_gateway_node
# ---------------------------------------------------------------------------


class TestFindGatewayNode:
    def test_returns_gateway_node(self):
        from app.api.projects import _find_gateway_node

        topo = {
            "nodes": [
                {"type": "vmNode", "data": {}},
                {
                    "type": "networkNode",
                    "data": {
                        "subtype": "gateway",
                        "gatewayMode": "nat-portforward",
                        "id": "gw1",
                    },
                },
            ]
        }
        result = _find_gateway_node(topo)
        assert result is not None
        assert result["data"]["id"] == "gw1"

    def test_returns_none_without_gateway(self):
        from app.api.projects import _find_gateway_node

        topo = {"nodes": [{"type": "vmNode", "data": {}}]}
        assert _find_gateway_node(topo) is None

    def test_returns_none_empty_topology(self):
        from app.api.projects import _find_gateway_node

        assert _find_gateway_node({}) is None
        assert _find_gateway_node({"nodes": []}) is None


# ---------------------------------------------------------------------------
# _build_destroy_context
# ---------------------------------------------------------------------------


class TestBuildDestroyContext:
    def test_builds_context_with_all_fields(self):
        from app.api.projects import _build_destroy_context

        proj = MagicMock()
        proj.id = "proj-id"
        proj.host_id = "host-id"
        proj.vni_map = {"net1": 100}
        proj.deployed_topology = {"nodes": [{"type": "vmNode"}]}
        proj.topology = None
        proj.dns_provider_id = "dns-1"
        proj.domain = "example.com"

        ctx = _build_destroy_context(proj)
        assert ctx["project_id"] == "proj-id"
        assert ctx["host_id"] == "host-id"
        assert ctx["vni_map"] == {"net1": 100}
        assert ctx["topology"]["nodes"][0]["type"] == "vmNode"
        assert ctx["dns_provider_id"] == "dns-1"
        assert ctx["domain"] == "example.com"

    def test_falls_back_to_topology(self):
        from app.api.projects import _build_destroy_context

        proj = MagicMock()
        proj.id = "p1"
        proj.host_id = "h1"
        proj.vni_map = None
        proj.deployed_topology = None
        proj.topology = {"nodes": []}
        proj.dns_provider_id = None
        proj.domain = None

        ctx = _build_destroy_context(proj)
        assert ctx["vni_map"] == {}
        assert ctx["topology"] == {"nodes": []}

    def test_deepcopy_isolation(self):
        from app.api.projects import _build_destroy_context

        vni = {"a": 1}
        proj = MagicMock()
        proj.id = "p2"
        proj.host_id = "h2"
        proj.vni_map = vni
        proj.deployed_topology = {"nodes": []}
        proj.topology = None
        proj.dns_provider_id = None
        proj.domain = None

        ctx = _build_destroy_context(proj)
        ctx["vni_map"]["b"] = 2
        assert "b" not in vni


# ---------------------------------------------------------------------------
# _find_changed_kubevirt_vms
# ---------------------------------------------------------------------------


class TestFindChangedKubevirtVms:
    def test_detects_changed_data(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"vcpus": 4}},
                {"id": "vm2", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        deployed = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {"vcpus": 2}},
                {"id": "vm2", "type": "vmNode", "data": {"vcpus": 2}},
            ]
        }
        result = _find_changed_kubevirt_vms(current, deployed)
        assert "vm1" in result
        assert "vm2" not in result

    def test_no_changes(self):
        from app.api.projects import _find_changed_kubevirt_vms

        topo = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"vcpus": 2}}]}
        assert _find_changed_kubevirt_vms(topo, topo) == []

    def test_ignores_new_vms(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {"nodes": [{"id": "new", "type": "vmNode", "data": {}}]}
        deployed = {"nodes": []}
        assert _find_changed_kubevirt_vms(current, deployed) == []

    def test_ignores_non_vm_nodes(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {"nodes": [{"id": "n1", "type": "networkNode", "data": {"a": 1}}]}
        deployed = {"nodes": [{"id": "n1", "type": "networkNode", "data": {"a": 2}}]}
        assert _find_changed_kubevirt_vms(current, deployed) == []


# ---------------------------------------------------------------------------
# _get_deployed_disk_info
# ---------------------------------------------------------------------------


class TestGetDeployedDiskInfo:
    def test_extracts_disk_info(self):
        from app.api.projects import _get_deployed_disk_info

        deployed = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {
                    "id": "d1",
                    "type": "storageNode",
                    "data": {
                        "library_item_id": "lib-1",
                        "size": 50,
                    },
                },
            ],
            "edges": [
                {"source": "vm1", "target": "d1"},
            ],
        }
        with patch("app.api.projects._find_vm_disks") as mock_find:
            mock_find.return_value = [
                {"node_id": "d1", "library_item_id": "lib-1", "size_gb": 50}
            ]
            libs, sizes = _get_deployed_disk_info("vm1", deployed)
        assert libs["d1"] == "lib-1"
        assert sizes["d1"] == 50

    def test_no_matching_vm(self):
        from app.api.projects import _get_deployed_disk_info

        deployed = {"nodes": []}
        with patch("app.api.projects._find_vm_disks") as mock_find:
            mock_find.return_value = []
            libs, sizes = _get_deployed_disk_info("nonexistent", deployed)
        assert libs == {}
        assert sizes == {}


# ---------------------------------------------------------------------------
# _resolve_disk_backing
# ---------------------------------------------------------------------------


class TestResolveDiskBacking:
    def test_library_disk(self):
        from app.api.projects import _resolve_disk_backing

        d = {"source": "library", "library_item_id": "item-1", "format": "qcow2"}
        with patch("app.api.projects._resolve_disk_backing.__module__"):
            pass
        with patch(
            "app.services.deploy_topology._image_cache_path",
            return_value="/var/lib/troshka/images/item-1.qcow2",
        ):
            path, is_lib = _resolve_disk_backing(d, None)
        assert path == "/var/lib/troshka/images/item-1.qcow2"
        assert is_lib is True

    def test_pattern_disk(self):
        from app.api.projects import _resolve_disk_backing

        d = {
            "source": "pattern",
            "patternId": "pat-1",
            "patternDiskId": "disk-a",
            "format": "qcow2",
        }
        path, is_lib = _resolve_disk_backing(d, None)
        assert "pat-1" in path
        assert "disk-a" in path
        assert is_lib is False

    def test_blank_disk(self):
        from app.api.projects import _resolve_disk_backing

        d = {"source": "blank"}
        path, is_lib = _resolve_disk_backing(d, None)
        assert path is None
        assert is_lib is False


# ---------------------------------------------------------------------------
# _classify_single_disk
# ---------------------------------------------------------------------------


class TestClassifySingleDisk:
    def test_new_disk_detected(self):
        from app.api.projects import _classify_single_disk

        d = {
            "node_id": "d-new",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "source": "blank",
        }
        dep_disk_libs = {}
        dep_disk_sizes = {}
        with patch("app.api.projects._disk_path", return_value="/fake/path"):
            with patch(
                "app.api.projects._resolve_disk_backing", return_value=(None, False)
            ):
                info = _classify_single_disk(
                    d, "p1", "vm1", dep_disk_libs, dep_disk_sizes, None
                )
        assert info["is_new"] is True
        assert info["image_changed"] is False

    def test_image_changed_detected(self):
        from app.api.projects import _classify_single_disk

        d = {
            "node_id": "d1",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "library_item_id": "new-lib",
            "source": "library",
        }
        dep_disk_libs = {"d1": "old-lib"}
        dep_disk_sizes = {"d1": 20}
        with patch("app.api.projects._disk_path", return_value="/fake/path"):
            with patch(
                "app.api.projects._resolve_disk_backing",
                return_value=("/cache/new-lib.qcow2", True),
            ):
                info = _classify_single_disk(
                    d, "p1", "vm1", dep_disk_libs, dep_disk_sizes, None
                )
        assert info[
            "image_changed"
        ]  # truthy (old_lib != new_lib and (old_lib or new_lib))
        assert info["is_library"] is True

    def test_size_grew_detected(self):
        from app.api.projects import _classify_single_disk

        d = {
            "node_id": "d1",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 40,
            "source": "blank",
        }
        dep_disk_libs = {"d1": None}
        dep_disk_sizes = {"d1": 20}
        with patch("app.api.projects._disk_path", return_value="/fake/path"):
            with patch(
                "app.api.projects._resolve_disk_backing", return_value=(None, False)
            ):
                info = _classify_single_disk(
                    d, "p1", "vm1", dep_disk_libs, dep_disk_sizes, None
                )
        assert info["size_grew"] is True
        assert info["image_changed"] is False

    def test_rotation_rate_included(self):
        from app.api.projects import _classify_single_disk

        d = {
            "node_id": "d1",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "source": "blank",
            "rotation_rate": 1,
        }
        with patch("app.api.projects._disk_path", return_value="/fake/path"):
            with patch(
                "app.api.projects._resolve_disk_backing", return_value=(None, False)
            ):
                info = _classify_single_disk(d, "p1", "vm1", {}, {}, None)
        assert info["rotation_rate"] == 1


# ---------------------------------------------------------------------------
# _accumulate_disk_info
# ---------------------------------------------------------------------------


class TestAccumulateDiskInfo:
    def test_new_disk_marks_changed(self):
        from app.api.projects import _accumulate_disk_info

        result = {
            "disk_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        info = {
            "path": "/fake/path",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": True,
            "is_library": False,
        }
        _accumulate_disk_info(info, result)
        assert result["any_disk_changed"] is True
        assert len(result["disk_list"]) == 1
        assert len(result["disks_to_create"]) == 1

    def test_image_changed_removes_old(self):
        from app.api.projects import _accumulate_disk_info

        result = {
            "disk_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        info = {
            "path": "/fake/path",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": "/cache/img.qcow2",
            "image_changed": True,
            "size_grew": False,
            "is_new": False,
            "is_library": True,
        }
        _accumulate_disk_info(info, result)
        assert "/fake/path" in result["files_to_remove"]
        assert result["needs_library_download"] is True

    def test_size_grew_adds_resize(self):
        from app.api.projects import _accumulate_disk_info

        result = {
            "disk_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        info = {
            "path": "/fake/path",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 40,
            "backing_file": None,
            "image_changed": False,
            "size_grew": True,
            "is_new": False,
            "is_library": False,
        }
        _accumulate_disk_info(info, result)
        assert len(result["disks_to_resize"]) == 1
        assert result["disks_to_resize"][0]["new_size_gb"] == 40

    def test_rotation_rate_forwarded(self):
        from app.api.projects import _accumulate_disk_info

        result = {
            "disk_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        }
        info = {
            "path": "/fake/path",
            "format": "qcow2",
            "bus": "virtio",
            "size_gb": 20,
            "backing_file": None,
            "image_changed": False,
            "size_grew": False,
            "is_new": False,
            "is_library": False,
            "rotation_rate": 1,
        }
        _accumulate_disk_info(info, result)
        assert result["disk_list"][0]["rotation_rate"] == 1


# ---------------------------------------------------------------------------
# _broadcast_vm_states
# ---------------------------------------------------------------------------


class TestBroadcastVmStates:
    @patch("app.api.projects.notify_project")
    @patch("app.services.troshkad_client.get_all_vm_states")
    def test_maps_states_correctly(self, mock_get_states, mock_notify):
        from app.api.projects import _broadcast_vm_states

        mock_get_states.return_value = {
            "troshka-p1234567-vm123456": "running",
            "troshka-p1234567-vm234567": "shut_off",
            "troshka-p1234567-vm345678": "paused",
        }
        h = MagicMock()
        current = {
            "nodes": [
                {"id": "vm12345678-full-id-1", "type": "vmNode"},
                {"id": "vm23456789-full-id-2", "type": "vmNode"},
                {"id": "vm34567890-full-id-3", "type": "vmNode"},
            ]
        }
        with patch(
            "app.services.deploy_topology._vm_domain_name",
            side_effect=[
                "troshka-p1234567-vm123456",
                "troshka-p1234567-vm234567",
                "troshka-p1234567-vm345678",
            ],
        ):
            _broadcast_vm_states(h, "p1234567-full", current)

        msg = mock_notify.call_args[0][1]
        assert msg["type"] == "vm-state"
        states = msg["states"]
        assert states["vm12345678-full-id-1"] == "running"
        assert states["vm23456789-full-id-2"] == "stopped"
        assert states["vm34567890-full-id-3"] == "paused"

    @patch("app.api.projects.notify_project")
    @patch("app.services.troshkad_client.get_all_vm_states")
    def test_swallows_exception(self, mock_get_states, mock_notify):
        from app.api.projects import _broadcast_vm_states

        mock_get_states.side_effect = Exception("connection refused")
        _broadcast_vm_states(MagicMock(), "p1", {"nodes": []})
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# _setup_reconfigure_networking
# ---------------------------------------------------------------------------


class TestSetupReconfigureNetworking:
    @patch("app.api.projects._setup_networks_via_troshkad", return_value=True)
    @patch(
        "app.services.deploy_service._get_network_lock",
        return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
    )
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_success(self, mock_del, mock_set, mock_lock, mock_setup):
        from app.api.projects import _setup_reconfigure_networking

        h = MagicMock()
        h.id = "h1"
        proj = MagicMock()
        s = MagicMock()
        result = _setup_reconfigure_networking(h, "p1", {"nodes": []}, {}, s, proj)
        assert result is True

    @patch(
        "app.api.projects._setup_networks_via_troshkad", return_value="Error: timeout"
    )
    @patch(
        "app.services.deploy_service._get_network_lock",
        return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
    )
    @patch("app.services.deploy_service._set_deploy_progress")
    @patch("app.services.deploy_service._delete_deploy_progress")
    def test_failure_sets_error(self, mock_del, mock_set, mock_lock, mock_setup):
        from app.api.projects import _setup_reconfigure_networking

        h = MagicMock()
        h.id = "h1"
        proj = MagicMock()
        s = MagicMock()
        result = _setup_reconfigure_networking(h, "p1", {"nodes": []}, {}, s, proj)
        assert result is False
        assert proj.state == "error"
        assert "Network setup failed" in proj.deploy_error


# ---------------------------------------------------------------------------
# _cache_images_and_metadata
# ---------------------------------------------------------------------------


class TestCacheImagesAndMetadata:
    @patch("app.api.projects.cache_library_images")
    @patch("app.services.deploy_service._setup_metadata_via_troshkad")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_caches_and_deploys_metadata(self, mock_set, mock_meta, mock_cache):
        from app.api.projects import _cache_images_and_metadata

        h = MagicMock()
        s = MagicMock()
        _cache_images_and_metadata(h, "p1", {"nodes": []}, {"net1": 100}, s)
        mock_cache.assert_called_once()
        mock_meta.assert_called_once()

    @patch("app.api.projects.cache_library_images")
    @patch(
        "app.services.deploy_service._setup_metadata_via_troshkad",
        side_effect=Exception("fail"),
    )
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_metadata_failure_non_fatal(self, mock_set, mock_meta, mock_cache):
        from app.api.projects import _cache_images_and_metadata

        # Should not raise
        _cache_images_and_metadata(MagicMock(), "p1", {}, {}, MagicMock())


# ---------------------------------------------------------------------------
# _create_bmc_bridge_if_needed
# ---------------------------------------------------------------------------


class TestCreateBmcBridgeIfNeeded:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    def test_calls_troshkad(self, mock_start, mock_wait):
        from app.api.projects import _create_bmc_bridge_if_needed

        h = MagicMock()
        bmc_config = {
            "bmc_network": {"cidr": "192.168.100.0/24"},
            "vms": [{"bmc_ip": "192.168.100.10"}],
        }
        _create_bmc_bridge_if_needed(h, "p1", {}, bmc_config)
        mock_start.assert_called_once()
        mock_wait.assert_called_once_with(h, "job-1", timeout=30)

    @patch(
        "app.api.projects.start_job",
        side_effect=MagicMock(
            side_effect=__import__(
                "app.services.troshkad_client", fromlist=["TroshkadError"]
            ).TroshkadError("fail")
        ),
    )
    def test_swallows_troshkad_error(self, mock_start):
        from app.api.projects import _create_bmc_bridge_if_needed
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("fail")
        bmc_config = {
            "bmc_network": {"cidr": "192.168.100.0/24"},
            "vms": [{"bmc_ip": "192.168.100.10"}],
        }
        # Should not raise
        _create_bmc_bridge_if_needed(MagicMock(), "p1", {}, bmc_config)


# ---------------------------------------------------------------------------
# _remove_vms_from_reconfigure
# ---------------------------------------------------------------------------


class TestRemoveVmsFromReconfigure:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-1")
    @patch("app.api.projects.troshkad_undefine_vm")
    def test_undefines_and_cleans_files(self, mock_undef, mock_start, mock_wait):
        from app.api.projects import _remove_vms_from_reconfigure

        with patch(
            "app.services.deploy_topology._vm_domain_name", return_value="dom-1"
        ):
            diff = {"removed_vms": [{"id": "vm-12345678"}]}
            _remove_vms_from_reconfigure(
                MagicMock(), "p1", diff, "/var/lib/troshka/vms/p1"
            )

        mock_undef.assert_called_once()
        mock_start.assert_called_once()

    @patch("app.api.projects.troshkad_undefine_vm")
    def test_empty_removed_vms_noop(self, mock_undef):
        from app.api.projects import _remove_vms_from_reconfigure

        _remove_vms_from_reconfigure(MagicMock(), "p1", {"removed_vms": []}, "/fake")
        mock_undef.assert_not_called()


# ---------------------------------------------------------------------------
# _get_storage_pool_for_host
# ---------------------------------------------------------------------------


class TestGetStoragePoolForHost:
    def test_no_pool_returns_none(self):
        from app.api.projects import _get_storage_pool_for_host

        h = MagicMock()
        h.storage_pool_id = None
        assert _get_storage_pool_for_host(h, MagicMock()) is None

    def test_with_pool_queries_db(self):
        from app.api.projects import _get_storage_pool_for_host

        h = MagicMock()
        h.storage_pool_id = "pool-1"
        s = MagicMock()
        pool_mock = MagicMock()
        s.query.return_value.filter_by.return_value.first.return_value = pool_mock
        result = _get_storage_pool_for_host(h, s)
        assert result == pool_mock


# ---------------------------------------------------------------------------
# _reconfigure_bmc
# ---------------------------------------------------------------------------


class TestReconfigureBmc:
    @patch("app.services.deploy_service._setup_bmc_via_troshkad", return_value=True)
    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_teardown_then_setup(self, mock_teardown, mock_setup):
        from app.api.projects import _reconfigure_bmc

        deployed = {
            "nodes": [
                {"type": "networkNode", "data": {"networkType": "bmc"}},
            ]
        }
        bmc_config = {"bmc_network": {}}
        errors = []
        _reconfigure_bmc(MagicMock(), "p1", deployed, bmc_config, errors)
        mock_teardown.assert_called_once()
        mock_setup.assert_called_once()
        assert len(errors) == 0

    @patch("app.services.deploy_service._teardown_bmc_via_troshkad")
    def test_no_bmc_in_deployed_skips_teardown(self, mock_teardown):
        from app.api.projects import _reconfigure_bmc

        deployed = {"nodes": []}
        errors = []
        _reconfigure_bmc(MagicMock(), "p1", deployed, None, errors)
        mock_teardown.assert_not_called()

    @patch(
        "app.services.deploy_service._setup_bmc_via_troshkad",
        return_value="BMC setup error msg",
    )
    def test_setup_failure_appends_error(self, mock_setup):
        from app.api.projects import _reconfigure_bmc

        deployed = {"nodes": []}
        bmc_config = {"bmc_network": {}}
        errors = []
        _reconfigure_bmc(MagicMock(), "p1", deployed, bmc_config, errors)
        assert len(errors) == 1
        assert "BMC setup failed" in errors[0]


# ---------------------------------------------------------------------------
# _finalize_kubevirt_reconfigure
# ---------------------------------------------------------------------------


class TestFinalizeKubevirtReconfigure:
    def test_cleans_topo_and_commits(self):
        import copy

        from app.api.projects import _finalize_kubevirt_reconfigure

        proj = MagicMock()
        s = MagicMock()
        mock_notify = MagicMock()
        current = {
            "nodes": [
                {
                    "data": {
                        "resolvedS3Path": "s3://bucket/key",
                        "presignedUrl": "https://...",
                        "ciGeneratedUserData": "#cloud-config...",
                        "name": "bastion",
                    }
                }
            ]
        }
        _finalize_kubevirt_reconfigure(proj, s, "p1", current, copy, mock_notify)

        assert proj.state == "active"
        assert proj.deploy_error is None
        s.commit.assert_called_once()
        mock_notify.assert_called_once()
        # Verify sensitive fields cleaned from deployed topology
        deployed = proj.deployed_topology
        node_data = deployed["nodes"][0]["data"]
        assert "resolvedS3Path" not in node_data
        assert "presignedUrl" not in node_data
        assert "ciGeneratedUserData" not in node_data
        assert node_data["name"] == "bastion"


# ---------------------------------------------------------------------------
# _build_kubevirt_vm_spec
# ---------------------------------------------------------------------------


class TestBuildKubevirtVmSpec:
    @patch("app.services.deploy_topology._find_vm_disks")
    def test_builds_spec_with_blank_disk(self, mock_find_disks):
        from app.api.projects import _build_kubevirt_vm_spec

        mock_find_disks.return_value = [
            {"node_id": "d1", "id": "d1", "size": 20, "format": "qcow2"}
        ]
        current = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm1",
                        "name": "bastion",
                        "vcpus": 4,
                        "ram": 8,
                        "firmware": "uefi",
                        "nics": [{"id": "nic1", "mac": "aa:bb:cc:dd:ee:ff"}],
                        "bootDevices": ["d1"],
                    },
                }
            ]
        }
        vm = {"name": "bastion", "vcpus": 4, "ram_gb": 8, "firmware": "uefi"}
        result = _build_kubevirt_vm_spec("vm1", vm, current)
        assert result["name"] == "bastion"
        assert result["cpus"] == 4
        assert result["memory"] == 8192
        assert result["firmware"] == "uefi"
        assert len(result["disks"]) == 1
        assert result["disks"][0]["blank"] is True

    @patch("app.services.deploy_topology._find_vm_disks")
    def test_pattern_disk_has_pattern_image(self, mock_find_disks):
        from app.api.projects import _build_kubevirt_vm_spec

        mock_find_disks.return_value = [
            {
                "node_id": "d1",
                "id": "d1",
                "size": 50,
                "format": "qcow2",
                "source": "pattern",
                "patternId": "pat-1",
                "resolvedS3Path": "patterns/pat-1/d1.qcow2",
            }
        ]
        current = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}}]}
        result = _build_kubevirt_vm_spec(
            "vm1", {"name": "vm", "vcpus": 2, "ram_gb": 4}, current
        )
        assert "patternImage" in result["disks"][0]
        assert result["disks"][0]["patternImage"]["s3Path"] == "patterns/pat-1/d1.qcow2"

    @patch("app.services.deploy_topology._find_vm_disks")
    def test_library_disk_has_library_image(self, mock_find_disks):
        from app.api.projects import _build_kubevirt_vm_spec

        mock_find_disks.return_value = [
            {
                "node_id": "d1",
                "id": "d1",
                "size": 50,
                "format": "qcow2",
                "source": "library",
                "libraryItemId": "lib-1",
                "resolvedS3Path": "library/lib-1.qcow2",
            }
        ]
        current = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"id": "vm1"}}]}
        result = _build_kubevirt_vm_spec(
            "vm1", {"name": "vm", "vcpus": 2, "ram_gb": 4}, current
        )
        assert "libraryImage" in result["disks"][0]


# ---------------------------------------------------------------------------
# _start_vm_if_needed
# ---------------------------------------------------------------------------


class TestStartVmIfNeeded:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    def test_starts_when_was_running(self, mock_start, mock_wait):
        from app.api.projects import _start_vm_if_needed

        vm_node = {"data": {"powerOnAtDeploy": False}}
        _start_vm_if_needed(MagicMock(), "dom-1", True, vm_node)
        mock_start.assert_called_once()

    @patch("app.api.projects.start_job")
    def test_skips_when_not_running_and_no_power_on(self, mock_start):
        from app.api.projects import _start_vm_if_needed

        vm_node = {"data": {"powerOnAtDeploy": False}}
        _start_vm_if_needed(MagicMock(), "dom-1", False, vm_node)
        mock_start.assert_not_called()

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    def test_starts_when_power_on_at_deploy(self, mock_start, mock_wait):
        from app.api.projects import _start_vm_if_needed

        vm_node = {"data": {}}  # powerOnAtDeploy defaults True
        _start_vm_if_needed(MagicMock(), "dom-1", False, vm_node)
        mock_start.assert_called_once()

    @patch("app.api.projects.start_job")
    def test_swallows_troshkad_error(self, mock_start):
        from app.api.projects import _start_vm_if_needed
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")
        vm_node = {"data": {"powerOnAtDeploy": True}}
        # Should not raise
        _start_vm_if_needed(MagicMock(), "dom-1", True, vm_node)


# ---------------------------------------------------------------------------
# _find_vm_node_in_topology
# ---------------------------------------------------------------------------


class TestFindVmNodeInTopology:
    def test_finds_node(self):
        from app.api.projects import _find_vm_node_in_topology

        topo = {"nodes": [{"id": "vm1", "type": "vmNode", "data": {"name": "b"}}]}
        result = _find_vm_node_in_topology(topo, "vm1")
        assert result is not None
        assert result["data"]["name"] == "b"

    def test_returns_none_not_found(self):
        from app.api.projects import _find_vm_node_in_topology

        topo = {"nodes": [{"id": "vm2", "type": "vmNode"}]}
        assert _find_vm_node_in_topology(topo, "vm1") is None

    def test_ignores_non_vm_nodes(self):
        from app.api.projects import _find_vm_node_in_topology

        topo = {"nodes": [{"id": "n1", "type": "networkNode"}]}
        assert _find_vm_node_in_topology(topo, "n1") is None


# ---------------------------------------------------------------------------
# _build_connected_topology
# ---------------------------------------------------------------------------


class TestBuildConnectedTopology:
    def test_includes_connected_nodes(self):
        from app.api.projects import _build_connected_topology

        topo = {
            "nodes": [
                {"id": "vm1", "type": "vmNode", "data": {}},
                {"id": "net1", "type": "networkNode", "data": {}},
                {"id": "disk1", "type": "storageNode", "data": {}},
                {"id": "vm2", "type": "vmNode", "data": {}},
            ],
            "edges": [
                {"source": "vm1", "target": "net1"},
                {"source": "vm1", "target": "disk1"},
            ],
        }
        result = _build_connected_topology(topo, "vm1")
        node_ids = {n["id"] for n in result["nodes"]}
        # _build_connected_topology returns only connected nodes, not the VM itself
        assert "net1" in node_ids
        assert "disk1" in node_ids
        assert "vm2" not in node_ids

    def test_empty_topology(self):
        from app.api.projects import _build_connected_topology

        result = _build_connected_topology({"nodes": [], "edges": []}, "vm1")
        assert result["nodes"] == []


# ---------------------------------------------------------------------------
# _build_redeploy_vm_data
# ---------------------------------------------------------------------------


class TestBuildRedeployVmData:
    def test_extracts_vm_data(self):
        from app.api.projects import _build_redeploy_vm_data

        vm_node = {
            "id": "vm1",
            "data": {
                "name": "bastion",
                "vcpus": 4,
                "ram": 16,
                "cloudInit": True,
                "bootDevices": ["d1"],
                "firmware": "uefi",
                "secureBoot": True,
            },
        }
        result = _build_redeploy_vm_data(vm_node)
        assert result["node_id"] == "vm1"
        assert result["name"] == "bastion"
        assert result["vcpus"] == 4
        assert result["ram_gb"] == 16
        assert result["cloud_init"] is True
        assert result["firmware"] == "uefi"
        assert result["secure_boot"] is True

    def test_defaults(self):
        from app.api.projects import _build_redeploy_vm_data

        vm_node = {"id": "vm2", "data": {}}
        result = _build_redeploy_vm_data(vm_node)
        assert result["vcpus"] == 2
        assert result["ram_gb"] == 4
        assert result["firmware"] == "bios"
        assert result["secure_boot"] is False


# ---------------------------------------------------------------------------
# _apply_disk_changes
# ---------------------------------------------------------------------------


class TestApplyDiskChanges:
    @patch("app.api.projects.cache_library_images")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_creates_disks(self, mock_set, mock_start, mock_wait, mock_cache):
        from app.api.projects import _apply_disk_changes

        changes = {
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [
                {
                    "path": "/vms/p1/d1.qcow2",
                    "size_gb": 20,
                    "format": "qcow2",
                    "backing_file": None,
                }
            ],
            "disks_to_resize": [],
        }
        _apply_disk_changes(MagicMock(), "p1", MagicMock(), {}, changes)
        assert mock_start.call_count == 1

    @patch("app.api.projects.cache_library_images")
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_removes_files_creates_and_resizes(
        self, mock_set, mock_start, mock_wait, mock_cache
    ):
        from app.api.projects import _apply_disk_changes

        changes = {
            "needs_library_download": True,
            "files_to_remove": ["/old/disk.qcow2"],
            "disks_to_create": [
                {
                    "path": "/new/d1.qcow2",
                    "size_gb": 20,
                    "format": "qcow2",
                    "backing_file": "/cache/img.qcow2",
                }
            ],
            "disks_to_resize": [{"path": "/new/d2.qcow2", "new_size_gb": 40}],
        }
        _apply_disk_changes(MagicMock(), "p1", MagicMock(), {}, changes)
        # remove + create + resize = 3 start_job calls
        assert mock_start.call_count == 3
        mock_cache.assert_called_once()
