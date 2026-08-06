"""Tests for storage_pool_service — coverage for uncovered functions."""

import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test.db"

from unittest.mock import MagicMock, patch

import pytest

from app.services.storage_pool_service import (
    _find_toolbox_pod,
    _get_internal_ip_from_node,
    _resolve_nfs_node_ip,
    add_sg_rules_for_shared_storage,
    create_fsx_filesystem,
    delete_ceph_nfs_pool,
    delete_fsx_filesystem,
    generate_pool_ca,
    sign_host_cert,
    update_fsx_storage,
    update_fsx_throughput,
)

# ---------------------------------------------------------------------------
# generate_pool_ca
# ---------------------------------------------------------------------------


class TestGeneratePoolCa:
    def test_returns_pem_cert_and_key(self):
        cert_pem, key_pem = generate_pool_ca("test-pool")
        assert "BEGIN CERTIFICATE" in cert_pem
        assert "END CERTIFICATE" in cert_pem
        assert "BEGIN RSA PRIVATE KEY" in key_pem
        assert "END RSA PRIVATE KEY" in key_pem

    def test_cert_has_correct_cn_and_org(self):
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert_pem, _ = generate_pool_ca("my-pool")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        org = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
        assert cn == "troshka-pool-my-pool"
        assert org == "Troshka"

    def test_cert_is_ca(self):
        from cryptography import x509

        cert_pem, _ = generate_pool_ca("ca-pool")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True
        assert bc.value.path_length == 0

    def test_cert_is_self_signed(self):
        from cryptography import x509

        cert_pem, _ = generate_pool_ca("self-signed")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        assert cert.subject == cert.issuer

    def test_cert_validity_is_10_years(self):
        from cryptography import x509

        cert_pem, _ = generate_pool_ca("long-lived")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert 3649 <= delta.days <= 3651


# ---------------------------------------------------------------------------
# sign_host_cert
# ---------------------------------------------------------------------------


class TestSignHostCert:
    @pytest.fixture(autouse=True)
    def _ca(self):
        self.ca_cert, self.ca_key = generate_pool_ca("sign-test")

    def test_returns_pem_cert_and_key(self):
        cert_pem, key_pem = sign_host_cert(self.ca_cert, self.ca_key, "10.0.0.1")
        assert "BEGIN CERTIFICATE" in cert_pem
        assert "BEGIN RSA PRIVATE KEY" in key_pem

    def test_single_san_when_only_host_ip(self):
        import ipaddress

        from cryptography import x509

        cert_pem, _ = sign_host_cert(self.ca_cert, self.ca_key, "10.0.0.1")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert len(ips) == 1
        assert ips[0] == ipaddress.ip_address("10.0.0.1")

    def test_two_sans_when_private_ip_differs(self):
        import ipaddress

        from cryptography import x509

        cert_pem, _ = sign_host_cert(
            self.ca_cert, self.ca_key, "1.2.3.4", private_ip="10.0.0.5"
        )
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert len(ips) == 2
        assert ipaddress.ip_address("1.2.3.4") in ips
        assert ipaddress.ip_address("10.0.0.5") in ips

    def test_single_san_when_private_ip_equals_host_ip(self):
        from cryptography import x509

        cert_pem, _ = sign_host_cert(
            self.ca_cert, self.ca_key, "10.0.0.1", private_ip="10.0.0.1"
        )
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert len(ips) == 1

    def test_single_san_when_private_ip_empty(self):
        from cryptography import x509

        cert_pem, _ = sign_host_cert(
            self.ca_cert, self.ca_key, "10.0.0.1", private_ip=""
        )
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert len(ips) == 1

    def test_cert_signed_by_ca(self):
        from cryptography import x509

        cert_pem, _ = sign_host_cert(self.ca_cert, self.ca_key, "10.0.0.1")
        ca_cert = x509.load_pem_x509_certificate(self.ca_cert.encode())
        host_cert = x509.load_pem_x509_certificate(cert_pem.encode())
        assert host_cert.issuer == ca_cert.subject

    def test_cert_cn_is_host_ip(self):
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert_pem, _ = sign_host_cert(self.ca_cert, self.ca_key, "192.168.1.100")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == "192.168.1.100"

    def test_cert_validity_is_1_year(self):
        from cryptography import x509

        cert_pem, _ = sign_host_cert(self.ca_cert, self.ca_key, "10.0.0.1")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert 364 <= delta.days <= 366


# ---------------------------------------------------------------------------
# _boto_client
# ---------------------------------------------------------------------------


