"""Tests for uncovered lines in app.api.projects — reconfigure helpers,
disk-change detection, deploy-related helpers, and kubevirt reconfigure functions.
"""

from unittest.mock import ANY, MagicMock, patch

import pytest
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

    def test_ignores_runtime_status_field(self):
        from app.api.projects import _find_changed_kubevirt_vms

        current = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"vcpus": 2, "status": "running"},
                }
            ]
        }
        deployed = {
            "nodes": [
                {
                    "id": "vm1",
                    "type": "vmNode",
                    "data": {"vcpus": 2, "status": "stopped"},
                }
            ]
        }
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
    def test_propagates_troshkad_error(self, mock_start):
        from app.api.projects import _start_vm_if_needed
        from app.services.troshkad_client import TroshkadError

        mock_start.side_effect = TroshkadError("connection refused")
        vm_node = {"data": {"powerOnAtDeploy": True}}
        # Start failures must propagate so a redeploy fails visibly.
        with pytest.raises(TroshkadError):
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


# ---------------------------------------------------------------------------
# _reconfigure_process_vms  (Priority 1)
# ---------------------------------------------------------------------------


class TestReconfigureProcessVms:
    @patch("app.api.projects._deploy_added_vms")
    @patch("app.api.projects._reconfigure_existing_vm")
    @patch("app.api.projects._extract_vms")
    def test_processes_existing_and_skips_added_removed(
        self, mock_extract, mock_reconfig, mock_deploy
    ):
        from app.api.projects import _reconfigure_process_vms

        mock_extract.return_value = [
            {"node_id": "vm1", "name": "existing"},
            {"node_id": "vm2", "name": "added"},
            {"node_id": "vm3", "name": "removed"},
        ]
        diff = {
            "added_vms": [{"id": "vm2"}],
            "removed_vms": [{"id": "vm3"}],
        }
        errors = []
        _reconfigure_process_vms(
            MagicMock(),
            "p1",
            MagicMock(),
            MagicMock(),
            {},
            {},
            {},
            set(),
            None,
            diff,
            errors,
        )
        # Only vm1 should be processed (not vm2=added, vm3=removed)
        mock_reconfig.assert_called_once()
        assert mock_reconfig.call_args[0][6] == {}  # vni_map
        mock_deploy.assert_called_once()

    @patch("app.api.projects._deploy_added_vms")
    @patch("app.api.projects._redeploy_vm_during_reconfigure")
    @patch("app.api.projects._reconfigure_existing_vm")
    @patch("app.api.projects._extract_vms")
    @patch(
        "app.services.deploy_topology.vm_ids_needing_redeploy",
        return_value={"vm-fw"},
    )
    def test_domain_defining_change_triggers_redeploy(
        self, mock_fw_ids, mock_extract, mock_reconfig, mock_redeploy, mock_deploy
    ):
        from app.api.projects import _reconfigure_process_vms

        mock_extract.return_value = [
            {"node_id": "vm-fw", "name": "fw-change"},
            {"node_id": "vm-ok", "name": "unchanged"},
        ]
        diff = {"added_vms": [], "removed_vms": []}
        errors = []
        proj = MagicMock()
        _reconfigure_process_vms(
            MagicMock(),
            "p1",
            MagicMock(),
            proj,
            {"nodes": []},
            {"nodes": []},
            {},
            set(),
            None,
            diff,
            errors,
        )
        mock_redeploy.assert_called_once()
        assert mock_redeploy.call_args[0][4] == "vm-fw"
        mock_reconfig.assert_called_once()
        assert mock_reconfig.call_args[0][5]["node_id"] == "vm-ok"
        mock_deploy.assert_not_called()

    @patch("app.api.projects._deploy_added_vms")
    @patch("app.api.projects._reconfigure_existing_vm")
    @patch("app.api.projects._extract_vms")
    def test_no_added_vms_skips_deploy(self, mock_extract, mock_reconfig, mock_deploy):
        from app.api.projects import _reconfigure_process_vms

        mock_extract.return_value = [{"node_id": "vm1", "name": "existing"}]
        diff = {"added_vms": [], "removed_vms": []}
        errors = []
        _reconfigure_process_vms(
            MagicMock(),
            "p1",
            MagicMock(),
            MagicMock(),
            {},
            {},
            {},
            set(),
            None,
            diff,
            errors,
        )
        mock_reconfig.assert_called_once()
        mock_deploy.assert_not_called()


# ---------------------------------------------------------------------------
# _deploy_added_vms  (lines 2945-2984)
# ---------------------------------------------------------------------------


class TestDeployAddedVms:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    @patch("app.api.projects._create_vm_via_troshkad")
    @patch("app.api.projects._create_vm_disks_via_troshkad")
    @patch("app.api.projects._create_seed_isos_via_troshkad")
    @patch("app.api.projects.cache_library_images")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_deploys_added_vms(
        self,
        mock_set,
        mock_find,
        mock_cache,
        mock_seed,
        mock_disks,
        mock_create,
        mock_start,
        mock_wait,
    ):
        from app.api.projects import _deploy_added_vms

        added = [
            {
                "id": "new-vm-1",
                "data": {"name": "newvm", "vcpus": 4, "ram": 8},
            }
        ]
        current = {"startOrder": []}
        errors = []
        with patch(
            "app.services.deploy_topology._vm_domain_name", return_value="dom-new"
        ):
            _deploy_added_vms(
                MagicMock(), "p1", MagicMock(), current, {}, added, errors
            )

        mock_cache.assert_called_once()
        mock_seed.assert_called_once()
        mock_disks.assert_called_once()
        mock_create.assert_called_once()
        mock_start.assert_called_once()
        assert len(errors) == 0

    @patch("app.api.projects._create_vm_via_troshkad")
    @patch("app.api.projects._create_vm_disks_via_troshkad")
    @patch("app.api.projects._create_seed_isos_via_troshkad")
    @patch("app.api.projects.cache_library_images")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_auto_start_disabled_skips_start(
        self, mock_set, mock_find, mock_cache, mock_seed, mock_disks, mock_create
    ):
        from app.api.projects import _deploy_added_vms

        added = [{"id": "vm-nostart", "data": {"name": "ns"}}]
        current = {"startOrder": [{"vmId": "vm-nostart", "autoStart": False}]}
        errors = []
        with patch("app.api.projects.start_job") as mock_start:
            _deploy_added_vms(
                MagicMock(), "p1", MagicMock(), current, {}, added, errors
            )
            mock_start.assert_not_called()

    @patch("app.api.projects._create_vm_via_troshkad")
    @patch(
        "app.api.projects._create_vm_disks_via_troshkad",
        side_effect=RuntimeError("disk fail"),
    )
    @patch("app.api.projects._create_seed_isos_via_troshkad")
    @patch("app.api.projects.cache_library_images")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_troshkad_error_appends_to_errors(
        self, mock_set, mock_find, mock_cache, mock_seed, mock_disks, mock_create
    ):
        from app.api.projects import _deploy_added_vms

        added = [{"id": "vm-fail", "data": {"name": "fail"}}]
        errors = []
        _deploy_added_vms(
            MagicMock(), "p1", MagicMock(), {"startOrder": []}, {}, added, errors
        )
        assert len(errors) == 1
        assert "vm-fail" in errors[0]


