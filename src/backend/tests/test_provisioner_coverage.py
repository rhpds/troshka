"""Tests for app.services.provisioner to improve SonarQube coverage."""

import os

os.environ.setdefault("TROSHKA_DATABASE__URL", "sqlite:///./test.db")

import math
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# get_public_ip
# ---------------------------------------------------------------------------
class TestGetPublicIp:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"  203.0.113.5\n"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        from app.services.provisioner import get_public_ip

        assert get_public_ip() == "203.0.113.5"

    @patch("urllib.request.urlopen", side_effect=OSError("timeout"))
    def test_failure_returns_none(self, _mock):
        from app.services.provisioner import get_public_ip

        assert get_public_ip() is None


# ---------------------------------------------------------------------------
# _get_ec2_client
# ---------------------------------------------------------------------------
class TestGetEc2Client:
    @patch("app.services.provisioner.boto3")
    @patch("app.services.provisioner.config")
    def test_with_explicit_credentials(self, mock_config, mock_boto):
        from app.services.provisioner import _get_ec2_client

        mock_config.aws.default_region = "us-west-2"
        mock_config.aws.access_key_id = "cfg-key"
        mock_config.aws.secret_access_key = "cfg-secret"

        creds = {
            "access_key_id": "AKIA_EXPLICIT",
            "secret_access_key": "SECRET_EXPLICIT",
        }
        _get_ec2_client(region="eu-west-1", credentials=creds)

        mock_boto.client.assert_called_once_with(
            "ec2",
            region_name="eu-west-1",
            aws_access_key_id="AKIA_EXPLICIT",
            aws_secret_access_key="SECRET_EXPLICIT",
        )

    @patch("app.services.provisioner.boto3")
    @patch("app.services.provisioner.config")
    def test_falls_back_to_config(self, mock_config, mock_boto):
        from app.services.provisioner import _get_ec2_client

        mock_config.aws.default_region = "us-east-1"
        mock_config.aws.access_key_id = "CFG_KEY"
        mock_config.aws.secret_access_key = "CFG_SECRET"

        _get_ec2_client()

        mock_boto.client.assert_called_once_with(
            "ec2",
            region_name="us-east-1",
            aws_access_key_id="CFG_KEY",
            aws_secret_access_key="CFG_SECRET",
        )


# ---------------------------------------------------------------------------
# find_rhel_ami
# ---------------------------------------------------------------------------
class TestFindRhelAmi:
    @patch("app.services.provisioner._get_ec2_client")
    def test_returns_latest_ami(self, mock_get_client):
        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_images.return_value = {
            "Images": [
                {"ImageId": "ami-older", "CreationDate": "2024-01-01T00:00:00Z"},
                {"ImageId": "ami-newest", "CreationDate": "2024-06-01T00:00:00Z"},
                {"ImageId": "ami-middle", "CreationDate": "2024-03-01T00:00:00Z"},
            ]
        }

        from app.services.provisioner import find_rhel_ami

        assert find_rhel_ami("us-east-1") == "ami-newest"

    @patch("app.services.provisioner._get_ec2_client")
    def test_no_images_raises(self, mock_get_client):
        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_images.return_value = {"Images": []}

        from app.services.provisioner import find_rhel_ami

        with pytest.raises(ValueError, match="No RHEL 9.4"):
            find_rhel_ami()


# ---------------------------------------------------------------------------
# _ensure_troshkad_rule
# ---------------------------------------------------------------------------
class TestEnsureTroshkadRule:
    def test_rule_already_exists(self):
        from app.services.provisioner import _ensure_troshkad_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {"IpPermissions": [{"FromPort": 31337, "ToPort": 31337}]}
            ]
        }
        _ensure_troshkad_rule(mock_ec2, "sg-123")
        mock_ec2.authorize_security_group_ingress.assert_not_called()

    @patch("app.services.provisioner.get_public_ip", return_value="10.0.0.1")
    def test_adds_rule_with_backend_ip(self, _mock_ip):
        from app.services.provisioner import _ensure_troshkad_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": []}]
        }
        _ensure_troshkad_rule(mock_ec2, "sg-123")
        call_kwargs = mock_ec2.authorize_security_group_ingress.call_args
        ip_ranges = call_kwargs.kwargs["IpPermissions"][0]["IpRanges"]
        assert ip_ranges[0]["CidrIp"] == "10.0.0.1/32"

    @patch("app.services.provisioner.get_public_ip", return_value=None)
    def test_adds_rule_fallback_cidr(self, _mock_ip):
        from app.services.provisioner import _ensure_troshkad_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": []}]
        }
        _ensure_troshkad_rule(mock_ec2, "sg-123")
        call_kwargs = mock_ec2.authorize_security_group_ingress.call_args
        ip_ranges = call_kwargs.kwargs["IpPermissions"][0]["IpRanges"]
        assert ip_ranges[0]["CidrIp"] == "0.0.0.0/0"


