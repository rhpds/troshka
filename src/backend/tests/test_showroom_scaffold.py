import pytest

from app.services.showroom_scaffold import (
    apply_showroom_deploy_overrides,
    build_nginx_config,
    build_showroom_from_config,
    export_showroom_section,
    parse_template_tabs,
    resolve_showroom_tabs,
)


def test_resolve_showroom_tabs_terminal_and_proxy():
    vms_def = {
        "control": {"nics": [{"network": "mgmt", "ip": "10.0.0.10"}]},
        "vscode": {"nics": [{"network": "mgmt", "ip": "10.0.0.20"}]},
    }
    vm_name_to_id = {"control": "vm-control", "vscode": "vm-vscode"}
    tabs = parse_template_tabs(
        [
            {
                "name": "AAP terminal",
                "type": "terminal",
                "vm": "control",
                "network": "mgmt",
            },
            {
                "name": "VS Code",
                "type": "proxy",
                "vm": "vscode",
                "network": "mgmt",
                "proxy_path": "/vscode/",
                "proxy_port": 8080,
            },
        ],
        vm_name_to_id,
        vms_def,
        {"mgmt": "net-mgmt"},
    )
    resolved = resolve_showroom_tabs(tabs, vms_def, vm_name_to_id)
    nginx = build_nginx_config(resolved)
    assert nginx.startswith("user root;\n")
    assert "/wetty_control" in nginx
    assert "10.0.0.20:8080" in nginx
    assert "/vscode/" in nginx


def test_resolve_showroom_tabs_name_based_proxy():
    """A generic (non-console) proxy_host targets the hostname (Host + SNI) via a
    single location block. Console hosts upgrade to the app-proxy vhost instead."""
    host = "myapp.apps.ocp.ocp.local"
    vms_def = {"control": {"nics": [{"network": "cluster", "ip": "10.0.0.10"}]}}
    vm_name_to_id = {"control": "vm-control"}
    tabs = parse_template_tabs(
        [
            {
                "name": "OCP Console",
                "type": "proxy",
                "network": "cluster",
                "proxy_host": host,
                "proxy_path": "/console/",
                "proxy_port": 443,
                "proxy_tls": True,
            },
        ],
        vm_name_to_id,
        vms_def,
        {"cluster": "net-cluster"},
    )
    assert tabs[0]["proxyHost"] == host
    resolved = resolve_showroom_tabs(tabs, vms_def, vm_name_to_id)
    assert resolved[0].get("warning") is None
    assert resolved[0]["proxyTarget"] == f"https://{host}:443"
    nginx = build_nginx_config(resolved)
    assert f"proxy_pass https://{host}:443;" in nginx
    assert f"proxy_set_header Host {host};" in nginx
    assert "proxy_ssl_server_name on;" in nginx
    assert f"proxy_ssl_name {host};" in nginx
    assert "proxy_ssl_verify off;" in nginx
    # Host header must be the backend vhost, not the browser $host
    assert "location /console/ {" in nginx
    console_block = nginx.split("location /console/ {", 1)[1]
    assert "proxy_set_header Host $host;" not in console_block.split("}", 1)[0]


def test_parse_template_tabs_name_based_proxy_no_vm_or_network():
    """A name-based proxy tab needs neither vm nor network (proxy_host is enough)."""
    host = "console-openshift-console.apps.ocp.ocp.local"
    tabs = parse_template_tabs(
        [
            {
                "name": "OCP Console",
                "type": "proxy",
                "proxy_host": host,
                "proxy_path": "/console/",
                "proxy_port": 443,
                "proxy_tls": True,
            },
        ],
        {},
        {},
        {},
    )
    assert tabs[0]["proxyHost"] == host
    assert "network" not in tabs[0]
    assert "vmId" not in tabs[0]


def test_resolve_showroom_tabs_per_tab_network():
    vms_def = {
        "control": {
            "nics": [
                {"network": "mgmt", "ip": "10.0.0.10"},
                {"network": "lab", "ip": "192.168.1.10"},
            ],
        },
    }
    vm_name_to_id = {"control": "vm-control"}
    tabs = parse_template_tabs(
        [
            {
                "name": "Mgmt shell",
                "type": "terminal",
                "vm": "control",
                "network": "mgmt",
            },
            {
                "name": "Lab shell",
                "type": "terminal",
                "vm": "control",
                "network": "lab",
            },
        ],
        vm_name_to_id,
        vms_def,
        {"mgmt": "net-mgmt", "lab": "net-lab"},
    )
    resolved = resolve_showroom_tabs(tabs, vms_def, vm_name_to_id)
    assert resolved[0]["wettyHost"] == "10.0.0.10"
    assert resolved[1]["wettyHost"] == "192.168.1.10"