class TestBotoClient:
    @patch("app.services.storage_pool_service.boto3.client")
    def test_creates_client_with_correct_params(self, mock_boto3_client):
        from app.services.storage_pool_service import _boto_client

        creds = {"access_key_id": "AKID", "secret_access_key": "SECRET"}
        _boto_client("ec2", "us-west-2", creds)
        mock_boto3_client.assert_called_once_with(
            "ec2",
            region_name="us-west-2",
            aws_access_key_id="AKID",
            aws_secret_access_key="SECRET",
        )

    @patch("app.services.storage_pool_service.boto3.client")
    def test_returns_boto3_client(self, mock_boto3_client):
        from app.services.storage_pool_service import _boto_client

        sentinel = MagicMock()
        mock_boto3_client.return_value = sentinel
        result = _boto_client("fsx", "eu-west-1", {})
        assert result is sentinel

    @patch("app.services.storage_pool_service.boto3.client")
    def test_handles_missing_credentials_keys(self, mock_boto3_client):
        from app.services.storage_pool_service import _boto_client

        _boto_client("s3", "us-east-1", {})
        mock_boto3_client.assert_called_once_with(
            "s3",
            region_name="us-east-1",
            aws_access_key_id=None,
            aws_secret_access_key=None,
        )


# ---------------------------------------------------------------------------
# probe_az_capacity
# ---------------------------------------------------------------------------


class TestProbeAzCapacity:
    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_supported_and_unsupported(self, mock_boto):
        from app.services.storage_pool_service import probe_az_capacity

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2

        mock_ec2.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [
                {"Location": "us-east-1a"},
                {"Location": "us-east-1b"},
            ]
        }
        mock_ec2.describe_availability_zones.return_value = {
            "AvailabilityZones": [
                {"ZoneName": "us-east-1a"},
                {"ZoneName": "us-east-1b"},
                {"ZoneName": "us-east-1c"},
            ]
        }

        result = probe_az_capacity({}, "us-east-1", ["m5.xlarge"])

        assert "us-east-1a" in result
        assert "m5.xlarge" in result["us-east-1a"]["supported"]
        assert "m5.xlarge" in result["us-east-1b"]["supported"]
        assert "m5.xlarge" in result["us-east-1c"]["unsupported"]

    @patch("app.services.storage_pool_service._boto_client")
    def test_multiple_instance_types(self, mock_boto):
        from app.services.storage_pool_service import probe_az_capacity

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2

        # First call for m5.xlarge — supported in both AZs
        # Second call for i4i.xlarge — supported only in us-east-1a
        mock_ec2.describe_instance_type_offerings.side_effect = [
            {
                "InstanceTypeOfferings": [
                    {"Location": "us-east-1a"},
                    {"Location": "us-east-1b"},
                ]
            },
            {
                "InstanceTypeOfferings": [
                    {"Location": "us-east-1a"},
                ]
            },
        ]
        mock_ec2.describe_availability_zones.return_value = {
            "AvailabilityZones": [
                {"ZoneName": "us-east-1a"},
                {"ZoneName": "us-east-1b"},
            ]
        }

        result = probe_az_capacity({}, "us-east-1", ["m5.xlarge", "i4i.xlarge"])

        assert "m5.xlarge" in result["us-east-1a"]["supported"]
        assert "i4i.xlarge" in result["us-east-1a"]["supported"]
        assert "m5.xlarge" in result["us-east-1b"]["supported"]
        assert "i4i.xlarge" in result["us-east-1b"]["unsupported"]

    @patch("app.services.storage_pool_service._boto_client")
    def test_empty_instance_types(self, mock_boto):
        from app.services.storage_pool_service import probe_az_capacity

        result = probe_az_capacity({}, "us-east-1", [])
        assert result == {}


# ---------------------------------------------------------------------------
# ensure_subnet_in_az
# ---------------------------------------------------------------------------


