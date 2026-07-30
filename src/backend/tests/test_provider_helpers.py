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


# ---------------------------------------------------------------------------
# _build_cluster_credentials
# ---------------------------------------------------------------------------


class TestBuildClusterCredentials:
    """Tests for _build_cluster_credentials()."""

    def test_ocpvirt_success(self):
        from app.api.providers import _build_cluster_credentials

        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            api_url="https://api.cluster.example.com:6443",
            token="sha256~token",
            namespace="troshka",
        )
        provider = _make_provider(type="ocpvirt")
        creds = _build_cluster_credentials(body, provider)

        assert creds["api_url"] == "https://api.cluster.example.com:6443"
        assert creds["token"] == "sha256~token"
        assert creds["namespace"] == "troshka"
        assert creds["verify_ssl"] is False
        assert provider.default_region == "troshka"
        assert provider.console_base_domain == "apps.cluster.example.com"

    def test_ocpvirt_with_iso_pvc(self):
        from app.api.providers import _build_cluster_credentials

        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            api_url="https://api.ocp.example.com:6443",
            token="tok",
            iso_pvc="rhel-iso-pvc",
        )
        provider = _make_provider(type="ocpvirt")
        creds = _build_cluster_credentials(body, provider)

        assert creds["iso_pvc"] == "rhel-iso-pvc"

    def test_kubevirt_success(self):
        from app.api.providers import _build_cluster_credentials

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
        creds = _build_cluster_credentials(body, provider)

        assert creds["api_url"] == "https://api.kv.example.com:6443"
        assert creds["token"] == "sha256~kvtoken"
        assert creds["namespace"] == "my-operator-ns"
        assert creds["cache_namespace"] == "my-cache-ns"
        assert creds["project_prefix"] == "lab-"
        assert provider.default_region == "my-operator-ns"
        assert provider.console_base_domain == "apps.kv.example.com"

    def test_missing_api_url_raises_400(self):
        from app.api.providers import _build_cluster_credentials

        body = ProviderCreate(
            name="ocp",
            type="ocpvirt",
            # api_url defaults to "" (falsy)
            token="tok",
        )
        provider = _make_provider(type="ocpvirt")
        with pytest.raises(HTTPException) as exc_info:
            _build_cluster_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "api_url" in str(exc_info.value.detail)

    def test_kubevirt_defaults(self):
        from app.api.providers import _build_cluster_credentials

        body = ProviderCreate(
            name="kv",
            type="kubevirt",
            api_url="https://api.kv.example.com:6443",
            token="tok",
            namespace="",  # empty → falls back to "troshka-operator"
            cache_namespace="",
            project_prefix="",
        )
        provider = _make_provider(type="kubevirt")
        creds = _build_cluster_credentials(body, provider)

        assert creds["namespace"] == "troshka-operator"
        assert creds["cache_namespace"] == "troshka-cache"
        assert creds["project_prefix"] == "troshka-"
        assert provider.default_region == "troshka-operator"


# ---------------------------------------------------------------------------
# _build_cloud_credentials
# ---------------------------------------------------------------------------