def test_build_ui_config_yaml_modern_showroom_format():
    from app.services.showroom_scaffold import build_ui_config_yaml

    resolved = [
        {
            "tab": {"name": "AAP terminal", "type": "terminal"},
            "wettyPath": "/wetty_control",
        },
        {
            "tab": {"name": "VS Code", "type": "proxy"},
            "proxyPath": "/vscode/",
        },
        {
            "tab": {
                "name": "Open an Issue",
                "type": "external",
                "url": "https://example.com/issues",
            },
        },
    ]
    yaml = build_ui_config_yaml(resolved, external_port=443)
    assert "type: showroom" in yaml
    assert "view_switcher:" in yaml
    assert "path: /wetty_control" in yaml
    assert "port: 443" in yaml
    assert "url: '/vscode/'" in yaml
    assert "external: true" in yaml


def test_build_showroom_from_config_creates_canvas_node():
    showroom_cfg = {
        "enabled": True,
        "content_repo": "https://example.com/repo.git",
        "content_ref": "main",
        "build_content": True,
        "disk_gb": 5,
        "tabs": [
            {
                "name": "Shell",
                "type": "terminal",
                "vm": "control",
                "network": "mgmt",
                "ssh_user": "rhel",
            },
        ],
    }
    vms_def = {"control": {"nics": [{"network": "mgmt", "ip": "10.0.0.10"}]}}
    vm_name_to_id = {"control": "vm-control"}
    net_ids = {"mgmt": "net-mgmt"}

    ctr_node, disk_nodes, disk_edges, nic_edges, meta = build_showroom_from_config(
        showroom_cfg, vm_name_to_id, vms_def, net_ids, 100, 200
    )

    assert ctr_node["data"]["isShowroom"] is True
    assert ctr_node["data"]["contentRepo"] == "https://example.com/repo.git"
    assert len(ctr_node["data"]["initContainers"]) == 3
    assert len(ctr_node["data"]["podContainers"]) == 3  # proxy, content, wetty
    assert len(disk_nodes) == 1
    assert len(disk_edges) == 1
    assert len(nic_edges) == 0
    assert ctr_node["data"]["infraNetworking"] is True
    assert "showroomNetwork" not in ctr_node["data"]
    assert ctr_node["data"]["nics"] == []
    assert "network" not in meta
    assert meta["tabs"][0]["vmId"] == "vm-control"
    assert meta["tabs"][0]["network"] == "mgmt"


def test_parse_template_tabs_ssh_port():
    tabs = parse_template_tabs(
        [
            {
                "name": "Shell",
                "type": "terminal",
                "vm": "control",
                "network": "mgmt",
                "ssh_port": 2222,
            },
        ],
        {"control": "vm-control"},
        {"control": {"nics": [{"network": "mgmt", "ip": "10.0.0.10"}]}},
        {"mgmt": "net-mgmt"},
    )
    assert tabs[0]["sshPort"] == 2222


def test_build_wetty_uses_tab_ssh_port():
    from app.services.showroom_scaffold import _build_wetty_containers

    tabs = [
        {
            "id": "tab-1",
            "name": "Shell",
            "type": "terminal",
            "vmId": "vm-control",
            "network": "mgmt",
            "sshPort": 2222,
        },
    ]
    resolved = [
        {
            "tab": tabs[0],
            "wettyPath": "/wetty_control",
            "wettyPort": 8001,
            "wettyHost": "10.0.0.10",
        },
    ]
    wetty = _build_wetty_containers(
        resolved,
        tabs,
        {"control": {"nics": [{"network": "mgmt", "ip": "10.0.0.10"}]}},
        {"control": "vm-control"},
        "disk-0",
    )
    assert wetty[0]["command"][3] == "--ssh-port=2222"


def test_apply_showroom_deploy_overrides():
    topology = {
        "nodes": [
            {
                "id": "sr-1",
                "type": "containerNode",
                "data": {
                    "name": "showroom",
                    "isShowroom": True,
                    "buildContent": False,
                    "contentRepo": "https://old.example/repo.git",
                    "contentRef": "main",
                    "initContainers": [
                        {
                            "name": "git-cloner",
                            "envVars": [
                                {
                                    "key": "GIT_REPO_URL",
                                    "value": "https://old.example/repo.git",
                                },
                                {"key": "GIT_REPO_REF", "value": "main"},
                            ],
                        },
                    ],
                },
            },
        ],
        "showroom": {
            "content_repo": "https://old.example/repo.git",
            "content_ref": "main",
            "build_content": False,
        },
    }
    apply_showroom_deploy_overrides(
        topology,
        content_ref="v1.0.0",
    )
    node = topology["nodes"][0]["data"]
    assert node["contentRef"] == "v1.0.0"
    assert node["buildContent"] is True
    assert topology["showroom"]["content_ref"] == "v1.0.0"
    assert topology["showroom"]["build_content"] is True
    git_cloner = node["initContainers"][0]
    assert git_cloner["envVars"][1]["value"] == "v1.0.0"