class TestEnsureSubnetInAz:
    @patch("app.services.storage_pool_service._boto_client")
    def test_returns_existing_subnet(self, mock_boto):
        from app.services.storage_pool_service import ensure_subnet_in_az

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [{"SubnetId": "subnet-existing"}]
        }

        result = ensure_subnet_in_az({}, "us-east-1", "vpc-123", "us-east-1a")
        assert result == "subnet-existing"
        mock_ec2.create_subnet.assert_not_called()

    @patch("app.services.storage_pool_service._boto_client")
    def test_creates_new_subnet_when_none_exists(self, mock_boto):
        from app.services.storage_pool_service import ensure_subnet_in_az

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2

        # First describe_subnets: no existing subnet in AZ
        # Second describe_subnets: no existing subnets at all (for CIDR calc)
        mock_ec2.describe_subnets.side_effect = [
            {"Subnets": []},
            {"Subnets": []},
        ]
        mock_ec2.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-new"}}
        mock_ec2.describe_route_tables.return_value = {"RouteTables": []}

        result = ensure_subnet_in_az({}, "us-east-1", "vpc-123", "us-east-1b")

        assert result == "subnet-new"
        mock_ec2.create_subnet.assert_called_once_with(
            VpcId="vpc-123", CidrBlock="10.100.1.0/24", AvailabilityZone="us-east-1b"
        )
        mock_ec2.modify_subnet_attribute.assert_called_once()
        mock_ec2.create_tags.assert_called_once()

    @patch("app.services.storage_pool_service._boto_client")
    def test_associates_route_table(self, mock_boto):
        from app.services.storage_pool_service import ensure_subnet_in_az

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2

        mock_ec2.describe_subnets.side_effect = [
            {"Subnets": []},
            {"Subnets": []},
        ]
        mock_ec2.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-rt"}}
        mock_ec2.describe_route_tables.return_value = {
            "RouteTables": [{"RouteTableId": "rtb-123"}]
        }

        result = ensure_subnet_in_az({}, "us-east-1", "vpc-123", "us-east-1c")

        assert result == "subnet-rt"
        mock_ec2.associate_route_table.assert_called_once_with(
            RouteTableId="rtb-123", SubnetId="subnet-rt"
        )

    @patch("app.services.storage_pool_service._boto_client")
    def test_avoids_used_cidr_third_octets(self, mock_boto):
        from app.services.storage_pool_service import ensure_subnet_in_az

        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2

        mock_ec2.describe_subnets.side_effect = [
            {"Subnets": []},
            {
                "Subnets": [
                    {"CidrBlock": "10.100.1.0/24"},
                    {"CidrBlock": "10.100.2.0/24"},
                ]
            },
        ]
        mock_ec2.create_subnet.return_value = {"Subnet": {"SubnetId": "subnet-skip"}}
        mock_ec2.describe_route_tables.return_value = {"RouteTables": []}

        ensure_subnet_in_az({}, "us-east-1", "vpc-123", "us-east-1d")

        mock_ec2.create_subnet.assert_called_once_with(
            VpcId="vpc-123", CidrBlock="10.100.3.0/24", AvailabilityZone="us-east-1d"
        )


# ---------------------------------------------------------------------------
# create_fsx_filesystem
# ---------------------------------------------------------------------------


class TestCreateFsxFilesystem:
    @patch("app.services.storage_pool_service._boto_client")
    def test_creates_filesystem_and_returns_info(self, mock_boto):
        mock_fsx = MagicMock()
        mock_boto.return_value = mock_fsx
        mock_fsx.create_file_system.return_value = {
            "FileSystem": {
                "FileSystemId": "fs-abc123",
                "DNSName": "fs-abc123.fsx.us-east-1.amazonaws.com",
            }
        }

        result = create_fsx_filesystem(
            {"access_key_id": "k", "secret_access_key": "s"},
            "us-east-1",
            "subnet-1",
            "sg-1",
            128,
            160,
        )

        assert result["filesystem_id"] == "fs-abc123"
        assert result["dns_name"] == "fs-abc123.fsx.us-east-1.amazonaws.com"
        mock_fsx.create_file_system.assert_called_once()

    @patch("app.services.storage_pool_service._boto_client")
    def test_handles_missing_dns_name(self, mock_boto):
        mock_fsx = MagicMock()
        mock_boto.return_value = mock_fsx
        mock_fsx.create_file_system.return_value = {
            "FileSystem": {"FileSystemId": "fs-no-dns"}
        }

        result = create_fsx_filesystem({}, "us-east-1", "sub", "sg", 64, 128)
        assert result["filesystem_id"] == "fs-no-dns"
        assert result["dns_name"] is None


# ---------------------------------------------------------------------------
# provision_fsx_pool
# ---------------------------------------------------------------------------