# ---------------------------------------------------------------------------
# _ensure_console_rule
# ---------------------------------------------------------------------------
class TestEnsureConsoleRule:
    def test_rule_already_exists(self):
        from app.services.provisioner import _ensure_console_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": [{"FromPort": 443, "ToPort": 443}]}]
        }
        _ensure_console_rule(mock_ec2, "sg-123")
        mock_ec2.authorize_security_group_ingress.assert_not_called()

    def test_adds_rule(self):
        from app.services.provisioner import _ensure_console_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": []}]
        }
        _ensure_console_rule(mock_ec2, "sg-123")
        mock_ec2.authorize_security_group_ingress.assert_called_once()

    def test_exception_suppressed(self):
        from app.services.provisioner import _ensure_console_rule

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": []}]
        }
        mock_ec2.authorize_security_group_ingress.side_effect = Exception("boom")
        # Should not raise
        _ensure_console_rule(mock_ec2, "sg-123")


# ---------------------------------------------------------------------------
# ensure_security_group
# ---------------------------------------------------------------------------
class TestEnsureSecurityGroup:
    @patch("app.services.provisioner._ensure_console_rule")
    @patch("app.services.provisioner._ensure_troshkad_rule")
    @patch("app.services.provisioner._get_ec2_client")
    def test_existing_sg_returned(self, mock_get_client, mock_troshkad, mock_console):
        from app.services.provisioner import ensure_security_group

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-existing"}]
        }
        result = ensure_security_group("vpc-abc")
        assert result == "sg-existing"
        mock_troshkad.assert_called_once_with(mock_ec2, "sg-existing")
        mock_console.assert_called_once_with(mock_ec2, "sg-existing")

    @patch("app.services.provisioner.get_public_ip", return_value="1.2.3.4")
    @patch("app.services.provisioner._get_ec2_client")
    def test_creates_new_sg(self, mock_get_client, _mock_ip):
        from app.services.provisioner import ensure_security_group

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_security_groups.return_value = {"SecurityGroups": []}
        mock_ec2.create_security_group.return_value = {"GroupId": "sg-new"}

        result = ensure_security_group("vpc-abc")
        assert result == "sg-new"
        mock_ec2.create_security_group.assert_called_once()
        mock_ec2.authorize_security_group_ingress.assert_called_once()
        mock_ec2.create_tags.assert_called_once()


# ---------------------------------------------------------------------------
# get_default_vpc_and_subnet
# ---------------------------------------------------------------------------
class TestGetDefaultVpcAndSubnet:
    @patch("app.services.provisioner._get_ec2_client")
    def test_success(self, mock_get_client):
        from app.services.provisioner import get_default_vpc_and_subnet

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-default"}]}
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-aaa"}]
        }
        vpc, subnet = get_default_vpc_and_subnet()
        assert vpc == "vpc-default"
        assert subnet == "subnet-aaa"

    @patch("app.services.provisioner._get_ec2_client")
    def test_no_default_vpc(self, mock_get_client):
        from app.services.provisioner import get_default_vpc_and_subnet

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        with pytest.raises(ValueError, match="No default VPC"):
            get_default_vpc_and_subnet()

    @patch("app.services.provisioner._get_ec2_client")
    def test_no_subnets(self, mock_get_client):
        from app.services.provisioner import get_default_vpc_and_subnet

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
        mock_ec2.describe_subnets.return_value = {"Subnets": []}

        with pytest.raises(ValueError, match="No subnets"):
            get_default_vpc_and_subnet()


