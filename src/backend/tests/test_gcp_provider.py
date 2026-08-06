"""Tests for GCP provider driver.

All GCP SDK calls are mocked — no real credentials or API access needed.
"""

import json
from importlib import reload
from unittest.mock import MagicMock, patch

import pytest

from app.services.providers import get_provider_driver
from app.services.providers.base import ProviderDriver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider():
    """Create a mock Provider with GCP credentials."""
    p = MagicMock()
    p.type = "gcp"
    p.gcp_project_id = "test-project"
    p.gcp_zone = "us-central1-a"
    p.gcp_subnet_id = "projects/test-project/regions/us-central1/subnetworks/troshka"
    p.default_image = "rhel-9-v20250101"
    p.console_zone_id = "console-example-com"
    p.console_base_domain = "console.example.com"
    p.get_credentials.return_value = {
        "service_account_json": {
            "type": "service_account",
            "project_id": "test-project",
        },
    }
    return p


def _make_host(instance_id="troshka-abcdef012345"):
    h = MagicMock()
    h.instance_id = instance_id
    h.storage_size_gb = 500
    h.auto_extend_increment_gb = 100
    h.auto_extend_max_gb = 2000
    h.agent_connected = False
    return h


def _mock_operation(name="op-123"):
    op = MagicMock()
    op.name = name
    return op


def _mock_compute_modules():
    """Build linked mock GCP compute modules for sys.modules patching.

    ``from google.cloud import compute_v1`` first looks up
    ``sys.modules["google.cloud"]`` then accesses ``.compute_v1``.
    Both must return the *same* mock object.
    """
    mock_cv1 = MagicMock()
    mock_cloud = MagicMock()
    mock_cloud.compute_v1 = mock_cv1
    return {
        "google": MagicMock(),
        "google.cloud": mock_cloud,
        "google.cloud.compute_v1": mock_cv1,
    }, mock_cv1


def _mock_dns_modules():
    """Build linked mock GCP Cloud DNS modules for sys.modules patching."""
    mock_dns = MagicMock()
    mock_cloud = MagicMock()
    mock_cloud.dns = mock_dns
    return {
        "google": MagicMock(),
        "google.cloud": mock_cloud,
        "google.cloud.dns": mock_dns,
    }, mock_dns


# ---------------------------------------------------------------------------
# Module-level helper function tests
# ---------------------------------------------------------------------------


