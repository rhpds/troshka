from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app.services.deploy_service as ds
from app.services.deploy_service import _showroom_fqdn


def _proj(dns=True):
    return SimpleNamespace(
        id="abcdef12-0000-0000",
        dns_provider_id=("d" if dns else None),
        guid="g",
        domain="example.com",
    )


def test_fqdn_when_dns_configured():
    p = SimpleNamespace(dns_provider_id="d", guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == "showroom.abc.example.com"


def test_fqdn_empty_when_no_dns():
    p = SimpleNamespace(dns_provider_id=None, guid="abc", domain="example.com")
    assert _showroom_fqdn(p) == ""


def test_ensure_tls_letsencrypt_path():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), patch.object(
        ds,
        "wait_for_job",
        side_effect=[
            {
                "status": "completed",
                "result": {"cert_path": "/f", "key_path": "/k", "mode": "letsencrypt"},
            },
            {"status": "completed", "result": {"pid": 1}},
        ],
    ), patch.object(ds, "create_dns_records", return_value=[]) as mk_dns, patch.object(
        ds,
        "_resolve_showroom_dns_provider",
        return_value=("route53", {"access_key_id": "AK"}),
    ):
        url = ds._ensure_showroom_tls(
            MagicMock(), host, _proj(), {"nodes": []}, "1.2.3.4", 5, "troshka-abcdef12"
        )
    assert url == "https://showroom.g.example.com"
    mk_dns.assert_called_once()


def test_ensure_tls_self_signed_when_no_dns():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), patch.object(
        ds,
        "wait_for_job",
        side_effect=[
            {
                "status": "completed",
                "result": {"cert_path": "/f", "key_path": "/k", "mode": "self-signed"},
            },
            {"status": "completed", "result": {"pid": 1}},
        ],
    ), patch.object(ds, "create_dns_records") as mk_dns:
        url = ds._ensure_showroom_tls(
            MagicMock(),
            host,
            _proj(dns=False),
            {"nodes": []},
            "1.2.3.4",
            5,
            "troshka-abcdef12",
        )
    assert url == "https://1.2.3.4"
    mk_dns.assert_not_called()


def test_teardown_stops_proxy_and_deletes_dns():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), patch.object(
        ds,
        "wait_for_job",
        return_value={"status": "completed", "result": {"stopped": True}},
    ), patch.object(ds, "delete_dns_records", return_value=[]) as mk_del, patch.object(
        ds, "_resolve_showroom_dns_provider", return_value=("route53", {"x": 1})
    ):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(), "1.2.3.4")
    mk_del.assert_called_once()


def test_teardown_skips_dns_when_none():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), patch.object(
        ds, "wait_for_job", return_value={"status": "completed", "result": {}}
    ), patch.object(ds, "delete_dns_records") as mk_del, patch.object(
        ds, "_resolve_showroom_dns_provider", return_value=(None, {})
    ):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(dns=False), "1.2.3.4")
    mk_del.assert_not_called()