# ---------------------------------------------------------------------------
# update_sg_troshkad_ip
# ---------------------------------------------------------------------------
class TestUpdateSgTroshkadIp:
    @patch("app.services.provisioner._get_ec2_client")
    def test_finds_and_replaces_old_rule(self, mock_get_client):
        from app.services.provisioner import update_sg_troshkad_ip

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        old_perm = {
            "FromPort": 31337,
            "ToPort": 31337,
            "IpRanges": [{"CidrIp": "9.9.9.9/32"}],
        }
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"IpPermissions": [old_perm]}]
        }

        update_sg_troshkad_ip("sg-123", "5.6.7.8")

        mock_ec2.revoke_security_group_ingress.assert_called_once_with(
            GroupId="sg-123", IpPermissions=[old_perm]
        )
        add_call = mock_ec2.authorize_security_group_ingress.call_args
        new_cidr = add_call.kwargs["IpPermissions"][0]["IpRanges"][0]["CidrIp"]
        assert new_cidr == "5.6.7.8/32"


# ---------------------------------------------------------------------------
# _resolve_subnet_ids
# ---------------------------------------------------------------------------
class TestResolveSubnetIds:
    def test_with_subnet_override(self):
        from app.services.provisioner import _resolve_subnet_ids

        mock_ec2 = MagicMock()
        result = _resolve_subnet_ids(
            mock_ec2, "vpc-1", "subnet-main", "subnet-override"
        )
        assert result == ["subnet-override"]
        mock_ec2.describe_subnets.assert_not_called()

    def test_without_override(self):
        from app.services.provisioner import _resolve_subnet_ids

        mock_ec2 = MagicMock()
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {"SubnetId": "subnet-main"},
                {"SubnetId": "subnet-b"},
                {"SubnetId": "subnet-c"},
            ]
        }
        result = _resolve_subnet_ids(mock_ec2, "vpc-1", "subnet-main", None)
        assert result == ["subnet-main", "subnet-b", "subnet-c"]


# ---------------------------------------------------------------------------
# _build_cloud_init_user_data
# ---------------------------------------------------------------------------
class TestBuildCloudInitUserData:
    def test_regular_host(self):
        from app.services.provisioner import _build_cloud_init_user_data

        ud = _build_cloud_init_user_data("host1", "id-123", False, None, None)
        assert "#cloud-config" in ud
        assert "hostname: host1" in ud
        assert "host_id: id-123" in ud
        assert "qemu-kvm" in ud
        assert "libvirt" in ud
        # Regular host has EBS setup
        assert "find_nvme_dev" in ud
        # Regular host has VM tuning
        assert "overcommit_memory" in ud

    def test_pattern_buffer_host(self):
        from app.services.provisioner import _build_cloud_init_user_data

        ud = _build_cloud_init_user_data("buf1", "id-456", True, None, None)
        assert "hostname: buf1" in ud
        assert "nvme-cli" in ud
        assert "mount-nvme.sh" in ud
        # Pattern buffer should NOT have EBS setup or VM tuning
        assert "find_nvme_dev" not in ud
        assert "overcommit_memory" not in ud

    def test_with_nfs_server(self):
        from app.services.provisioner import _build_cloud_init_user_data

        ud = _build_cloud_init_user_data(
            "host2", "id-789", False, "10.0.0.50", "/exports/shared"
        )
        assert "10.0.0.50:/exports/shared" in ud
        assert "virt_use_nfs" in ud

    def test_without_nfs_server(self):
        from app.services.provisioner import _build_cloud_init_user_data

        ud = _build_cloud_init_user_data("host3", "id-abc", False, None, None)
        assert "virt_use_nfs" not in ud

    def test_pattern_buffer_with_nfs(self):
        from app.services.provisioner import _build_cloud_init_user_data

        ud = _build_cloud_init_user_data(
            "buf2", "id-def", True, "10.0.0.50", "/exports"
        )
        assert "10.0.0.50:/exports" in ud
        assert "mount-nvme.sh" in ud


