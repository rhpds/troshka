"""Unit tests for showroom_scaffold helpers extracted for Sonar S3776."""

import pytest

from app.services.showroom_scaffold import (
    _collect_app_proxy_hosts,
    _copy_exported_optional_fields,
    _copy_optional_tab_fields,
    _export_showroom_tab,
    _nginx_proxy_location,
    _nginx_tab_locations,
    _nginx_wetty_location,
    _parse_one_template_tab,
    _patch_git_cloner_env,
    _resolve_exported_dns_network,
    _resolve_one_showroom_tab,
    _resolve_vm_proxy_tab,
    _set_showroom_field,
    _write_showroom_content_fields,
    apply_showroom_deploy_overrides,
    build_nginx_config,
    export_showroom_section,
    parse_template_tabs,
    resolve_showroom_tabs,
)


class TestSetShowroomField:
    def test_writes_camel_and_snake(self):
        data: dict = {}
        meta: dict = {}
        _set_showroom_field(
            data, meta, camel="contentRepo", snake="content_repo", value="r"
        )
        assert data["contentRepo"] == "r"
        assert meta["content_repo"] == "r"

    def test_skips_none_targets(self):
        meta: dict = {}
        _set_showroom_field(
            None, meta, camel="contentRef", snake="content_ref", value="main"
        )
        assert meta["content_ref"] == "main"


class TestPatchGitClonerEnv:
    def test_updates_repo_and_ref(self):
        data = {
            "initContainers": [
                {
                    "name": "git-cloner",
                    "envVars": [
                        {"key": "GIT_REPO_URL", "value": "old"},
                        {"key": "GIT_REPO_REF", "value": "old-ref"},
                        {"key": "OTHER", "value": "x"},
                    ],
                },
                {"name": "other", "envVars": [{"key": "GIT_REPO_URL", "value": "no"}]},
            ]
        }
        _patch_git_cloner_env(data, "new-repo", "new-ref")
        evs = data["initContainers"][0]["envVars"]
        assert evs[0]["value"] == "new-repo"
        assert evs[1]["value"] == "new-ref"
        assert data["initContainers"][1]["envVars"][0]["value"] == "no"


class TestWriteShowroomContentFields:
    def test_writes_all_three(self):
        data: dict = {}
        meta: dict = {}
        _write_showroom_content_fields(data, meta, "repo", "ref", True)
        assert data["contentRepo"] == "repo"
        assert data["contentRef"] == "ref"
        assert data["buildContent"] is True
        assert meta["content_repo"] == "repo"
        assert meta["content_ref"] == "ref"
        assert meta["build_content"] is True

    def test_skips_none_values(self):
        data: dict = {"contentRepo": "keep"}
        meta: dict = {}
        _write_showroom_content_fields(data, meta, None, "only-ref", None)
        assert data["contentRepo"] == "keep"
        assert data["contentRef"] == "only-ref"
        assert "buildContent" not in data


class TestApplyShowroomDeployOverrides:
    def test_meta_only(self):
        topo = {"showroom": {"content_repo": "old"}, "nodes": []}
        apply_showroom_deploy_overrides(
            topo, content_repo="https://example.com/r.git", content_ref="dev"
        )
        assert topo["showroom"]["content_repo"] == "https://example.com/r.git"
        assert topo["showroom"]["content_ref"] == "dev"
        assert topo["showroom"]["build_content"] is True

    def test_noop_when_all_none(self):
        topo = {"showroom": {"content_repo": "keep"}, "nodes": []}
        apply_showroom_deploy_overrides(topo)
        assert topo["showroom"]["content_repo"] == "keep"


