from app.services.showroom_scaffold import (
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
            {"name": "AAP terminal", "type": "terminal", "vm": "control"},
            {
                "name": "VS Code",
                "type": "proxy",
                "vm": "vscode",
                "proxy_path": "/vscode/",
                "proxy_port": 8080,
            },
        ],
        vm_name_to_id,
        vms_def,
    )
    resolved = resolve_showroom_tabs(tabs, "mgmt", vms_def, vm_name_to_id)
    nginx = build_nginx_config(resolved)
    assert "/wetty_control" in nginx
    assert "10.0.0.20:8080" in nginx
    assert "/vscode/" in nginx


def test_build_showroom_from_config_creates_canvas_node():
    showroom_cfg = {
        "enabled": True,
        "content_repo": "https://example.com/repo.git",
        "content_ref": "main",
        "build_content": True,
        "network": "mgmt",
        "ip": "10.0.0.5",
        "disk_gb": 5,
        "tabs": [
            {"name": "Shell", "type": "terminal", "vm": "control", "ssh_user": "rhel"},
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
    assert len(nic_edges) == 1
    assert meta["tabs"][0]["vmId"] == "vm-control"