def test_build_app_proxy_config_per_host_literal_blocks():
    """One server block per internal host, matched by the deterministic public
    hostname (pid + suffix captured from Host). Literal upstream (no resolver),
    generic redirect rewrites host->public while leaving redirect_uri .local,
    and X-Frame-Options is stripped so the console embeds in the iframe.
    The apps domain is derived from the input hosts."""
    from app.services.showroom_scaffold import build_app_proxy_config

    conf = build_app_proxy_config(
        [
            "console-openshift-console.apps.ocp.ocp.local",
            "oauth-openshift.apps.ocp.ocp.local",
        ]
    )

    # deterministic auto-host match (tpf-<pid>-<code>-<ns>): pid + "<ns>.apps.<cluster>"
    # captured from the request Host. Short codes (con/oauth) keep the auto-generated
    # host under the 63-char DNS label limit and avoid needing routes/custom-host.
    assert (
        'server_name "~^tpf-(?<troshka_pid>[0-9a-f]{8})-con-(?<troshka_ns>.+)$";'
        in conf
    )
    assert (
        'server_name "~^tpf-(?<troshka_pid>[0-9a-f]{8})-oauth-(?<troshka_ns>.+)$";'
        in conf
    )
    # literal upstream => no resolver / map needed
    assert "proxy_pass https://console-openshift-console.apps.ocp.ocp.local;" in conf
    assert "proxy_set_header Host console-openshift-console.apps.ocp.ocp.local;" in conf
    assert "proxy_ssl_name oauth-openshift.apps.ocp.ocp.local;" in conf
    assert "resolver" not in conf
    assert "map " not in conf
    # embedding
    assert "proxy_hide_header X-Frame-Options;" in conf
    # per-host redirect: internal host -> public equivalent tpf-$pid-<code>-$ns
    assert (
        "proxy_redirect ~^https://console-openshift-console\\.apps\\.ocp\\.ocp\\.local"
        "(?<troshka_rest>.*)$ https://tpf-$troshka_pid-con-$troshka_ns$troshka_rest;"
        in conf
    )
    assert (
        "proxy_redirect ~^https://oauth-openshift\\.apps\\.ocp\\.ocp\\.local"
        "(?<troshka_rest>.*)$ https://tpf-$troshka_pid-oauth-$troshka_ns$troshka_rest;"
        in conf
    )
    assert "proxy_cookie_domain .apps.ocp.ocp.local $host;" in conf
    # body rewrite: SERVER_FLAGS .local host refs -> public. Match "//<host>"
    # (the // from https://) so the URL-encoded redirect_uri stays .local.
    assert 'proxy_set_header Accept-Encoding "";' in conf
    assert "sub_filter_once off;" in conf
    assert (
        'sub_filter "//console-openshift-console.apps.ocp.ocp.local" '
        '"//tpf-$troshka_pid-con-$troshka_ns";' in conf
    )
    assert (
        'sub_filter "//oauth-openshift.apps.ocp.ocp.local" '
        '"//tpf-$troshka_pid-oauth-$troshka_ns";' in conf
    )
    # must NOT rewrite the bare host (would corrupt the encoded redirect_uri)
    assert 'sub_filter "console-openshift-console.apps.ocp.ocp.local"' not in conf


def test_build_app_proxy_config_empty_is_blank():
    """No app proxies -> empty string (nothing to bake)."""
    from app.services.showroom_scaffold import build_app_proxy_config

    assert build_app_proxy_config([]) == ""


def test_build_app_proxy_config_deferred_resolver():
    """With resolver_ips, the upstream is resolved at request time (variable
    proxy_pass + resolver) so nginx starts even before the cluster exists —
    fixes the console-upstream CrashLoopBackOff on KubeVirt."""
    from app.services.showroom_scaffold import build_app_proxy_config

    conf = build_app_proxy_config(
        ["console-openshift-console.apps.ocp.local"],
        resolver_ips=["10.0.0.1", "10.0.0.2"],
    )
    assert "resolver 10.0.0.1 10.0.0.2 valid=10s ipv6=off;" in conf
    assert 'set $troshka_up_0 "console-openshift-console.apps.ocp.local";' in conf
    assert "proxy_pass https://$troshka_up_0;" in conf
    # NOT a literal upstream (which would force startup-time resolution).
    assert "proxy_pass https://console-openshift-console.apps.ocp.local;" not in conf