class TestBuildCloudCredentials:
    """Tests for _build_cloud_credentials()."""

    def test_gcp_success(self):
        from app.api.providers import _build_cloud_credentials

        sa_json = {"type": "service_account", "project_id": "proj"}
        body = ProviderCreate(
            name="gcp",
            type="gcp",
            gcp_project_id="proj",
            service_account_json=json.dumps(sa_json),
        )
        provider = _make_provider(type="gcp")
        creds = _build_cloud_credentials(body, provider)

        assert creds["service_account_json"] == sa_json
        assert provider.gcp_project_id == "proj"

    def test_gcp_invalid_json_raises_400(self):
        from app.api.providers import _build_cloud_credentials

        body = ProviderCreate(
            name="gcp",
            type="gcp",
            gcp_project_id="proj",
            service_account_json="not valid json {{{",
        )
        provider = _make_provider(type="gcp")
        with pytest.raises(HTTPException) as exc_info:
            _build_cloud_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "valid JSON" in str(exc_info.value.detail)

    def test_gcp_missing_fields_raises_400(self):
        from app.api.providers import _build_cloud_credentials

        body = ProviderCreate(
            name="gcp",
            type="gcp",
            # missing gcp_project_id and service_account_json
        )
        provider = _make_provider(type="gcp")
        with pytest.raises(HTTPException) as exc_info:
            _build_cloud_credentials(body, provider)
        assert exc_info.value.status_code == 400
        assert "gcp_project_id" in str(exc_info.value.detail)

    def test_azure_success(self):
        from app.api.providers import _build_cloud_credentials

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
        creds = _build_cloud_credentials(body, provider)

        assert creds["tenant_id"] == "tenant-1"
        assert creds["client_id"] == "client-1"
        assert creds["client_secret"] == "secret-1"
        assert creds["subscription_id"] == "sub-1"
        assert provider.azure_subscription_id == "sub-1"
        assert provider.azure_location == "eastus"

    def test_azure_missing_fields_raises_400(self):
        from app.api.providers import _build_cloud_credentials

        body = ProviderCreate(
            name="az",
            type="azure",
            azure_tenant_id="tenant-1",
            # missing client_id, client_secret, subscription_id
        )
        provider = _make_provider(type="azure")
        with pytest.raises(HTTPException) as exc_info:
            _build_cloud_credentials(body, provider)
        assert exc_info.value.status_code == 400

    def test_azure_location_falls_back_to_default_region(self):
        from app.api.providers import _build_cloud_credentials

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
        _build_cloud_credentials(body, provider)

        assert provider.azure_location == "westus"


# ---------------------------------------------------------------------------
# _ensure_namespaces
# ---------------------------------------------------------------------------


class TestEnsureNamespaces:
    """Tests for _ensure_namespaces()."""

    def test_namespace_exists(self):
        from app.api.providers import _ensure_namespaces

        core_api = MagicMock()
        # read_namespace succeeds → namespace exists
        result = _ensure_namespaces(core_api, [("troshka-operator", "operator")])

        assert result == {"operator": "ok"}
        core_api.read_namespace.assert_called_once_with("troshka-operator")
        core_api.create_namespace.assert_not_called()

    def test_namespace_created(self):
        from app.api.providers import _ensure_namespaces

        core_api = MagicMock()
        core_api.read_namespace.side_effect = Exception("Not Found")
        # create_namespace succeeds

        result = _ensure_namespaces(core_api, [("troshka-cache", "cache")])

        assert result == {"cache": "ok (just created)"}
        core_api.create_namespace.assert_called_once()

    def test_namespace_no_access(self):
        from app.api.providers import _ensure_namespaces

        core_api = MagicMock()
        core_api.read_namespace.side_effect = Exception("Not Found")
        core_api.create_namespace.side_effect = Exception("Forbidden")

        result = _ensure_namespaces(core_api, [("locked-ns", "locked")])

        assert result == {"locked": "no access"}

    def test_multiple_namespaces(self):
        from app.api.providers import _ensure_namespaces

        core_api = MagicMock()

        def read_ns_side_effect(name):
            if name == "ns-exists":
                return MagicMock()
            raise Exception("Not Found")

        def create_ns_side_effect(body):
            if body["metadata"]["name"] == "ns-created":
                return MagicMock()
            raise Exception("Forbidden")

        core_api.read_namespace.side_effect = read_ns_side_effect
        core_api.create_namespace.side_effect = create_ns_side_effect

        result = _ensure_namespaces(
            core_api,
            [
                ("ns-exists", "existing"),
                ("ns-created", "new"),
                ("ns-denied", "denied"),
            ],
        )

        assert result == {
            "existing": "ok",
            "new": "ok (just created)",
            "denied": "no access",
        }

    def test_empty_list(self):
        from app.api.providers import _ensure_namespaces

        core_api = MagicMock()
        result = _ensure_namespaces(core_api, [])
        assert result == {}


# ---------------------------------------------------------------------------
# _enqueue_cluster_host_provision
# ---------------------------------------------------------------------------


