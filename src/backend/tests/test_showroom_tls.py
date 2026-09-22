from types import SimpleNamespace

from app.services.deploy_service import _showroom_fqdn


def test_fqdn_when_dns_configured():
    p = SimpleNamespace(dns_provider_id="d", guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == "showroom.abc.example.com"


def test_fqdn_empty_when_no_dns():
    p = SimpleNamespace(dns_provider_id=None, guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == ""