def test_build_app_proxy_config_serves_friendly_page_on_upstream_error():
    """An unready upstream (VIP still settling, console operator not up) must show
    a friendly auto-refresh page, not raw nginx 502. Service-agnostic wording so
    it fits any proxied app (console today, AAP2 etc. later)."""
    from app.services.showroom_scaffold import build_app_proxy_config

    conf = build_app_proxy_config(
        ["console-openshift-console.apps.ocp.local"],
        resolver_ips=["10.0.0.2"],
    )
    assert "error_page 502 503 504" in conf
    assert "@service_starting" in conf
    assert "location @service_starting" in conf
    # Generic, service-agnostic wording (fits console today, AAP2 etc. later).
    assert "The service is still starting" in conf
    assert "cluster is still starting" not in conf.lower()


def test_dns_network_resolver_ips():
    from app.services.showroom_scaffold import dns_network_resolver_ips

    # No explicit dnsServerIp -> both .1 (troshkad) and .2 (KubeVirt).
    assert dns_network_resolver_ips("10.0.0.0/24") == ["10.0.0.1", "10.0.0.2"]
    assert dns_network_resolver_ips("10.9.0.0/24") == ["10.9.0.1", "10.9.0.2"]
    # Explicit dnsServerIp wins.
    assert dns_network_resolver_ips("10.0.0.0/24", "10.0.0.53") == ["10.0.0.53"]
    # Unusable CIDR -> empty (build_app_proxy_config falls back to literal).
    assert dns_network_resolver_ips("") == []


def test_parse_template_tabs_proxy_hosts_list():
    """A proxy tab may declare proxy_hosts[]; [0] is the iframe target."""
    tabs = parse_template_tabs(
        [
            {
                "name": "OCP Console",
                "type": "proxy",
                "proxy_hosts": [
                    "console-openshift-console.apps.ocp.ocp.local",
                    "oauth-openshift.apps.ocp.ocp.local",
                ],
                "proxy_tls": True,
                "proxy_port": 443,
            },
        ],
        {},
        {},
        {},
    )
    assert tabs[0]["proxyHosts"] == [
        "console-openshift-console.apps.ocp.ocp.local",
        "oauth-openshift.apps.ocp.ocp.local",
    ]


def test_proxy_hosts_use_app_proxy_not_location():
    """An explicit proxy_hosts tab resolves to an app-proxy tab: no inline location
    block, and the base config includes the deploy-written conf.d snippet."""
    tabs = parse_template_tabs(
        [
            {
                "name": "OCP Console",
                "type": "proxy",
                "proxy_hosts": [
                    "console-openshift-console.apps.ocp.ocp.local",
                    "oauth-openshift.apps.ocp.ocp.local",
                ],
                "proxy_port": 443,
                "proxy_tls": True,
            },
        ],
        {},
        {},
        {},
    )
    resolved = resolve_showroom_tabs(tabs, {}, {})
    assert resolved[0]["appProxyHosts"] == [
        "console-openshift-console.apps.ocp.ocp.local",
        "oauth-openshift.apps.ocp.ocp.local",
    ]
    nginx = build_nginx_config(resolved)
    # app-proxy tabs are served by dedicated server blocks, not a path location
    assert "location /console" not in nginx
    # the app-proxy vhost is baked inline (literal upstream)
    assert "proxy_pass https://console-openshift-console.apps.ocp.ocp.local;" in nginx
    assert "proxy_pass https://oauth-openshift.apps.ocp.ocp.local;" in nginx


def test_app_proxy_internal_hosts_dedupes_and_orders():
    """Deploy needs the ordered, de-duplicated internal hosts to create routes."""
    from app.services.showroom_scaffold import app_proxy_internal_hosts

    tabs = parse_template_tabs(
        [
            {
                "name": "OCP Console",
                "type": "proxy",
                "proxy_hosts": [
                    "console-openshift-console.apps.ocp.ocp.local",
                    "oauth-openshift.apps.ocp.ocp.local",
                ],
            },
            {
                "name": "Console again",
                "type": "proxy",
                "proxy_hosts": [
                    "console-openshift-console.apps.ocp.ocp.local",
                    "argocd.apps.ocp.ocp.local",
                ],
            },
        ],
        {},
        {},
        {},
    )
    assert app_proxy_internal_hosts(tabs) == [
        "console-openshift-console.apps.ocp.ocp.local",
        "oauth-openshift.apps.ocp.ocp.local",
        "argocd.apps.ocp.ocp.local",
    ]