class TestEnqueueClusterHostProvision:
    """Tests for _enqueue_cluster_host_provision()."""

    @patch("app.api.providers.enqueue_job", create=True)
    def test_ocpvirt_enqueues_ocpvirt_job(self, mock_enqueue):
        from app.api.providers import _enqueue_cluster_host_provision

        provider = _make_provider(type="ocpvirt")
        db = MagicMock()

        with patch("app.core.redis.enqueue_job", mock_enqueue):
            _enqueue_cluster_host_provision(provider, db)

        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

        # Verify the host was created with the right attributes
        host = db.add.call_args[0][0]
        assert host.state == "provisioning"
        assert host.host_type == "kubevirt-cluster"
        assert host.provider_id == "prov-0001"

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args
        assert call_args.kwargs.get("queue_name") == "host_lifecycle"

    @patch("app.api.providers.enqueue_job", create=True)
    def test_kubevirt_enqueues_kubevirt_job(self, mock_enqueue):
        from app.api.providers import _enqueue_cluster_host_provision

        provider = _make_provider(type="kubevirt")
        db = MagicMock()

        with patch("app.core.redis.enqueue_job", mock_enqueue):
            _enqueue_cluster_host_provision(provider, db)

        db.add.assert_called_once()
        db.commit.assert_called_once()

        host = db.add.call_args[0][0]
        assert host.state == "provisioning"
        assert host.host_type == "kubevirt-cluster"

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args
        assert call_args.kwargs.get("queue_name") == "host_lifecycle"

    @patch("app.api.providers.enqueue_job", create=True)
    def test_host_region_from_provider(self, mock_enqueue):
        from app.api.providers import _enqueue_cluster_host_provision

        provider = _make_provider(type="ocpvirt", default_region="my-ns")
        db = MagicMock()

        with patch("app.core.redis.enqueue_job", mock_enqueue):
            _enqueue_cluster_host_provision(provider, db)

        host = db.add.call_args[0][0]
        assert host.region == "my-ns"

    @patch("app.api.providers.enqueue_job", create=True)
    def test_ec2_does_not_enqueue(self, mock_enqueue):
        """Non-cluster provider types do not match either branch."""
        from app.api.providers import _enqueue_cluster_host_provision

        provider = _make_provider(type="ec2")
        db = MagicMock()

        with patch("app.core.redis.enqueue_job", mock_enqueue):
            _enqueue_cluster_host_provision(provider, db)

        # Host is still created (function creates it before the type check)
        db.add.assert_called_once()
        # But no job is enqueued
        mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# _cleanup_kubevirt_k8s_resources
# ---------------------------------------------------------------------------


class TestCleanupKubevirtK8sResources:
    """Tests for _cleanup_kubevirt_k8s_resources()."""

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch("kubernetes.client.AppsV1Api")
    @patch("kubernetes.client.ApiextensionsV1Api")
    def test_success_returns_core_api(
        self, mock_ext_api_cls, mock_apps_api_cls, mock_get_clients
    ):
        from app.api.providers import _cleanup_kubevirt_k8s_resources

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)

        provider = _make_provider(type="kubevirt")
        creds = {"namespace": "op-ns", "cache_namespace": "cache-ns"}

        result = _cleanup_kubevirt_k8s_resources(provider, "prov-0001", creds)

        assert result is mock_core
        mock_apps_api_cls.return_value.delete_namespaced_deployment.assert_called_once()
        mock_core.delete_namespaced_service_account.assert_called_once()
        # 3 CRDs deleted
        assert (
            mock_ext_api_cls.return_value.delete_custom_resource_definition.call_count
            == 3
        )
        # 2 namespaces deleted (op-ns + cache-ns)
        assert mock_core.delete_namespace.call_count == 2

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_connection_failure_returns_none(self, mock_get_clients):
        from app.api.providers import _cleanup_kubevirt_k8s_resources

        mock_get_clients.side_effect = ConnectionError("Cannot connect")

        provider = _make_provider(type="kubevirt")
        creds = {"namespace": "op-ns"}

        result = _cleanup_kubevirt_k8s_resources(provider, "prov-0001", creds)

        assert result is None

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch("kubernetes.client.AppsV1Api")
    @patch("kubernetes.client.ApiextensionsV1Api")
    def test_individual_deletion_failures_silenced(
        self, mock_ext_api_cls, mock_apps_api_cls, mock_get_clients
    ):
        from app.api.providers import _cleanup_kubevirt_k8s_resources

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)

        # Make every delete fail
        mock_apps_api_cls.return_value.delete_namespaced_deployment.side_effect = (
            Exception("fail")
        )
        mock_core.delete_namespaced_service_account.side_effect = Exception("fail")
        mock_ext_api_cls.return_value.delete_custom_resource_definition.side_effect = (
            Exception("fail")
        )
        mock_core.delete_namespace.side_effect = Exception("fail")

        provider = _make_provider(type="kubevirt")
        creds = {"namespace": "op-ns", "cache_namespace": "cache-ns"}

        # Should not raise — individual failures are silenced
        result = _cleanup_kubevirt_k8s_resources(provider, "prov-0001", creds)

        assert result is mock_core

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    @patch("kubernetes.client.AppsV1Api")
    @patch("kubernetes.client.ApiextensionsV1Api")
    def test_uses_default_namespace_names(
        self, mock_ext_api_cls, mock_apps_api_cls, mock_get_clients
    ):
        from app.api.providers import _cleanup_kubevirt_k8s_resources

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)

        provider = _make_provider(type="kubevirt")
        creds = {}  # no namespace/cache_namespace → use defaults

        _cleanup_kubevirt_k8s_resources(provider, "prov-0001", creds)

        # Should use defaults: "troshka-operator" and "troshka-cache"
        delete_ns_calls = mock_core.delete_namespace.call_args_list
        deleted_ns_names = [c.kwargs.get("name") for c in delete_ns_calls]
        assert "troshka-operator" in deleted_ns_names
        assert "troshka-cache" in deleted_ns_names