class TestParseInstanceType:
    def test_known_highmem_type(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, ram_mb = _parse_instance_type("n2-highmem-32")
        assert vcpus == 32
        assert ram_mb == 256 * 1024

    def test_smallest_highmem(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, ram_mb = _parse_instance_type("n2-highmem-4")
        assert vcpus == 4
        assert ram_mb == 32 * 1024

    def test_none_defaults(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, ram_mb = _parse_instance_type(None)
        assert vcpus == 32
        assert ram_mb == 256 * 1024

    def test_empty_string_defaults(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, ram_mb = _parse_instance_type("")
        assert vcpus == 32

    def test_unknown_type_falls_back(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, ram_mb = _parse_instance_type("e2-standard-8")
        assert vcpus == 8
        assert ram_mb == 8 * 8 * 1024  # 8 GB/vCPU fallback

    def test_unparsable_suffix(self):
        from app.services.providers.gcp import _parse_instance_type

        vcpus, _ram = _parse_instance_type("custom-machine")
        assert vcpus == 32  # default when last segment is not an int


class TestZoneToRegion:
    def test_standard_zones(self):
        from app.services.providers.gcp import _zone_to_region

        assert _zone_to_region("us-central1-a") == "us-central1"
        assert _zone_to_region("europe-west1-b") == "europe-west1"
        assert _zone_to_region("asia-east2-c") == "asia-east2"


class TestResolveBootImageUrl:
    def test_trusted_https_url(self):
        from app.services.providers.gcp import _resolve_boot_image_url

        url = "https://compute.googleapis.com/compute/v1/projects/rhel-cloud/global/images/rhel-9"
        assert _resolve_boot_image_url(url, "proj") == url

    def test_trusted_www_url(self):
        from app.services.providers.gcp import _resolve_boot_image_url

        url = "https://www.googleapis.com/compute/v1/projects/rhel-cloud/global/images/rhel-9"
        assert _resolve_boot_image_url(url, "proj") == url

    def test_untrusted_host_raises(self):
        from app.services.providers.gcp import _resolve_boot_image_url

        with pytest.raises(ValueError, match="Untrusted"):
            _resolve_boot_image_url("https://evil.example.com/image", "proj")

    def test_projects_path(self):
        from app.services.providers.gcp import _resolve_boot_image_url

        result = _resolve_boot_image_url(
            "projects/rhel-cloud/global/images/rhel-9", "proj"
        )
        assert (
            result
            == "https://compute.googleapis.com/compute/v1/projects/rhel-cloud/global/images/rhel-9"
        )

    def test_plain_image_name(self):
        from app.services.providers.gcp import _resolve_boot_image_url

        result = _resolve_boot_image_url("my-image", "my-project")
        assert "my-project/global/images/my-image" in result


class TestBuildCloudInit:
    def test_without_nfs(self):
        from app.services.providers.gcp import _build_cloud_init

        result = _build_cloud_init("host-123")
        assert "host_id: host-123" in result
        assert "/var/lib/troshka/shared" not in result

    def test_with_nfs(self):
        from app.services.providers.gcp import _build_cloud_init

        result = _build_cloud_init(
            "host-123", nfs_server="10.0.0.5", nfs_path="/exports/troshka"
        )
        assert "/var/lib/troshka/shared" in result
        assert "10.0.0.5:/exports/troshka" in result
        assert "nconnect=16" in result


class TestGetCredentials:
    def test_from_dict(self):
        mock_sa = MagicMock()
        mock_creds = MagicMock()
        mock_sa.Credentials.from_service_account_info.return_value = mock_creds
        mock_oauth2 = MagicMock()
        mock_oauth2.service_account = mock_sa

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(),
                "google.oauth2": mock_oauth2,
                "google.oauth2.service_account": mock_sa,
            },
        ):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            result = gcp_mod._get_credentials(
                {"service_account_json": {"type": "service_account"}}
            )
            mock_sa.Credentials.from_service_account_info.assert_called_once_with(
                {"type": "service_account"}
            )
            assert result is mock_creds

    def test_from_json_string(self):
        mock_sa = MagicMock()
        mock_creds = MagicMock()
        mock_sa.Credentials.from_service_account_info.return_value = mock_creds
        mock_oauth2 = MagicMock()
        mock_oauth2.service_account = mock_sa

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(),
                "google.oauth2": mock_oauth2,
                "google.oauth2.service_account": mock_sa,
            },
        ):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            sa_str = json.dumps({"type": "service_account", "project_id": "test"})
            gcp_mod._get_credentials({"service_account_json": sa_str})
            call_arg = mock_sa.Credentials.from_service_account_info.call_args[0][0]
            assert call_arg["project_id"] == "test"


class TestPollOperation:
    def test_done_immediately(self):
        modules, mock_cv1 = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            done = mock_cv1.Operation.Status.DONE
            result = MagicMock(status=done, error=None)
            get_fn = MagicMock(return_value=result)
            assert gcp_mod._poll_operation_until_done(get_fn) is result
            get_fn.assert_called_once()

    def test_done_with_error(self):
        modules, mock_cv1 = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            done = mock_cv1.Operation.Status.DONE
            err = MagicMock(message="quota exceeded")
            result = MagicMock(status=done)
            result.error.errors = [err]
            with pytest.raises(RuntimeError, match="quota exceeded"):
                gcp_mod._poll_operation_until_done(MagicMock(return_value=result))

    def test_polls_until_done(self):
        modules, mock_cv1 = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            done_sentinel = mock_cv1.Operation.Status.DONE
            pending = MagicMock(status="RUNNING")
            done = MagicMock(status=done_sentinel, error=None)
            get_fn = MagicMock(side_effect=[pending, pending, done])
            with patch.object(gcp_mod.time, "sleep"):
                result = gcp_mod._poll_operation_until_done(get_fn)
            assert result is done
            assert get_fn.call_count == 3


class TestWaitForOperation:
    def test_zone_operation(self):
        modules, mock_cv1 = _mock_compute_modules()
        mock_zone_ops = MagicMock()
        done = mock_cv1.Operation.Status.DONE
        result = MagicMock(status=done, error=None)
        mock_zone_ops.get.return_value = result
        mock_cv1.ZoneOperationsClient.return_value = mock_zone_ops

        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            op = MagicMock(name="op-123")
            op.name = "op-123"
            out = gcp_mod._wait_for_operation(
                op, "proj", zone="us-central1-a", creds=MagicMock()
            )
            assert out is result
            mock_zone_ops.get.assert_called_with(
                project="proj", zone="us-central1-a", operation="op-123"
            )

    def test_region_operation(self):
        modules, mock_cv1 = _mock_compute_modules()
        mock_region_ops = MagicMock()
        done = mock_cv1.Operation.Status.DONE
        result = MagicMock(status=done, error=None)
        mock_region_ops.get.return_value = result
        mock_cv1.RegionOperationsClient.return_value = mock_region_ops

        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            op = MagicMock()
            op.name = "op-456"
            out = gcp_mod._wait_for_operation(
                op, "proj", region="us-central1", creds=MagicMock()
            )
            assert out is result

    def test_global_operation(self):
        modules, mock_cv1 = _mock_compute_modules()
        mock_global_ops = MagicMock()
        done = mock_cv1.Operation.Status.DONE
        result = MagicMock(status=done, error=None)
        mock_global_ops.get.return_value = result
        mock_cv1.GlobalOperationsClient.return_value = mock_global_ops

        with patch.dict("sys.modules", modules):
            import app.services.providers.gcp as gcp_mod

            reload(gcp_mod)
            op = MagicMock()
            op.name = "op-789"
            out = gcp_mod._wait_for_operation(op, "proj", creds=MagicMock())
            assert out is result


# ---------------------------------------------------------------------------
# GCPDriver class method tests
#
# For most driver methods we patch the module-level helpers (_get_credentials,
# _get_compute_client, _wait_for_operation, etc.) so the tests never need
# real GCP SDK packages.  Methods that do their own ``from google.cloud
# import compute_v1`` (provision, resize, extend, allocate_eip, associate_eip)
# also need sys.modules patching via _mock_compute_modules().
# ---------------------------------------------------------------------------


class TestGCPDriverProvision:
    @patch("app.services.providers.gcp._generate_ssh_keypair")
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_provision_host_basic(
        self,
        mock_get_creds,
        mock_compute_client,
        mock_disks_client,
        mock_wait,
        mock_keygen,
    ):
        mock_get_creds.return_value = MagicMock()
        mock_keygen.return_value = (
            "fake-openssh-key-data\n",  # pragma: allowlist secret
            "ssh-rsa AAAA fake",
        )

        mock_compute = MagicMock()
        mock_compute.insert.return_value = _mock_operation()
        running_inst = MagicMock(status="RUNNING")
        iface = MagicMock(network_i_p="10.0.1.5")
        iface.access_configs = [MagicMock(nat_i_p="35.192.0.1")]
        running_inst.network_interfaces = [iface]
        mock_compute.get.return_value = running_inst
        mock_compute_client.return_value = mock_compute

        mock_disks = MagicMock()
        mock_disks.insert.return_value = _mock_operation()
        mock_disks_client.return_value = mock_disks

        provider = _make_provider()
        from app.services.providers.gcp import GCPDriver

        driver = GCPDriver()

        modules, mock_cv1 = _mock_compute_modules()
        with patch("app.services.providers.gcp.time.sleep"):
            with patch.dict("sys.modules", modules):
                result = driver.provision_host(
                    provider, "test-host-id-1234567890", "n2-highmem-16", 500
                )

        assert result["host_id"] == "test-host-id-1234567890"
        assert result["public_ip"] == "35.192.0.1"
        assert result["private_ip"] == "10.0.1.5"
        assert result["total_vcpus"] == 16
        assert result["total_ram_mb"] == 128 * 1024
        assert result["instance_type"] == "n2-highmem-16"
        assert result["storage_size_gb"] == 500
        assert result["_ssh_user"] == "troshka"
        assert result["_ssh_port"] == 22

    @patch(
        "app.services.providers.gcp._generate_ssh_keypair", return_value=("key", "pub")
    )
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_provision_no_image_raises(self, mock_get_creds, *_):
        mock_get_creds.return_value = MagicMock()
        provider = _make_provider()
        provider.default_image = None

        from app.services.providers.gcp import GCPDriver

        with pytest.raises(ValueError, match="No boot image"):
            GCPDriver().provision_host(provider, "hid", "n2-highmem-8", 200)

    @patch("app.services.providers.gcp._generate_ssh_keypair")
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_provision_timeout_raises(
        self,
        mock_get_creds,
        mock_compute_client,
        mock_disks_client,
        mock_wait,
        mock_keygen,
    ):
        mock_get_creds.return_value = MagicMock()
        mock_keygen.return_value = ("key", "pub")

        mock_compute = MagicMock()
        mock_compute.insert.return_value = _mock_operation()
        mock_compute.get.return_value = MagicMock(
            status="STAGING", network_interfaces=[]
        )
        mock_compute_client.return_value = mock_compute

        mock_disks_client.return_value = MagicMock(
            insert=MagicMock(return_value=_mock_operation())
        )

        provider = _make_provider()
        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch("app.services.providers.gcp.time.sleep"):
            with patch.dict("sys.modules", modules):
                with pytest.raises(RuntimeError, match="did not reach RUNNING"):
                    GCPDriver().provision_host(provider, "hid", "n2-highmem-8", 200)

    @patch("app.services.providers.gcp._generate_ssh_keypair")
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_provision_pattern_buffer(
        self,
        mock_get_creds,
        mock_compute_client,
        mock_disks_client,
        mock_wait,
        mock_keygen,
    ):
        """Pattern buffer: nested virt disabled, MIGRATE maintenance."""
        mock_get_creds.return_value = MagicMock()
        mock_keygen.return_value = ("key", "pub")

        mock_compute = MagicMock()
        mock_compute.insert.return_value = _mock_operation()
        inst = MagicMock(status="RUNNING")
        iface = MagicMock(network_i_p="10.0.1.5")
        iface.access_configs = [MagicMock(nat_i_p="35.1.2.3")]
        inst.network_interfaces = [iface]
        mock_compute.get.return_value = inst
        mock_compute_client.return_value = mock_compute
        mock_disks_client.return_value = MagicMock(
            insert=MagicMock(return_value=_mock_operation())
        )

        provider = _make_provider()
        from app.services.providers.gcp import GCPDriver

        modules, mock_cv1 = _mock_compute_modules()
        with patch("app.services.providers.gcp.time.sleep"):
            with patch.dict("sys.modules", modules):
                GCPDriver().provision_host(
                    provider, "hid", "n2-highmem-8", 200, host_type="pattern_buffer"
                )

        amf = mock_cv1.AdvancedMachineFeatures
        assert amf.called
        assert amf.call_args[1]["enable_nested_virtualization"] is False
        sched = mock_cv1.Scheduling
        assert sched.call_args[1]["on_host_maintenance"] == "MIGRATE"


class TestGCPDriverTerminate:
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_terminate_host(
        self, mock_get_creds, mock_compute_client, mock_disks_client, mock_wait
    ):
        mock_get_creds.return_value = MagicMock()
        mock_compute = MagicMock(delete=MagicMock(return_value=_mock_operation()))
        mock_compute_client.return_value = mock_compute
        mock_disks = MagicMock(delete=MagicMock(return_value=_mock_operation()))
        mock_disks_client.return_value = mock_disks

        from app.services.providers.gcp import GCPDriver

        GCPDriver().terminate_host(_make_provider(), "troshka-abcdef012345")

        mock_compute.delete.assert_called_once_with(
            project="test-project",
            zone="us-central1-a",
            instance="troshka-abcdef012345",
        )
        mock_disks.delete.assert_called_once_with(
            project="test-project",
            zone="us-central1-a",
            disk="troshka-data-abcdef012345",
        )

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_terminate_already_gone(
        self, mock_get_creds, mock_compute_client, mock_disks_client, _
    ):
        mock_get_creds.return_value = MagicMock()
        mock_compute_client.return_value = MagicMock(
            delete=MagicMock(side_effect=Exception("was not found"))
        )
        mock_disks_client.return_value = MagicMock(
            delete=MagicMock(side_effect=Exception("was not found"))
        )

        from app.services.providers.gcp import GCPDriver

        GCPDriver().terminate_host(
            _make_provider(), "troshka-abcdef012345"
        )  # should not raise

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_terminate_real_error(self, mock_get_creds, mock_compute_client, *_):
        mock_get_creds.return_value = MagicMock()
        mock_compute_client.return_value = MagicMock(
            delete=MagicMock(side_effect=Exception("Permission denied"))
        )

        from app.services.providers.gcp import GCPDriver

        with pytest.raises(Exception, match="Permission denied"):
            GCPDriver().terminate_host(_make_provider(), "troshka-abc")


class TestGCPDriverHostStatus:
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_running(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        inst = MagicMock(status="RUNNING")
        iface = MagicMock(network_i_p="10.0.1.5")
        iface.access_configs = [MagicMock(nat_i_p="35.192.0.1")]
        inst.network_interfaces = [iface]
        mock_compute_client.return_value = MagicMock(get=MagicMock(return_value=inst))

        from app.services.providers.gcp import GCPDriver

        result = GCPDriver().get_host_status(_make_provider(), "troshka-abc")
        assert result["state"] == "running"
        assert result["public_ip"] == "35.192.0.1"
        assert result["private_ip"] == "10.0.1.5"

    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_stopped(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        inst = MagicMock(status="TERMINATED", network_interfaces=[])
        mock_compute_client.return_value = MagicMock(get=MagicMock(return_value=inst))

        from app.services.providers.gcp import GCPDriver

        result = GCPDriver().get_host_status(_make_provider(), "troshka-abc")
        assert result["state"] == "stopped"
        assert result["public_ip"] is None

    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_all_state_mappings(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        mappings = {
            "RUNNING": "running",
            "TERMINATED": "stopped",
            "STOPPED": "stopped",
            "SUSPENDED": "stopped",
            "STAGING": "pending",
            "PROVISIONING": "pending",
            "STOPPING": "stopping",
            "SUSPENDING": "stopping",
            "WEIRD": "unknown",
        }
        from app.services.providers.gcp import GCPDriver

        driver = GCPDriver()
        for gcp_state, expected in mappings.items():
            inst = MagicMock(status=gcp_state, network_interfaces=[])
            mock_compute_client.return_value = MagicMock(
                get=MagicMock(return_value=inst)
            )
            result = driver.get_host_status(_make_provider(), "inst")
            assert result["state"] == expected, f"{gcp_state} -> {result['state']}"

    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_not_found(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        mock_compute_client.return_value = MagicMock(
            get=MagicMock(side_effect=Exception("was not found"))
        )

        from app.services.providers.gcp import GCPDriver

        assert GCPDriver().get_host_status(_make_provider(), "gone") is None

    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_real_error(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        mock_compute_client.return_value = MagicMock(
            get=MagicMock(side_effect=Exception("Permission denied"))
        )

        from app.services.providers.gcp import GCPDriver

        with pytest.raises(Exception, match="Permission denied"):
            GCPDriver().get_host_status(_make_provider(), "abc")


class TestGCPDriverPowerstate:
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_running(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        inst = MagicMock(status="RUNNING", network_interfaces=[])
        mock_compute_client.return_value = MagicMock(get=MagicMock(return_value=inst))

        from app.services.providers.gcp import GCPDriver

        assert GCPDriver().get_host_powerstate(_make_provider(), "abc") == "running"

    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_not_found(self, mock_get_creds, mock_compute_client):
        mock_get_creds.return_value = MagicMock()
        mock_compute_client.return_value = MagicMock(
            get=MagicMock(side_effect=Exception("was not found"))
        )

        from app.services.providers.gcp import GCPDriver

        assert GCPDriver().get_host_powerstate(_make_provider(), "gone") == "unknown"


class TestGCPDriverStartStop:
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_start_host(self, mock_get_creds, mock_compute_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_compute = MagicMock(start=MagicMock(return_value=_mock_operation()))
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        GCPDriver().start_host(_make_provider(), "troshka-abc")
        mock_compute.start.assert_called_once_with(
            project="test-project", zone="us-central1-a", instance="troshka-abc"
        )

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_stop_host(self, mock_get_creds, mock_compute_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_compute = MagicMock(stop=MagicMock(return_value=_mock_operation()))
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        GCPDriver().stop_host(_make_provider(), "troshka-abc")
        mock_compute.stop.assert_called_once_with(
            project="test-project", zone="us-central1-a", instance="troshka-abc"
        )


class TestGCPDriverResize:
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_resize_host(self, mock_get_creds, mock_compute_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_compute = MagicMock()
        mock_compute.stop.return_value = _mock_operation()
        mock_compute.start.return_value = _mock_operation()
        mock_compute.set_machine_type.return_value = _mock_operation()
        mock_compute.get.return_value = MagicMock(status="TERMINATED")
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch("app.services.providers.gcp.time.sleep"):
            with patch.dict("sys.modules", modules):
                result = GCPDriver().resize_host(
                    _make_provider(), "troshka-abc", "n2-highmem-64"
                )

        assert result["instance_type"] == "n2-highmem-64"
        assert result["total_vcpus"] == 64
        assert result["total_ram_mb"] == 512 * 1024
        mock_compute.stop.assert_called_once()
        mock_compute.set_machine_type.assert_called_once()
        mock_compute.start.assert_called_once()

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_resize_stop_timeout(self, mock_get_creds, mock_compute_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_compute = MagicMock()
        mock_compute.stop.return_value = _mock_operation()
        mock_compute.get.return_value = MagicMock(status="STOPPING")  # never TERMINATED
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        with patch("app.services.providers.gcp.time.sleep"):
            with pytest.raises(RuntimeError, match="did not stop"):
                GCPDriver().resize_host(
                    _make_provider(), "troshka-abc", "n2-highmem-64"
                )


class TestGCPDriverExtendStorage:
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_extend_basic(self, mock_get_creds, mock_disks_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_disks_client.return_value = MagicMock(
            resize=MagicMock(return_value=_mock_operation())
        )
        host = _make_host()
        db = MagicMock()

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().extend_host_storage(_make_provider(), host, db)

        assert result == {"old_size_gb": 500, "new_size_gb": 600}
        assert host.storage_size_gb == 600
        db.commit.assert_called_once()

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_extend_respects_max(self, mock_get_creds, mock_disks_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_disks_client.return_value = MagicMock(
            resize=MagicMock(return_value=_mock_operation())
        )
        host = _make_host()
        host.storage_size_gb = 1950

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().extend_host_storage(
                _make_provider(), host, MagicMock()
            )
        assert result["new_size_gb"] == 2000  # capped at max

    @patch("app.services.providers.gcp._get_credentials")
    def test_extend_at_max_raises(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        host = _make_host()
        host.storage_size_gb = 2000

        from app.services.providers.gcp import GCPDriver

        with pytest.raises(ValueError, match="Cannot extend"):
            GCPDriver().extend_host_storage(_make_provider(), host, MagicMock())

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_extend_custom_increment(
        self, mock_get_creds, mock_disks_client, mock_wait
    ):
        mock_get_creds.return_value = MagicMock()
        mock_disks_client.return_value = MagicMock(
            resize=MagicMock(return_value=_mock_operation())
        )

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        host = _make_host()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().extend_host_storage(
                _make_provider(), host, MagicMock(), increment_gb=200
            )
        assert result["new_size_gb"] == 700

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_disks_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_extend_with_agent_connected(
        self, mock_get_creds, mock_disks_client, mock_wait
    ):
        mock_get_creds.return_value = MagicMock()
        mock_disks_client.return_value = MagicMock(
            resize=MagicMock(return_value=_mock_operation())
        )
        host = _make_host()
        host.agent_connected = True

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            with patch(
                "app.services.troshkad_client.start_job", return_value={"job_id": "j1"}
            ):
                with patch("app.services.troshkad_client.wait_for_job"):
                    result = GCPDriver().extend_host_storage(
                        _make_provider(), host, MagicMock()
                    )
        assert result["new_size_gb"] == 600


class TestGCPDriverEIP:
    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_allocate_eip(self, mock_get_creds, mock_addr_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_addr = MagicMock()
        mock_addr.insert.return_value = _mock_operation()
        mock_addr.get.return_value = MagicMock(address="35.200.1.1")
        mock_addr_client.return_value = mock_addr

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().allocate_eip(
                _make_provider(), _make_host(), "eip-uuid-12345678"
            )

        assert result["public_ip"] == "35.200.1.1"
        assert result["allocation_id"] == "troshka-eip-eip-uuid-123"
        assert mock_addr.insert.call_args.kwargs["region"] == "us-central1"

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_associate_eip(
        self, mock_get_creds, mock_addr_client, mock_compute_client, mock_wait
    ):
        mock_get_creds.return_value = MagicMock()
        mock_addr_client.return_value = MagicMock(
            get=MagicMock(return_value=MagicMock(address="35.200.1.1"))
        )
        mock_compute = MagicMock()
        mock_compute.delete_access_config.return_value = _mock_operation()
        mock_compute.add_access_config.return_value = _mock_operation()
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().associate_eip(
                _make_provider(), _make_host(), "troshka-eip-abc"
            )

        assert result == {}
        mock_compute.delete_access_config.assert_called_once()
        mock_compute.add_access_config.assert_called_once()

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_compute_client")
    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_associate_eip_no_existing_access_config(
        self, mock_get_creds, mock_addr_client, mock_compute_client, mock_wait
    ):
        """delete_access_config not found should not prevent add."""
        mock_get_creds.return_value = MagicMock()
        mock_addr_client.return_value = MagicMock(
            get=MagicMock(return_value=MagicMock(address="35.200.1.1"))
        )
        mock_compute = MagicMock()
        mock_compute.delete_access_config.side_effect = Exception("was not found")
        mock_compute.add_access_config.return_value = _mock_operation()
        mock_compute_client.return_value = mock_compute

        from app.services.providers.gcp import GCPDriver

        modules, _ = _mock_compute_modules()
        with patch.dict("sys.modules", modules):
            result = GCPDriver().associate_eip(
                _make_provider(), _make_host(), "troshka-eip-abc"
            )
        assert result == {}
        mock_compute.add_access_config.assert_called_once()

    @patch("app.services.providers.gcp._wait_for_operation")
    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_release_eip(self, mock_get_creds, mock_addr_client, mock_wait):
        mock_get_creds.return_value = MagicMock()
        mock_addr = MagicMock(delete=MagicMock(return_value=_mock_operation()))
        mock_addr_client.return_value = mock_addr

        from app.services.providers.gcp import GCPDriver

        GCPDriver().release_eip(_make_provider(), "troshka-eip-abc")
        mock_addr.delete.assert_called_once_with(
            project="test-project", region="us-central1", address="troshka-eip-abc"
        )

    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_release_eip_already_gone(self, mock_get_creds, mock_addr_client):
        mock_get_creds.return_value = MagicMock()
        mock_addr_client.return_value = MagicMock(
            delete=MagicMock(side_effect=Exception("was not found"))
        )

        from app.services.providers.gcp import GCPDriver

        GCPDriver().release_eip(
            _make_provider(), "troshka-eip-gone"
        )  # should not raise

    @patch("app.services.providers.gcp._get_addresses_client")
    @patch("app.services.providers.gcp._get_credentials")
    def test_release_eip_real_error(self, mock_get_creds, mock_addr_client):
        mock_get_creds.return_value = MagicMock()
        mock_addr_client.return_value = MagicMock(
            delete=MagicMock(side_effect=Exception("Permission denied"))
        )

        from app.services.providers.gcp import GCPDriver

        with pytest.raises(Exception, match="Permission denied"):
            GCPDriver().release_eip(_make_provider(), "troshka-eip-abc")


class TestGCPDriverConsole:
    def _dns_mocks(self):
        """Create mock DNS module with a client that returns a zone mock."""
        mock_zone = MagicMock()
        mock_dns_client = MagicMock()
        mock_dns_client.zone.return_value = mock_zone
        mock_dns_mod = MagicMock()
        mock_dns_mod.Client.return_value = mock_dns_client
        mock_cloud = MagicMock()
        mock_cloud.dns = mock_dns_mod
        modules = {
            "google": MagicMock(),
            "google.cloud": mock_cloud,
            "google.cloud.dns": mock_dns_mod,
        }
        return modules, mock_dns_mod, mock_zone

    @patch("app.services.providers.gcp._get_credentials")
    def test_setup_console_creates_zone(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_zone.exists.return_value = False
        mock_zone.name_servers = ["ns1.google.com", "ns2.google.com"]

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            result = GCPDriver().setup_console(_make_provider(), "console.example.com")

        assert result["console_base_domain"] == "console.example.com"
        assert result["console_zone_id"] == "console-example-com"
        assert result["console_nameservers"] == ["ns1.google.com", "ns2.google.com"]
        mock_zone.create.assert_called_once()

    @patch("app.services.providers.gcp._get_credentials")
    def test_setup_console_existing_zone(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_zone.exists.return_value = True
        mock_zone.name_servers = ["ns1.google.com"]

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            result = GCPDriver().setup_console(_make_provider(), "console.example.com")
        mock_zone.create.assert_not_called()
        assert result["console_nameservers"] == ["ns1.google.com"]

    @patch("app.services.providers.gcp._get_credentials")
    def test_create_console_record(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_zone.list_resource_record_sets.return_value = []
        mock_changes = MagicMock()
        mock_zone.changes.return_value = mock_changes

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            GCPDriver().create_console_record(
                _make_provider(), _make_host(), "h1.console.example.com", "35.200.1.1"
            )

        mock_zone.resource_record_set.assert_called_once_with(
            "h1.console.example.com.", "A", 60, ["35.200.1.1"]
        )
        mock_changes.add_record_set.assert_called_once()
        mock_changes.create.assert_called_once()

    def test_create_console_record_no_zone_id(self):
        provider = _make_provider()
        provider.console_zone_id = None
        from app.services.providers.gcp import GCPDriver

        GCPDriver().create_console_record(
            provider, _make_host(), "h.example.com", "1.2.3.4"
        )  # no-op

    @patch("app.services.providers.gcp._get_credentials")
    def test_delete_console_record(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_rs = MagicMock()
        mock_rs.name = "h1.console.example.com."
        mock_rs.record_type = "A"
        mock_zone.list_resource_record_sets.return_value = [mock_rs]
        mock_changes = MagicMock()
        mock_zone.changes.return_value = mock_changes

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            GCPDriver().delete_console_record(
                _make_provider(), _make_host(), "h1.console.example.com", "35.200.1.1"
            )
        mock_changes.delete_record_set.assert_called_once_with(mock_rs)

    def test_delete_console_record_no_zone_id(self):
        provider = _make_provider()
        provider.console_zone_id = None
        from app.services.providers.gcp import GCPDriver

        GCPDriver().delete_console_record(
            provider, _make_host(), "h.example.com", "1.2.3.4"
        )

    @patch("app.services.providers.gcp._get_credentials")
    def test_delete_console_zone(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_zone.exists.return_value = True
        ns = MagicMock(record_type="NS")
        soa = MagicMock(record_type="SOA")
        a_rec = MagicMock(record_type="A")
        mock_zone.list_resource_record_sets.return_value = [ns, soa, a_rec]
        mock_changes = MagicMock()
        mock_zone.changes.return_value = mock_changes

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            with patch("app.services.providers.gcp.time.sleep"):
                GCPDriver().delete_console(_make_provider())

        mock_changes.delete_record_set.assert_called_once_with(a_rec)  # NS/SOA skipped
        mock_zone.delete.assert_called_once()

    def test_delete_console_no_zone_id(self):
        provider = _make_provider()
        provider.console_zone_id = None
        from app.services.providers.gcp import GCPDriver

        GCPDriver().delete_console(provider)  # returns early

    @patch("app.services.providers.gcp._get_credentials")
    def test_delete_console_zone_not_found(self, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        modules, _, mock_zone = self._dns_mocks()
        mock_zone.exists.return_value = False

        from app.services.providers.gcp import GCPDriver

        with patch.dict("sys.modules", modules):
            GCPDriver().delete_console(_make_provider())
        mock_zone.delete.assert_not_called()


class TestGCPDriverNoOps:
    def test_delete_key_pair(self):
        from app.services.providers.gcp import GCPDriver

        GCPDriver().delete_key_pair(_make_provider(), "some-key")

    def test_update_eip_ports(self):
        from app.services.providers.gcp import GCPDriver

        GCPDriver().update_eip_ports(
            _make_provider(), _make_host(), "alloc-id", [{"port": 80}]
        )


class TestGCPDriverDispatch:
    def test_get_gcp_driver(self):
        provider = MagicMock(type="gcp")
        driver = get_provider_driver(provider)
        from app.services.providers.gcp import GCPDriver

        assert isinstance(driver, GCPDriver)
        assert isinstance(driver, ProviderDriver)


class TestGenerateSSHKeypair:
    def test_generates_keypair(self):
        from app.services.providers.gcp import _generate_ssh_keypair

        private_key, public_key = _generate_ssh_keypair()
        assert private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert public_key.startswith("ssh-rsa ")


class TestGCPConstants:
    def test_curated_types(self):
        from app.services.providers.gcp import (
            GCP_CURATED_INSTANCE_TYPES,
            GCP_DEFAULT_INSTANCE_TYPE,
        )

        assert GCP_DEFAULT_INSTANCE_TYPE == "n2-highmem-32"
        assert GCP_DEFAULT_INSTANCE_TYPE in GCP_CURATED_INSTANCE_TYPES
        assert len(GCP_CURATED_INSTANCE_TYPES) == 7

    def test_ram_map_covers_curated(self):
        from app.services.providers.gcp import (
            GCP_CURATED_INSTANCE_TYPES,
            GCP_RAM_PER_VCPU_GB,
        )

        for itype in GCP_CURATED_INSTANCE_TYPES:
            assert itype in GCP_RAM_PER_VCPU_GB