def test_build_ui_config_app_proxy_emits_placeholder_url():
    """App-proxy tabs render with a deploy-substituted URL placeholder for the
    iframe target (proxyHosts[0]); deploy swaps in the public host."""
    from app.services.showroom_scaffold import build_ui_config_yaml

    resolved = [
        {
            "tab": {"name": "OCP Console", "type": "proxy"},
            "appProxyHosts": [
                "console-openshift-console.apps.ocp.ocp.local",
                "oauth-openshift.apps.ocp.ocp.local",
            ],
        },
    ]
    yaml = build_ui_config_yaml(resolved, external_port=443)
    assert "name: OCP Console" in yaml
    assert "__TROSHKA_APP_PROXY__console-openshift-console.apps.ocp.ocp.local__" in yaml


def test_build_nginx_config_bakes_app_proxy_server_blocks():
    """App-proxy server blocks are baked inline in the main nginx config (no
    deploy-time injection): a dedicated server per internal host."""
    resolved = [
        {
            "tab": {"name": "OCP Console", "type": "proxy"},
            "appProxyHosts": [
                "console-openshift-console.apps.ocp.ocp.local",
                "oauth-openshift.apps.ocp.ocp.local",
            ],
        },
    ]
    nginx = build_nginx_config(resolved)
    assert (
        'server_name "~^tpf-(?<troshka_pid>[0-9a-f]{8})-con-(?<troshka_ns>.+)$";'
        in nginx
    )
    assert "proxy_pass https://oauth-openshift.apps.ocp.ocp.local;" in nginx
    # no deploy-time include needed
    assert "conf.d" not in nginx


def test_app_proxy_public_host():
    from app.services.showroom_scaffold import app_proxy_public_host

    # Auto-generated host: tpf-<pid8>-<code>-<namespace>.<apps_domain> (no custom-host)
    assert (
        app_proxy_public_host(
            "6fcf0e3e-08d8-4911",
            "console-openshift-console.apps.ocp.ocp.local",
            "apps.ocpvdev01.dal13.infra.demo.redhat.com",
            "sandbox-8zsqb-troshka",
        )
        == "tpf-6fcf0e3e-con-sandbox-8zsqb-troshka.apps.ocpvdev01.dal13.infra.demo.redhat.com"
    )


def test_fill_app_proxy_tab_urls():
    from app.services.showroom_scaffold import fill_app_proxy_tab_urls

    ui = (
        "tabs:\n"
        "  - name: OCP Console\n"
        "    url: '__TROSHKA_APP_PROXY__console-openshift-console.apps.ocp.ocp.local__'\n"
    )
    out = fill_app_proxy_tab_urls(
        ui, "6fcf0e3e", "apps.ocpvdev01.example.com", "sandbox-8zsqb-troshka"
    )
    assert (
        "url: 'https://tpf-6fcf0e3e-con-sandbox-8zsqb-troshka.apps.ocpvdev01.example.com'"
        in out
    )
    assert "__TROSHKA_APP_PROXY__" not in out


def test_derive_apps_domain():
    from app.services.showroom_scaffold import derive_apps_domain

    assert (
        derive_apps_domain(
            "troshka-pf-6fcf0e3e-showroom-443-troshka.apps.ocpvdev01.dal13.infra.demo.redhat.com"
        )
        == "apps.ocpvdev01.dal13.infra.demo.redhat.com"
    )
    assert derive_apps_domain("") == ""
    assert derive_apps_domain("nohost") == ""


def test_build_app_proxy_config_derives_apps_domain_from_host():
    """The apps domain is derived per host (not hardcoded). Hosts with different
    base domains get their own derived proxy_redirect and proxy_cookie_domain."""
    from app.services.showroom_scaffold import build_app_proxy_config

    conf = build_app_proxy_config(
        [
            "console-openshift-console.apps.mycluster.example.com",
            "oauth-openshift.apps.mycluster.example.com",
        ]
    )

    # derived apps domain: apps.mycluster.example.com; per-host redirect -> public
    assert (
        "proxy_redirect ~^https://console-openshift-console\\.apps\\.mycluster\\.example\\.com"
        "(?<troshka_rest>.*)$ https://tpf-$troshka_pid-con-$troshka_ns$troshka_rest;"
        in conf
    )
    assert "proxy_cookie_domain .apps.mycluster.example.com $host;" in conf
    # ensure no hardcoded ocp.ocp.local
    assert "ocp.ocp.local" not in conf