# ---------------------------------------------------------------------------
# _cleanup_kubevirt_db_resources
# ---------------------------------------------------------------------------


class TestCleanupKubevirtDbResources:
    """Tests for _cleanup_kubevirt_db_resources()."""

    def test_with_projects_and_core_api(self):
        from app.api.providers import _cleanup_kubevirt_db_resources

        db = MagicMock()
        provider = _make_provider(type="kubevirt")
        core_api = MagicMock()
        creds = {"project_prefix": "lab-"}

        mock_host = MagicMock()
        mock_host.id = "host-001"
        mock_project = MagicMock()
        mock_project.id = "proj-0001-abcd-efgh"

        db.query.return_value.filter_by.return_value.all.return_value = [mock_host]
        db.query.return_value.filter.return_value.all.return_value = [mock_project]

        _cleanup_kubevirt_db_resources(db, provider, creds, core_api)

        # Namespace should be deleted via core_api
        core_api.delete_namespace.assert_called_once_with(name="lab-proj-000")
        # Project and host should be deleted from DB
        db.delete.assert_any_call(mock_project)
        db.delete.assert_any_call(mock_host)

    def test_without_core_api_skips_namespace_delete(self):
        from app.api.providers import _cleanup_kubevirt_db_resources

        db = MagicMock()
        provider = _make_provider(type="kubevirt")
        creds = {"project_prefix": "troshka-"}

        mock_host = MagicMock()
        mock_host.id = "host-001"
        mock_project = MagicMock()
        mock_project.id = "proj-0001-abcd-efgh"

        db.query.return_value.filter_by.return_value.all.return_value = [mock_host]
        db.query.return_value.filter.return_value.all.return_value = [mock_project]

        _cleanup_kubevirt_db_resources(db, provider, creds, None)

        # Project still deleted from DB, but no namespace call
        db.delete.assert_any_call(mock_project)
        db.delete.assert_any_call(mock_host)

    def test_no_hosts(self):
        from app.api.providers import _cleanup_kubevirt_db_resources

        db = MagicMock()
        provider = _make_provider(type="kubevirt")
        core_api = MagicMock()
        creds = {}

        db.query.return_value.filter_by.return_value.all.return_value = []

        _cleanup_kubevirt_db_resources(db, provider, creds, core_api)

        # No hosts means no projects queried, no deletes
        core_api.delete_namespace.assert_not_called()
        db.delete.assert_not_called()

    def test_namespace_delete_failure_silenced(self):
        from app.api.providers import _cleanup_kubevirt_db_resources

        db = MagicMock()
        provider = _make_provider(type="kubevirt")
        core_api = MagicMock()
        core_api.delete_namespace.side_effect = Exception("Forbidden")
        creds = {"project_prefix": "troshka-"}

        mock_host = MagicMock()
        mock_host.id = "host-001"
        mock_project = MagicMock()
        mock_project.id = "proj-0001-abcd-efgh"

        db.query.return_value.filter_by.return_value.all.return_value = [mock_host]
        db.query.return_value.filter.return_value.all.return_value = [mock_project]

        # Should not raise
        _cleanup_kubevirt_db_resources(db, provider, creds, core_api)

        # Project and host still deleted even if namespace deletion failed
        db.delete.assert_any_call(mock_project)
        db.delete.assert_any_call(mock_host)