# ---------------------------------------------------------------------------
# _apply_kubevirt_vm_changes  (lines 2596-2636)
# ---------------------------------------------------------------------------


class TestApplyKubevirtVmChanges:
    def test_deletes_removed_vms(self):
        from app.api.projects import _apply_kubevirt_vm_changes

        custom_api = MagicMock()
        diff = {"removed_vms": ["vm-abcdefgh"]}
        _apply_kubevirt_vm_changes(custom_api, "ns1", "p1234567", diff, [], {}, {})
        custom_api.delete_namespaced_custom_object.assert_called_once()
        call_kwargs = custom_api.delete_namespaced_custom_object.call_args
        assert call_kwargs[1]["name"] == "vm-vm-abcde"

    def test_patches_changed_vms(self):
        from app.api.projects import _apply_kubevirt_vm_changes

        custom_api = MagicMock()
        existing_cr = {"spec": {"old": True}, "metadata": {}}
        custom_api.get_namespaced_custom_object.return_value = existing_cr

        diff = {"removed_vms": []}
        changed_ids = ["vm-changed1"]
        current_vms = {"vm-changed1": {"name": "bastion", "vcpus": 4, "ram_gb": 8}}
        current = {
            "nodes": [
                {
                    "id": "vm-changed1",
                    "type": "vmNode",
                    "data": {
                        "id": "vm-changed1",
                        "nics": [],
                        "bootDevices": [],
                    },
                }
            ]
        }

        with patch("app.api.projects._build_kubevirt_vm_spec") as mock_spec:
            mock_spec.return_value = {"name": "bastion", "cpus": 4}
            _apply_kubevirt_vm_changes(
                custom_api,
                "ns1",
                "p1234567",
                diff,
                changed_ids,
                current_vms,
                current,
            )

        custom_api.replace_namespaced_custom_object.assert_called_once()

    def test_delete_exception_ignored(self):
        from app.api.projects import _apply_kubevirt_vm_changes

        custom_api = MagicMock()
        custom_api.delete_namespaced_custom_object.side_effect = Exception("404")
        diff = {"removed_vms": ["vm-xx"]}
        # Should not raise
        _apply_kubevirt_vm_changes(custom_api, "ns", "p1", diff, [], {}, {})

    def test_patch_exception_logged(self):
        from app.api.projects import _apply_kubevirt_vm_changes

        custom_api = MagicMock()
        custom_api.get_namespaced_custom_object.side_effect = Exception("not found")
        diff = {"removed_vms": []}
        current_vms = {"vm-err": {"name": "x", "vcpus": 2, "ram_gb": 4}}
        current = {
            "nodes": [
                {
                    "id": "vm-err",
                    "type": "vmNode",
                    "data": {"id": "vm-err", "nics": [], "bootDevices": []},
                }
            ]
        }
        with patch("app.api.projects._build_kubevirt_vm_spec", return_value={}):
            _apply_kubevirt_vm_changes(
                custom_api, "ns", "p1", diff, ["vm-err"], current_vms, current
            )
        custom_api.replace_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# _do_reconfigure_kubevirt  (lines 2658-2750)
# ---------------------------------------------------------------------------


class TestDoReconfigureKubevirt:
    @patch("app.services.deploy_service._resolve_disk_s3_paths")
    @patch(
        "app.services.deploy_service._setup_kubevirt_s3_clients",
        return_value=(None, None, MagicMock(), "bucket", {}, None, "", {}),
    )
    @patch("app.api.projects._finalize_kubevirt_reconfigure")
    @patch("app.api.projects._sync_eips_for_reconfigure")
    @patch("app.api.projects._wait_kubevirt_vms_ready", return_value=None)
    @patch("app.api.projects._apply_kubevirt_vm_changes")
    @patch("app.api.projects._find_changed_kubevirt_vms", return_value=[])
    def test_happy_path(
        self,
        mock_find,
        mock_apply,
        mock_wait,
        mock_eips,
        mock_finalize,
        mock_s3,
        mock_resolve,
    ):
        from app.api.projects import _do_reconfigure_kubevirt

        mock_session = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "p1"
        mock_host = MagicMock()
        mock_host.id = "h1"
        mock_host.provider_id = "prov1"
        mock_provider = MagicMock()

        call_count = {"n": 0}
        results = [mock_proj, mock_host, mock_provider]

        def _fake_first():
            idx = call_count["n"]
            call_count["n"] += 1
            return results[idx] if idx < len(results) else MagicMock()

        mock_session.query.return_value.filter_by.return_value.first = _fake_first

        current = {"nodes": []}
        deployed = {"nodes": []}

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.providers.kubevirt._get_k8s_clients",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ):
                with patch(
                    "app.services.providers.kubevirt._project_ns",
                    return_value="troshka-p1",
                ):
                    with patch(
                        "app.services.deploy_topology.diff_topologies",
                        return_value={
                            "added_vms": [],
                            "removed_vms": [],
                            "changed_vms": [],
                            "has_changes": False,
                        },
                    ):
                        with patch(
                            "app.services.deploy_topology._extract_vms",
                            return_value=[],
                        ):
                            with patch(
                                "app.services.deploy_service._set_deploy_progress"
                            ):
                                with patch(
                                    "app.services.deploy_service._delete_deploy_progress"
                                ):
                                    with patch(
                                        "app.services.deploy_service._patch_kubevirt_gateway_forwards"
                                    ):
                                        _do_reconfigure_kubevirt(
                                            "p1", "h1", current, deployed
                                        )

        mock_finalize.assert_called_once()

    def test_no_project_returns_early(self):
        from app.api.projects import _do_reconfigure_kubevirt

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _do_reconfigure_kubevirt("p1", "h1", {}, {})
        mock_session.commit.assert_not_called()

    def test_no_provider_sets_error(self):
        from app.api.projects import _do_reconfigure_kubevirt

        mock_session = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "p1"
        mock_host = MagicMock()
        mock_host.provider_id = "prov1"

        call_count = {"n": 0}
        results = [mock_proj, mock_host, None]

        def _fake_first():
            idx = call_count["n"]
            call_count["n"] += 1
            return results[idx] if idx < len(results) else None

        mock_session.query.return_value.filter_by.return_value.first = _fake_first

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _do_reconfigure_kubevirt("p1", "h1", {}, {})

        assert mock_proj.state == "error"
        assert "No provider" in mock_proj.deploy_error
        mock_session.commit.assert_called()

    def test_wait_error_returns_early(self):
        from app.api.projects import _do_reconfigure_kubevirt

        mock_session = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "p1"
        mock_host = MagicMock()
        mock_host.id = "h1"
        mock_host.provider_id = "prov1"
        mock_provider = MagicMock()

        call_count = {"n": 0}
        results = [mock_proj, mock_host, mock_provider]

        def _fake_first():
            idx = call_count["n"]
            call_count["n"] += 1
            return results[idx] if idx < len(results) else MagicMock()

        mock_session.query.return_value.filter_by.return_value.first = _fake_first

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.providers.kubevirt._get_k8s_clients",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ):
                with patch(
                    "app.services.providers.kubevirt._project_ns",
                    return_value="ns",
                ):
                    with patch(
                        "app.services.deploy_topology.diff_topologies",
                        return_value={
                            "added_vms": [],
                            "removed_vms": [],
                            "changed_vms": [],
                            "has_changes": False,
                        },
                    ):
                        with patch(
                            "app.services.deploy_topology._extract_vms",
                            return_value=[],
                        ):
                            with patch(
                                "app.services.deploy_service._set_deploy_progress"
                            ):
                                with patch(
                                    "app.services.deploy_service._delete_deploy_progress"
                                ):
                                    with patch(
                                        "app.api.projects._apply_kubevirt_vm_changes"
                                    ):
                                        with patch(
                                            "app.api.projects._find_changed_kubevirt_vms",
                                            return_value=[],
                                        ):
                                            with patch(
                                                "app.api.projects._wait_kubevirt_vms_ready",
                                                return_value="vm_error",
                                            ):
                                                with patch(
                                                    "app.api.projects._sync_eips_for_reconfigure"
                                                ) as mock_eips:
                                                    _do_reconfigure_kubevirt(
                                                        "p1", "h1", {}, {}
                                                    )
                                                    mock_eips.assert_not_called()