class TestParseOneTemplateTab:
    def test_terminal_with_vm(self):
        tab = _parse_one_template_tab(
            {
                "name": "t1",
                "type": "terminal",
                "vm": "bastion",
                "network": "mgmt",
                "ssh_user": "lab",
            },
            {"bastion": "vm-1"},
            {"mgmt": "net-1"},
            [],
        )
        assert tab["vmId"] == "vm-1"
        assert tab["networkId"] == "net-1"
        assert tab["sshUser"] == "lab"

    def test_unknown_vm_raises(self):
        with pytest.raises(ValueError, match="unknown VM"):
            _parse_one_template_tab(
                {"name": "t", "type": "terminal", "vm": "missing", "network": "mgmt"},
                {},
                {"mgmt": "n1"},
                [],
            )

    def test_requires_network(self):
        with pytest.raises(ValueError, match="requires network"):
            _parse_one_template_tab(
                {"name": "t", "type": "terminal"},
                {},
                {},
                [],
            )

    def test_cluster_terminal_skips_network(self):
        tab = _parse_one_template_tab(
            {"name": "oc", "type": "terminal", "target": "clusters"},
            {},
            {},
            [],
        )
        assert tab["target"] == "clusters"
        assert "network" not in tab


class TestCopyOptionalTabFields:
    def test_proxy_fields(self):
        tab: dict = {}
        _copy_optional_tab_fields(
            tab,
            {
                "proxy_path": "/x/",
                "proxy_port": 443,
                "proxy_tls": True,
                "proxy_host": "h.example",
                "proxy_hosts": ["a", "b"],
                "url": "https://ext",
            },
        )
        assert tab["proxyPath"] == "/x/"
        assert tab["proxyPort"] == 443
        assert tab["proxyTls"] is True
        assert tab["proxyHost"] == "h.example"
        assert tab["proxyHosts"] == ["a", "b"]
        assert tab["url"] == "https://ext"


class TestResolveShowroomTabHelpers:
    def test_external(self):
        item, port = _resolve_one_showroom_tab(
            {"type": "external", "url": "https://x"}, {}, {}, 8001
        )
        assert item == {"tab": {"type": "external", "url": "https://x"}}
        assert port == 8001

    def test_app_proxy_hosts(self):
        tab = {"type": "proxy", "proxyHosts": ["console.apps.x"]}
        item, port = _resolve_one_showroom_tab(tab, {}, {}, 8001)
        assert item["appProxyHosts"] == ["console.apps.x"]
        assert port == 8001

    def test_cluster_terminal_increments_port(self):
        tab = {"type": "terminal", "target": "clusters"}
        item, port = _resolve_one_showroom_tab(tab, {}, {}, 8001)
        assert item["ocTerminal"] is True
        assert item["wettyPort"] == 8001
        assert port == 8002

    def test_vm_missing_warning(self):
        item, _ = _resolve_one_showroom_tab(
            {"type": "terminal", "vmId": "missing"}, {}, {}, 8001
        )
        assert "warning" in item

    def test_vm_proxy(self):
        result = _resolve_vm_proxy_tab(
            {"proxyPort": 8080, "proxyTls": False}, "vscode", "10.0.0.5"
        )
        assert result["proxyTarget"] == "http://10.0.0.5:8080"
        assert result["proxyPath"].endswith("/")


class TestNginxHelpers:
    def test_wetty_location(self):
        lines = _nginx_wetty_location(
            {"wettyPath": "/wetty_bastion/", "wettyPort": 8001}
        )
        assert any("location ^~ /wetty_bastion" in ln for ln in lines)
        assert any("8001/wetty_bastion" in ln for ln in lines)

    def test_proxy_tls_with_sni(self):
        lines = _nginx_proxy_location(
            {
                "proxyPath": "/app/",
                "proxyTarget": "https://h:443",
                "proxyTls": True,
                "proxyHost": "h",
            }
        )
        joined = "\n".join(lines)
        assert "proxy_ssl_verify off;" in joined
        assert "proxy_ssl_server_name on;" in joined
        assert "proxy_ssl_name h;" in joined

    def test_collect_app_proxy_hosts_dedupes(self):
        hosts = _collect_app_proxy_hosts(
            [
                {"appProxyHosts": ["a", "b"]},
                {"appProxyHosts": ["b", "c"]},
                {},
            ]
        )
        assert hosts == ["a", "b", "c"]

    def test_tab_locations_emits_both(self):
        resolved = [
            {
                "tab": {"type": "terminal"},
                "wettyPath": "/wetty_x",
                "wettyPort": 8001,
            },
            {
                "tab": {"type": "proxy"},
                "proxyPath": "/p/",
                "proxyTarget": "http://1.2.3.4:80",
            },
        ]
        blocks = _nginx_tab_locations(resolved)
        text = "\n".join(blocks)
        assert "/wetty_x" in text
        assert "1.2.3.4:80" in text