def test_build_app_proxy_config_skips_empty_hosts():
    """Empty proxy_hosts entries must not emit `proxy_pass https://;` (nginx rejects
    it and the showroom proxy fails to start)."""
    from app.services.showroom_scaffold import build_app_proxy_config

    conf = build_app_proxy_config(
        ["", "  ", "console-openshift-console.apps.ocp.ocp.local"]
    )
    assert "proxy_pass https://;" not in conf
    assert "proxy_pass https://console-openshift-console.apps.ocp.ocp.local;" in conf
    # only one server block (the valid host)
    assert conf.count("server {") == 1


def test_app_proxy_internal_hosts_skips_empty():
    from app.services.showroom_scaffold import app_proxy_internal_hosts

    tabs = [{"proxyHosts": ["", "console-openshift-console.apps.ocp.ocp.local", "  "]}]
    assert app_proxy_internal_hosts(tabs) == [
        "console-openshift-console.apps.ocp.ocp.local"
    ]


_OCP_CLUSTERS = [{"id": "ocp", "name": "ocp", "baseDomain": "local"}]


def test_parse_template_tabs_cluster_linked():
    """A proxy tab with `cluster: <name>` is cluster-managed: the loader stamps
    clusterId and DERIVES the tab name + console/oauth hosts from the cluster."""
    tabs = parse_template_tabs(
        [{"type": "proxy", "cluster": "ocp", "proxy_port": 443, "proxy_tls": True}],
        {},
        {},
        {},
        _OCP_CLUSTERS,
    )
    assert tabs[0]["clusterId"] == "ocp"
    assert tabs[0]["name"] == "ocp Console"
    assert tabs[0]["proxyHosts"] == [
        "console-openshift-console.apps.ocp.local",
        "oauth-openshift.apps.ocp.local",
    ]
    # No VM / network required for a cluster-linked proxy tab.
    assert "vmId" not in tabs[0]
    assert "network" not in tabs[0]


def test_parse_template_tabs_cluster_link_unknown_raises():
    """A `cluster:` reference that matches no cluster is a template error."""
    with pytest.raises(ValueError, match="unknown cluster 'nope'"):
        parse_template_tabs(
            [{"type": "proxy", "cluster": "nope"}], {}, {}, {}, _OCP_CLUSTERS
        )


def test_export_showroom_section_cluster_tab_round_trips():
    """A managed console tab exports as `cluster: <name>` (not static hosts) and
    re-imports to the same clusterId + derived hosts."""
    topology = {"clusters": _OCP_CLUSTERS}
    showroom_node = {
        "id": "sr1",
        "data": {
            "isShowroom": True,
            "contentRepo": "https://example.com/repo.git",
            "showroomTabs": [
                {
                    "id": "t1",
                    "name": "ocp Console",
                    "type": "proxy",
                    "clusterId": "ocp",
                    "proxyHosts": [
                        "console-openshift-console.apps.ocp.local",
                        "oauth-openshift.apps.ocp.local",
                    ],
                    "proxyPort": 443,
                    "proxyTls": True,
                }
            ],
        },
    }
    exported = export_showroom_section(topology, [showroom_node], {}, [], {})
    assert exported is not None
    tab = exported["tabs"][0]
    assert tab["cluster"] == "ocp"

    # Re-import the exported tab and confirm it is managed again.
    reparsed = parse_template_tabs(exported["tabs"], {}, {}, {}, _OCP_CLUSTERS)
    assert reparsed[0]["clusterId"] == "ocp"
    assert reparsed[0]["proxyHosts"] == [
        "console-openshift-console.apps.ocp.local",
        "oauth-openshift.apps.ocp.local",
    ]


# ---------------------------------------------------------------------------
# Cluster terminal: bastionless local oc shell (no VM, no bastion)
# ---------------------------------------------------------------------------


def _cluster_terminal_tabs():
    return parse_template_tabs(
        [{"type": "terminal", "target": "clusters", "name": "Cluster Terminal"}],
        {},
        {},
        {},
        [],
    )


def test_parse_cluster_terminal_needs_no_vm_or_network():
    tabs = _cluster_terminal_tabs()
    assert tabs[0]["type"] == "terminal"
    assert tabs[0]["target"] == "clusters"
    assert "vmId" not in tabs[0] and "network" not in tabs[0]


def test_resolve_cluster_terminal_is_local_shell():
    resolved = resolve_showroom_tabs(_cluster_terminal_tabs(), {}, {})
    item = resolved[0]
    assert item["ocTerminal"] is True
    assert item["wettyPath"] == "/wetty_clusters"
    assert item["wettyPort"]
    assert "wettyHost" not in item  # local shell, not SSH-to-VM


