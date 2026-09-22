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
    ), patch(
        "app.services.dns_service.create_dns_records", return_value=[]
    ) as mk_dns, patch.object(
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
    ), patch("app.services.dns_service.create_dns_records") as mk_dns:
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
    ), patch(
        "app.services.dns_service.delete_dns_records", return_value=[]
    ) as mk_del, patch.object(
        ds, "_resolve_showroom_dns_provider", return_value=("route53", {"x": 1})
    ):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(), "1.2.3.4")
    mk_del.assert_called_once()


def test_teardown_skips_dns_when_none():
    host = MagicMock()
    with patch.object(ds, "start_job", return_value="j"), patch.object(
        ds, "wait_for_job", return_value={"status": "completed", "result": {}}
    ), patch("app.services.dns_service.delete_dns_records") as mk_del, patch.object(
        ds, "_resolve_showroom_dns_provider", return_value=(None, {})
    ):
        ds._teardown_showroom_tls(MagicMock(), host, _proj(dns=False), "1.2.3.4")
    mk_del.assert_not_called()


def test_maybe_setup_skips_route_providers():
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="kubevirt")
    sess = MagicMock()
    sess.get.return_value = prov
    with patch.object(ds, "_ensure_showroom_tls") as mk:
        ds._maybe_setup_showroom_tls(
            sess, host, {"nodes": []}, _proj(), [{"ip": "1.2.3.4"}], {"net": 5}
        )
    mk.assert_not_called()


def test_maybe_setup_runs_for_cloud_with_showroom():
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="ec2")
    proj = _proj()
    proj.deployed_topology = None
    sess = MagicMock()
    sess.get.return_value = prov
    topo = {"nodes": [{"type": "containerNode", "data": {"name": "showroom"}}]}
    with patch(
        "app.services.vxlan._topology_has_showroom", return_value=True
    ), patch.object(ds, "_ensure_showroom_tls", return_value="https://x") as mk:
        ds._maybe_setup_showroom_tls(
            sess, host, topo, proj, [{"ip": "1.2.3.4"}], {"net": 5}
        )
    mk.assert_called_once()
    assert proj.deployed_topology["_showroom_url"] == "https://x"


def test_maybe_setup_does_not_leak_url_into_editable_topology():
    """deployed_topology may be the SAME dict object as topology (aliased in
    _deploy_complete_and_notify). Writing _showroom_url must NOT mutate the
    editable topology, or the canvas reads dirty."""
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="ec2")
    proj = _proj()
    shared = {"nodes": [{"type": "containerNode", "data": {"name": "showroom"}}]}
    proj.topology = shared
    proj.deployed_topology = shared  # aliased to topology
    sess = MagicMock()
    sess.get.return_value = prov
    with patch(
        "app.services.vxlan._topology_has_showroom", return_value=True
    ), patch.object(ds, "_ensure_showroom_tls", return_value="https://x"):
        ds._maybe_setup_showroom_tls(
            sess, host, shared, proj, [{"ip": "1.2.3.4"}], {"net": 5}
        )
    assert proj.deployed_topology["_showroom_url"] == "https://x"
    # The editable topology must NOT have gained the URL.
    assert "_showroom_url" not in proj.topology


def test_maybe_setup_is_non_fatal_on_error():
    """A failure anywhere in the body must never raise — it runs before the
    client notification on the deploy-completion path."""
    host = SimpleNamespace(provider_id="p")
    prov = SimpleNamespace(type="ec2")
    proj = _proj()
    proj.deployed_topology = None
    sess = MagicMock()
    sess.get.return_value = prov
    with patch(
        "app.services.vxlan._topology_has_showroom", return_value=True
    ), patch.object(ds, "_ensure_showroom_tls", side_effect=RuntimeError("boom")):
        # Must not raise.
        ds._maybe_setup_showroom_tls(
            sess,
            host,
            {"nodes": []},
            proj,
            [{"ip": "1.2.3.4"}],
            {"net": 5},
        )


def test_teardown_guards_none_project():
    """A None project must return early, never raising in _showroom_fqdn(None)."""
    host = MagicMock()
    with patch.object(ds, "start_job") as mk_start:
        ds._teardown_showroom_tls(MagicMock(), host, None, "1.2.3.4")
    mk_start.assert_not_called()
