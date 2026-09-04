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

    # deterministic public hostname match; pid + suffix captured from the request
    assert (
        'server_name "~^troshka-pf-(?<troshka_pid>[0-9a-f]{8})-console-openshift-console'
        '\\.(?<troshka_suffix>apps\\..+)$";' in conf
    )
    assert (
        'server_name "~^troshka-pf-(?<troshka_pid>[0-9a-f]{8})-oauth-openshift'
        '\\.(?<troshka_suffix>apps\\..+)$";' in conf
    )
    # literal upstream => no resolver / map needed
    assert "proxy_pass https://console-openshift-console.apps.ocp.ocp.local;" in conf
    assert "proxy_set_header Host console-openshift-console.apps.ocp.ocp.local;" in conf
    assert "proxy_ssl_name oauth-openshift.apps.ocp.ocp.local;" in conf
    assert "resolver" not in conf
    assert "map " not in conf
    # embedding
    assert "proxy_hide_header X-Frame-Options;" in conf
    # generic redirect: derived apps domain (ocp.ocp.local in this case) -> troshka-pf-$troshka_pid-<label>.$suffix
    assert (
        "proxy_redirect ~^https://(?<troshka_h>[^.]+)\\.apps\\.ocp\\.ocp\\.local"
        "(?<troshka_rest>.*)$ https://troshka-pf-$troshka_pid-$troshka_h.$troshka_suffix"
        "$troshka_rest;" in conf
    )
    assert "proxy_cookie_domain .apps.ocp.ocp.local $host;" in conf
    # body rewrite: SERVER_FLAGS .local host refs -> public. Match "//<host>"
    # (the // from https://) so the URL-encoded redirect_uri stays .local.
    assert 'proxy_set_header Accept-Encoding "";' in conf
    assert "sub_filter_once off;" in conf
    assert (
        'sub_filter "//console-openshift-console.apps.ocp.ocp.local" '
        '"//troshka-pf-$troshka_pid-console-openshift-console.$troshka_suffix";' in conf
    )
    assert (
        'sub_filter "//oauth-openshift.apps.ocp.ocp.local" '
        '"//troshka-pf-$troshka_pid-oauth-openshift.$troshka_suffix";' in conf
    )
    # must NOT rewrite the bare host (would corrupt the encoded redirect_uri)
    assert 'sub_filter "console-openshift-console.apps.ocp.ocp.local"' not in conf


def test_build_app_proxy_config_empty_is_blank():
    """No app proxies -> empty string (nothing to bake)."""
    from app.services.showroom_scaffold import build_app_proxy_config

    assert build_app_proxy_config([]) == ""


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
        'server_name "~^troshka-pf-(?<troshka_pid>[0-9a-f]{8})-console-openshift-console'
        '\\.(?<troshka_suffix>apps\\..+)$";' in nginx
    )
    assert "proxy_pass https://oauth-openshift.apps.ocp.ocp.local;" in nginx
    # no deploy-time include needed
    assert "conf.d" not in nginx


def test_app_proxy_public_host():
    from app.services.showroom_scaffold import app_proxy_public_host

    assert (
        app_proxy_public_host(
            "6fcf0e3e-08d8-4911",
            "console-openshift-console.apps.ocp.ocp.local",
            "apps.ocpvdev01.dal13.infra.demo.redhat.com",
        )
        == "troshka-pf-6fcf0e3e-console-openshift-console.apps.ocpvdev01.dal13.infra.demo.redhat.com"
    )


def test_fill_app_proxy_tab_urls():
    from app.services.showroom_scaffold import fill_app_proxy_tab_urls

    ui = (
        "tabs:\n"
        "  - name: OCP Console\n"
        "    url: '__TROSHKA_APP_PROXY__console-openshift-console.apps.ocp.ocp.local__'\n"
    )
    out = fill_app_proxy_tab_urls(ui, "6fcf0e3e", "apps.ocpvdev01.example.com")
    assert (
        "url: 'https://troshka-pf-6fcf0e3e-console-openshift-console.apps.ocpvdev01.example.com'"
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

    # derived apps domain: apps.mycluster.example.com
    assert (
        "proxy_redirect ~^https://(?<troshka_h>[^.]+)\\.apps\\.mycluster\\.example\\.com"
        "(?<troshka_rest>.*)$ https://troshka-pf-$troshka_pid-$troshka_h.$troshka_suffix"
        "$troshka_rest;" in conf
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
    from app.services.showroom_scaffold import _build_wetty_containers

    resolved = resolve_showroom_tabs(_cluster_terminal_tabs(), {}, {})
    ctrs = _build_wetty_containers(resolved, [], {}, {}, "disk-0")
    c = next(c for c in ctrs if c["name"] == "wetty-clusters")
    # runs the privilege-dropping wrapper via wetty --command, NOT --ssh-host
    assert "--command" in c["command"]
    assert "/showroom/bin/cluster-shell" in c["command"]
    assert not any(a.startswith("--ssh-host") for a in c["command"])
    assert c["mounts"] == [{"diskNodeId": "disk-0", "mountPath": "/showroom"}]


def test_cluster_terminal_adds_oc_fetch_init_and_proxied_path():
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
    assert "oc-fetch" in init_names
    assert "wetty-clusters" in pod_names
    # oc-fetch fetches oc + writes the wrapper onto the shared disk
    oc_fetch = next(c for c in data["initContainers"] if c["name"] == "oc-fetch")
    assert "wget" in oc_fetch["command"] and "/showroom/bin/oc" in oc_fetch["command"]
    # supply-chain guard: verify the download's sha256 is in the published sums
    assert "sha256sum" in oc_fetch["command"]
    assert "sha256sum.txt" in oc_fetch["command"]
    assert "exit 1" in oc_fetch["command"]  # abort on checksum mismatch
    # mounts the shared showroom disk (same disk the container mounts)
    assert oc_fetch["mounts"][0]["mountPath"] == "/showroom"
    assert oc_fetch["mounts"][0]["diskNodeId"] == data["mounts"][0]["diskNodeId"]
    # nginx proxies the terminal path
    resolved = resolve_showroom_tabs(_cluster_terminal_tabs(), {}, {})
    nginx = build_nginx_config(resolved)
    assert "/wetty_clusters" in nginx


def test_no_oc_fetch_without_cluster_terminal():
    cfg = {"enabled": True, "content_repo": "https://example.com/repo.git", "tabs": []}
    ctr_node, *_ = build_showroom_from_config(cfg, {}, {}, {}, 0, 0)
    init_names = [c["name"] for c in ctr_node["data"]["initContainers"]]
    assert "oc-fetch" not in init_names