# ---------------------------------------------------------------------------
# _launch_with_subnet_fallback
# ---------------------------------------------------------------------------
class TestLaunchWithSubnetFallback:
    def test_first_subnet_works(self):
        from app.services.provisioner import _launch_with_subnet_fallback

        mock_ec2 = MagicMock()
        mock_ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-ok"}]}

        launch_kwargs = {"NetworkInterfaces": [{"SubnetId": ""}]}
        result = _launch_with_subnet_fallback(
            mock_ec2, ["subnet-a"], launch_kwargs, "m5.xlarge"
        )
        assert result["Instances"][0]["InstanceId"] == "i-ok"
        assert mock_ec2.run_instances.call_count == 1

    def test_first_fails_unsupported_second_succeeds(self):
        from app.services.provisioner import _launch_with_subnet_fallback

        mock_ec2 = MagicMock()
        # Create a real ClientError-like exception class on the mock
        client_error = type("ClientError", (Exception,), {})
        mock_ec2.exceptions.ClientError = client_error
        mock_ec2.run_instances.side_effect = [
            client_error("Unsupported in this AZ"),
            {"Instances": [{"InstanceId": "i-second"}]},
        ]

        launch_kwargs = {"NetworkInterfaces": [{"SubnetId": ""}]}
        result = _launch_with_subnet_fallback(
            mock_ec2, ["subnet-a", "subnet-b"], launch_kwargs, "m5.xlarge"
        )
        assert result["Instances"][0]["InstanceId"] == "i-second"
        assert mock_ec2.run_instances.call_count == 2

    def test_all_subnets_fail(self):
        from app.services.provisioner import _launch_with_subnet_fallback

        mock_ec2 = MagicMock()
        client_error = type("ClientError", (Exception,), {})
        mock_ec2.exceptions.ClientError = client_error
        mock_ec2.run_instances.side_effect = [
            client_error("Unsupported AZ-a"),
            client_error("Unsupported AZ-b"),
        ]

        launch_kwargs = {"NetworkInterfaces": [{"SubnetId": ""}]}
        with pytest.raises(client_error):
            _launch_with_subnet_fallback(
                mock_ec2, ["subnet-a", "subnet-b"], launch_kwargs, "m5.xlarge"
            )

    def test_non_unsupported_error_reraises_immediately(self):
        from app.services.provisioner import _launch_with_subnet_fallback

        mock_ec2 = MagicMock()
        client_error = type("ClientError", (Exception,), {})
        mock_ec2.exceptions.ClientError = client_error
        mock_ec2.run_instances.side_effect = client_error("InsufficientCapacity")

        launch_kwargs = {"NetworkInterfaces": [{"SubnetId": ""}]}
        with pytest.raises(client_error, match="InsufficientCapacity"):
            _launch_with_subnet_fallback(
                mock_ec2, ["subnet-a", "subnet-b"], launch_kwargs, "m5.xlarge"
            )
        # Should only try the first subnet before re-raising
        assert mock_ec2.run_instances.call_count == 1