# ---------------------------------------------------------------------------
# _sync_eips_for_reconfigure  (lines 2851-2914)
# ---------------------------------------------------------------------------


class TestSyncEipsForReconfigure:
    def test_no_external_ips_returns_early(self):
        from app.api.projects import _sync_eips_for_reconfigure

        errors = []
        _sync_eips_for_reconfigure(
            MagicMock(), MagicMock(), MagicMock(), "p1", {}, errors
        )
        assert len(errors) == 0

    @patch("app.api.projects._sync_transit_ports")
    @patch("app.services.eip_service.sync_security_group_rules")
    @patch("app.services.eip_service.associate_eip")
    @patch("app.services.eip_service.allocate_eip")
    def test_allocates_and_associates_eips(
        self, mock_alloc, mock_assoc, mock_sync, mock_transit
    ):
        from app.api.projects import _sync_eips_for_reconfigure

        mock_eip = MagicMock()
        mock_eip.state = "allocated"
        mock_eip.public_ip = "1.2.3.4"
        mock_eip.private_ip = "10.0.0.5"
        mock_eip.canvas_eip_id = "eip1"
        mock_eip.port_map = None
        mock_alloc.return_value = mock_eip

        s = MagicMock()
        mock_provider = MagicMock(id="prov1")
        # Use a function that returns what we need based on call context
        s.query.return_value.filter_by.return_value.first.return_value = None

        proj = MagicMock()
        proj.provider_id = "prov1"
        h = MagicMock()
        h.provider_id = "prov1"

        current = {
            "externalIps": [{"id": "eip1"}],
        }
        errors = []

        # Patch the Provider query to return a provider
        with patch("app.api.projects._find_gateway_node", return_value=None):
            with patch("app.api.projects._sync_eips_for_reconfigure.__module__"):
                pass
            # Rather than fighting the mock chain, patch allocate_eip to
            # handle the "no existing" case: allocate_eip returns mock_eip
            # and the provider query is also mocked. We set first() to None
            # (for the ElasticIp query), then mock the Provider query
            # separately.
            # Actually, let's just use a simpler approach: override s.query
            # to return different things based on the model argument.

            eip_filter = MagicMock()
            eip_filter.first.return_value = None  # no existing EIP
            eip_filter.all.return_value = [mock_eip]

            provider_filter = MagicMock()
            provider_filter.first.return_value = mock_provider

            def _query_dispatch(model):
                m = MagicMock()
                model_name = getattr(model, "__name__", str(model))
                if "ElasticIp" in model_name:
                    m.filter_by.return_value = eip_filter
                elif "Provider" in model_name:
                    m.filter_by.return_value = provider_filter
                return m

            s.query.side_effect = _query_dispatch

            _sync_eips_for_reconfigure(s, proj, h, "p1", current, errors)

        mock_alloc.assert_called_once()
        mock_assoc.assert_called_once()
        assert current["externalIps"][0]["ip"] == "1.2.3.4"

    def test_exception_appends_error(self):
        from app.api.projects import _sync_eips_for_reconfigure

        s = MagicMock()
        s.query.side_effect = Exception("db error")
        proj = MagicMock()
        proj.provider_id = "prov1"
        errors = []
        current = {"externalIps": [{"id": "eip1"}]}
        _sync_eips_for_reconfigure(s, proj, MagicMock(), "p1", current, errors)
        assert len(errors) == 1
        assert "EIP" in errors[0]


# ---------------------------------------------------------------------------
# _apply_eip_runtime_to_topology
# ---------------------------------------------------------------------------


class TestApplyEipRuntimeToTopology:
    def test_copies_transit_port_map_from_db(self):
        from app.api.projects import _apply_eip_runtime_to_topology

        eip = MagicMock()
        eip.canvas_eip_id = "eip1"
        eip.public_ip = "150.240.37.146"
        eip.private_ip = None
        eip.port_map = {"443": 40000, "2022": 40001}

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        external_ips = [{"id": "eip1", "name": "IP-1"}]
        _apply_eip_runtime_to_topology(s, "project1", external_ips)

        assert external_ips[0]["ip"] == "150.240.37.146"
        assert external_ips[0]["_private_ip"] is None
        assert external_ips[0]["_transit_port_map"] == {"443": 40000, "2022": 40001}

    def test_clears_stale_transit_port_map(self):
        from app.api.projects import _apply_eip_runtime_to_topology

        eip = MagicMock()
        eip.canvas_eip_id = "eip1"
        eip.public_ip = "1.2.3.4"
        eip.private_ip = "10.0.0.5"
        eip.port_map = None

        s = MagicMock()
        s.query.return_value.filter_by.return_value.all.return_value = [eip]

        external_ips = [{"id": "eip1", "_transit_port_map": {"443": 40000}}]
        _apply_eip_runtime_to_topology(s, "project1", external_ips)

        assert external_ips[0]["_private_ip"] == "10.0.0.5"
        assert "_transit_port_map" not in external_ips[0]


# ---------------------------------------------------------------------------
# _sync_transit_ports  (lines 2786-2842)
# ---------------------------------------------------------------------------