class TestExportHelpers:
    def test_export_tab_with_cluster_and_network_id(self):
        entry = _export_showroom_tab(
            {
                "name": "Console",
                "type": "proxy",
                "clusterId": "c1",
                "networkId": "n1",
                "sshUser": "lab",
            },
            {},
            {"n1": {"data": {"name": "cluster"}}},
            {"c1": "ocp"},
        )
        assert entry["cluster"] == "ocp"
        assert entry["network"] == "cluster"
        assert entry["ssh_user"] == "lab"

    def test_copy_exported_optional(self):
        entry: dict = {}
        _copy_exported_optional_fields(
            entry,
            {
                "sshPass": "p",
                "sshPort": 22,
                "proxyPath": "/x/",
                "proxyPort": 443,
                "proxyTls": True,
                "url": "https://e",
            },
        )
        assert entry["ssh_pass"] == "p"
        assert entry["proxy_tls"] is True
        assert entry["url"] == "https://e"

    def test_resolve_dns_from_meta(self):
        assert (
            _resolve_exported_dns_network({}, {"showroom": {"dns_network": "dns"}})
            == "dns"
        )

    def test_export_showroom_section_integration(self):
        topology = {
            "clusters": [{"id": "c1", "name": "ocp"}],
            "showroom": {"dns_network": "dnsnet"},
        }
        containers = [
            {
                "data": {
                    "isShowroom": True,
                    "contentRepo": "r",
                    "contentRef": "main",
                    "buildContent": True,
                    "nics": [{"ip": "10.0.0.50"}],
                    "mounts": [{"path": "/data"}],
                    "showroomTabs": [
                        {
                            "name": "t",
                            "type": "terminal",
                            "vmId": "vm-1",
                            "network": "mgmt",
                        }
                    ],
                }
            }
        ]
        exported = export_showroom_section(
            topology, containers, {"vm-1": "bastion"}, [], {}
        )
        assert exported["ip"] == "10.0.0.50"
        assert exported["dns_network"] == "dnsnet"
        assert exported["disk_gb"] == 5
        assert exported["tabs"][0]["vm"] == "bastion"


class TestParseAndResolveRoundTrip:
    def test_parse_template_tabs_list(self):
        tabs = parse_template_tabs(
            [
                {
                    "name": "A",
                    "type": "terminal",
                    "vm": "bastion",
                    "network": "mgmt",
                }
            ],
            {"bastion": "vm-1"},
            {"bastion": {"nics": [{"network": "mgmt", "ip": "10.0.0.1"}]}},
            {"mgmt": "net-1"},
        )
        assert len(tabs) == 1
        resolved = resolve_showroom_tabs(
            tabs,
            {"bastion": {"nics": [{"network": "mgmt", "ip": "10.0.0.1"}]}},
            {"bastion": "vm-1"},
        )
        nginx = build_nginx_config(resolved)
        assert "/wetty_bastion" in nginx


class TestAppProxyServerHelpers:
    def test_proxy_lines_literal_and_resolver(self):
        from app.services.showroom_scaffold import _app_proxy_proxy_lines

        literal = _app_proxy_proxy_lines("console.apps.x", 0, None)
        assert literal == ["    proxy_pass https://console.apps.x;"]
        resolved = _app_proxy_proxy_lines(
            "console.apps.x", 1, "    resolver 10.0.0.2 valid=10s ipv6=off;"
        )
        assert 'set $troshka_up_1 "console.apps.x";' in resolved[1]
        assert "proxy_pass https://$troshka_up_1;" in resolved[2]
