from app.services.showroom_scaffold import (
    apply_showroom_deploy_overrides,
    build_nginx_config,
    build_showroom_from_config,
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


def test_build_app_proxy_config_maps_and_rewrites():
    """App-proxy vhost maps public->internal, preserves Host/SNI, rewrites redirects,
    and strips X-Frame-Options so the console embeds in the iframe."""
    from app.services.showroom_scaffold import build_app_proxy_config

    mappings = [
        {
            "internal_host": "console-openshift-console.apps.ocp.ocp.local",
            "public_host": "troshka-pf-6fcf0e3e-console-443-tr.apps.ocpvdev01.example.com",
        },
        {
            "internal_host": "oauth-openshift.apps.ocp.ocp.local",
            "public_host": "troshka-pf-6fcf0e3e-oauth-443-tr.apps.ocpvdev01.example.com",
        },
    ]
    conf = build_app_proxy_config(mappings)

    # resolver is required for a variable ($troshka_backend) upstream
    assert "resolver" in conf
    # forward map: public host -> internal host
    assert "map $host $troshka_backend {" in conf
    assert (
        "troshka-pf-6fcf0e3e-console-443-tr.apps.ocpvdev01.example.com"
        "   console-openshift-console.apps.ocp.ocp.local" in conf
        or "console-openshift-console.apps.ocp.ocp.local" in conf
    )
    # single server block handles all public hosts
    assert "server_name" in conf
    assert "oauth-openshift.apps.ocp.ocp.local" in conf
    # proxied with backend Host + SNI
    assert "proxy_pass https://$troshka_backend" in conf
    assert "proxy_ssl_name $troshka_backend;" in conf
    assert "proxy_set_header Host $troshka_backend;" in conf
    # Defect C: allow embedding in the showroom iframe
    assert "proxy_hide_header X-Frame-Options;" in conf
    # redirect host .local -> public, per app (redirect_uri query param untouched)
    assert (
        "proxy_redirect https://console-openshift-console.apps.ocp.ocp.local/ "
        "https://troshka-pf-6fcf0e3e-console-443-tr.apps.ocpvdev01.example.com/;"
    ) in conf
    assert (
        "proxy_redirect https://oauth-openshift.apps.ocp.ocp.local/ "
        "https://troshka-pf-6fcf0e3e-oauth-443-tr.apps.ocpvdev01.example.com/;"
    ) in conf
    # cookie domain rewrite so the session cookie applies on the public host
    assert "proxy_cookie_domain" in conf


def test_build_app_proxy_config_empty_is_blank():
    """No app proxies -> empty snippet (safe to include before deploy fills it)."""
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
    # app-proxy tabs are served by the deploy-time vhost, not an inline location
    assert "location /console" not in nginx
    assert "console-openshift-console" not in nginx
    # base config loads whatever the deploy writes into conf.d
    assert "include /showroom/nginx/conf.d/*.conf;" in nginx


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


def test_build_nginx_config_sets_hash_sizes_before_first_map():
    """Hash bucket sizes must precede the first map ($http_upgrade); nginx commits
    map_hash_bucket_size when it parses the first map, so a later one errors."""
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
    assert "map_hash_bucket_size" in nginx
    assert "server_names_hash_bucket_size" in nginx
    assert nginx.index("map_hash_bucket_size") < nginx.index("map $http_upgrade")