class TestSyncTransitPorts:
    @patch("app.services.providers.get_provider_driver")
    def test_kubevirt_direct_update(self, mock_driver_fn):
        from app.api.projects import _sync_transit_ports

        driver = MagicMock()
        mock_driver_fn.return_value = driver

        provider = MagicMock()
        provider.type = "kubevirt"
        h = MagicMock()
        s = MagicMock()
        eip = MagicMock()
        eip.canvas_eip_id = "eip1"
        eip.allocation_id = "alloc1"
        s.query.return_value.filter_by.return_value = [eip]

        gw_node = {
            "data": {
                "portForwards": [
                    # 443 is served by an OpenShift Route — must NOT reach the EIP LB
                    {
                        "extIpId": "eip1",
                        "extPort": "443",
                        "intPort": "80",
                        "proto": "tcp",
                    },
                    {"extIpId": "eip1", "extPort": "9090", "proto": "tcp"},
                ]
            }
        }
        with patch("app.services.providers.kubevirt._project_ns", return_value="ns1"):
            _sync_transit_ports(s, provider, h, "p1234567", gw_node)

        driver.update_eip_ports.assert_called_once()
        call_kwargs = driver.update_eip_ports.call_args
        assert call_kwargs[1]["namespace"] == "ns1"
        # only the non-web port (9090) is bound to the EIP; 443 goes via a Route
        eip_ports = {p["port"] for p in call_kwargs[0][3]}
        assert eip_ports == {9090}

    @patch(
        "app.services.eip_service.allocate_transit_ports", return_value={"8443": 443}
    )
    @patch("app.services.providers.get_provider_driver")
    def test_non_kubevirt_allocates_transit(self, mock_driver_fn, mock_alloc):
        from app.api.projects import _sync_transit_ports

        driver = MagicMock()
        mock_driver_fn.return_value = driver

        provider = MagicMock()
        provider.type = "gcp"
        h = MagicMock()
        s = MagicMock()
        eip = MagicMock()
        eip.canvas_eip_id = "eip1"
        eip.allocation_id = "alloc1"
        s.query.return_value.filter_by.return_value = [eip]

        # Cloud providers have no ingress: 443 stays bound to the EIP LB.
        gw_node = {"data": {"portForwards": [{"extIpId": "eip1", "extPort": "443"}]}}
        _sync_transit_ports(s, provider, h, "p1234567", gw_node)

        mock_alloc.assert_called_once()
        driver.update_eip_ports.assert_called_once()

    @patch("app.services.providers.get_provider_driver")
    def test_no_port_forwards_for_eip_skips(self, mock_driver_fn):
        from app.api.projects import _sync_transit_ports

        driver = MagicMock()
        mock_driver_fn.return_value = driver

        provider = MagicMock()
        provider.type = "ec2"
        s = MagicMock()
        eip = MagicMock()
        eip.canvas_eip_id = "eip1"
        s.query.return_value.filter_by.return_value = [eip]

        gw_node = {"data": {"portForwards": [{"extIpId": "other-eip"}]}}
        _sync_transit_ports(s, provider, MagicMock(), "p1", gw_node)
        driver.update_eip_ports.assert_not_called()


# ---------------------------------------------------------------------------
# _finalize_reconfigure  (lines 3270-3317)
# ---------------------------------------------------------------------------


class TestFinalizeReconfigure:
    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.placement.sync_host_capacity")
    @patch("app.api.projects._reconfigure_bmc")
    def test_no_errors_sets_deployed_topology(
        self, mock_bmc, mock_sync, mock_del, mock_notify, mock_broadcast
    ):
        from app.api.projects import _finalize_reconfigure

        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": [{"type": "vmNode", "data": {"name": "vm1"}}]}
        s.refresh = lambda p: None
        h = MagicMock()

        with patch(
            "app.services.deploy_topology._extract_bmc_config", return_value=None
        ):
            _finalize_reconfigure(s, proj, h, "p1234567", {"nodes": []}, {}, [])

        assert proj.state == "active"
        assert proj.deploy_error is None
        s.commit.assert_called()

    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.placement.sync_host_capacity")
    @patch("app.api.projects._reconfigure_bmc")
    def test_errors_set_deploy_error(
        self, mock_bmc, mock_sync, mock_del, mock_notify, mock_broadcast
    ):
        from app.api.projects import _finalize_reconfigure

        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": []}
        s.refresh = lambda p: None

        with patch(
            "app.services.deploy_topology._extract_bmc_config", return_value=None
        ):
            _finalize_reconfigure(
                s, proj, MagicMock(), "p1", {}, {}, ["error1", "error2"]
            )

        assert proj.state == "active"
        assert "error1" in proj.deploy_error
        assert "error2" in proj.deploy_error

    @patch("app.api.projects._broadcast_vm_states")
    @patch("app.services.ws_pubsub.notify_project")
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.placement.sync_host_capacity")
    @patch("app.api.projects._reconfigure_bmc")
    def test_bmc_config_included_in_deployed_topology(
        self, mock_bmc, mock_sync, mock_del, mock_notify, mock_broadcast
    ):
        from app.api.projects import _finalize_reconfigure

        s = MagicMock()
        proj = MagicMock()
        proj.topology = {"nodes": []}
        s.refresh = lambda p: None
        bmc_conf = {
            "bmc_network": {"bmcUsername": "admin", "bmcPassword": "secret"},
            "vms": [
                {
                    "node_id": "vm1",
                    "bmc_ip": "192.168.100.10",
                    "domain_name": "dom1",
                }
            ],
        }

        with patch(
            "app.services.deploy_topology._extract_bmc_config",
            return_value=bmc_conf,
        ):
            _finalize_reconfigure(
                s, proj, MagicMock(), "p1234567", {"nodes": []}, {}, []
            )

        deployed = proj.deployed_topology
        assert "bmc" in deployed
        assert "vm1" in deployed["bmc"]["vms"]
        assert deployed["bmc"]["username"] == "admin"


# ---------------------------------------------------------------------------
# _cleanup_old_vm_files  (line ~3561)
# ---------------------------------------------------------------------------


class TestCleanupOldVmFiles:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="j1")
    @patch("app.api.projects._seed_path", return_value="/seed/path")
    @patch("app.api.projects._disk_path", return_value="/disk/path")
    @patch(
        "app.api.projects._find_vm_disks",
        return_value=[{"node_id": "d1", "format": "qcow2"}],
    )
    def test_removes_disk_and_seed(
        self, mock_find, mock_disk_path, mock_seed_path, mock_start, mock_wait
    ):
        from app.api.projects import _cleanup_old_vm_files

        _cleanup_old_vm_files(MagicMock(), "p1", "vm1", {"nodes": []})
        mock_start.assert_called_once()
        payload = mock_start.call_args[0][2]
        assert "/disk/path" in payload["paths"]
        assert "/seed/path" in payload["paths"]

    @patch("app.api.projects._seed_path", return_value="/seed/path")
    @patch(
        "app.api.projects._find_vm_disks",
        return_value=[{"node_id": "d1", "format": "iso"}],
    )
    def test_skips_iso_disks(self, mock_find, mock_seed_path):
        from app.api.projects import _cleanup_old_vm_files

        with patch("app.api.projects.start_job", return_value="j1") as mock_start:
            with patch("app.api.projects.wait_for_job"):
                _cleanup_old_vm_files(MagicMock(), "p1", "vm1", {})

        payload = mock_start.call_args[0][2]
        # ISO disks should not produce a disk_path entry, only seed
        assert len(payload["paths"]) == 1
        assert payload["paths"][0] == "/seed/path"

    @patch("app.api.projects._seed_path", return_value="/seed/path")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    def test_troshkad_error_non_fatal(self, mock_find, mock_seed_path):
        from app.api.projects import _cleanup_old_vm_files
        from app.services.troshkad_client import TroshkadError

        with patch("app.api.projects.start_job", side_effect=TroshkadError("fail")):
            # Should not raise
            with patch(
                "app.services.deploy_topology._vm_domain_name", return_value="dom"
            ):
                _cleanup_old_vm_files(MagicMock(), "p1", "vm1", {})