# ---------------------------------------------------------------------------
# provision_host — end-to-end with mocked EC2
# ---------------------------------------------------------------------------
class TestProvisionHost:
    def _setup_mock_ec2(self, mock_get_client):
        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-main"}]
        }
        mock_ec2.create_key_pair.return_value = {"KeyMaterial": "-----BEGIN RSA-----"}
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [
                {
                    "MemoryInfo": {"SizeInMiB": 65536},
                    "VCpuInfo": {"DefaultVCpus": 16},
                    "NetworkInfo": {"Ipv4AddressesPerInterface": 15},
                }
            ]
        }
        mock_ec2.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-provision"}]
        }
        mock_ec2.get_waiter.return_value = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "PublicIpAddress": "54.1.2.3",
                            "PrivateIpAddress": "10.0.0.5",
                        }
                    ]
                }
            ]
        }
        return mock_ec2

    @patch(
        "app.services.provisioner._build_cloud_init_user_data",
        return_value="#cloud-config",
    )
    @patch("app.services.provisioner.ensure_security_group", return_value="sg-auto")
    @patch("app.services.provisioner._get_ec2_client")
    def test_provision_with_ami_id(self, mock_get_client, _mock_sg, _mock_ci):
        from app.services.provisioner import provision_host

        _mock_ec2 = self._setup_mock_ec2(mock_get_client)

        result = provision_host(
            instance_type="m5.xlarge",
            ami_id="ami-explicit",
            vpc_id="vpc-1",
            subnet_id="subnet-main",
        )

        assert result["instance_id"] == "i-provision"
        assert result["public_ip"] == "54.1.2.3"
        assert result["private_ip"] == "10.0.0.5"
        assert result["ami_id"] == "ami-explicit"
        assert result["total_vcpus"] == 16
        assert result["total_ram_mb"] == 65536
        assert result["max_eips"] == 14
        assert result["private_key"] == "-----BEGIN RSA-----"
        assert result["state"] == "active"

    @patch("app.services.provisioner.find_rhel_ami", return_value="ami-found")
    @patch(
        "app.services.provisioner._build_cloud_init_user_data",
        return_value="#cloud-config",
    )
    @patch("app.services.provisioner.ensure_security_group", return_value="sg-auto")
    @patch("app.services.provisioner._get_ec2_client")
    @patch("app.services.provisioner.config")
    def test_provision_without_ami_id(
        self, mock_config, mock_get_client, _sg, _ci, mock_find_ami
    ):
        from app.services.provisioner import provision_host

        mock_config.aws.default_instance_type = None
        mock_config.aws.default_ami = None
        _mock_ec2 = self._setup_mock_ec2(mock_get_client)

        result = provision_host(vpc_id="vpc-1", subnet_id="subnet-main")
        mock_find_ami.assert_called_once()
        assert result["ami_id"] == "ami-found"

    @patch(
        "app.services.provisioner._build_cloud_init_user_data",
        return_value="#cloud-config",
    )
    @patch("app.services.provisioner.ensure_security_group", return_value="sg-auto")
    @patch("app.services.provisioner._get_ec2_client")
    def test_provision_pattern_buffer(self, mock_get_client, _sg, _mock_ci):
        from app.services.provisioner import provision_host

        mock_ec2 = self._setup_mock_ec2(mock_get_client)

        _result = provision_host(
            instance_type="i3.xlarge",
            ami_id="ami-pb",
            vpc_id="vpc-1",
            subnet_id="subnet-main",
            host_type="pattern_buffer",
        )

        # Pattern buffer should not set CpuOptions for nested virt
        launch_call = mock_ec2.run_instances.call_args
        assert "CpuOptions" not in launch_call.kwargs

    @patch(
        "app.services.provisioner._build_cloud_init_user_data",
        return_value="#cloud-config",
    )
    @patch("app.services.provisioner._get_ec2_client")
    def test_provision_with_console_zone(self, mock_get_client, _mock_ci):
        from app.services.provisioner import provision_host

        mock_ec2 = self._setup_mock_ec2(mock_get_client)

        _result = provision_host(
            instance_type="m5.xlarge",
            ami_id="ami-console",
            vpc_id="vpc-1",
            subnet_id="subnet-main",
            security_group_id="sg-pre",
            console_zone_id="ZXXXX",
        )

        launch_call = mock_ec2.run_instances.call_args
        assert launch_call.kwargs["IamInstanceProfile"] == {
            "Name": "troshka-certbot-profile"
        }

    @patch(
        "app.services.provisioner._build_cloud_init_user_data",
        return_value="#cloud-config",
    )
    @patch("app.services.provisioner._get_ec2_client")
    def test_provision_no_vpc_raises(self, mock_get_client, _mock_ci):
        from app.services.provisioner import provision_host

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2

        with pytest.raises(ValueError, match="VPC and subnet must be configured"):
            provision_host(instance_type="m5.xlarge", ami_id="ami-1")


