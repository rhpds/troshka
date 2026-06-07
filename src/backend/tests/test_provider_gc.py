from unittest.mock import MagicMock, patch

from app.models.elastic_ip import ElasticIp
from app.models.provider import Provider
from tests.conftest import TestSession


_db = TestSession()
_provider = Provider(name="gc-test-provider", type="ec2", default_region="us-east-1", state="active")
_provider.set_credentials({"access_key_id": "fake", "secret_access_key": "fake"})
_provider.security_group_id = "sg-gctest"
_db.add(_provider)
_db.commit()
_db.refresh(_provider)
GC_PROVIDER_ID = _provider.id
_db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_releases_orphan_eips(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-orphan1",
                "PublicIp": "54.0.0.1",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "troshka"},
                    {"Key": "troshka-project-id", "Value": "nonexistent-project-id"},
                ],
            },
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": []}]
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=False)

    assert result["eips_released"] == 1
    mock_ec2.release_address.assert_called_once_with(AllocationId="eipalloc-orphan1")
    db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_dry_run_does_not_release(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-orphan2",
                "PublicIp": "54.0.0.2",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "troshka"},
                    {"Key": "troshka-project-id", "Value": "nonexistent-project-id-2"},
                ],
            },
        ]
    }
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": []}]
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=True)

    assert result["eips_released"] == 0
    assert result["eips_would_release"] == 1
    mock_ec2.release_address.assert_not_called()
    db.close()


@patch("app.services.provider_gc_service._get_ec2_client")
def test_gc_removes_stale_sg_rules(mock_get_client):
    mock_ec2 = MagicMock()
    mock_ec2.describe_addresses.return_value = {"Addresses": []}
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"IpPermissions": [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 9090, "ToPort": 9090,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "troshka-pf:dead-project:9090"}]},
        ]}],
    }
    mock_get_client.return_value = mock_ec2

    db = TestSession()
    provider = db.query(Provider).filter_by(id=GC_PROVIDER_ID).first()

    from app.services.provider_gc_service import reconcile_provider
    result = reconcile_provider(db, provider, dry_run=False)

    assert result["sg_rules_removed"] == 1
    mock_ec2.revoke_security_group_ingress.assert_called_once()
    db.close()