# ---------------------------------------------------------------------------
# _cache_redeploy_images  (line ~3626)
# ---------------------------------------------------------------------------


class TestCacheRedeployImages:
    @patch("app.api.projects.cache_library_images")
    def test_updates_progress(self, mock_cache):
        from app.api.projects import _cache_redeploy_images, _redeploy_progress

        _cache_redeploy_images(MagicMock(), MagicMock(), {"nodes": []}, "dom-test")
        assert (
            "dom-test" not in _redeploy_progress
            or _redeploy_progress.get("dom-test", {}).get("step") == "downloading"
        )
        mock_cache.assert_called_once()


# ---------------------------------------------------------------------------
# _create_redeploy_vm  (line ~3636)
# ---------------------------------------------------------------------------


class TestCreateRedeployVm:
    @patch(
        "app.api.projects.wait_for_job",
        return_value={"status": "completed", "result": {}},
    )
    @patch("app.api.projects._wait_redeploy_disk_jobs")
    @patch("app.api.projects._create_vm_via_troshkad", return_value="create-job")
    @patch("app.api.projects._create_vm_disks_via_troshkad", return_value=["disk-job"])
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.api.projects._build_redeploy_vm_data", return_value={"node_id": "vm1"})
    @patch("app.api.projects._create_seed_isos_via_troshkad")
    def test_creates_seed_disks_and_vm(
        self,
        mock_seed,
        mock_build,
        mock_find,
        mock_disks,
        mock_create,
        mock_wait_disks,
        mock_wait,
    ):
        from app.api.projects import _create_redeploy_vm

        vm_node = {"id": "vm1", "data": {"name": "test"}}
        pool = MagicMock()
        pool.mode = "local"
        _create_redeploy_vm(
            MagicMock(), "p1", vm_node, {"nodes": []}, {}, pool, "vm1", "dom-1"
        )
        mock_seed.assert_called_once()
        mock_disks.assert_called_once()
        mock_wait_disks.assert_called_once_with(ANY, ["disk-job"])
        mock_create.assert_called_once()
        mock_wait.assert_called_once()


# ---------------------------------------------------------------------------
# _do_redeploy_bg  (lines 3661-3708)
# ---------------------------------------------------------------------------


class TestDoRedeployBg:
    @patch("app.api.projects._notify_redeploy_vm_state")
    @patch("app.api.projects._start_vm_if_needed")
    @patch("app.api.projects._create_redeploy_vm")
    @patch("app.api.projects._setup_pxe_via_troshkad")
    @patch("app.api.projects._cache_redeploy_images")
    @patch("app.api.projects._build_connected_topology", return_value={"nodes": []})
    @patch(
        "app.api.projects._find_vm_node_in_topology",
        return_value={"id": "vm1", "type": "vmNode", "data": {}},
    )
    @patch("app.api.projects._cleanup_old_vm_files")
    @patch("app.api.projects.troshkad_undefine_vm")
    @patch(
        "app.api.projects.troshkad_get_vm_state",
        return_value={"state": "running"},
    )
    def test_happy_path(
        self,
        mock_state,
        mock_undef,
        mock_cleanup,
        mock_find_node,
        mock_connected,
        mock_cache,
        mock_pxe,
        mock_create,
        mock_start,
        mock_notify,
    ):
        from app.api.projects import _do_redeploy_bg

        mock_session = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "p1"
        mock_proj.topology = {"nodes": []}
        mock_proj.vni_map = {}
        mock_host = MagicMock()
        mock_host.id = "h1"
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            mock_proj,
            mock_host,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.deploy_topology._vm_domain_name",
                return_value="dom-vm1",
            ):
                with patch("app.api.projects._vm_dir", return_value="/vms/p1"):
                    with patch(
                        "app.services.deploy_service._get_host_pool",
                        return_value=None,
                    ):
                        _do_redeploy_bg("p1", "h1", "vm1")

        mock_undef.assert_called_once()
        mock_cleanup.assert_called_once()
        mock_create.assert_called_once()
        mock_start.assert_called_once()
        mock_notify.assert_called_once()
        mock_session.commit.assert_called()

    def test_no_project_returns_early(self):
        from app.api.projects import _do_redeploy_bg

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            _do_redeploy_bg("p1", "h1", "vm1")
        mock_session.commit.assert_not_called()

    @patch("app.api.projects._cleanup_old_vm_files")
    @patch("app.api.projects.troshkad_undefine_vm")
    @patch(
        "app.api.projects.troshkad_get_vm_state",
        return_value={"state": "running"},
    )
    def test_vm_node_not_found_returns_early(
        self, mock_state, mock_undef, mock_cleanup
    ):
        from app.api.projects import _do_redeploy_bg

        mock_session = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "p1"
        mock_proj.topology = {"nodes": []}
        mock_proj.vni_map = {}
        mock_host = MagicMock()
        mock_host.id = "h1"
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [
            mock_proj,
            mock_host,
        ]

        with patch("app.core.database.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.deploy_topology._vm_domain_name",
                return_value="dom-x",
            ):
                with patch("app.api.projects._vm_dir"):
                    with patch(
                        "app.api.projects._find_vm_node_in_topology",
                        return_value=None,
                    ):
                        _do_redeploy_bg("p1", "h1", "vm-missing")

        mock_session.commit.assert_not_called()