# ---------------------------------------------------------------------------
# resize_instance
# ---------------------------------------------------------------------------
class TestResizeInstance:
    @patch("app.services.provisioner._resize_swap_volume")
    @patch("app.services.provisioner._get_ec2_client")
    def test_success_path(self, mock_get_client, mock_resize_swap):
        from app.services.provisioner import resize_instance

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_instance_types.return_value = {
            "InstanceTypes": [
                {
                    "MemoryInfo": {"SizeInMiB": 131072},
                    "VCpuInfo": {"DefaultVCpus": 32},
                    "NetworkInfo": {"Ipv4AddressesPerInterface": 30},
                }
            ]
        }

        result = resize_instance("i-abc", "m5.8xlarge")

        mock_ec2.modify_instance_attribute.assert_called_once_with(
            InstanceId="i-abc",
            InstanceType={"Value": "m5.8xlarge"},
        )
        assert result["instance_type"] == "m5.8xlarge"
        assert result["total_vcpus"] == 32
        assert result["total_ram_mb"] == 131072
        assert result["max_eips"] == 29
        expected_swap = max(math.ceil(131072 / 1024), 1)
        mock_resize_swap.assert_called_once_with(mock_ec2, "i-abc", expected_swap)


# ---------------------------------------------------------------------------
# _resize_swap_volume
# ---------------------------------------------------------------------------
class TestResizeSwapVolume:
    def test_no_volumes_found(self):
        from app.services.provisioner import _resize_swap_volume

        mock_ec2 = MagicMock()
        mock_ec2.describe_volumes.return_value = {"Volumes": []}

        _resize_swap_volume(mock_ec2, "i-abc", 64)
        mock_ec2.detach_volume.assert_not_called()

    def test_same_size_no_op(self):
        from app.services.provisioner import _resize_swap_volume

        mock_ec2 = MagicMock()
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [
                {
                    "VolumeId": "vol-old",
                    "Size": 64,
                    "AvailabilityZone": "us-east-1a",
                }
            ]
        }

        _resize_swap_volume(mock_ec2, "i-abc", 64)
        mock_ec2.detach_volume.assert_not_called()

    def test_resize_detach_delete_create_attach(self):
        from app.services.provisioner import _resize_swap_volume

        mock_ec2 = MagicMock()
        mock_ec2.describe_volumes.return_value = {
            "Volumes": [
                {
                    "VolumeId": "vol-old",
                    "Size": 32,
                    "AvailabilityZone": "us-east-1a",
                }
            ]
        }
        mock_waiter = MagicMock()
        mock_ec2.get_waiter.return_value = mock_waiter
        mock_ec2.create_volume.return_value = {"VolumeId": "vol-new"}

        _resize_swap_volume(mock_ec2, "i-abc", 128)

        mock_ec2.detach_volume.assert_called_once()
        mock_ec2.delete_volume.assert_called_once_with(VolumeId="vol-old")
        mock_ec2.create_volume.assert_called_once()
        create_args = mock_ec2.create_volume.call_args
        assert create_args.kwargs["Size"] == 128
        assert create_args.kwargs["AvailabilityZone"] == "us-east-1a"
        mock_ec2.attach_volume.assert_called_once()
        attach_args = mock_ec2.attach_volume.call_args
        assert attach_args.kwargs["VolumeId"] == "vol-new"


# ---------------------------------------------------------------------------
# terminate_host
# ---------------------------------------------------------------------------
class TestTerminateHost:
    @patch("app.services.provisioner._get_ec2_client")
    def test_basic_termination(self, mock_get_client):
        from app.services.provisioner import terminate_host

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2

        terminate_host("i-term")
        mock_ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-term"])


# ---------------------------------------------------------------------------
# get_host_status
# ---------------------------------------------------------------------------
class TestGetHostStatus:
    @patch("app.services.provisioner._get_ec2_client")
    def test_success(self, mock_get_client):
        from app.services.provisioner import get_host_status

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "State": {"Name": "running"},
                            "PublicIpAddress": "54.9.8.7",
                            "PrivateIpAddress": "10.0.0.9",
                        }
                    ]
                }
            ]
        }

        result = get_host_status("i-status")
        assert result == {
            "instance_id": "i-status",
            "state": "running",
            "public_ip": "54.9.8.7",
            "private_ip": "10.0.0.9",
        }

    @patch("app.services.provisioner._get_ec2_client")
    def test_exception_returns_none(self, mock_get_client):
        from app.services.provisioner import get_host_status

        mock_ec2 = MagicMock()
        mock_get_client.return_value = mock_ec2
        mock_ec2.describe_instances.side_effect = Exception("not found")

        assert get_host_status("i-gone") is None
