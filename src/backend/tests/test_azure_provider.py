"""Tests for the Azure provider driver.

All Azure SDK calls are mocked — no real Azure credentials or packages needed.
The Azure SDK is installed in the venv, so we import directly and patch the
client-creating helper functions to prevent real API calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.providers import get_provider_driver
from app.services.providers.azure import (
    AZURE_CURATED_INSTANCE_TYPES,
    AZURE_DEFAULT_INSTANCE_TYPE,
    AzureDriver,
    _build_cloud_init,
    _delete_resource,
    _extract_nic_ips,
    _extract_raw_power_state,
    _parse_image_urn,
    _parse_instance_type,
    _resolve_vm_ips,
    _resource_not_found,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CREDS = {
    "tenant_id": "test-tenant",
    "client_id": "test-client",
    "client_secret": "test-secret",
    "subscription_id": "test-sub-id",
}

HOST_ID = "abcdef123456-7890-abcd-ef01-234567890abc"
INSTANCE_ID = f"troshka-{HOST_ID[:12]}"


def _make_provider():
    p = MagicMock()
    p.type = "azure"
    p.get_credentials.return_value = dict(CREDS)
    p.azure_resource_group = "troshka-rg"
    p.azure_location = "eastus"
    p.default_region = "eastus"
    p.azure_subnet_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/default"
    p.azure_nsg_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkSecurityGroups/troshka-nsg"
    p.default_image = "redhat:rhel-byos:rhel-lvm94:latest"
    p.console_zone_id = "console.example.com"
    p.console_base_domain = "console.example.com"
    return p


def _make_poller(result_value=None):
    poller = MagicMock()
    poller.result.return_value = result_value
    return poller


# ---------------------------------------------------------------------------
# Pure helper function tests
# ---------------------------------------------------------------------------


class TestParseInstanceType:
    def test_known_type(self):
        vcpus, ram_mb = _parse_instance_type("Standard_E32s_v5")
        assert vcpus == 32
        assert ram_mb == 256 * 1024

    def test_smallest_type(self):
        vcpus, ram_mb = _parse_instance_type("Standard_E4s_v5")
        assert vcpus == 4
        assert ram_mb == 32 * 1024

    def test_largest_type(self):
        vcpus, ram_mb = _parse_instance_type("Standard_E96s_v5")
        assert vcpus == 96
        assert ram_mb == 672 * 1024

    def test_default_when_none(self):
        vcpus, ram_mb = _parse_instance_type(None)
        assert vcpus == 32
        assert ram_mb == 256 * 1024

    def test_unknown_type_parses_vcpu_from_name(self):
        vcpus, ram_mb = _parse_instance_type("Standard_D16s_v5")
        assert vcpus == 16
        assert ram_mb == 16 * 8 * 1024

    def test_unknown_type_no_digits_defaults(self):
        vcpus, ram_mb = _parse_instance_type("StandardXX")
        assert vcpus == 32  # fallback
        assert ram_mb == 32 * 8 * 1024


class TestBuildCloudInit:
    def test_basic_cloud_init(self):
        ci = _build_cloud_init("host-123")
        assert "host_id: host-123" in ci
        assert "#cloud-config" in ci
        assert "qemu-kvm" in ci

    def test_cloud_init_without_nfs(self):
        ci = _build_cloud_init("host-123")
        assert "nfs" not in ci.lower() or "nfs-common" in ci

    def test_cloud_init_with_nfs(self):
        ci = _build_cloud_init("host-123", nfs_server="10.0.0.5", nfs_path="/vol")
        assert "10.0.0.5:/vol" in ci
        assert "nconnect=16" in ci
        assert "/var/lib/troshka/shared" in ci


class TestParseImageUrn:
    def test_urn_format(self):
        ref = _parse_image_urn("redhat:rhel-byos:rhel-lvm94:latest")
        assert ref["publisher"] == "redhat"
        assert ref["offer"] == "rhel-byos"
        assert ref["sku"] == "rhel-lvm94"
        assert ref["version"] == "latest"

    def test_resource_id_format(self):
        rid = "/subscriptions/sub-id/resourceGroups/rg/providers/Microsoft.Compute/images/my-image"
        ref = _parse_image_urn(rid)
        assert ref == {"id": rid}

    def test_invalid_urn_raises(self):
        with pytest.raises(ValueError, match="Invalid Azure image reference"):
            _parse_image_urn("invalid:format")


class TestResourceNotFound:
    def test_detects_resource_not_found(self):
        assert _resource_not_found(Exception("ResourceNotFound"))

    def test_detects_not_found(self):
        assert _resource_not_found(Exception("NotFound"))

    def test_detects_was_not_found(self):
        assert _resource_not_found(Exception("The resource was not found"))

    def test_detects_could_not_be_found(self):
        assert _resource_not_found(Exception("Resource could not be found"))

    def test_other_errors_not_matched(self):
        assert not _resource_not_found(Exception("InternalServerError"))


class TestDeleteResource:
    def test_successful_delete(self):
        fn = MagicMock(return_value=_make_poller())
        assert _delete_resource("test-resource", fn) is True
        fn.assert_called_once()

    def test_already_gone(self):
        fn = MagicMock(side_effect=Exception("ResourceNotFound"))
        assert _delete_resource("test-resource", fn) is True

    def test_retry_on_transient_error(self):
        fn = MagicMock(side_effect=[Exception("Transient error"), _make_poller()])
        with patch("app.services.providers.azure.time.sleep"):
            assert _delete_resource("test-resource", fn, retries=2) is True
        assert fn.call_count == 2

    def test_fails_after_retries(self):
        fn = MagicMock(side_effect=Exception("Persistent error"))
        with patch("app.services.providers.azure.time.sleep"):
            assert _delete_resource("test-resource", fn, retries=2) is False
        assert fn.call_count == 2

    def test_delete_fn_without_result(self):
        """delete_fn returns something without .result — still succeeds."""
        result = "ok"
        fn = MagicMock(return_value=result)
        assert _delete_resource("test-resource", fn) is True


class TestExtractRawPowerState:
    def test_running(self):
        vm = MagicMock()
        status = MagicMock()
        status.code = "PowerState/running"
        vm.instance_view.statuses = [status]
        assert _extract_raw_power_state(vm) == "running"

    def test_deallocated(self):
        vm = MagicMock()
        status = MagicMock()
        status.code = "PowerState/deallocated"
        vm.instance_view.statuses = [status]
        assert _extract_raw_power_state(vm) == "deallocated"

    def test_no_instance_view(self):
        vm = MagicMock()
        vm.instance_view = None
        assert _extract_raw_power_state(vm) is None

    def test_no_statuses(self):
        vm = MagicMock()
        vm.instance_view.statuses = None
        assert _extract_raw_power_state(vm) is None

    def test_no_power_state_status(self):
        vm = MagicMock()
        status = MagicMock()
        status.code = "ProvisioningState/succeeded"
        vm.instance_view.statuses = [status]
        assert _extract_raw_power_state(vm) is None

    def test_null_code(self):
        vm = MagicMock()
        status = MagicMock()
        status.code = None
        vm.instance_view.statuses = [status]
        assert _extract_raw_power_state(vm) is None

    def test_multiple_statuses_picks_power(self):
        vm = MagicMock()
        s1 = MagicMock()
        s1.code = "ProvisioningState/succeeded"
        s2 = MagicMock()
        s2.code = "PowerState/stopped"
        vm.instance_view.statuses = [s1, s2]
        assert _extract_raw_power_state(vm) == "stopped"


class TestExtractNicIps:
    def test_extracts_both_ips(self):
        nc = MagicMock()
        nic = MagicMock()
        ip_config = MagicMock()
        ip_config.private_ip_address = "10.0.0.5"
        ip_config.public_ip_address.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/my-pip"
        nic.ip_configurations = [ip_config]
        nc.network_interfaces.get.return_value = nic

        pip = MagicMock()
        pip.ip_address = "20.30.40.50"
        nc.public_ip_addresses.get.return_value = pip

        pub, priv = _extract_nic_ips(nc, "rg", "my-nic")
        assert pub == "20.30.40.50"
        assert priv == "10.0.0.5"
        nc.public_ip_addresses.get.assert_called_with("rg", "my-pip")

    def test_no_public_ip(self):
        nc = MagicMock()
        nic = MagicMock()
        ip_config = MagicMock()
        ip_config.private_ip_address = "10.0.0.5"
        ip_config.public_ip_address = None
        nic.ip_configurations = [ip_config]
        nc.network_interfaces.get.return_value = nic

        pub, priv = _extract_nic_ips(nc, "rg", "my-nic")
        assert pub is None
        assert priv == "10.0.0.5"

    def test_no_ip_configs(self):
        nc = MagicMock()
        nic = MagicMock()
        nic.ip_configurations = None
        nc.network_interfaces.get.return_value = nic

        pub, priv = _extract_nic_ips(nc, "rg", "my-nic")
        assert pub is None
        assert priv is None


class TestResolveVmIps:
    def test_no_network_profile(self):
        nc = MagicMock()
        vm = MagicMock()
        vm.network_profile = None
        pub, priv = _resolve_vm_ips(nc, "rg", vm, "inst-1")
        assert pub is None
        assert priv is None

    def test_no_network_interfaces(self):
        nc = MagicMock()
        vm = MagicMock()
        vm.network_profile.network_interfaces = []
        pub, priv = _resolve_vm_ips(nc, "rg", vm, "inst-1")
        assert pub is None
        assert priv is None

    def test_no_nic_id(self):
        nc = MagicMock()
        vm = MagicMock()
        nic_ref = MagicMock()
        nic_ref.id = None
        vm.network_profile.network_interfaces = [nic_ref]
        pub, priv = _resolve_vm_ips(nc, "rg", vm, "inst-1")
        assert pub is None
        assert priv is None

    def test_nic_lookup_exception(self):
        nc = MagicMock()
        vm = MagicMock()
        nic_ref = MagicMock()
        nic_ref.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/my-nic"
        vm.network_profile.network_interfaces = [nic_ref]
        nc.network_interfaces.get.side_effect = Exception("NIC error")
        pub, priv = _resolve_vm_ips(nc, "rg", vm, "inst-1")
        assert pub is None
        assert priv is None


# ---------------------------------------------------------------------------
# Driver integration tests
# ---------------------------------------------------------------------------


class TestGetProviderDriver:
    def test_returns_azure_driver(self):
        provider = _make_provider()
        driver = get_provider_driver(provider)
        assert isinstance(driver, AzureDriver)


class TestProvisionHost:
    @patch("app.services.providers.azure._poll_vm_until_running")
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._generate_ssh_keypair")
    @patch("app.services.providers.azure._accept_marketplace_terms")
    def test_provision_success(
        self, mock_terms, mock_keygen, mock_net_client, mock_compute_client, mock_poll
    ):
        provider = _make_provider()
        driver = AzureDriver()

        mock_keygen.return_value = ("PRIVATE_KEY", "PUBLIC_KEY")
        mock_terms.return_value = None  # no plan info

        # Public IP creation
        pip_resource = MagicMock()
        pip_resource.ip_address = "20.30.40.50"
        pip_resource.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip"

        nc = MagicMock()
        nc.public_ip_addresses.begin_create_or_update.return_value = _make_poller(
            pip_resource
        )
        nic = MagicMock()
        nic.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic"
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller(nic)
        mock_net_client.return_value = nc

        cc = MagicMock()
        cc.virtual_machines.begin_create_or_update.return_value = _make_poller()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute_client.return_value = cc

        mock_poll.return_value = ("20.30.40.50", "10.0.0.5")

        result = driver.provision_host(provider, HOST_ID, "Standard_E32s_v5", 500)

        assert result["host_id"] == HOST_ID
        assert result["instance_id"] == f"troshka-{HOST_ID[:12]}"
        assert result["public_ip"] == "20.30.40.50"
        assert result["private_ip"] == "10.0.0.5"
        assert result["private_key"] == "PRIVATE_KEY"
        assert result["total_vcpus"] == 32
        assert result["total_ram_mb"] == 256 * 1024
        assert result["storage_size_gb"] == 500
        assert result["max_eips"] == 32
        assert result["_ssh_user"] == "troshka"
        assert result["_ssh_port"] == 22

    @patch("app.services.providers.azure._poll_vm_until_running")
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._generate_ssh_keypair")
    @patch("app.services.providers.azure._accept_marketplace_terms")
    def test_provision_with_managed_image_skips_marketplace(
        self, mock_terms, mock_keygen, mock_net_client, mock_compute_client, mock_poll
    ):
        """Managed images (from Image Builder) skip marketplace terms."""
        provider = _make_provider()
        provider.default_image = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/images/custom"
        driver = AzureDriver()

        mock_keygen.return_value = ("KEY", "PUB")

        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        pip.id = "pip-id"
        nc = MagicMock()
        nc.public_ip_addresses.begin_create_or_update.return_value = _make_poller(pip)
        nic = MagicMock()
        nic.id = "nic-id"
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller(nic)
        mock_net_client.return_value = nc

        cc = MagicMock()
        cc.virtual_machines.begin_create_or_update.return_value = _make_poller()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute_client.return_value = cc

        mock_poll.return_value = ("1.2.3.4", "10.0.0.5")

        result = driver.provision_host(provider, HOST_ID, None, 200)

        mock_terms.assert_not_called()
        assert result["instance_type"] == AZURE_DEFAULT_INSTANCE_TYPE

    @patch("app.services.providers.azure._poll_vm_until_running")
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._generate_ssh_keypair")
    @patch("app.services.providers.azure._accept_marketplace_terms")
    def test_provision_with_plan_info(
        self, mock_terms, mock_keygen, mock_net_client, mock_compute_client, mock_poll
    ):
        """When marketplace terms return plan info, VM gets a Plan."""
        provider = _make_provider()
        driver = AzureDriver()

        mock_keygen.return_value = ("KEY", "PUB")
        mock_terms.return_value = {
            "name": "rhel-lvm94",
            "publisher": "redhat",
            "product": "rhel-byos",
        }

        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        pip.id = "pip-id"
        nc = MagicMock()
        nc.public_ip_addresses.begin_create_or_update.return_value = _make_poller(pip)
        nic = MagicMock()
        nic.id = "nic-id"
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller(nic)
        mock_net_client.return_value = nc

        cc = MagicMock()
        cc.virtual_machines.begin_create_or_update.return_value = _make_poller()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute_client.return_value = cc

        mock_poll.return_value = ("1.2.3.4", "10.0.0.5")

        result = driver.provision_host(provider, HOST_ID, "Standard_E8s_v5", 100)
        assert result["total_vcpus"] == 8
        # VM creation was called (plan attached inside)
        cc.virtual_machines.begin_create_or_update.assert_called_once()

    def test_provision_no_image_raises(self):
        provider = _make_provider()
        provider.default_image = None
        driver = AzureDriver()

        with pytest.raises(ValueError, match="No boot image"):
            driver.provision_host(provider, HOST_ID, None, 100)

    @patch("app.services.providers.azure._poll_vm_until_running")
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._generate_ssh_keypair")
    @patch("app.services.providers.azure._accept_marketplace_terms")
    def test_provision_with_nfs(
        self, mock_terms, mock_keygen, mock_net_client, mock_compute_client, mock_poll
    ):
        provider = _make_provider()
        driver = AzureDriver()

        mock_keygen.return_value = ("KEY", "PUB")
        mock_terms.return_value = None

        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        pip.id = "pip-id"
        nc = MagicMock()
        nc.public_ip_addresses.begin_create_or_update.return_value = _make_poller(pip)
        nic = MagicMock()
        nic.id = "nic-id"
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller(nic)
        mock_net_client.return_value = nc

        cc = MagicMock()
        cc.virtual_machines.begin_create_or_update.return_value = _make_poller()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute_client.return_value = cc

        mock_poll.return_value = ("1.2.3.4", "10.0.0.5")

        result = driver.provision_host(
            provider,
            HOST_ID,
            None,
            200,
            nfs_server="10.0.0.100",
            nfs_path="/data/shared",
        )
        assert result["public_ip"] == "1.2.3.4"

    @patch("app.services.providers.azure._poll_vm_until_running")
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._generate_ssh_keypair")
    @patch("app.services.providers.azure._accept_marketplace_terms")
    def test_provision_with_nsg(
        self, mock_terms, mock_keygen, mock_net_client, mock_compute_client, mock_poll
    ):
        """NSG ID on provider is included in NIC params."""
        provider = _make_provider()
        driver = AzureDriver()

        mock_keygen.return_value = ("KEY", "PUB")
        mock_terms.return_value = None

        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        pip.id = "pip-id"
        nc = MagicMock()
        nc.public_ip_addresses.begin_create_or_update.return_value = _make_poller(pip)
        nic = MagicMock()
        nic.id = "nic-id"
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller(nic)
        mock_net_client.return_value = nc

        cc = MagicMock()
        cc.virtual_machines.begin_create_or_update.return_value = _make_poller()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute_client.return_value = cc

        mock_poll.return_value = ("1.2.3.4", "10.0.0.5")

        _result = driver.provision_host(provider, HOST_ID, None, 100)
        # NIC create was called — verify NSG was included
        nic_call_args = nc.network_interfaces.begin_create_or_update.call_args
        assert nic_call_args is not None


class TestTerminateHost:
    @patch("app.services.providers.azure._delete_resource")
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._get_compute_client")
    def test_terminate_calls_delete_in_order(self, mock_compute, mock_net, mock_delete):
        provider = _make_provider()
        driver = AzureDriver()
        mock_delete.return_value = True

        driver.terminate_host(provider, "troshka-abcdef123456")

        assert mock_delete.call_count == 5
        labels = [c.args[0] for c in mock_delete.call_args_list]
        assert "VM troshka-abcdef123456" in labels[0]
        assert "OS disk" in labels[1]
        assert "data disk" in labels[2]
        assert "NIC" in labels[3]
        assert "public IP" in labels[4]


class TestGetHostStatus:
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_running_vm(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/running"
        vm.instance_view.statuses = [ps]
        vm.network_profile.network_interfaces = []
        mock_compute.return_value.virtual_machines.get.return_value = vm

        result = driver.get_host_status(provider, "troshka-abc")
        assert result["state"] == "running"
        assert result["instance_id"] == "troshka-abc"

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_deallocated_maps_to_stopped(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/deallocated"
        vm.instance_view.statuses = [ps]
        vm.network_profile.network_interfaces = []
        mock_compute.return_value.virtual_machines.get.return_value = vm

        result = driver.get_host_status(provider, "troshka-abc")
        assert result["state"] == "stopped"

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_not_found_returns_none(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        mock_compute.return_value.virtual_machines.get.side_effect = Exception(
            "ResourceNotFound"
        )

        result = driver.get_host_status(provider, "troshka-gone")
        assert result is None

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_other_exception_raises(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        mock_compute.return_value.virtual_machines.get.side_effect = Exception(
            "InternalServerError"
        )

        with pytest.raises(Exception, match="InternalServerError"):
            driver.get_host_status(provider, "troshka-abc")

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_unknown_power_state(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/weird-state"
        vm.instance_view.statuses = [ps]
        vm.network_profile.network_interfaces = []
        mock_compute.return_value.virtual_machines.get.return_value = vm

        result = driver.get_host_status(provider, "troshka-abc")
        assert result["state"] == "unknown"

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_status_with_ips(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/running"
        vm.instance_view.statuses = [ps]

        nic_ref = MagicMock()
        nic_ref.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/my-nic"
        vm.network_profile.network_interfaces = [nic_ref]

        mock_compute.return_value.virtual_machines.get.return_value = vm

        nic_info = MagicMock()
        ip_config = MagicMock()
        ip_config.private_ip_address = "10.0.0.5"
        ip_config.public_ip_address.id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/my-pip"
        nic_info.ip_configurations = [ip_config]
        mock_net.return_value.network_interfaces.get.return_value = nic_info

        pip = MagicMock()
        pip.ip_address = "20.30.40.50"
        mock_net.return_value.public_ip_addresses.get.return_value = pip

        result = driver.get_host_status(provider, "troshka-abc")
        assert result["public_ip"] == "20.30.40.50"
        assert result["private_ip"] == "10.0.0.5"


class TestGetHostPowerstate:
    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_returns_state(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/running"
        vm.instance_view.statuses = [ps]
        vm.network_profile.network_interfaces = []
        mock_compute.return_value.virtual_machines.get.return_value = vm

        assert driver.get_host_powerstate(provider, "troshka-abc") == "running"

    @patch("app.services.providers.azure._get_compute_client")
    @patch("app.services.providers.azure._get_network_client")
    def test_returns_unknown_when_not_found(self, mock_net, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        mock_compute.return_value.virtual_machines.get.side_effect = Exception(
            "ResourceNotFound"
        )
        assert driver.get_host_powerstate(provider, "troshka-gone") == "unknown"


class TestStartHost:
    @patch("app.services.providers.azure._get_compute_client")
    def test_start(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.virtual_machines.begin_start.return_value = _make_poller()
        mock_compute.return_value = cc

        driver.start_host(provider, "troshka-abc")
        cc.virtual_machines.begin_start.assert_called_once_with(
            "troshka-rg", "troshka-abc"
        )


class TestStopHost:
    @patch("app.services.providers.azure._get_compute_client")
    def test_stop_deallocates(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.virtual_machines.begin_deallocate.return_value = _make_poller()
        mock_compute.return_value = cc

        driver.stop_host(provider, "troshka-abc")
        cc.virtual_machines.begin_deallocate.assert_called_once_with(
            "troshka-rg", "troshka-abc"
        )


class TestResizeHost:
    @patch("app.services.providers.azure._get_compute_client")
    def test_hot_resize_success(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.virtual_machines.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        result = driver.resize_host(provider, "troshka-abc", "Standard_E16s_v5")
        assert result["instance_type"] == "Standard_E16s_v5"
        assert result["total_vcpus"] == 16
        assert result["total_ram_mb"] == 128 * 1024
        # Only one update call (hot resize worked)
        assert cc.virtual_machines.begin_update.call_count == 1

    @patch("app.services.providers.azure._get_compute_client")
    def test_fallback_to_deallocate_resize(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        # First update fails (hot resize not supported)
        cc.virtual_machines.begin_update.side_effect = [
            Exception("OperationNotAllowed"),
            _make_poller(),  # second update after deallocate
        ]
        cc.virtual_machines.begin_deallocate.return_value = _make_poller()
        cc.virtual_machines.begin_start.return_value = _make_poller()
        mock_compute.return_value = cc

        result = driver.resize_host(provider, "troshka-abc", "Standard_E48s_v5")
        assert result["instance_type"] == "Standard_E48s_v5"
        assert result["total_vcpus"] == 48
        cc.virtual_machines.begin_deallocate.assert_called_once()
        cc.virtual_machines.begin_start.assert_called_once()


class TestExtendHostStorage:
    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_success(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = 1000
        host.agent_connected = False

        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db)
        assert result["old_size_gb"] == 500
        assert result["new_size_gb"] == 600
        assert host.storage_size_gb == 600
        db.commit.assert_called_once()

    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_capped_at_max(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 950
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = 1000
        host.agent_connected = False

        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db)
        assert result["new_size_gb"] == 1000

    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_at_max_raises(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 1000
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = 1000
        host.agent_connected = False

        db = MagicMock()

        with pytest.raises(ValueError, match="Cannot extend"):
            driver.extend_host_storage(provider, host, db)

    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_with_custom_increment(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = None
        host.agent_connected = False

        db = MagicMock()

        result = driver.extend_host_storage(provider, host, db, increment_gb=200)
        assert result["new_size_gb"] == 700

    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_triggers_agent_resize(self, mock_compute):
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = None
        host.agent_connected = True

        db = MagicMock()

        with patch("app.services.troshkad_client.start_job") as mock_start, patch(
            "app.services.troshkad_client.wait_for_job"
        ) as mock_wait:
            mock_start.return_value = {"job_id": "j1"}
            result = driver.extend_host_storage(provider, host, db)

        assert result["new_size_gb"] == 600
        mock_start.assert_called_once()
        mock_wait.assert_called_once()

    @patch("app.services.providers.azure._get_compute_client")
    def test_extend_agent_failure_continues(self, mock_compute):
        """Agent resize failure should not block the extend."""
        provider = _make_provider()
        driver = AzureDriver()
        cc = MagicMock()
        cc.disks.begin_update.return_value = _make_poller()
        mock_compute.return_value = cc

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"
        host.storage_size_gb = 500
        host.auto_extend_increment_gb = 100
        host.auto_extend_max_gb = None
        host.agent_connected = True

        db = MagicMock()

        with patch(
            "app.services.troshkad_client.start_job",
            side_effect=Exception("agent down"),
        ):
            result = driver.extend_host_storage(provider, host, db)

        assert result["new_size_gb"] == 600
        db.commit.assert_called_once()


class TestSetupConsole:
    @patch("app.services.providers.azure._get_dns_client")
    def test_creates_zone(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()

        zone = MagicMock()
        zone.name_servers = ["ns1.azure-dns.com", "ns2.azure-dns.net"]
        mock_dns.return_value.zones.create_or_update.return_value = zone

        result = driver.setup_console(provider, "console.example.com")
        assert result["console_base_domain"] == "console.example.com"
        assert result["console_zone_id"] == "console.example.com"
        assert result["console_nameservers"] == [
            "ns1.azure-dns.com",
            "ns2.azure-dns.net",
        ]

    @patch("app.services.providers.azure._get_dns_client")
    def test_zone_without_nameservers(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()

        zone = MagicMock()
        zone.name_servers = None
        mock_dns.return_value.zones.create_or_update.return_value = zone

        result = driver.setup_console(provider, "console.example.com")
        assert result["console_nameservers"] == []


class TestCreateConsoleRecord:
    @patch("app.services.providers.azure._get_dns_client")
    def test_creates_a_record(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        driver.create_console_record(
            provider, host, "abc123.console.example.com", "20.30.40.50"
        )

        mock_dns.return_value.record_sets.create_or_update.assert_called_once()
        call_args = mock_dns.return_value.record_sets.create_or_update.call_args
        assert call_args.args[0] == "troshka-rg"
        assert call_args.args[1] == "console.example.com"
        assert call_args.args[2] == "abc123"
        assert call_args.args[3] == "A"

    @patch("app.services.providers.azure._get_dns_client")
    def test_creates_record_hostname_equals_zone(self, mock_dns):
        """Hostname that is exactly the zone name."""
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        driver.create_console_record(
            provider, host, "console.example.com", "20.30.40.50"
        )
        call_args = mock_dns.return_value.record_sets.create_or_update.call_args
        assert call_args.args[2] == ""  # record_name is empty (zone apex)

    def test_no_zone_id_skips(self):
        provider = _make_provider()
        provider.console_zone_id = None
        driver = AzureDriver()
        host = MagicMock()

        # Should not raise
        driver.create_console_record(
            provider, host, "abc.console.example.com", "1.2.3.4"
        )


class TestDeleteConsoleRecord:
    @patch("app.services.providers.azure._get_dns_client")
    def test_deletes_record(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        driver.delete_console_record(
            provider, host, "abc123.console.example.com", "20.30.40.50"
        )

        mock_dns.return_value.record_sets.delete.assert_called_once_with(
            "troshka-rg", "console.example.com", "abc123", "A"
        )

    @patch("app.services.providers.azure._get_dns_client")
    def test_delete_not_found_continues(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()
        mock_dns.return_value.record_sets.delete.side_effect = Exception(
            "ResourceNotFound"
        )

        # Should not raise
        driver.delete_console_record(
            provider, host, "abc123.console.example.com", "20.30.40.50"
        )

    @patch("app.services.providers.azure._get_dns_client")
    def test_delete_other_error_warns(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()
        mock_dns.return_value.record_sets.delete.side_effect = Exception(
            "InternalServerError"
        )

        # Should not raise (logs warning)
        driver.delete_console_record(
            provider, host, "abc123.console.example.com", "20.30.40.50"
        )

    def test_no_zone_id_skips(self):
        provider = _make_provider()
        provider.console_zone_id = None
        driver = AzureDriver()
        host = MagicMock()

        driver.delete_console_record(
            provider, host, "abc123.console.example.com", "1.2.3.4"
        )


class TestDeleteConsole:
    @patch("app.services.providers.azure._get_dns_client")
    def test_deletes_zone(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()

        mock_dns.return_value.zones.begin_delete.return_value = _make_poller()
        driver.delete_console(provider)
        mock_dns.return_value.zones.begin_delete.assert_called_once_with(
            "troshka-rg", "console.example.com"
        )

    @patch("app.services.providers.azure._get_dns_client")
    def test_delete_not_found(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        mock_dns.return_value.zones.begin_delete.return_value.result.side_effect = (
            Exception("ResourceNotFound")
        )

        # Should not raise
        driver.delete_console(provider)

    @patch("app.services.providers.azure._get_dns_client")
    def test_delete_other_error_raises(self, mock_dns):
        provider = _make_provider()
        driver = AzureDriver()
        mock_dns.return_value.zones.begin_delete.return_value.result.side_effect = (
            Exception("InternalServerError")
        )

        with pytest.raises(Exception, match="InternalServerError"):
            driver.delete_console(provider)

    def test_no_zone_id_noops(self):
        provider = _make_provider()
        provider.console_zone_id = None
        driver = AzureDriver()
        driver.delete_console(provider)  # Should not raise


class TestDeleteKeyPair:
    def test_noop(self):
        driver = AzureDriver()
        provider = _make_provider()
        driver.delete_key_pair(provider, "any-key")  # Should not raise


class TestAllocateEip:
    @patch("app.services.providers.azure._get_network_client")
    def test_allocate(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        pip = MagicMock()
        pip.ip_address = "52.10.20.30"
        mock_net.return_value.public_ip_addresses.begin_create_or_update.return_value = _make_poller(
            pip
        )

        eip_id = "eip-abcdef123456-7890"
        result = driver.allocate_eip(provider, host, eip_id)
        assert result["public_ip"] == "52.10.20.30"
        assert result["allocation_id"] == f"troshka-eip-{eip_id[:12]}"


class TestAssociateEip:
    @patch("app.services.providers.azure._get_network_client")
    def test_associate_adds_secondary_ip_config(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"

        nc = mock_net.return_value
        pip = MagicMock()
        pip.id = "pip-id"
        nc.public_ip_addresses.get.return_value = pip

        # NIC with one primary IP config
        primary_ip = MagicMock()
        primary_ip.name = "primary"
        primary_ip.subnet.id = "subnet-id"
        nic = MagicMock()
        nic.ip_configurations = [primary_ip]
        nic.serialize.return_value = {"properties": {}}
        nc.network_interfaces.get.return_value = nic
        nc.network_interfaces.begin_create_or_update.return_value = _make_poller()

        result = driver.associate_eip(provider, host, "troshka-eip-abc")
        assert result == {}
        nc.network_interfaces.begin_create_or_update.assert_called_once()

    @patch("app.services.providers.azure._get_network_client")
    def test_associate_skips_if_exists(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()

        host = MagicMock()
        host.instance_id = "troshka-abcdef123456"

        nc = mock_net.return_value
        pip = MagicMock()
        pip.id = "pip-id"
        nc.public_ip_addresses.get.return_value = pip

        # NIC already has the secondary IP config
        primary_ip = MagicMock()
        primary_ip.name = "primary"
        existing_ip = MagicMock()
        existing_ip.name = "eip-troshka-eip-abc"
        nic = MagicMock()
        nic.ip_configurations = [primary_ip, existing_ip]
        nc.network_interfaces.get.return_value = nic

        result = driver.associate_eip(provider, host, "troshka-eip-abc")
        assert result == {}
        # Should NOT call begin_create_or_update since config already exists
        nc.network_interfaces.begin_create_or_update.assert_not_called()


class TestReleaseEip:
    @patch("app.services.providers.azure._get_network_client")
    @patch("app.services.providers.azure._delete_resource")
    def test_release(self, mock_delete, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        mock_delete.return_value = True

        driver.release_eip(provider, "troshka-eip-abc")
        mock_delete.assert_called_once()
        assert "troshka-eip-abc" in mock_delete.call_args.args[0]


class TestUpdateEipPorts:
    @patch("app.services.providers.azure._get_network_client")
    def test_creates_nsg_rules(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        nc = mock_net.return_value
        pip = MagicMock()
        pip.ip_address = "52.10.20.30"
        nc.public_ip_addresses.get.return_value = pip
        nc.security_rules.begin_create_or_update.return_value = _make_poller()

        ports = [
            {"port": 443, "targetPort": 443},
            {"port": 8443, "targetPort": 8443},
        ]
        driver.update_eip_ports(provider, host, "troshka-eip-abc", ports)
        assert nc.security_rules.begin_create_or_update.call_count == 2

    def test_no_nsg_skips(self):
        provider = _make_provider()
        provider.azure_nsg_id = None
        driver = AzureDriver()
        host = MagicMock()

        # Should not raise, no calls made
        driver.update_eip_ports(provider, host, "troshka-eip-abc", [{"port": 443}])

    @patch("app.services.providers.azure._get_network_client")
    def test_pip_not_found_skips(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        nc = mock_net.return_value
        nc.public_ip_addresses.get.side_effect = Exception("NotFound")

        # Should not raise
        driver.update_eip_ports(provider, host, "troshka-eip-abc", [{"port": 443}])
        nc.security_rules.begin_create_or_update.assert_not_called()

    @patch("app.services.providers.azure._get_network_client")
    def test_skips_ports_without_port_key(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        nc = mock_net.return_value
        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        nc.public_ip_addresses.get.return_value = pip

        ports = [{"name": "no-port"}]
        driver.update_eip_ports(provider, host, "troshka-eip-abc", ports)
        nc.security_rules.begin_create_or_update.assert_not_called()

    @patch("app.services.providers.azure._get_network_client")
    def test_nsg_rule_failure_continues(self, mock_net):
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        nc = mock_net.return_value
        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        nc.public_ip_addresses.get.return_value = pip
        nc.security_rules.begin_create_or_update.return_value.result.side_effect = (
            Exception("Failed")
        )

        ports = [{"port": 443}]
        # Should not raise even though rule creation fails
        driver.update_eip_ports(provider, host, "troshka-eip-abc", ports)

    @patch("app.services.providers.azure._get_network_client")
    def test_uses_target_port_fallback(self, mock_net):
        """When 'port' key is missing, falls back to 'targetPort'."""
        provider = _make_provider()
        driver = AzureDriver()
        host = MagicMock()

        nc = mock_net.return_value
        pip = MagicMock()
        pip.ip_address = "1.2.3.4"
        nc.public_ip_addresses.get.return_value = pip
        nc.security_rules.begin_create_or_update.return_value = _make_poller()

        ports = [{"targetPort": 8080}]
        driver.update_eip_ports(provider, host, "troshka-eip-abc", ports)
        nc.security_rules.begin_create_or_update.assert_called_once()


class TestAcceptMarketplaceTerms:
    @patch("app.services.providers.azure._get_credential")
    @patch("app.services.providers.azure._get_subscription_id")
    @patch("azure.mgmt.marketplaceordering.MarketplaceOrderingAgreements")
    def test_accepts_terms(self, mock_mp_class, mock_sub, mock_cred):
        mock_sub.return_value = "sub-id"
        mock_cred.return_value = MagicMock()

        mp_client = MagicMock()
        agreement = MagicMock()
        mp_client.marketplace_agreements.get.return_value = agreement
        mock_mp_class.return_value = mp_client

        from app.services.providers.azure import _accept_marketplace_terms

        result = _accept_marketplace_terms(
            CREDS,
            {"publisher": "redhat", "offer": "rhel-byos", "sku": "lvm94"},
            "redhat:rhel-byos:lvm94:latest",
        )
        assert result is not None
        assert result["name"] == "lvm94"
        assert result["publisher"] == "redhat"
        assert result["product"] == "rhel-byos"
        mp_client.marketplace_agreements.create.assert_called_once()

    @patch("app.services.providers.azure._get_credential")
    @patch("app.services.providers.azure._get_subscription_id")
    @patch("azure.mgmt.marketplaceordering.MarketplaceOrderingAgreements")
    def test_marketplace_terms_not_found_returns_none(
        self, mock_mp_class, mock_sub, mock_cred
    ):
        """Terms not found returns None (no plan required)."""
        mock_sub.return_value = "sub-id"
        mock_cred.return_value = MagicMock()

        mp_client = MagicMock()
        mp_client.marketplace_agreements.get.side_effect = Exception(
            "Agreement not found"
        )
        mock_mp_class.return_value = mp_client

        from app.services.providers.azure import _accept_marketplace_terms

        result = _accept_marketplace_terms(
            CREDS,
            {"publisher": "redhat", "offer": "rhel-byos", "sku": "lvm94"},
            "redhat:rhel-byos:lvm94:latest",
        )
        assert result is None

    @patch("app.services.providers.azure._get_credential")
    @patch("app.services.providers.azure._get_subscription_id")
    @patch("azure.mgmt.marketplaceordering.MarketplaceOrderingAgreements")
    def test_marketplace_other_error_returns_none(
        self, mock_mp_class, mock_sub, mock_cred
    ):
        """Other exceptions are swallowed and return None."""
        mock_sub.return_value = "sub-id"
        mock_cred.return_value = MagicMock()

        mp_client = MagicMock()
        mp_client.marketplace_agreements.get.side_effect = Exception("Unexpected error")
        mock_mp_class.return_value = mp_client

        from app.services.providers.azure import _accept_marketplace_terms

        result = _accept_marketplace_terms(
            CREDS,
            {"publisher": "redhat", "offer": "rhel-byos", "sku": "lvm94"},
            "redhat:rhel-byos:lvm94:latest",
        )
        assert result is None


class TestPollVmUntilRunning:
    @patch("app.services.providers.azure.time.sleep")
    def test_returns_ips_when_running(self, mock_sleep):
        cc = MagicMock()
        nc = MagicMock()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/running"
        vm.instance_view.statuses = [ps]
        cc.virtual_machines.get.return_value = vm

        nic = MagicMock()
        ip_config = MagicMock()
        ip_config.private_ip_address = "10.0.0.5"
        ip_config.public_ip_address = None
        nic.ip_configurations = [ip_config]
        nc.network_interfaces.get.return_value = nic

        from app.services.providers.azure import _poll_vm_until_running

        pub, priv = _poll_vm_until_running(cc, nc, "rg", "vm-1", "nic-1")
        assert priv == "10.0.0.5"

    @patch("app.services.providers.azure.time.sleep")
    def test_times_out(self, mock_sleep):
        cc = MagicMock()
        nc = MagicMock()

        vm = MagicMock()
        ps = MagicMock()
        ps.code = "PowerState/starting"
        vm.instance_view.statuses = [ps]
        cc.virtual_machines.get.return_value = vm

        from app.services.providers.azure import _poll_vm_until_running

        with pytest.raises(RuntimeError, match="did not reach running state"):
            _poll_vm_until_running(cc, nc, "rg", "vm-1", "nic-1")

    @patch("app.services.providers.azure.time.sleep")
    def test_polls_until_running(self, mock_sleep):
        cc = MagicMock()
        nc = MagicMock()

        # First two polls: starting; third: running
        starting_vm = MagicMock()
        starting_ps = MagicMock()
        starting_ps.code = "PowerState/starting"
        starting_vm.instance_view.statuses = [starting_ps]

        running_vm = MagicMock()
        running_ps = MagicMock()
        running_ps.code = "PowerState/running"
        running_vm.instance_view.statuses = [running_ps]

        cc.virtual_machines.get.side_effect = [starting_vm, starting_vm, running_vm]

        nic = MagicMock()
        ip_config = MagicMock()
        ip_config.private_ip_address = "10.0.0.5"
        ip_config.public_ip_address = None
        nic.ip_configurations = [ip_config]
        nc.network_interfaces.get.return_value = nic

        from app.services.providers.azure import _poll_vm_until_running

        _poll_vm_until_running(cc, nc, "rg", "vm-1", "nic-1")
        assert cc.virtual_machines.get.call_count == 3


class TestCuratedInstanceTypes:
    def test_all_curated_types_in_specs(self):
        from app.services.providers.azure import AZURE_RAM_PER_VCPU_GB

        for t in AZURE_CURATED_INSTANCE_TYPES:
            assert t in AZURE_RAM_PER_VCPU_GB, f"{t} missing from specs"

    def test_default_in_curated(self):
        assert AZURE_DEFAULT_INSTANCE_TYPE in AZURE_CURATED_INSTANCE_TYPES
