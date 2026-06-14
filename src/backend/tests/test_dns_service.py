from unittest.mock import patch

from app.services.dns_service import (
    create_dns_records,
    delete_dns_records,
    resolve_dns_records,
)


def test_resolve_dns_records_replaces_tokens():
    templates = [
        {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
        {"name": "*.apps.{guid}.{domain}", "type": "A", "target": "eip"},
    ]
    records = resolve_dns_records(
        templates, guid="abc123", domain="lab.example.com", eip="1.2.3.4"
    )
    assert records[0]["name"] == "api.abc123.lab.example.com"
    assert records[0]["value"] == "1.2.3.4"
    assert records[1]["name"] == "*.apps.abc123.lab.example.com"
    assert records[1]["value"] == "1.2.3.4"


def test_resolve_dns_records_handles_missing_eip():
    templates = [
        {"name": "api.{guid}.{domain}", "type": "A", "target": "eip"},
    ]
    records = resolve_dns_records(templates, guid="abc", domain="ex.com", eip=None)
    assert records[0]["value"] is None


def test_resolve_dns_records_non_eip_target():
    templates = [
        {
            "name": "cname.{guid}.{domain}",
            "type": "CNAME",
            "target": "other.example.com",
        },
    ]
    records = resolve_dns_records(templates, guid="abc", domain="ex.com", eip="1.2.3.4")
    assert records[0]["value"] == "other.example.com"


@patch("app.services.dns_service._nsupdate_create")
def test_create_dns_records_nsupdate(mock_nsupdate):
    provider_config = {
        "server": "10.0.0.53",
        "port": 53,
        "key_name": "update-key",
        "key_secret": "secret",
        "key_algorithm": "hmac-sha256",
        "default_zone": "example.com",
    }
    records = [
        {"name": "api.abc.example.com", "type": "A", "value": "1.2.3.4"},
    ]
    errors = create_dns_records("nsupdate", provider_config, records, ttl=30)
    mock_nsupdate.assert_called_once_with(provider_config, records, 30, errors)
    assert errors == []


@patch("app.services.dns_service._nsupdate_delete")
def test_delete_dns_records_nsupdate(mock_nsupdate):
    provider_config = {
        "server": "10.0.0.53",
        "key_name": "update-key",
        "key_secret": "secret",
        "key_algorithm": "hmac-sha256",
        "default_zone": "example.com",
    }
    records = [
        {"name": "api.abc.example.com", "type": "A", "value": "1.2.3.4"},
    ]
    errors = delete_dns_records("nsupdate", provider_config, records)
    mock_nsupdate.assert_called_once_with(provider_config, records, errors)
    assert errors == []


def test_create_dns_records_unknown_type():
    errors = create_dns_records("cloudflare", {}, [])
    assert len(errors) == 1
    assert "Unknown" in errors[0]