def test_build_wetty_cluster_terminal_container():
    from app.services.showroom_scaffold import (
        CLUSTER_TERMINAL_IMAGE,
        _build_wetty_containers,
    )

    resolved = resolve_showroom_tabs(_cluster_terminal_tabs(), {}, {})
    ctrs = _build_wetty_containers(resolved, [], {}, {}, "disk-0")
    c = next(c for c in ctrs if c["name"] == "wetty-clusters")
    # purpose-built glibc image with oc + cluster-shell baked in (no oc-fetch)
    assert c["image"] == CLUSTER_TERMINAL_IMAGE
    # runs the baked wrapper via wetty --command, NOT --ssh-host
    assert "--command" in c["command"]
    assert "/usr/local/bin/cluster-shell" in c["command"]
    assert not any(a.startswith("--ssh-host") for a in c["command"])
    # still mounts the shared disk for the injected kubeconfig
    assert c["mounts"] == [{"diskNodeId": "disk-0", "mountPath": "/showroom"}]
    # wetty must run as ROOT to spawn the --command shell, but the interactive
    # shell should drop to labuser. cluster-shell does that via setpriv, which
    # needs SETUID/SETGID — force-dropped under KubeVirt's restricted SCC. Grant
    # them here (no runAsUser override → stays root).
    assert c["securityContext"] == {"capabilities": {"add": ["SETUID", "SETGID"]}}
    assert "runAsUser" not in c["securityContext"]


def test_cluster_terminal_uses_baked_image_no_oc_fetch():
    from app.services.showroom_scaffold import CLUSTER_TERMINAL_IMAGE

    cfg = {
        "enabled": True,
        "content_repo": "https://example.com/repo.git",
        "tabs": [
            {"type": "terminal", "target": "clusters", "name": "Cluster Terminal"}
        ],
    }
    ctr_node, _disks, _de, _ne, _meta = build_showroom_from_config(
        cfg, {}, {}, {}, 0, 0, clusters=[{"id": "ocp", "name": "ocp"}]
    )
    data = ctr_node["data"]
    init_names = [c["name"] for c in data["initContainers"]]
    pod_names = [c["name"] for c in data["podContainers"]]
    # oc is baked into the terminal image now — no oc-fetch init container
    assert "oc-fetch" not in init_names
    assert "wetty-clusters" in pod_names
    wetty = next(c for c in data["podContainers"] if c["name"] == "wetty-clusters")
    assert wetty["image"] == CLUSTER_TERMINAL_IMAGE
    # nginx proxies the terminal path
    resolved = resolve_showroom_tabs(_cluster_terminal_tabs(), {}, {})
    nginx = build_nginx_config(resolved)
    assert "/wetty_clusters" in nginx


def test_no_oc_fetch_without_cluster_terminal():
    cfg = {"enabled": True, "content_repo": "https://example.com/repo.git", "tabs": []}
    ctr_node, *_ = build_showroom_from_config(cfg, {}, {}, {}, 0, 0)
    init_names = [c["name"] for c in ctr_node["data"]["initContainers"]]
    assert "oc-fetch" not in init_names


# ---------------------------------------------------------------------------
# regenerate_showroom_containers: rebuild an existing node's spec from its tabs
# ---------------------------------------------------------------------------


def _showroom_node_with_cluster_terminal():
    """A showroom node whose podContainers are MISSING the wetty-clusters
    container (as the frontend materialization leaves it) but whose showroomTabs
    include a cluster terminal + app-proxy console."""
    tabs = parse_template_tabs(
        [
            {"type": "terminal", "target": "clusters", "name": "Cluster Terminal"},
        ],
        {},
        {},
        {},
        [],
    )
    return {
        "id": "ctr-showroom",
        "type": "containerNode",
        "data": {
            "name": "showroom",
            "isShowroom": True,
            "contentRepo": "https://example.com/repo.git",
            "contentRef": "main",
            "showroomTabs": tabs,
            "mounts": [{"diskNodeId": "disk-abc", "mountPath": "/showroom"}],
            # deliberately incomplete: only proxy + content, no wetty-clusters
            "podContainers": [{"name": "proxy"}, {"name": "content"}],
            "initContainers": [{"name": "git-cloner"}],
        },
    }


def test_regenerate_showroom_containers_adds_wetty():
    from app.services.showroom_scaffold import (
        CLUSTER_TERMINAL_IMAGE,
        regenerate_showroom_containers,
    )

    node = _showroom_node_with_cluster_terminal()
    regenerate_showroom_containers(node, {}, {})
    data = node["data"]

    pod_names = [c["name"] for c in data["podContainers"]]
    assert "wetty-clusters" in pod_names  # was missing before
    assert "proxy" in pod_names and "content" in pod_names

    # oc is baked into the terminal image — no oc-fetch init container
    init_names = [c["name"] for c in data["initContainers"]]
    assert "oc-fetch" not in init_names

    wetty = next(c for c in data["podContainers"] if c["name"] == "wetty-clusters")
    assert wetty["image"] == CLUSTER_TERMINAL_IMAGE
    assert "/usr/local/bin/cluster-shell" in wetty["command"]