class TestKubevirtVmRedeploy:
    @patch("app.api.projects._notify_redeploy_vm_state")
    @patch("app.api.projects._wait_troshkavm_redeploy_ready", return_value="Running")
    @patch("app.api.projects._create_troshkavm_cr")
    @patch("app.api.projects._wait_kubevirt_pvcs_deleted")
    @patch("app.api.projects._wait_troshkavm_deleted")
    @patch("app.api.projects._delete_troshkavm_cr")
    @patch(
        "app.api.projects._build_kubevirt_vm_spec",
        return_value={"disks": [{"id": "disk0001"}]},
    )
    @patch("app.api.projects._kubevirt_vm_was_running", return_value=True)
    @patch("app.api.projects._resolve_kubevirt_topology_for_redeploy")
    @patch("app.api.projects._kubevirt_project_ns", return_value="troshka-p1")
    @patch("app.api.projects._get_k8s_clients_for_kubevirt")
    @patch(
        "app.api.projects._find_vm_node_in_topology",
        return_value={"id": "vm1", "type": "vmNode", "data": {}},
    )
    @patch(
        "app.services.deploy_topology._extract_vms",
        return_value=[{"node_id": "vm1", "name": "rtr2", "vcpus": 2, "ram_gb": 4}],
    )
    def test_execute_kubevirt_redeploy_recreates_troshkavm(
        self,
        mock_extract,
        mock_find_node,
        mock_clients,
        mock_ns,
        mock_resolve,
        mock_was_running,
        mock_build_spec,
        mock_delete_cr,
        mock_wait_deleted,
        mock_wait_pvcs,
        mock_create_cr,
        mock_wait_ready,
        mock_notify,
    ):
        from app.api.projects import _execute_kubevirt_vm_redeploy

        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "metadata": {
                "ownerReferences": [{"kind": "TroshkaProject"}],
                "labels": {"troshka-project": "p1"},
            }
        }
        mock_clients.return_value = (mock_custom, MagicMock(), None)

        mock_host = MagicMock()
        mock_host.provider_id = "prov-1"
        mock_host.host_type = "kubevirt-cluster"
        mock_proj = MagicMock()
        mock_proj.topology = {"nodes": []}
        mock_proj.vni_map = {}
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            MagicMock()
        )

        with patch(
            "app.services.deploy_topology._vm_domain_name",
            return_value="dom-vm1",
        ):
            _execute_kubevirt_vm_redeploy(
                mock_host, mock_session, mock_proj, "p1", "vm1"
            )

        mock_delete_cr.assert_called_once()
        mock_wait_deleted.assert_called_once()
        mock_wait_pvcs.assert_called_once()
        mock_create_cr.assert_called_once()
        mock_wait_ready.assert_called_once()
        mock_notify.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("app.api.projects._execute_kubevirt_vm_redeploy")
    def test_execute_vm_redeploy_dispatches_kubevirt(self, mock_kv_redeploy):
        from app.api.projects import _execute_vm_redeploy

        mock_host = MagicMock()
        mock_host.host_type = "kubevirt-cluster"
        mock_proj = MagicMock()
        mock_session = MagicMock()

        _execute_vm_redeploy(mock_host, mock_session, mock_proj, "p1", "vm1")

        mock_kv_redeploy.assert_called_once_with(
            mock_host,
            mock_session,
            mock_proj,
            "p1",
            "vm1",
            update_deployed_topology=True,
        )

    def test_kubevirt_redeploy_pvc_names(self):
        from app.api.projects import _kubevirt_redeploy_pvc_names

        names = _kubevirt_redeploy_pvc_names(
            "vm-abcdef01",
            {
                "disks": [{"id": "disk0001"}, {"id": "disk0002"}],
                "cdrom": {"s3Path": "s3://bucket/iso"},
            },
        )
        assert names == [
            "vm-abcdef01-disk-disk0001",
            "vm-abcdef01-disk-disk0002",
            "vm-abcdef01-cdrom",
        ]


# ---------------------------------------------------------------------------
# _wait_kubevirt_vms_ready  (lines ~2544-2582)
# ---------------------------------------------------------------------------


class TestWaitKubevirtVmsReady:
    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_all_ready_returns_none(self, mock_set, mock_del):
        from app.api.projects import _wait_kubevirt_vms_ready

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [{"status": {"state": "Running"}, "spec": {"name": "vm1"}}]
        }
        proj = MagicMock()
        s = MagicMock()
        result = _wait_kubevirt_vms_ready(
            custom_api, "ns", "p1", proj, s, deadline_secs=1
        )
        assert result is None

    @patch("app.services.deploy_service._delete_deploy_progress")
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_vm_error_returns_string(self, mock_set, mock_del):
        from app.api.projects import _wait_kubevirt_vms_ready

        custom_api = MagicMock()
        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "status": {"state": "Error", "message": "disk not found"},
                    "spec": {"name": "vm-bad"},
                }
            ]
        }
        proj = MagicMock()
        s = MagicMock()
        result = _wait_kubevirt_vms_ready(
            custom_api, "ns", "p1", proj, s, deadline_secs=1
        )
        assert result == "vm_error"
        assert proj.state == "error"
        assert "vm-bad" in proj.deploy_error


# ---------------------------------------------------------------------------
# redeploy_project endpoint  (lines 3759-3797)
# ---------------------------------------------------------------------------