# ---------------------------------------------------------------------------
# _test_kubevirt_provider
# ---------------------------------------------------------------------------


class TestTestKubevirtProvider:
    """Tests for _test_kubevirt_provider()."""

    @patch("app.api.providers._ensure_namespaces")
    @patch("app.api.providers._check_crds_installed")
    @patch("app.api.providers._check_operator_deployment")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_success(
        self,
        mock_get_clients,
        mock_check_operator,
        mock_check_crds,
        mock_ensure_ns,
    ):
        from app.api.providers import _test_kubevirt_provider

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)

        # Simulate 3 nodes
        mock_nodes = MagicMock()
        mock_nodes.items = [MagicMock(), MagicMock(), MagicMock()]
        mock_core.list_node.return_value = mock_nodes

        mock_check_operator.return_value = "running (1 replica)"
        mock_check_crds.return_value = (True, "installed")
        mock_ensure_ns.return_value = {"operator": "ok", "cache": "ok"}

        provider = _make_provider(type="kubevirt")
        creds = {
            "api_url": "https://api.kv.example.com:6443",
            "namespace": "troshka-operator",
            "cache_namespace": "troshka-cache",
        }

        result = _test_kubevirt_provider(provider, creds)

        assert result["status"] == "ok"
        assert result["cluster"] == "https://api.kv.example.com:6443"
        assert result["nodes"] == 3
        assert result["operator"] == "running (1 replica)"
        assert result["crds"] == "installed"
        assert result["crds_installed"] is True
        assert result["namespaces"] == {"operator": "ok", "cache": "ok"}

    @patch("app.api.providers._ensure_namespaces")
    @patch("app.api.providers._check_crds_installed")
    @patch("app.api.providers._check_operator_deployment")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_uses_default_namespaces(
        self,
        mock_get_clients,
        mock_check_operator,
        mock_check_crds,
        mock_ensure_ns,
    ):
        from app.api.providers import _test_kubevirt_provider

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)
        mock_core.list_node.return_value = MagicMock(items=[])
        mock_check_operator.return_value = "not installed"
        mock_check_crds.return_value = (False, "missing")
        mock_ensure_ns.return_value = {}

        provider = _make_provider(type="kubevirt")
        creds = {}  # no namespace/cache_namespace → defaults

        _test_kubevirt_provider(provider, creds)

        # Should call _check_operator_deployment with default "troshka-operator"
        mock_check_operator.assert_called_once_with(
            mock_custom, mock_core, "troshka-operator"
        )
        # Should pass default namespaces to _ensure_namespaces
        mock_ensure_ns.assert_called_once_with(
            mock_core,
            [("troshka-operator", "operator"), ("troshka-cache", "cache")],
        )

    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_connection_failure_raises(self, mock_get_clients):
        from app.api.providers import _test_kubevirt_provider

        mock_get_clients.side_effect = ConnectionError("Cannot connect to cluster")

        provider = _make_provider(type="kubevirt")
        creds = {"api_url": "https://api.kv.example.com:6443"}

        with pytest.raises(ConnectionError):
            _test_kubevirt_provider(provider, creds)

    @patch("app.api.providers._ensure_namespaces")
    @patch("app.api.providers._check_crds_installed")
    @patch("app.api.providers._check_operator_deployment")
    @patch("app.services.providers.kubevirt._get_k8s_clients")
    def test_zero_nodes(
        self,
        mock_get_clients,
        mock_check_operator,
        mock_check_crds,
        mock_ensure_ns,
    ):
        from app.api.providers import _test_kubevirt_provider

        mock_custom = MagicMock()
        mock_core = MagicMock()
        mock_api_client = MagicMock()
        mock_get_clients.return_value = (mock_custom, mock_core, mock_api_client)
        mock_core.list_node.return_value = MagicMock(items=[])
        mock_check_operator.return_value = "not installed"
        mock_check_crds.return_value = (False, "missing")
        mock_ensure_ns.return_value = {"operator": "no access"}

        provider = _make_provider(type="kubevirt")
        creds = {"api_url": "https://api.kv.example.com:6443"}

        result = _test_kubevirt_provider(provider, creds)

        assert result["status"] == "ok"
        assert result["nodes"] == 0
        assert result["crds_installed"] is False