def test_regenerate_showroom_containers_preserves_disk_id():
    from app.services.showroom_scaffold import regenerate_showroom_containers

    node = _showroom_node_with_cluster_terminal()
    regenerate_showroom_containers(node, {}, {})
    for c in node["data"]["initContainers"] + node["data"]["podContainers"]:
        for m in c.get("mounts", []):
            assert m["diskNodeId"] == "disk-abc"


def test_regenerate_showroom_containers_noop_without_disk():
    from app.services.showroom_scaffold import regenerate_showroom_containers

    node = _showroom_node_with_cluster_terminal()
    node["data"]["mounts"] = []  # no /showroom disk -> cannot regenerate
    before = node["data"]["podContainers"]
    regenerate_showroom_containers(node, {}, {})
    assert node["data"]["podContainers"] is before  # unchanged


def test_app_proxy_route_code_and_name():
    from app.services.showroom_scaffold import (
        app_proxy_route_code,
        app_proxy_route_name,
    )

    # Known console/oauth hosts get short readable codes.
    assert app_proxy_route_code("console-openshift-console.apps.ocp.local") == "con"
    assert app_proxy_route_code("oauth-openshift.apps.ocp.local") == "oauth"
    assert (
        app_proxy_route_name(
            "d0cc03f4-1111", "console-openshift-console.apps.ocp.local"
        )
        == "tpf-d0cc03f4-con"
    )
    # Unknown host -> slug + hash (stable, DNS-safe, short).
    code = app_proxy_route_code("grafana.apps.ocp.local")
    assert code.startswith("grafana-") and len(code) <= 15
    assert app_proxy_route_code("grafana.apps.ocp.local") == code  # deterministic


def test_ns_from_showroom_hostname():
    from app.services.deploy_service import _ns_from_showroom_hostname

    # ocpvirt: troshka-pf-<pid>-showroom-443-<ns>.apps.<cluster>
    assert (
        _ns_from_showroom_hostname(
            "troshka-pf-d0cc03f4-showroom-443-sandbox-8zsqb-troshka.apps.ocpv06.dal10.infra.demo.redhat.com"
        )
        == "sandbox-8zsqb-troshka"
    )
    # kubevirt: rt-showroom-443-<ns>.apps.<cluster>
    assert (
        _ns_from_showroom_hostname(
            "rt-showroom-443-troshka-275876b6.apps.ocpv09.dal13.infra.demo.redhat.com"
        )
        == "troshka-275876b6"
    )
    assert _ns_from_showroom_hostname("") == ""
    assert _ns_from_showroom_hostname("no-port-here.apps.x") == ""


def test_nginx_config_init_writes_ui_config_to_served_www():
    """The nginx-config init must write ui-config.yml to BOTH /showroom/repo AND
    the served /showroom/www — pattern deploys skip the antora build (which would
    otherwise produce /showroom/www), so the pid-specific ui-config only reaches
    the browser if this init writes www directly."""
    from app.services.showroom_scaffold import _build_init_containers

    inits = _build_init_containers("repo", "ref", "bm5n", "dWk=", "disk-1")
    nginx_cfg = next(ic for ic in inits if ic["name"] == "nginx-config")
    cmd = nginx_cfg["command"]
    assert "/showroom/www/ui-config.yml" in cmd
    assert "/showroom/repo/ui-config.yml" in cmd
    assert "/showroom/nginx/nginx.conf" in cmd


def test_nginx_config_init_makes_www_writable_for_antora():
    """nginx-config runs as root (busybox) and pre-creates /showroom/www, but the
    antora-builder that fills it runs as a non-root user (antora image USER 1001)
    under podman on troshkad hosts. Without making www writable, antora gets
    EACCES on mkdir /showroom/www/modules. The init must chmod www world-writable
    so the non-root antora build can create its output tree (regression: 2f12cd20
    added the root-owned www without this)."""
    from app.services.showroom_scaffold import _build_init_containers

    inits = _build_init_containers("repo", "ref", "bm5n", "dWk=", "disk-1")
    nginx_cfg = next(ic for ic in inits if ic["name"] == "nginx-config")
    cmd = nginx_cfg["command"]
    assert "chmod 0777 /showroom/www" in cmd
    # The chmod must precede the antora build's use of www — i.e. appear in the
    # same nginx-config command that creates www.
    assert cmd.index("mkdir") < cmd.index("chmod 0777 /showroom/www")
