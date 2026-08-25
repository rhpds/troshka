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