# ── version_key tests ──


class TestVersionKey:
    def test_sorts_rhel_versions(self):
        """version_key is a local function inside discover_images. Test sorting."""
        images = [
            {"Name": "RHEL-9.4.0-x86_64-20240501"},
            {"Name": "RHEL-9.2.0-x86_64-20240301"},
            {"Name": "RHEL-10.1.0-x86_64-20240601"},
        ]
        import re

        def version_key(img):
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", img["Name"])
            return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

        sorted_imgs = sorted(images, key=version_key, reverse=True)
        assert "10.1.0" in sorted_imgs[0]["Name"]
        assert "9.4.0" in sorted_imgs[1]["Name"]

    def test_version_key_no_match(self):
        import re

        def version_key(img):
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", img["Name"])
            return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

        result = version_key({"Name": "unknown-image"})
        assert result == (0, 0, 0)


# ── _test_s3_provider edge cases ──


class TestTestS3ProviderEdgeCases:
    @patch("boto3.client")
    def test_s3_list_objects_empty(self, mock_boto):
        mock_s3 = MagicMock()
        mock_sts = MagicMock()
        mock_boto.side_effect = lambda svc, **kw: (mock_s3 if svc == "s3" else mock_sts)
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        provider = _make_provider(type="s3")
        creds = {
            "access_key_id": "test",
            "secret_access_key": "test",
            "endpoint_url": "https://s3.example.com",
            "bucket": "test-bucket",
            "region": "us-east-1",
        }
        result = _test_s3_provider(provider, creds)
        assert result["status"] == "ok"

    @patch("boto3.client")
    def test_s3_connection_error(self, mock_boto):
        mock_boto.side_effect = Exception("ConnectionRefused")

        provider = _make_provider(type="s3")
        creds = {
            "access_key_id": "test",
            "secret_access_key": "test",
            "endpoint_url": "https://s3.example.com",
            "bucket": "test-bucket",
            "region": "us-east-1",
        }
        with pytest.raises(Exception, match="ConnectionRefused"):
            _test_s3_provider(provider, creds)


# ---------------------------------------------------------------------------
# _build_provider_response — additional edge cases
# ---------------------------------------------------------------------------


class TestBuildProviderResponseEdgeCases:
    """Additional edge cases for _build_provider_response()."""

    def test_kubevirt_type_fields(self):
        provider = _make_provider(
            type="kubevirt",
            console_base_domain="apps.kv.example.com",
        )
        provider.get_credentials = MagicMock(
            return_value={
                "api_url": "https://api.kv.example.com:6443",
                "token": "sha256~tok",
                "namespace": "troshka-operator",
                "cache_namespace": "troshka-cache",
                "project_prefix": "troshka-",
            }
        )
        provider.credentials = '{"api_url":"x"}'
        resp = _build_provider_response(provider)

        assert resp.type == "kubevirt"
        assert resp.console_configured is True
        assert resp.has_credentials is True

    def test_s3_readonly_type(self):
        provider = _make_provider(
            type="s3_readonly",
            credentials='{"access_key_id":"AK","bucket":"my-bucket"}',
        )
        provider.get_credentials = MagicMock(
            return_value={"access_key_id": "AK", "bucket": "my-bucket"}
        )
        resp = _build_provider_response(provider)

        assert resp.type == "s3_readonly"
        assert resp.has_credentials is True

    def test_no_created_at(self):
        provider = _make_provider(created_at=None)
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.created_at == ""

    def test_all_overrides_combined(self):
        provider = _make_provider()
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(
            provider,
            has_credentials=True,
            endpoint_url="https://custom.s3.com",
            host_count=99,
        )

        assert resp.has_credentials is True
        assert resp.endpoint_url == "https://custom.s3.com"
        assert resp.host_count == 99

    def test_credentials_none_no_endpoint_lookup(self):
        """When credentials is None, endpoint_url is not extracted."""
        provider = _make_provider(credentials=None)
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.endpoint_url is None
        assert resp.iso_pvc is None

    def test_security_group_id(self):
        provider = _make_provider(security_group_id="sg-test123")
        provider.get_credentials = MagicMock(return_value={})
        resp = _build_provider_response(provider)

        assert resp.security_group_id == "sg-test123"


