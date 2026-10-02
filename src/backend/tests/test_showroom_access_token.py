"""Showroom static capability-token helpers and nginx gate."""

from app.services.showroom_scaffold import (
    build_app_proxy_config,
    build_nginx_config,
    ensure_showroom_access_token,
    fill_app_proxy_tab_urls,
    fill_app_proxy_tab_urls_eip,
    mint_showroom_access_token,
    showroom_url_with_token,
)


def test_mint_showroom_access_token_is_urlsafe():
    token = mint_showroom_access_token()
    assert len(token) >= 32
    assert "/" not in token
    assert "+" not in token


def test_ensure_showroom_access_token_reuses_existing():
    store = {"_showroom_access_token": "existing-token-value"}
    assert ensure_showroom_access_token(store) == "existing-token-value"
    assert store["_showroom_access_token"] == "existing-token-value"


def test_ensure_showroom_access_token_mints_when_missing():
    store: dict = {}
    token = ensure_showroom_access_token(store)
    assert token
    assert store["_showroom_access_token"] == token


def test_showroom_url_with_token_appends_query():
    assert (
        showroom_url_with_token("https://showroom.example.com", "abc")
        == "https://showroom.example.com/?token=abc"
    )
    assert (
        showroom_url_with_token("https://showroom.example.com/", "abc")
        == "https://showroom.example.com/?token=abc"
    )
    assert (
        showroom_url_with_token("https://showroom.example.com/?x=1", "abc")
        == "https://showroom.example.com/?x=1&token=abc"
    )


def test_build_nginx_config_without_token_has_no_gate():
    nginx = build_nginx_config([])
    assert "troshka_sr" not in nginx
    assert "return 401" not in nginx


def test_build_nginx_config_gates_main_server_with_token():
    nginx = build_nginx_config([], access_token="tok123")
    assert "map $arg_token $troshka_sr_arg_ok" in nginx
    assert '    "tok123" 1;' in nginx
    assert "map $cookie_troshka_sr $troshka_sr_cookie_ok" in nginx
    assert "return 401" in nginx
    assert "troshka_sr=tok123" in nginx
    assert "SameSite=None" in nginx


def test_build_nginx_config_gates_console_but_not_oauth():
    resolved = [
        {
            "tab": {"name": "OCP Console", "type": "proxy"},
            "appProxyHosts": [
                "console-openshift-console.apps.ocp.ocp.local",
                "oauth-openshift.apps.ocp.ocp.local",
            ],
        },
    ]
    nginx = build_nginx_config(resolved, access_token="tok123")
    # Split server blocks: console gated, oauth not
    con_idx = nginx.index("tpf-(?<troshka_pid>[0-9a-f]{8})-con-ocp-")
    oauth_idx = nginx.index("tpf-(?<troshka_pid>[0-9a-f]{8})-oauth-")
    # Find return 401 relative to each server — oauth block must not contain gate
    oauth_block = nginx[oauth_idx : oauth_idx + 800]
    assert "return 401" not in oauth_block
    # console / main still gated
    assert nginx.count("return 401") >= 1
    assert "troshka_sr=tok123" in nginx[con_idx : con_idx + 900]


def test_build_app_proxy_config_exempts_oauth_host():
    conf = build_app_proxy_config(
        [
            "console-openshift-console.apps.ocp.ocp.local",
            "oauth-openshift.apps.ocp.ocp.local",
        ],
        access_token="tok123",
        oauth_exempt_hosts={"oauth-openshift.apps.ocp.ocp.local"},
    )
    assert conf.count("server {") == 2
    assert conf.count("return 401") == 1  # console only
    # oauth server block: after its server_name, no return 401 before next server
    oauth_idx = conf.index("-oauth-")
    next_server = conf.find("\nserver {", oauth_idx)
    oauth_block = conf[oauth_idx : next_server if next_server > 0 else len(conf)]
    assert "return 401" not in oauth_block


def test_fill_app_proxy_tab_urls_appends_token():
    ui = (
        "tabs:\n"
        "  - name: OCP Console\n"
        "    url: '__TROSHKA_APP_PROXY__console-openshift-console.apps.ocp.ocp.local__'\n"
    )
    out = fill_app_proxy_tab_urls(
        ui,
        "6fcf0e3e",
        "apps.ocpvdev01.example.com",
        "sandbox-8zsqb-troshka",
        access_token="tok123",
    )
    assert (
        "url: 'https://tpf-6fcf0e3e-con-ocp-sandbox-8zsqb-troshka.apps.ocpvdev01.example.com/?token=tok123'"
        in out
    )


def test_fill_app_proxy_tab_urls_eip_appends_token():
    ui = (
        "tabs:\n"
        "  - name: ocp Console\n"
        "    url: '__TROSHKA_APP_PROXY__console-openshift-console.apps.ocp.local__'\n"
    )
    out = fill_app_proxy_tab_urls_eip(
        ui, "e0b9ad55-ae28-477c", "184.194.236.17", access_token="tok123"
    )
    assert (
        "url: 'https://tpf-e0b9ad55-con-ocp-lab.184.194.236.17.sslip.io/?token=tok123'"
        in out
    )