class TestRedeployProjectEndpoint:
    def _create_project(self, state="active", host_id=None, topology=None):
        import uuid

        from app.models.project import Project

        db = TestSession()
        proj = Project(
            id=str(uuid.uuid4()),
            name=f"redeploy-test-{uuid.uuid4().hex[:6]}",
            owner_id=_user.id,
            state=state,
            topology=topology,
            host_id=host_id,
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)
        db.close()
        return proj

    def test_404_not_found(self):
        resp = client.post("/api/v1/projects/nonexistent/redeploy", headers=HEADERS)
        assert resp.status_code == 404

    def test_409_wrong_state(self):
        proj = self._create_project(state="deploying")
        resp = client.post(f"/api/v1/projects/{proj.id}/redeploy", headers=HEADERS)
        assert resp.status_code == 409

    def test_400_no_topology(self):
        proj = self._create_project(state="active", topology=None)
        resp = client.post(f"/api/v1/projects/{proj.id}/redeploy", headers=HEADERS)
        assert resp.status_code == 400

    def test_400_no_vms(self):
        proj = self._create_project(
            state="active",
            topology={"nodes": [{"type": "networkNode", "data": {}}], "edges": []},
        )
        resp = client.post(f"/api/v1/projects/{proj.id}/redeploy", headers=HEADERS)
        assert resp.status_code == 400

    @patch("app.core.redis.enqueue_job")
    @patch("app.services.deploy_service._mark_deploy_cancelled")
    def test_success_no_host(self, mock_cancel, mock_enqueue):
        proj = self._create_project(
            state="active",
            topology={
                "nodes": [
                    {
                        "id": "vm1",
                        "type": "vmNode",
                        "data": {"vcpus": 2, "ram": 4, "name": "vm"},
                    }
                ],
                "edges": [],
            },
        )
        resp = client.post(f"/api/v1/projects/{proj.id}/redeploy", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deploying"
        mock_enqueue.assert_called_once()


# ---------------------------------------------------------------------------
# _reconfigure_existing_vm  (covers lines ~3169-3268)
# ---------------------------------------------------------------------------


class TestReconfigureExistingVm:
    @patch("app.api.projects.troshkad_reconfigure_vm")
    @patch(
        "app.api.projects.troshkad_get_vm_config",
        return_value={
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "nics": [{"bridge": "br-100"}],
            "disks": ["/vms/p1/d1.qcow2"],
            "cdroms": [],
        },
    )
    @patch("app.api.projects._apply_disk_changes")
    @patch(
        "app.api.projects._detect_disk_changes",
        return_value={
            "disk_list": [{"path": "/vms/p1/d1.qcow2", "format": "qcow2"}],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        },
    )
    @patch("app.api.projects._find_vm_networks")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_unchanged_vm_skips_reconfigure(
        self,
        mock_set,
        mock_find_disks,
        mock_find_nets,
        mock_detect,
        mock_apply,
        mock_config,
        mock_reconfig,
    ):
        from app.api.projects import _reconfigure_existing_vm

        mock_find_nets.return_value = [{"bridge": "br-100", "mac": "aa:bb:cc:dd:ee:ff"}]
        vm = {
            "node_id": "vm1",
            "name": "test",
            "vcpus": 2,
            "ram_gb": 4,
            "cloud_init": False,
        }
        current = {"nodes": [], "edges": []}
        deployed = {"nodes": [], "edges": []}

        with patch(
            "app.services.deploy_topology._vm_domain_name", return_value="dom-vm1"
        ):
            with patch(
                "app.services.deploy_topology._resolve_boot_devs",
                return_value=["hd"],
            ):
                _reconfigure_existing_vm(
                    MagicMock(),
                    "p1234567",
                    MagicMock(),
                    current,
                    deployed,
                    vm,
                    {},
                    set(),
                    None,
                    {"added_vms": [], "removed_vms": [], "changed_vms": []},
                    [],
                )

        mock_reconfig.assert_not_called()
        mock_apply.assert_not_called()

    @patch("app.api.projects.troshkad_reconfigure_vm")
    @patch(
        "app.api.projects.troshkad_get_vm_config",
        return_value={
            "boot_devs": ["hd"],
            "vcpus": 2,
            "ram_mb": 4096,
            "nics": [{"bridge": "br-100"}],
            "disks": ["/vms/p1/d1.qcow2"],
            "cdroms": [],
        },
    )
    @patch("app.api.projects._apply_disk_changes")
    @patch(
        "app.api.projects._detect_disk_changes",
        return_value={
            "disk_list": [{"path": "/vms/p1/d1.qcow2", "format": "qcow2"}],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        },
    )
    @patch("app.api.projects._find_vm_networks")
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_changed_vcpus_triggers_reconfigure(
        self,
        mock_set,
        mock_find_disks,
        mock_find_nets,
        mock_detect,
        mock_apply,
        mock_config,
        mock_reconfig,
    ):
        from app.api.projects import _reconfigure_existing_vm

        mock_find_nets.return_value = [{"bridge": "br-100", "mac": "aa:bb:cc:dd:ee:ff"}]
        vm = {
            "node_id": "vm1",
            "name": "test",
            "vcpus": 4,
            "ram_gb": 4,
            "cloud_init": False,
        }
        current = {"nodes": [], "edges": []}
        deployed = {"nodes": [], "edges": []}

        with patch(
            "app.services.deploy_topology._vm_domain_name", return_value="dom-vm1"
        ):
            with patch(
                "app.services.deploy_topology._resolve_boot_devs",
                return_value=["hd"],
            ):
                _reconfigure_existing_vm(
                    MagicMock(),
                    "p1234567",
                    MagicMock(),
                    current,
                    deployed,
                    vm,
                    {},
                    set(),
                    None,
                    {"added_vms": [], "removed_vms": [], "changed_vms": []},
                    [],
                )

        mock_reconfig.assert_called_once()
        call_kwargs = mock_reconfig.call_args
        assert call_kwargs[1]["vcpus"] == 4

    @patch("app.api.projects._apply_disk_changes")
    @patch(
        "app.api.projects._detect_disk_changes",
        return_value={
            "disk_list": [],
            "cdrom_list": [],
            "any_disk_changed": False,
            "needs_library_download": False,
            "files_to_remove": [],
            "disks_to_create": [],
            "disks_to_resize": [],
        },
    )
    @patch("app.api.projects._find_vm_networks", return_value=[])
    @patch("app.api.projects._find_vm_disks", return_value=[])
    @patch("app.services.deploy_service._set_deploy_progress")
    def test_no_config_adds_to_added_vms(
        self,
        mock_set,
        mock_find_disks,
        mock_find_nets,
        mock_detect,
        mock_apply,
    ):
        from app.api.projects import _reconfigure_existing_vm

        with patch("app.api.projects.troshkad_get_vm_config", return_value=None):
            vm = {
                "node_id": "vm1",
                "name": "test",
                "vcpus": 2,
                "ram_gb": 4,
                "cloud_init": False,
            }
            current = {
                "nodes": [{"id": "vm1", "type": "vmNode", "data": {}}],
                "edges": [],
            }
            deployed = {"nodes": [], "edges": []}
            diff = {"added_vms": [], "removed_vms": [], "changed_vms": []}

            with patch(
                "app.services.deploy_topology._vm_domain_name", return_value="dom"
            ):
                with patch(
                    "app.services.deploy_topology._resolve_boot_devs",
                    return_value=[],
                ):
                    _reconfigure_existing_vm(
                        MagicMock(),
                        "p1234567",
                        MagicMock(),
                        current,
                        deployed,
                        vm,
                        {},
                        set(),
                        None,
                        diff,
                        [],
                    )

            assert len(diff["added_vms"]) == 1
            assert diff["added_vms"][0]["id"] == "vm1"


# ---------------------------------------------------------------------------
# _exec_troshkad  (extracted from vm_exec)
# ---------------------------------------------------------------------------


class TestExecTroshkad:
    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-ga")
    def test_guest_agent_success(self, mock_start, mock_wait):
        from app.api.projects import _exec_troshkad

        mock_wait.return_value = {
            "status": "completed",
            "result": {"output": "hello", "error": "", "exit_code": 0},
        }
        host = MagicMock()
        result = _exec_troshkad(
            host=host,
            project_id="p1234567",
            vm_id="vm-abc",
            methods=["guest-agent"],
            method="guest-agent",
            vm_ip="",
            username="cloud-user",
            password="pass",
            private_key="",
            root_password="",
            command="whoami",
            timeout=60,
            force_tty=False,
        )
        assert result["method"] == "guest-agent"
        assert result["output"] == "hello"
        assert result["exit_code"] == 0
        mock_start.assert_called_once()

    def test_all_methods_fail_raises_503(self):
        from fastapi import HTTPException

        from app.api.projects import _exec_troshkad
        from app.services.troshkad_client import TroshkadError

        host = MagicMock()

        with patch(
            "app.api.projects.start_job",
            side_effect=TroshkadError("connection refused"),
        ):
            try:
                _exec_troshkad(
                    host=host,
                    project_id="p1234567",
                    vm_id="vm-abc",
                    methods=["guest-agent", "ssh"],
                    method="auto",
                    vm_ip="",
                    username="cloud-user",
                    password="",
                    private_key="",
                    root_password="",
                    command="whoami",
                    timeout=60,
                    force_tty=False,
                )
                assert False, "Should have raised HTTPException"
            except HTTPException as exc:
                assert exc.status_code == 503
                assert "All exec methods failed" in exc.detail

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-ssh")
    def test_ssh_success(self, mock_start, mock_wait):
        from app.api.projects import _exec_troshkad

        mock_wait.return_value = {
            "status": "completed",
            "result": {"output": "uid=0", "error": "", "exit_code": 0},
        }
        result = _exec_troshkad(
            host=MagicMock(),
            project_id="p1234567",
            vm_id="vm-abc",
            methods=["ssh"],
            method="ssh",
            vm_ip="10.0.0.5",
            username="cloud-user",
            password="pass",
            private_key="",
            root_password="",
            command="id",
            timeout=60,
            force_tty=False,
        )
        assert result["method"] == "ssh"
        assert result["output"] == "uid=0"

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job")
    def test_auto_ssh_failure_falls_through(self, mock_start, mock_wait):
        from app.api.projects import _exec_troshkad

        mock_start.side_effect = ["job-ssh", "job-console", "job-serial"]

        def wait_side_effect(host, job_id, timeout=60):
            if job_id == "job-ssh":
                return {
                    "status": "completed",
                    "result": {
                        "output": "",
                        "error": "connect to host 10.0.0.5 port 22: No route to host\n",
                        "exit_code": 255,
                    },
                }
            if job_id == "job-console":
                return {
                    "status": "completed",
                    "result": {
                        "output": "",
                        "error": "Could not reach shell prompt",
                    },
                }
            return {
                "status": "completed",
                "result": {"output": "rtr1>", "error": ""},
            }

        mock_wait.side_effect = wait_side_effect

        result = _exec_troshkad(
            host=MagicMock(),
            project_id="p1234567",
            vm_id="vm-abc",
            methods=["ssh", "console", "serial"],
            method="auto",
            vm_ip="10.0.0.5",
            username="admin",
            password="pass",
            private_key="",
            root_password="",
            command="show version",
            timeout=60,
            force_tty=False,
        )
        assert result["method"] == "serial"
        assert result["output"] == "rtr1>"
        assert mock_start.call_count == 3

    @patch("app.api.projects.wait_for_job")
    @patch("app.api.projects.start_job", return_value="job-ssh")
    def test_explicit_ssh_returns_nonzero_exit(self, mock_start, mock_wait):
        from app.api.projects import _exec_troshkad

        mock_wait.return_value = {
            "status": "completed",
            "result": {
                "output": "",
                "error": "connect failed",
                "exit_code": 255,
            },
        }
        result = _exec_troshkad(
            host=MagicMock(),
            project_id="p1234567",
            vm_id="vm-abc",
            methods=["ssh"],
            method="ssh",
            vm_ip="10.0.0.5",
            username="admin",
            password="pass",
            private_key="",
            root_password="",
            command="show version",
            timeout=60,
            force_tty=False,
        )
        assert result["method"] == "ssh"
        assert result["exit_code"] == 255

    def test_resolve_exec_params_skips_guest_agent_without_cloud_init(self):
        from app.api.projects import _resolve_exec_params

        vm_node = {"data": {"cloudInit": False, "nics": [{"ip": "10.0.0.5"}]}}
        params = _resolve_exec_params({"method": "auto"}, vm_node)
        assert params["methods"] == ["ssh", "console", "serial"]

    def test_resolve_serial_exec_type_from_canvas(self):
        from app.api.projects import _resolve_serial_exec_type

        vm_node = {"data": {"serialExecType": "ios"}}
        assert _resolve_serial_exec_type({}, vm_node) == "ios"

    def test_resolve_serial_exec_type_from_ansible_group(self):
        from app.api.projects import _resolve_serial_exec_type

        vm_node = {"data": {"tags": {"AnsibleGroup": "routers,cisco_iosxe"}}}
        assert _resolve_serial_exec_type({}, vm_node) == "ios"

    def test_single_method_failure_raises_immediately(self):
        from fastapi import HTTPException

        from app.api.projects import _exec_troshkad
        from app.services.troshkad_client import TroshkadError

        with patch("app.api.projects.start_job", side_effect=TroshkadError("timeout")):
            try:
                _exec_troshkad(
                    host=MagicMock(),
                    project_id="p1234567",
                    vm_id="vm-abc",
                    methods=["guest-agent"],
                    method="guest-agent",
                    vm_ip="",
                    username="cloud-user",
                    password="pass",
                    private_key="",
                    root_password="",
                    command="whoami",
                    timeout=60,
                    force_tty=False,
                )
                assert False, "Should have raised HTTPException"
            except HTTPException as exc:
                assert exc.status_code == 503
                assert "guest-agent exec failed" in exc.detail


# ---------------------------------------------------------------------------
# _exec_kubevirt  (extracted from vm_exec)
# ---------------------------------------------------------------------------


class TestExecKubevirt:
    @patch("app.api.projects._exec_kubevirt.__module__", "app.api.projects")
    def test_guest_agent_success(self):
        from app.api.projects import _exec_kubevirt

        with patch(
            "app.services.providers.kubevirt.kubevirt_exec_guest_agent",
            return_value={"output": "ok", "method": "guest-agent"},
        ):
            result = _exec_kubevirt(
                provider=MagicMock(),
                project_id="p1",
                vm_id="vm1",
                methods=["guest-agent"],
                vm_ip="",
                username="cloud-user",
                password="pass",
                root_password="",
                command="whoami",
                timeout=60,
            )
        assert result["method"] == "guest-agent"
        assert result["output"] == "ok"

    def test_all_methods_fail_raises_503(self):
        from fastapi import HTTPException

        from app.api.projects import _exec_kubevirt

        with patch(
            "app.services.providers.kubevirt.kubevirt_exec_guest_agent",
            side_effect=Exception("agent down"),
        ):
            with patch(
                "app.services.providers.kubevirt.kubevirt_exec_ssh",
                side_effect=Exception("ssh down"),
            ):
                try:
                    _exec_kubevirt(
                        provider=MagicMock(),
                        project_id="p1",
                        vm_id="vm1",
                        methods=["guest-agent", "ssh"],
                        vm_ip="10.0.0.5",
                        username="cloud-user",
                        password="pass",
                        root_password="",
                        command="whoami",
                        timeout=60,
                    )
                    assert False, "Should have raised HTTPException"
                except HTTPException as exc:
                    assert exc.status_code == 503
                    assert "All exec methods failed" in exc.detail

    def test_ssh_skipped_without_ip(self):
        from fastapi import HTTPException

        from app.api.projects import _exec_kubevirt

        try:
            _exec_kubevirt(
                provider=MagicMock(),
                project_id="p1",
                vm_id="vm1",
                methods=["ssh"],
                vm_ip="",
                username="cloud-user",
                password="pass",
                root_password="",
                command="whoami",
                timeout=60,
            )
            assert False, "Should have raised HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 503
            assert "ssh: no VM IP or credentials" in exc.detail

    def test_serial_junos_success(self):
        from app.api.projects import _exec_kubevirt

        with patch(
            "app.services.providers.kubevirt_serial.kubevirt_exec_serial",
            return_value={
                "output": "Hostname: rtr3",
                "error": "",
                "exit_code": 0,
                "method": "serial-junos",
            },
        ):
            result = _exec_kubevirt(
                provider=MagicMock(),
                project_id="p1",
                vm_id="vm1",
                methods=["serial"],
                vm_ip="",
                username="root",
                password="",
                root_password="",
                command="show version",
                timeout=60,
                serial_exec_type="junos",
            )
        assert result["method"] == "serial-junos"
        assert result["output"] == "Hostname: rtr3"


def test_sync_showroom_prunes_stale_vni(monkeypatch):
    """A VNI for a deleted network is pruned from vni_map on save, so it can't
    corrupt min(vni_map) (which drives the showroom infra IP)."""
    from types import SimpleNamespace

    from app.api import projects as proj_api

    monkeypatch.setattr(proj_api, "_resolve_provider_type", lambda p: "ocpvirt")
    monkeypatch.setattr(
        "app.services.deploy_topology.inject_showroom_gateway_port_forwards",
        lambda *a, **k: None,
    )
    project = SimpleNamespace(
        vni_map={"live-net": 1952, "deleted-net": 1000}, provider=None
    )
    topology = {
        "nodes": [
            {"id": "live-net", "type": "networkNode", "data": {"subtype": "network"}},
        ]
    }
    proj_api._sync_showroom_topology_on_save(None, project, topology)
    assert project.vni_map == {"live-net": 1952}