# ---------------------------------------------------------------------------
# update_provider endpoint — credential update logic
# ---------------------------------------------------------------------------

import uuid

from fastapi.testclient import TestClient

from app.core.auth import create_jwt, hash_password
from app.core.database import get_db
from app.main import app
from app.models.provider import Provider as ProviderModel
from app.models.user import User as UserModel
from tests.conftest import TestSession, get_test_db

app.dependency_overrides[get_db] = get_test_db
_prov_client = TestClient(app)

# Module-level fixtures for provider API tests
_prov_db = TestSession()
_prov_admin = UserModel(
    email="prov-admin@example.com",
    display_name="Prov Admin",
    role="admin",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_prov_db.add(_prov_admin)
_prov_db.commit()
_prov_db.refresh(_prov_admin)
_PROV_ADMIN_TOKEN = create_jwt(
    user_id=_prov_admin.id, email=_prov_admin.email, role=_prov_admin.role
)
PROV_ADMIN_HEADERS = {"Authorization": f"Bearer {_PROV_ADMIN_TOKEN}"}

_prov_user = UserModel(
    email="prov-user@example.com",
    display_name="Prov User",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_prov_db.add(_prov_user)
_prov_db.commit()
_prov_db.refresh(_prov_user)
_PROV_USER_TOKEN = create_jwt(
    user_id=_prov_user.id, email=_prov_user.email, role=_prov_user.role
)
PROV_USER_HEADERS = {"Authorization": f"Bearer {_PROV_USER_TOKEN}"}
_prov_db.close()


def _create_test_provider(ptype="ec2", creds=None, **overrides):
    """Create a provider in the test DB and return its ID."""
    import json as _json

    db = TestSession()
    prov = ProviderModel(
        id=str(uuid.uuid4()),
        name=overrides.pop("name", f"test-{uuid.uuid4().hex[:6]}"),
        type=ptype,
        credentials=_json.dumps(
            creds or {"access_key_id": "AK", "secret_access_key": "SK"}
        ),
        default_region=overrides.pop("default_region", "us-east-1"),
        state=overrides.pop("state", "active"),
    )
    for k, v in overrides.items():
        setattr(prov, k, v)
    db.add(prov)
    db.commit()
    db.refresh(prov)
    pid = prov.id
    db.close()
    return pid


class TestListProvidersEndpoint:
    """Tests for GET /providers/ endpoint."""

    def test_list_returns_200(self):
        resp = _prov_client.get("/api/v1/providers/", headers=PROV_ADMIN_HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_includes_created_provider(self):
        name = f"list-prov-{uuid.uuid4().hex[:6]}"
        pid = _create_test_provider(name=name)
        resp = _prov_client.get("/api/v1/providers/", headers=PROV_ADMIN_HEADERS)
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert pid in ids

    def test_list_requires_admin(self):
        resp = _prov_client.get("/api/v1/providers/", headers=PROV_USER_HEADERS)
        assert resp.status_code == 403


class TestUpdateProviderEndpoint:
    """Tests for PATCH /providers/{id} endpoint — credential update logic."""

    def test_update_basic_fields(self):
        pid = _create_test_provider()
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={"name": "updated-name", "default_region": "eu-west-1"},
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated-name"
        assert resp.json()["default_region"] == "eu-west-1"

    def test_update_provider_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _prov_client.patch(
            f"/api/v1/providers/{fake_id}",
            json={"name": "nope"},
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_update_aws_credentials(self):
        """Updating access_key_id/secret_access_key merges into credentials dict."""
        pid = _create_test_provider(
            ptype="ec2",
            creds={"access_key_id": "OLD_AK", "secret_access_key": "OLD_SK"},
        )
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={"access_key_id": "NEW_AK", "secret_access_key": "NEW_SK"},
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["has_credentials"] is True

        # Verify the credentials were updated in DB
        db = TestSession()
        prov = db.query(ProviderModel).filter_by(id=pid).first()
        creds = prov.get_credentials()
        assert creds["access_key_id"] == "NEW_AK"
        assert creds["secret_access_key"] == "NEW_SK"
        db.close()

    def test_update_aws_credentials_partial(self):
        """Updating only access_key_id keeps existing secret_access_key."""
        pid = _create_test_provider(
            ptype="ec2",
            creds={"access_key_id": "OLD_AK", "secret_access_key": "KEEP_SK"},
        )
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={"access_key_id": "NEW_AK"},
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        db = TestSession()
        prov = db.query(ProviderModel).filter_by(id=pid).first()
        creds = prov.get_credentials()
        assert creds["access_key_id"] == "NEW_AK"
        assert creds["secret_access_key"] == "KEEP_SK"
        db.close()

    def test_update_ocp_credentials(self):
        """Updating api_url/token/namespace on ocpvirt provider."""
        pid = _create_test_provider(
            ptype="ocpvirt",
            creds={
                "api_url": "https://old.example.com:6443",
                "token": "old-token",
                "namespace": "old-ns",
            },
        )
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={
                "api_url": "https://new.example.com:6443",
                "token": "new-token",
                "namespace": "new-ns",
            },
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        db = TestSession()
        prov = db.query(ProviderModel).filter_by(id=pid).first()
        creds = prov.get_credentials()
        assert creds["api_url"] == "https://new.example.com:6443"
        assert creds["token"] == "new-token"
        assert creds["namespace"] == "new-ns"
        # namespace update also sets default_region for ocpvirt
        assert prov.default_region == "new-ns"
        db.close()

    def test_update_kubevirt_namespace_sets_default_region(self):
        """Updating namespace on kubevirt provider updates default_region."""
        pid = _create_test_provider(
            ptype="kubevirt",
            creds={
                "api_url": "https://api.kv.example.com:6443",
                "token": "tok",
                "namespace": "troshka-operator",
                "cache_namespace": "troshka-cache",
                "project_prefix": "troshka-",
            },
        )
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={
                "namespace": "custom-ns",
                "cache_namespace": "custom-cache",
                "project_prefix": "lab-",
            },
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        db = TestSession()
        prov = db.query(ProviderModel).filter_by(id=pid).first()
        creds = prov.get_credentials()
        assert creds["namespace"] == "custom-ns"
        assert creds["cache_namespace"] == "custom-cache"
        assert creds["project_prefix"] == "lab-"
        assert prov.default_region == "custom-ns"
        db.close()

    def test_update_state(self):
        pid = _create_test_provider()
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={"state": "disabled"},
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "disabled"

    def test_update_vpc_and_subnet(self):
        pid = _create_test_provider()
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={
                "vpc_id": "vpc-new",
                "subnet_id": "subnet-new",
                "security_group_id": "sg-new",
            },
            headers=PROV_ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vpc_id"] == "vpc-new"
        assert data["subnet_id"] == "subnet-new"
        assert data["security_group_id"] == "sg-new"

    def test_update_requires_admin(self):
        pid = _create_test_provider()
        resp = _prov_client.patch(
            f"/api/v1/providers/{pid}",
            json={"name": "nope"},
            headers=PROV_USER_HEADERS,
        )
        assert resp.status_code == 403


class TestDeleteProviderEndpoint:
    """Tests for DELETE /providers/{id} — covers lines 518-532."""

    def test_delete_provider_no_hosts(self):
        pid = _create_test_provider()
        resp = _prov_client.delete(
            f"/api/v1/providers/{pid}", headers=PROV_ADMIN_HEADERS
        )
        assert resp.status_code == 204

        db = TestSession()
        assert db.query(ProviderModel).filter_by(id=pid).first() is None
        db.close()

    def test_delete_provider_not_found(self):
        fake_id = str(uuid.uuid4())
        resp = _prov_client.delete(
            f"/api/v1/providers/{fake_id}", headers=PROV_ADMIN_HEADERS
        )
        assert resp.status_code == 404

    def test_delete_provider_with_hosts_rejected(self):
        from app.models.host import Host

        pid = _create_test_provider()
        db = TestSession()
        h = Host(
            id=str(uuid.uuid4()),
            state="active",
            host_type="ec2",
            provider_id=pid,
        )
        db.add(h)
        db.commit()
        db.close()

        resp = _prov_client.delete(
            f"/api/v1/providers/{pid}", headers=PROV_ADMIN_HEADERS
        )
        assert resp.status_code == 409
        assert "hosts" in resp.json()["detail"].lower()

    def test_delete_requires_admin(self):
        pid = _create_test_provider()
        resp = _prov_client.delete(
            f"/api/v1/providers/{pid}", headers=PROV_USER_HEADERS
        )
        assert resp.status_code == 403