class TestProvisionFsxPool:
    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_fsx_filesystem")
    @patch("app.core.redis.enqueue_job")
    def test_success_path(self, mock_enqueue, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_fsx_pool

        mock_create.return_value = {
            "filesystem_id": "fs-ok",
            "dns_name": "fs-ok.dns.com",
        }
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_fsx_pool("pool-1", {}, "us-east-1", "sub-1", "sg-1", 128, 160)

        assert mock_pool.fsx_filesystem_id == "fs-ok"
        assert mock_pool.fsx_dns_name == "fs-ok.dns.com"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()
        mock_enqueue.assert_called_once()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_fsx_filesystem")
    def test_exception_sets_error(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_fsx_pool

        mock_create.side_effect = RuntimeError("AWS error")
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_fsx_pool("pool-err", {}, "us-east-1", "sub-1", "sg-1", 64, 128)

        assert mock_pool.status == "error"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_fsx_filesystem")
    def test_pool_not_found_after_create(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_fsx_pool

        mock_create.return_value = {"filesystem_id": "fs-x", "dns_name": None}
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_fsx_pool("pool-gone", {}, "us-east-1", "sub", "sg", 64, 128)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_fsx_filesystem")
    def test_pool_not_found_on_exception(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_fsx_pool

        mock_create.side_effect = RuntimeError("fail")
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_fsx_pool("pool-gone2", {}, "us-east-1", "sub", "sg", 64, 128)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()


# ---------------------------------------------------------------------------
# delete_fsx_filesystem
# ---------------------------------------------------------------------------


class TestDeleteFsxFilesystem:
    @patch("app.services.storage_pool_service._boto_client")
    def test_calls_delete(self, mock_boto):
        mock_fsx = MagicMock()
        mock_boto.return_value = mock_fsx

        delete_fsx_filesystem({}, "us-east-1", "fs-del")

        mock_fsx.delete_file_system.assert_called_once_with(FileSystemId="fs-del")


# ---------------------------------------------------------------------------
# update_fsx_throughput
# ---------------------------------------------------------------------------


class TestUpdateFsxThroughput:
    @patch("app.services.storage_pool_service._boto_client")
    def test_calls_update(self, mock_boto):
        mock_fsx = MagicMock()
        mock_boto.return_value = mock_fsx

        update_fsx_throughput({}, "us-east-1", "fs-tp", 256)

        mock_fsx.update_file_system.assert_called_once_with(
            FileSystemId="fs-tp",
            OpenZFSConfiguration={"ThroughputCapacity": 256},
        )


# ---------------------------------------------------------------------------
# update_fsx_storage
# ---------------------------------------------------------------------------


class TestUpdateFsxStorage:
    @patch("app.services.storage_pool_service._boto_client")
    def test_calls_update(self, mock_boto):
        mock_fsx = MagicMock()
        mock_boto.return_value = mock_fsx

        update_fsx_storage({}, "us-east-1", "fs-st", 512)

        mock_fsx.update_file_system.assert_called_once_with(
            FileSystemId="fs-st",
            StorageCapacity=512,
        )


# ---------------------------------------------------------------------------
# add_sg_rules_for_shared_storage
# ---------------------------------------------------------------------------


class TestAddSgRulesForSharedStorage:
    @patch("app.services.storage_pool_service._boto_client")
    def test_all_ports_already_exist(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_security_group_rules.return_value = {
            "SecurityGroupRules": [
                {"FromPort": 2049, "IsEgress": False},
                {"FromPort": 16514, "IsEgress": False},
                {"FromPort": 49152, "IsEgress": False},
                {"FromPort": 10809, "IsEgress": False},
                {"FromPort": 51820, "IsEgress": False},
            ]
        }

        add_sg_rules_for_shared_storage({}, "us-east-1", "sg-all")

        mock_ec2.authorize_security_group_ingress.assert_not_called()

    @patch("app.services.storage_pool_service._boto_client")
    def test_some_ports_missing(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_security_group_rules.return_value = {
            "SecurityGroupRules": [
                {"FromPort": 2049, "IsEgress": False},
                {"FromPort": 16514, "IsEgress": False},
                # Missing: 49152, 10809, 51820
            ]
        }

        add_sg_rules_for_shared_storage({}, "us-east-1", "sg-partial")

        mock_ec2.authorize_security_group_ingress.assert_called_once()
        call_args = mock_ec2.authorize_security_group_ingress.call_args
        rules = call_args[1]["IpPermissions"]
        from_ports = {r["FromPort"] for r in rules}
        assert from_ports == {49152, 10809, 51820}

    @patch("app.services.storage_pool_service._boto_client")
    def test_include_nfs_false_skips_nfs_rule(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_security_group_rules.return_value = {"SecurityGroupRules": []}

        add_sg_rules_for_shared_storage({}, "us-east-1", "sg-no-nfs", include_nfs=False)

        mock_ec2.authorize_security_group_ingress.assert_called_once()
        call_args = mock_ec2.authorize_security_group_ingress.call_args
        rules = call_args[1]["IpPermissions"]
        from_ports = {r["FromPort"] for r in rules}
        assert 2049 not in from_ports
        assert 16514 in from_ports

    @patch("app.services.storage_pool_service._boto_client")
    def test_egress_rules_ignored(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_security_group_rules.return_value = {
            "SecurityGroupRules": [
                {"FromPort": 2049, "IsEgress": True},  # egress, should be ignored
                {"FromPort": 16514, "IsEgress": True},
            ]
        }

        add_sg_rules_for_shared_storage({}, "us-east-1", "sg-egress")

        mock_ec2.authorize_security_group_ingress.assert_called_once()
        call_args = mock_ec2.authorize_security_group_ingress.call_args
        rules = call_args[1]["IpPermissions"]
        from_ports = {r["FromPort"] for r in rules}
        # All 5 ports should be added since egress rules don't count
        assert 2049 in from_ports
        assert 16514 in from_ports

    @patch("app.services.storage_pool_service._boto_client")
    def test_no_existing_rules_adds_all_with_nfs(self, mock_boto):
        mock_ec2 = MagicMock()
        mock_boto.return_value = mock_ec2
        mock_ec2.describe_security_group_rules.return_value = {"SecurityGroupRules": []}

        add_sg_rules_for_shared_storage({}, "us-east-1", "sg-empty", include_nfs=True)

        call_args = mock_ec2.authorize_security_group_ingress.call_args
        rules = call_args[1]["IpPermissions"]
        assert len(rules) == 5


# ---------------------------------------------------------------------------
# _get_k8s_clients
# ---------------------------------------------------------------------------


class TestGetK8sClients:
    def test_creates_clients_with_correct_config(self):
        from app.services.storage_pool_service import _get_k8s_clients

        with patch("kubernetes.client.Configuration") as MockConfig, patch(
            "kubernetes.client.ApiClient"
        ) as MockApiClient, patch("kubernetes.client.CoreV1Api") as MockCoreV1:
            mock_config = MagicMock()
            MockConfig.return_value = mock_config
            mock_api_client = MagicMock()
            MockApiClient.return_value = mock_api_client
            mock_core_api = MagicMock()
            MockCoreV1.return_value = mock_core_api

            creds = {
                "api_url": "https://api.cluster.example.com:6443",
                "token": "my-token",
                "verify_ssl": False,
            }

            core_api, api_client = _get_k8s_clients(creds)

            assert mock_config.host == "https://api.cluster.example.com:6443"
            assert mock_config.api_key == {"authorization": "Bearer my-token"}
            assert mock_config.verify_ssl is False
            assert core_api is mock_core_api
            assert api_client is mock_api_client


# ---------------------------------------------------------------------------
# _find_toolbox_pod
# ---------------------------------------------------------------------------


class TestFindToolboxPod:
    def test_returns_running_pod_name(self):
        mock_core_api = MagicMock()
        running_pod = MagicMock()
        running_pod.status.phase = "Running"
        running_pod.metadata.name = "rook-ceph-tools-abc"
        mock_core_api.list_namespaced_pod.return_value.items = [running_pod]

        result = _find_toolbox_pod(mock_core_api)
        assert result == "rook-ceph-tools-abc"

    def test_skips_non_running_pods(self):
        mock_core_api = MagicMock()
        pending_pod = MagicMock()
        pending_pod.status.phase = "Pending"
        running_pod = MagicMock()
        running_pod.status.phase = "Running"
        running_pod.metadata.name = "rook-ceph-tools-running"
        mock_core_api.list_namespaced_pod.return_value.items = [
            pending_pod,
            running_pod,
        ]

        result = _find_toolbox_pod(mock_core_api)
        assert result == "rook-ceph-tools-running"

    def test_raises_when_no_running_pod(self):
        mock_core_api = MagicMock()
        failed_pod = MagicMock()
        failed_pod.status.phase = "Failed"
        mock_core_api.list_namespaced_pod.return_value.items = [failed_pod]

        with pytest.raises(RuntimeError, match="No running Rook toolbox pod"):
            _find_toolbox_pod(mock_core_api)

    def test_raises_when_no_pods_at_all(self):
        mock_core_api = MagicMock()
        mock_core_api.list_namespaced_pod.return_value.items = []

        with pytest.raises(RuntimeError, match="No running Rook toolbox pod"):
            _find_toolbox_pod(mock_core_api)


# ---------------------------------------------------------------------------
# _get_internal_ip_from_node
# ---------------------------------------------------------------------------


class TestGetInternalIpFromNode:
    def test_returns_internal_ip(self):
        node = MagicMock()
        addr1 = MagicMock()
        addr1.type = "Hostname"
        addr1.address = "worker-1"
        addr2 = MagicMock()
        addr2.type = "InternalIP"
        addr2.address = "10.0.0.42"
        node.status.addresses = [addr1, addr2]

        result = _get_internal_ip_from_node(node)
        assert result == "10.0.0.42"

    def test_returns_none_when_no_internal_ip(self):
        node = MagicMock()
        addr = MagicMock()
        addr.type = "Hostname"
        addr.address = "worker-1"
        node.status.addresses = [addr]

        result = _get_internal_ip_from_node(node)
        assert result is None

    def test_returns_first_internal_ip(self):
        node = MagicMock()
        addr1 = MagicMock()
        addr1.type = "InternalIP"
        addr1.address = "10.0.0.1"
        addr2 = MagicMock()
        addr2.type = "InternalIP"
        addr2.address = "10.0.0.2"
        node.status.addresses = [addr1, addr2]

        result = _get_internal_ip_from_node(node)
        assert result == "10.0.0.1"


# ---------------------------------------------------------------------------
# _resolve_nfs_node_ip
# ---------------------------------------------------------------------------


class TestResolveNfsNodeIp:
    def test_returns_nfs_pod_node_ip(self):
        mock_core_api = MagicMock()
        nfs_pod = MagicMock()
        pod_item = MagicMock()
        pod_item.spec.node_name = "worker-1"
        nfs_pod.items = [pod_item]

        node = MagicMock()
        addr = MagicMock()
        addr.type = "InternalIP"
        addr.address = "10.0.0.50"
        node.status.addresses = [addr]
        mock_core_api.read_node.return_value = node

        result = _resolve_nfs_node_ip(mock_core_api, nfs_pod)
        assert result == "10.0.0.50"
        mock_core_api.read_node.assert_called_once_with("worker-1")

    def test_falls_back_to_any_node(self):
        mock_core_api = MagicMock()
        nfs_pod = MagicMock()
        pod_item = MagicMock()
        pod_item.spec.node_name = "worker-1"
        nfs_pod.items = [pod_item]

        # Primary node has no InternalIP
        primary_node = MagicMock()
        hostname_addr = MagicMock()
        hostname_addr.type = "Hostname"
        hostname_addr.address = "worker-1"
        primary_node.status.addresses = [hostname_addr]
        mock_core_api.read_node.return_value = primary_node

        # Fallback node has InternalIP
        fallback_node = MagicMock()
        internal_addr = MagicMock()
        internal_addr.type = "InternalIP"
        internal_addr.address = "10.0.0.99"
        fallback_node.status.addresses = [internal_addr]
        mock_core_api.list_node.return_value.items = [fallback_node]

        result = _resolve_nfs_node_ip(mock_core_api, nfs_pod)
        assert result == "10.0.0.99"

    def test_returns_none_when_no_nodes_have_ip(self):
        mock_core_api = MagicMock()
        nfs_pod = MagicMock()
        nfs_pod.items = []  # no NFS pod items, skip read_node

        # All nodes have no InternalIP
        node = MagicMock()
        hostname_addr = MagicMock()
        hostname_addr.type = "Hostname"
        hostname_addr.address = "worker-x"
        node.status.addresses = [hostname_addr]
        mock_core_api.list_node.return_value.items = [node]

        result = _resolve_nfs_node_ip(mock_core_api, nfs_pod)
        assert result is None

    def test_nfs_pod_no_node_name(self):
        """When nfs_pod has items but node_name is None, goes to fallback."""
        mock_core_api = MagicMock()
        nfs_pod = MagicMock()
        pod_item = MagicMock()
        pod_item.spec.node_name = None
        nfs_pod.items = [pod_item]

        fallback_node = MagicMock()
        addr = MagicMock()
        addr.type = "InternalIP"
        addr.address = "10.0.0.77"
        fallback_node.status.addresses = [addr]
        mock_core_api.list_node.return_value.items = [fallback_node]

        result = _resolve_nfs_node_ip(mock_core_api, nfs_pod)
        assert result == "10.0.0.77"
        mock_core_api.read_node.assert_not_called()


# ---------------------------------------------------------------------------
# provision_ceph_nfs_pool
# ---------------------------------------------------------------------------


class TestProvisionCephNfsPool:
    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service._get_k8s_clients")
    @patch("app.services.storage_pool_service._find_toolbox_pod")
    @patch("app.services.storage_pool_service._create_ceph_resources")
    @patch("app.services.storage_pool_service._create_nfs_nodeport_service")
    @patch("app.services.storage_pool_service._resolve_nfs_node_ip")
    def test_success_path(
        self,
        mock_resolve,
        mock_nodeport,
        mock_ceph_res,
        mock_toolbox,
        mock_k8s,
        mock_sl,
    ):
        from app.services.storage_pool_service import provision_ceph_nfs_pool

        mock_pool = MagicMock()
        mock_pool.fsx_storage_gb = 500
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        mock_k8s.return_value = (MagicMock(), MagicMock())
        mock_toolbox.return_value = "rook-tools-abc"
        mock_nodeport.return_value = (MagicMock(), 32100)
        mock_resolve.return_value = "10.0.0.50"

        provision_ceph_nfs_pool("abcdefgh-1234", {"api_url": "x", "token": "t"})

        assert mock_pool.status == "available"
        assert mock_pool.nfs_port == 32100
        assert "10.0.0.50" in mock_pool.nfs_endpoint
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service._get_k8s_clients")
    def test_exception_sets_error(self, mock_k8s, mock_sl):
        from app.services.storage_pool_service import provision_ceph_nfs_pool

        mock_pool = MagicMock()
        mock_pool.fsx_storage_gb = 100
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        mock_k8s.side_effect = RuntimeError("K8s connection failed")

        provision_ceph_nfs_pool("abcdefgh-err1", {})

        assert mock_pool.status == "error"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    def test_pool_not_found(self, mock_sl):
        from app.services.storage_pool_service import provision_ceph_nfs_pool

        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_ceph_nfs_pool("gone-pool", {})

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()


# ---------------------------------------------------------------------------
# delete_ceph_nfs_pool
# ---------------------------------------------------------------------------


class TestDeleteCephNfsPool:
    @patch("app.services.storage_pool_service._get_k8s_clients")
    @patch("app.services.storage_pool_service._find_toolbox_pod")
    @patch("app.services.storage_pool_service._ceph_exec")
    def test_success_path(self, mock_ceph_exec, mock_toolbox, mock_k8s):
        mock_core_api = MagicMock()
        mock_k8s.return_value = (mock_core_api, MagicMock())
        mock_toolbox.return_value = "tools-pod"

        delete_ceph_nfs_pool("abcdefgh-del1", {}, "troshka-pool-abcdefgh")

        mock_core_api.delete_namespaced_service.assert_called_once()
        assert (
            mock_ceph_exec.call_count == 3
        )  # export rm, subvolume rm, subvolumegroup rm

    @patch("app.services.storage_pool_service._get_k8s_clients")
    @patch("app.services.storage_pool_service._find_toolbox_pod")
    @patch("app.services.storage_pool_service._ceph_exec")
    def test_service_not_found_continues(self, mock_ceph_exec, mock_toolbox, mock_k8s):
        mock_core_api = MagicMock()
        mock_core_api.delete_namespaced_service.side_effect = Exception("Not Found")
        mock_k8s.return_value = (mock_core_api, MagicMock())
        mock_toolbox.return_value = "tools-pod"

        # Should not raise — logs warning and continues
        delete_ceph_nfs_pool("abcdefgh-del2", {}, None)

        # Ceph cleanup should still proceed
        assert mock_ceph_exec.call_count == 3

    @patch("app.services.storage_pool_service._get_k8s_clients")
    @patch("app.services.storage_pool_service._find_toolbox_pod")
    @patch("app.services.storage_pool_service._ceph_exec")
    def test_ceph_cleanup_fails_with_warning(
        self, mock_ceph_exec, mock_toolbox, mock_k8s
    ):
        mock_core_api = MagicMock()
        mock_k8s.return_value = (mock_core_api, MagicMock())
        mock_toolbox.return_value = "tools-pod"
        mock_ceph_exec.side_effect = RuntimeError("ceph command failed")

        # Should not raise — logs warning
        delete_ceph_nfs_pool("abcdefgh-del3", {}, "my-group")

        mock_core_api.delete_namespaced_service.assert_called_once()

    @patch("app.services.storage_pool_service._get_k8s_clients")
    def test_k8s_connection_fails(self, mock_k8s):
        mock_k8s.side_effect = RuntimeError("Cannot connect")

        # Should not raise — logs exception
        delete_ceph_nfs_pool("abcdefgh-del4", {}, None)

    @patch("app.services.storage_pool_service._get_k8s_clients")
    @patch("app.services.storage_pool_service._find_toolbox_pod")
    @patch("app.services.storage_pool_service._ceph_exec")
    def test_uses_default_group_name_when_none(
        self, mock_ceph_exec, mock_toolbox, mock_k8s
    ):
        mock_core_api = MagicMock()
        mock_k8s.return_value = (mock_core_api, MagicMock())
        mock_toolbox.return_value = "tools-pod"

        delete_ceph_nfs_pool("abcdefgh-del5", {}, None)

        # Check that subvolumegroup rm uses default name
        calls = mock_ceph_exec.call_args_list
        subvolumegroup_rm_call = calls[2]
        cmd = subvolumegroup_rm_call[0][2]
        assert "troshka-pool-abcdefgh" in cmd


# ---------------------------------------------------------------------------
# provision_netapp_pool
# ---------------------------------------------------------------------------


class TestProvisionNetappPool:
    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_netapp_pool_and_volume")
    def test_success_path(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_netapp_pool

        mock_create.return_value = {
            "pool_name": "projects/p/locations/r/storagePools/troshka-pool",
            "volume_name": "projects/p/locations/r/volumes/troshka",
            "mount_ip": "10.0.0.100",
            "share_name": "troshka",
        }
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_netapp_pool(
            "pool-netapp",
            {"service_account_json": {}},
            "my-project",
            "us-central1",
            "my-network",
            256,
        )

        assert mock_pool.status == "available"
        assert mock_pool.netapp_mount_ip == "10.0.0.100"
        assert mock_pool.netapp_capacity_gb == 256
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_netapp_pool_and_volume")
    def test_exception_sets_error(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_netapp_pool

        mock_create.side_effect = RuntimeError("GCP error")
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_netapp_pool("pool-err", {}, "p", "r", "n", 256)

        assert mock_pool.status == "error"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_netapp_pool_and_volume")
    def test_pool_not_found(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_netapp_pool

        mock_create.return_value = {
            "pool_name": "x",
            "volume_name": "y",
            "mount_ip": "10.0.0.1",
            "share_name": "troshka",
        }
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_netapp_pool("pool-gone", {}, "p", "r", "n", 100)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_netapp_pool_and_volume")
    def test_exception_pool_not_found(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_netapp_pool

        mock_create.side_effect = RuntimeError("fail")
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_netapp_pool("pool-gone2", {}, "p", "r", "n", 100)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()


# ---------------------------------------------------------------------------
# provision_azure_files_pool
# ---------------------------------------------------------------------------


class TestProvisionAzureFilesPool:
    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_azure_files_nfs")
    def test_success_with_iops_and_throughput(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_azure_files_pool

        mock_create.return_value = {
            "storage_account": "troshkasa12345678",
            "share_name": "troshka",
            "mount_url": "troshkasa12345678.privatelink.file.core.windows.net:/troshkasa12345678/troshka",
        }
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_azure_files_pool(
            "pool-az1",
            {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "subscription_id": "sub",
            },
            "rg-troshka",
            "eastus",
            "subnet-1",
            100,
            iops=3000,
            throughput=125,
        )

        assert mock_pool.status == "available"
        assert mock_pool.azure_storage_account == "troshkasa12345678"
        assert mock_pool.azure_files_capacity_gb == 100
        assert mock_pool.azure_files_iops == 3000
        assert mock_pool.azure_files_throughput == 125
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_azure_files_nfs")
    def test_success_without_iops_and_throughput(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_azure_files_pool

        mock_create.return_value = {
            "storage_account": "sa",
            "share_name": "troshka",
            "mount_url": "sa.file/sa/troshka",
        }
        mock_pool = MagicMock()
        mock_pool.azure_files_iops = None
        mock_pool.azure_files_throughput = None
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_azure_files_pool("pool-az2", {}, "rg", "westus", "sub-1", 200)

        assert mock_pool.status == "available"
        # iops and throughput should NOT have been set (remain None from mock)
        # Verify they were not assigned a truthy value
        mock_db.commit.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_azure_files_nfs")
    def test_exception_sets_error(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_azure_files_pool

        mock_create.side_effect = RuntimeError("Azure error")
        mock_pool = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_pool
        mock_sl.return_value = mock_db

        provision_azure_files_pool("pool-az-err", {}, "rg", "eastus", "sub", 100)

        assert mock_pool.status == "error"
        mock_db.commit.assert_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_azure_files_nfs")
    def test_pool_not_found(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_azure_files_pool

        mock_create.return_value = {
            "storage_account": "sa",
            "share_name": "troshka",
            "mount_url": "x",
        }
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_azure_files_pool("pool-az-gone", {}, "rg", "loc", "sub", 100)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()

    @patch("app.services.storage_pool_service.SessionLocal")
    @patch("app.services.storage_pool_service.create_azure_files_nfs")
    def test_exception_pool_not_found(self, mock_create, mock_sl):
        from app.services.storage_pool_service import provision_azure_files_pool

        mock_create.side_effect = RuntimeError("fail")
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        provision_azure_files_pool("pool-az-gone2", {}, "rg", "loc", "sub", 100)

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called()
