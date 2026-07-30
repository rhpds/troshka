"""Tests for extracted helper functions in app.api.providers."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.providers import (
    ProviderCreate,
    _build_provider_credentials,
    _build_provider_response,
    _check_crds_installed,
    _check_operator_deployment,
    _test_s3_provider,
)


def _make_provider(**overrides):
    """Create a mock Provider with sensible defaults."""
    p = MagicMock()
    p.id = "prov-0001"
    p.name = "test-provider"
    p.type = overrides.pop("type", "ec2")
    p.default_region = "us-east-1"
    p.default_image = "ami-12345"
    p.vpc_id = "vpc-abc"
    p.subnet_id = "subnet-def"
    p.security_group_id = "sg-ghi"
    p.console_zone_id = None
    p.console_base_domain = None
    p.console_nameservers = None
    p.state = "active"
    p.credentials = None
    p.hosts = []
    p.created_at = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
    p.gcp_project_id = None
    p.gcp_network_id = None
    p.gcp_subnet_id = None
    p.gcp_firewall_policy = None
    p.gcp_zone = None
    p.azure_subscription_id = None
    p.azure_resource_group = None
    p.azure_vnet_id = None
    p.azure_subnet_id = None
    p.azure_nsg_id = None
    p.azure_location = None
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


# ---------------------------------------------------------------------------
# _build_provider_credentials
# ---------------------------------------------------------------------------


class TestBuildProviderCredentials:
    """Tests for _build_provider_credentials()."""

    def test_ec2_returns_access_keys(self):
        body = ProviderCreate(
            name="aws",
            type="ec2",
            access_key_id="AKID",
            secret_access_key="SECRET",
            default_region="us-west-2",
        )
        provider = _make_provider(type="ec2")
        creds = _build_provider_credentials(body, provider)

        assert creds["access_key_id"] == "AKID"
        assert creds["secret_access_key"] == "SECRET"
        assert "bucket" not in creds

    def test_ec2_with_bucket_and_endpoint(self):
        body = ProviderCreate(
            name="s3store",
            type="s3",
            access_key_id="AKID",
            secret_access_key="SECRET",
            bucket="my-bucket",
            endpoint_url="https://s3.custom.example.com",
        )
        provider = _make_provider(type="s3")
        creds = _build_provider_credentials(body, provider)

        assert creds["bucket"] == "my-bucket"
        assert creds["endpoint_url"] == "https://s3.custom.example.com"

    def test_gcp_returns_service_account_json(self):
        sa_json = {"type": "service_account", "project_id": "proj"}
        body = ProviderCreate(
            name="gcp",
            type="gcp",
            gcp_project_id="proj",
            service_account_json=json.dumps(sa_json),
        )
        provider = _make_provider(type="gcp")
        creds = _build_provider_credentials(body, provider)

        assert creds["service_account_json"] == sa_json
        assert provider.gcp_project_id == "proj"

    def test_gcp_missing_fields_raises_400(self):
        body = ProviderCreate(
            name="gcp",
            type="gcp",
        )
        provider = _make_provider(type="gcp")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "gcp_project_id" in str(exc_info.value.detail)

    def test_gcp_invalid_json_raises_400(self):
        body = ProviderCreate(
            name="gcp",
            type="gcp",
            gcp_project_id="proj",
            service_account_json="not valid json {{{",
        )
        provider = _make_provider(type="gcp")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "valid JSON" in str(exc_info.value.detail)

    def test_azure_returns_credential_fields(self):
        body = ProviderCreate(
            name="az",
            type="azure",
            azure_tenant_id="tenant-1",
            azure_client_id="client-1",
            azure_client_secret="secret-1",
            azure_subscription_id="sub-1",
            azure_location="eastus",
        )
        provider = _make_provider(type="azure")
        creds = _build_provider_credentials(body, provider)

        assert creds["tenant_id"] == "tenant-1"
        assert creds["client_id"] == "client-1"
        assert creds["client_secret"] == "secret-1"
        assert creds["subscription_id"] == "sub-1"
        assert provider.azure_subscription_id == "sub-1"
        assert provider.azure_location == "eastus"

    def test_azure_missing_fields_raises_400(self):
        body = ProviderCreate(
            name="az",
            type="azure",
            azure_tenant_id="tenant-1",
            # missing client_id, client_secret, subscription_id
        )
        provider = _make_provider(type="azure")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400

    def test_azure_location_falls_back_to_default_region(self):
        body = ProviderCreate(
            name="az",
            type="azure",
            azure_tenant_id="t",
            azure_client_id="c",
            azure_client_secret="s",
            azure_subscription_id="sub",
            default_region="westus",
            # azure_location not set
        )
        provider = _make_provider(type="azure")
        _build_provider_credentials(body, provider)

        assert provider.azure_location == "westus"

    def test_ocpvirt_returns_api_url_and_token(self):
        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            api_url="https://api.cluster.example.com:6443",
            token="sha256~token",
            namespace="troshka",
        )
        provider = _make_provider(type="ocpvirt")
        creds = _build_provider_credentials(body, provider)

        assert creds["api_url"] == "https://api.cluster.example.com:6443"
        assert creds["token"] == "sha256~token"
        assert creds["namespace"] == "troshka"
        assert creds["verify_ssl"] is False
        assert provider.default_region == "troshka"
        assert provider.console_base_domain == "apps.cluster.example.com"

    def test_ocpvirt_missing_fields_raises_400(self):
        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            # missing api_url and token
        )
        provider = _make_provider(type="ocpvirt")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400

    def test_ocpvirt_with_iso_pvc(self):
        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            api_url="https://api.cluster.example.com:6443",
            token="tok",
            iso_pvc="rhel-iso-pvc",
        )
        provider = _make_provider(type="ocpvirt")
        creds = _build_provider_credentials(body, provider)

        assert creds["iso_pvc"] == "rhel-iso-pvc"

    def test_kubevirt_returns_full_config(self):
        body = ProviderCreate(
            name="kv",
            type="kubevirt",
            api_url="https://api.kv.example.com:6443",
            token="sha256~kvtoken",
            namespace="my-operator-ns",
            cache_namespace="my-cache-ns",
            project_prefix="lab-",
        )
        provider = _make_provider(type="kubevirt")
        creds = _build_provider_credentials(body, provider)

        assert creds["api_url"] == "https://api.kv.example.com:6443"
        assert creds["token"] == "sha256~kvtoken"
        assert creds["namespace"] == "my-operator-ns"
        assert creds["cache_namespace"] == "my-cache-ns"
        assert creds["project_prefix"] == "lab-"
        assert provider.default_region == "my-operator-ns"
        assert provider.console_base_domain == "apps.kv.example.com"

    def test_kubevirt_defaults(self):
        body = ProviderCreate(
            name="kv",
            type="kubevirt",
            api_url="https://api.kv.example.com:6443",
            token="tok",
            # namespace defaults to "troshka" from ProviderCreate,
            # but kubevirt branch does `body.namespace or "troshka-operator"`,
            # so "troshka" (truthy) is used as-is.
        )
        provider = _make_provider(type="kubevirt")
        creds = _build_provider_credentials(body, provider)

        assert creds["namespace"] == "troshka"
        assert creds["cache_namespace"] == "troshka-cache"
        assert creds["project_prefix"] == "troshka-"

    def test_kubevirt_missing_fields_raises_400(self):
        body = ProviderCreate(
            name="kv",
            type="kubevirt",
        )
        provider = _make_provider(type="kubevirt")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400

    def test_unknown_type_raises_400(self):
        body = ProviderCreate(
            name="bad",
            type="unknown_cloud",
        )
        provider = _make_provider(type="unknown_cloud")
        with pytest.raises(HTTPException) as exc_info:
            _build_provider_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "Unknown provider type" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# _build_provider_response
# ---------------------------------------------------------------------------


class TestBuildProviderResponse:
    """Tests for _build_provider_response()."""

    def test_basic_fields(self):
        provider = _make_provider()
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.id == "prov-0001"
        assert resp.name == "test-provider"
        assert resp.type == "ec2"
        assert resp.default_region == "us-east-1"
        assert resp.state == "active"
        assert resp.host_count == 0
        assert resp.has_credentials is False
        assert resp.console_configured is False
        assert resp.created_at == "2025-01-15T12:00:00+00:00"

    def test_with_console_config(self):
        provider = _make_provider(
            console_zone_id="Z123",
            console_base_domain="console.example.com",
            console_nameservers=["ns1.aws.com", "ns2.aws.com"],
        )
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.console_configured is True
        assert resp.console_base_domain == "console.example.com"
        assert resp.console_nameservers == ["ns1.aws.com", "ns2.aws.com"]

    def test_console_configured_from_base_domain_only(self):
        provider = _make_provider(
            console_zone_id=None,
            console_base_domain="apps.cluster.example.com",
        )
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.console_configured is True

    def test_has_credentials_from_provider(self):
        provider = _make_provider(credentials='{"access_key_id": "AK"}')
        provider.get_credentials = MagicMock(return_value={"access_key_id": "AK"})
        resp = _build_provider_response(provider)

        assert resp.has_credentials is True

    def test_has_credentials_override(self):
        provider = _make_provider()
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider, has_credentials=True)

        assert resp.has_credentials is True

    def test_endpoint_url_from_credentials(self):
        provider = _make_provider(
            credentials='{"endpoint_url": "https://s3.custom.com"}'
        )
        provider.get_credentials = MagicMock(
            return_value={"endpoint_url": "https://s3.custom.com"}
        )
        resp = _build_provider_response(provider)

        assert resp.endpoint_url == "https://s3.custom.com"

    def test_endpoint_url_override(self):
        provider = _make_provider()
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(
            provider, endpoint_url="https://override.example.com"
        )

        assert resp.endpoint_url == "https://override.example.com"

    def test_host_count_from_hosts_list(self):
        provider = _make_provider()
        provider.hosts = [MagicMock(), MagicMock(), MagicMock()]
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.host_count == 3

    def test_host_count_override(self):
        provider = _make_provider()
        provider.hosts = [MagicMock()]
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider, host_count=42)

        assert resp.host_count == 42

    def test_gcp_fields(self):
        provider = _make_provider(
            type="gcp",
            gcp_project_id="my-project",
            gcp_network_id="net-link",
            gcp_subnet_id="subnet-link",
            gcp_firewall_policy="troshka-fw",
            gcp_zone="us-central1-a",
        )
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.gcp_project_id == "my-project"
        assert resp.gcp_network_id == "net-link"
        assert resp.gcp_zone == "us-central1-a"

    def test_azure_fields(self):
        provider = _make_provider(
            type="azure",
            azure_subscription_id="sub-1",
            azure_resource_group="troshka-rg",
            azure_vnet_id="/subscriptions/.../vnet",
            azure_subnet_id="/subscriptions/.../subnet",
            azure_nsg_id="/subscriptions/.../nsg",
            azure_location="eastus",
        )
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.azure_subscription_id == "sub-1"
        assert resp.azure_resource_group == "troshka-rg"
        assert resp.azure_location == "eastus"

    def test_iso_pvc_from_credentials(self):
        provider = _make_provider(
            type="ocpvirt",
            credentials='{"iso_pvc": "rhel-iso"}',
        )
        provider.get_credentials = MagicMock(return_value={"iso_pvc": "rhel-iso"})
        resp = _build_provider_response(provider)

        assert resp.iso_pvc == "rhel-iso"

    def test_iso_pvc_none_when_no_credentials(self):
        provider = _make_provider(type="ocpvirt", credentials=None)
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.iso_pvc is None


# ---------------------------------------------------------------------------
# _check_crds_installed
# ---------------------------------------------------------------------------


class TestCheckCrdsInstalled:
    """Tests for _check_crds_installed()."""

    def test_crds_exist(self):
        api_client = MagicMock()
        mock_ext_api = MagicMock()

        with patch("kubernetes.client.ApiextensionsV1Api", return_value=mock_ext_api):
            result, status = _check_crds_installed(api_client)

        assert result is True
        assert status == "installed"
        mock_ext_api.read_custom_resource_definition.assert_called_once_with(
            "troshkaprojects.troshka.redhat.com"
        )

    def test_crds_missing_404(self):
        from kubernetes.client.exceptions import ApiException as K8sApiException

        api_client = MagicMock()
        mock_ext_api = MagicMock()
        mock_ext_api.read_custom_resource_definition.side_effect = K8sApiException(
            status=404, reason="Not Found"
        )

        with patch("kubernetes.client.ApiextensionsV1Api", return_value=mock_ext_api):
            result, status = _check_crds_installed(api_client)

        assert result is False
        assert status == "missing"

    def test_crds_forbidden_403(self):
        from kubernetes.client.exceptions import ApiException as K8sApiException

        api_client = MagicMock()
        mock_ext_api = MagicMock()
        mock_ext_api.read_custom_resource_definition.side_effect = K8sApiException(
            status=403, reason="Forbidden"
        )

        with patch("kubernetes.client.ApiextensionsV1Api", return_value=mock_ext_api):
            result, status = _check_crds_installed(api_client)

        assert result is False
        assert "no permission" in status

    def test_crds_generic_error(self):
        api_client = MagicMock()
        mock_ext_api = MagicMock()
        mock_ext_api.read_custom_resource_definition.side_effect = ConnectionError(
            "timeout"
        )

        with patch("kubernetes.client.ApiextensionsV1Api", return_value=mock_ext_api):
            result, status = _check_crds_installed(api_client)

        assert result is False
        assert status == "missing"


# ---------------------------------------------------------------------------
# _check_operator_deployment
# ---------------------------------------------------------------------------


class TestCheckOperatorDeployment:
    """Tests for _check_operator_deployment()."""

    def test_deployment_ready(self):
        custom_api = MagicMock()
        core_api = MagicMock()

        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "troshka-operator"},
                    "status": {"readyReplicas": 1},
                }
            ]
        }

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "running (1 replica)"

    def test_deployment_not_ready(self):
        custom_api = MagicMock()
        core_api = MagicMock()

        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "troshka-operator"},
                    "status": {"readyReplicas": 0},
                }
            ]
        }

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "not ready"

    def test_deployment_missing_from_namespace(self):
        custom_api = MagicMock()
        core_api = MagicMock()

        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "some-other-deployment"},
                    "status": {"readyReplicas": 1},
                }
            ]
        }

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "namespace exists, deployment missing"

    def test_namespace_not_found(self):
        custom_api = MagicMock()
        core_api = MagicMock()
        core_api.read_namespace.side_effect = Exception("Not Found")

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "not installed"

    def test_empty_deployment_list(self):
        custom_api = MagicMock()
        core_api = MagicMock()

        custom_api.list_namespaced_custom_object.return_value = {"items": []}

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "namespace exists, deployment missing"

    def test_deployment_no_status_key(self):
        custom_api = MagicMock()
        core_api = MagicMock()

        custom_api.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {"name": "troshka-operator"},
                    # no "status" key
                }
            ]
        }

        result = _check_operator_deployment(custom_api, core_api, "troshka-operator")
        assert result == "not ready"


# ---------------------------------------------------------------------------
# _test_s3_provider
# ---------------------------------------------------------------------------


class TestTestS3Provider:
    """Tests for _test_s3_provider().

    boto3 is imported locally inside _test_s3_provider, so we patch
    at the boto3 module level rather than app.api.providers.boto3.
    """

    @patch("boto3.client")
    def test_success(self, mock_boto3_client):
        mock_s3 = MagicMock()
        mock_sts = MagicMock()
        mock_boto3_client.side_effect = lambda svc, **kw: (
            mock_s3 if svc == "s3" else mock_sts
        )
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        provider = _make_provider(type="s3", default_region="us-east-1")
        creds = {
            "access_key_id": "AKID",
            "secret_access_key": "SECRET",
            "bucket": "my-bucket",
        }
        result = _test_s3_provider(provider, creds)

        assert result["status"] == "ok"
        assert result["bucket"] == "my-bucket"
        assert result["account"] == "123456789012"
        mock_s3.head_bucket.assert_called_once_with(
            Bucket="my-bucket", ExpectedBucketOwner="123456789012"
        )

    @patch("boto3.client")
    def test_default_bucket(self, mock_boto3_client):
        mock_s3 = MagicMock()
        mock_sts = MagicMock()
        mock_boto3_client.side_effect = lambda svc, **kw: (
            mock_s3 if svc == "s3" else mock_sts
        )
        mock_sts.get_caller_identity.return_value = {"Account": "111"}

        provider = _make_provider(type="s3")
        creds = {"access_key_id": "AK", "secret_access_key": "SK"}
        result = _test_s3_provider(provider, creds)

        assert result["bucket"] == "troshka-images"

    @patch("boto3.client")
    def test_bucket_not_found(self, mock_boto3_client):
        mock_s3 = MagicMock()
        mock_sts = MagicMock()
        mock_boto3_client.side_effect = lambda svc, **kw: (
            mock_s3 if svc == "s3" else mock_sts
        )
        mock_sts.get_caller_identity.return_value = {"Account": "111"}

        # Build a realistic ClientError with the right structure
        error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
        client_error_cls = type(
            "ClientError",
            (Exception,),
            {
                "__init__": lambda self, resp, op: setattr(self, "response", resp),
            },
        )
        mock_s3.exceptions.ClientError = client_error_cls
        mock_s3.head_bucket.side_effect = client_error_cls(error_response, "HeadBucket")

        provider = _make_provider(type="s3")
        creds = {"access_key_id": "AK", "secret_access_key": "SK", "bucket": "gone"}
        result = _test_s3_provider(provider, creds)

        assert result["status"] == "ok"
        assert result["bucket_missing"] is True
        assert "does not exist" in result["message"]

    @patch("boto3.client")
    def test_bucket_access_denied(self, mock_boto3_client):
        mock_s3 = MagicMock()
        mock_sts = MagicMock()
        mock_boto3_client.side_effect = lambda svc, **kw: (
            mock_s3 if svc == "s3" else mock_sts
        )
        mock_sts.get_caller_identity.return_value = {"Account": "111"}

        error_response = {"Error": {"Code": "403", "Message": "Forbidden"}}
        client_error_cls = type(
            "ClientError",
            (Exception,),
            {
                "__init__": lambda self, resp, op: setattr(self, "response", resp),
            },
        )
        mock_s3.exceptions.ClientError = client_error_cls
        mock_s3.head_bucket.side_effect = client_error_cls(error_response, "HeadBucket")

        provider = _make_provider(type="s3")
        creds = {
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "bucket": "locked",
        }
        result = _test_s3_provider(provider, creds)

        assert result["status"] == "ok"
        assert result["bucket_denied"] is True
        assert "no access" in result["message"]

    @patch("boto3.client")
    def test_connection_error(self, mock_boto3_client):
        mock_boto3_client.side_effect = ConnectionError("Could not connect")

        provider = _make_provider(type="s3")
        creds = {"access_key_id": "AK", "secret_access_key": "SK"}

        with pytest.raises(ConnectionError):
            _test_s3_provider(provider, creds)
