"""Build canvas-equivalent showroom pod topology from declarative template YAML."""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any

NOOKBAG_BUNDLE = "https://github.com/rhpds/nookbag/releases/download/nookbag-v0.3.2/nookbag-v0.3.2.zip"
WETTY_IMAGE = "quay.io/rhpds/wetty:v2.5"
WETTY_BASE_PORT = 8001

# Cluster-terminal (bastionless oc shell): an init container fetches `oc` and
# writes a privilege-dropping shell wrapper onto the shared showroom disk; the
# terminal container runs that wrapper via wetty. See resolve_showroom_tabs.
# ubi9/ubi (not -minimal) ships curl + tar + gzip + the CA bundle, so curl
# VALIDATES TLS on the download (busybox's wget applet cannot; ubi-minimal lacks
# tar/gzip). A larger one-time pull, cached per host.
OC_FETCH_IMAGE = "registry.access.redhat.com/ubi9/ubi:latest"
OC_CLIENT_URL = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
# Published checksums for the stable client dir. oc-fetch also verifies the
# download's sha256 appears here before extracting — defense in depth on top of
# TLS validation (catches CDN corruption; both fetches are TLS-validated).
OC_SHA256SUM_URL = (
    "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/sha256sum.txt"
)
# The user's interactive shell drops to a non-root uid with no sudo (the wetty
# image ships setpriv; the image has no sudo). oc + the merged kubeconfig live on
# the shared showroom disk, populated by the oc-fetch init container and (post
# install) the deploy monitor. The wetty process itself still runs as the image
# default so it can allocate the PTY; only the user's shell is unprivileged.
_CLUSTER_SHELL_PATH = "/showroom/bin/cluster-shell"
_CLUSTER_SHELL_SCRIPT = (
    "#!/bin/sh\n"
    "export KUBECONFIG=/showroom/kube/config\n"
    "export PATH=/showroom/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
    "export HOME=/tmp\n"
    "exec setpriv --reuid=1000 --regid=1000 --clear-groups --init-groups sh\n"
)
_STORAGE_EDGE_STYLE = {
    "stroke": "rgba(251,191,36,0.6)",
    "strokeWidth": 2,
    "strokeDasharray": "4 4",
}
_SHOWROOM_DISK_Y_OFFSET = 70


def _id() -> str:
    return str(uuid.uuid4())


def _mac() -> str:
    return "52:54:00:" + ":".join(f"{b:02x}" for b in uuid.uuid4().bytes[:3])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "vm"


def _find_showroom_container(topology: dict[str, Any]) -> dict[str, Any] | None:
    for node in topology.get("nodes", []):
        if node.get("type") != "containerNode":
            continue
        data = node.get("data", {})
        if data.get("isShowroom") or data.get("name") == "showroom":
            return node
    return None


def apply_showroom_deploy_overrides(
    topology: dict[str, Any],
    *,
    content_repo: str | None = None,
    content_ref: str | None = None,
    build_content: bool | None = None,
) -> None:
    """Apply showroom content overrides when deploying from a pattern snapshot."""
    if content_repo is None and content_ref is None and build_content is None:
        return

    showroom_node = _find_showroom_container(topology)
    showroom_meta = topology.get("showroom")
    if not isinstance(showroom_meta, dict):
        showroom_meta = None
    if not showroom_node and not showroom_meta:
        return

    if build_content is None and (content_repo is not None or content_ref is not None):
        build_content = True

    data = showroom_node.get("data", {}) if showroom_node else {}
    if content_repo is not None:
        if showroom_node is not None:
            data["contentRepo"] = content_repo
        if showroom_meta is not None:
            showroom_meta["content_repo"] = content_repo
    if content_ref is not None:
        if showroom_node is not None:
            data["contentRef"] = content_ref
        if showroom_meta is not None:
            showroom_meta["content_ref"] = content_ref
    if build_content is not None:
        if showroom_node is not None:
            data["buildContent"] = build_content
        if showroom_meta is not None:
            showroom_meta["build_content"] = build_content

    if showroom_node is not None and (
        content_repo is not None or content_ref is not None
    ):
        for ic in data.get("initContainers", []):
            if ic.get("name") != "git-cloner":
                continue
            for ev in ic.get("envVars", []):
                if content_repo is not None and ev.get("key") == "GIT_REPO_URL":
                    ev["value"] = content_repo
                if content_ref is not None and ev.get("key") == "GIT_REPO_REF":
                    ev["value"] = content_ref


def _vm_ip_on_network(vms_def: dict[str, Any], vm_name: str, network_name: str) -> str:
    for nic in vms_def.get(vm_name, {}).get("nics", []):
        if nic.get("network") == network_name:
            return str(nic.get("ip", "") or "")
    return ""


def parse_template_tabs(
    tabs_yaml: list[dict[str, Any]],
    vm_name_to_id: dict[str, str],
    vms_def: dict[str, Any],
    net_ids: dict[str, str],
    clusters: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert template tab entries (vm names) to canvas showroomTabs (vmIds)."""
    tabs: list[dict[str, Any]] = []
    for raw in tabs_yaml:
        tab_type = raw.get("type", "terminal")
        tab: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "name": raw.get("name", tab_type),
            "type": tab_type,
        }
        vm_name = raw.get("vm", "")
        if vm_name:
            if vm_name not in vm_name_to_id:
                raise ValueError(f"Showroom tab references unknown VM '{vm_name}'")
            tab["vmId"] = vm_name_to_id[vm_name]
        # A cluster-linked proxy tab derives its name + hosts from the cluster.
        cluster_linked = _apply_cluster_link(tab, raw, clusters or [])
        # Name-based proxy tabs resolve their upstream via the showroom's DNS,
        # so they need neither a VM nor a per-tab network.
        name_based_proxy = tab_type == "proxy" and (
            bool(raw.get("proxy_host") or raw.get("proxy_hosts")) or cluster_linked
        )
        # Cluster terminal: a local oc shell (all deployed clusters' kubeconfigs)
        # served from a container — no VM, no bastion, no per-tab network.
        cluster_terminal = tab_type == "terminal" and raw.get("target") == "clusters"
        if cluster_terminal:
            tab["target"] = "clusters"
        network = raw.get("network", "")
        if (
            tab_type != "external"
            and not name_based_proxy
            and not cluster_terminal
            and not network
        ):
            raise ValueError(f"Showroom tab '{tab['name']}' requires network")
        if network:
            if network not in net_ids:
                raise ValueError(f"Showroom tab references unknown network '{network}'")
            tab["network"] = network
            tab["networkId"] = net_ids[network]
        if raw.get("ssh_user"):
            tab["sshUser"] = raw["ssh_user"]
        if raw.get("ssh_pass"):
            tab["sshPass"] = raw["ssh_pass"]
        if raw.get("ssh_port") is not None:
            tab["sshPort"] = int(raw["ssh_port"])
        if raw.get("proxy_path"):
            tab["proxyPath"] = raw["proxy_path"]
        if raw.get("proxy_port") is not None:
            tab["proxyPort"] = int(raw["proxy_port"])
        if raw.get("proxy_tls"):
            tab["proxyTls"] = bool(raw["proxy_tls"])
        if raw.get("proxy_host"):
            tab["proxyHost"] = raw["proxy_host"]
        proxy_hosts = _resolve_proxy_hosts(raw)
        if proxy_hosts:
            tab["proxyHosts"] = proxy_hosts
        if raw.get("url"):
            tab["url"] = raw["url"]
        tabs.append(tab)
    return tabs


def _resolve_proxy_hosts(raw: dict[str, Any]) -> list[str]:
    """Ordered internal hosts an app-proxy tab must expose; [0] is the iframe
    target, the rest are companions (e.g. oauth). Requires an explicit
    ``proxy_hosts`` list; a lone ``proxy_host`` remains a generic location proxy.
    """
    return list(raw.get("proxy_hosts") or [])


def cluster_console_hosts(name: str, base_domain: str) -> list[str]:
    """Console + oauth internal hosts for a cluster (mirrors the frontend
    ``clusterConsoleHosts``); [0] is the iframe target, [1] the login companion.
    """
    return [
        f"console-openshift-console.apps.{name}.{base_domain}",
        f"oauth-openshift.apps.{name}.{base_domain}",
    ]


def cluster_console_tab_name(name: str) -> str:
    """Managed console-proxy tab name (mirrors the frontend ``clusterConsoleTabName``)."""
    return f"{name} Console"


def _resolve_tab_cluster(ref: str, clusters: list[dict[str, Any]]) -> dict[str, Any]:
    """Find the cluster a tab's ``cluster:`` key names, by cluster name."""
    for cluster in clusters:
        if cluster.get("name") == ref:
            return cluster
    raise ValueError(f"Showroom tab references unknown cluster '{ref}'")


def _apply_cluster_link(
    tab: dict[str, Any], raw: dict[str, Any], clusters: list[dict[str, Any]]
) -> bool:
    """Link a proxy tab to an OCP cluster when ``raw`` has a ``cluster:`` key.

    Stamps ``clusterId`` and DERIVES the tab name + proxy hosts (console +
    oauth) from the cluster's name/baseDomain, matching the frontend quick-add
    (``+ OpenShift Console Proxy Tab``) so the tab is cluster-managed and
    ``syncClusterProxyTabs`` keeps it current on rename / TLD change. Returns
    ``True`` when the tab was linked.
    """
    ref = raw.get("cluster")
    if tab.get("type") != "proxy" or not ref:
        return False
    cluster = _resolve_tab_cluster(str(ref), clusters)
    name = cluster["name"]
    base_domain = cluster.get("baseDomain") or "ocp.local"
    tab["clusterId"] = cluster["id"]
    tab["name"] = cluster_console_tab_name(name)
    tab["proxyHosts"] = cluster_console_hosts(name, base_domain)
    return True


def app_proxy_internal_hosts(tabs: list[dict[str, Any]]) -> list[str]:
    """Ordered, de-duplicated internal hosts across all app-proxy tabs.

    Deploy creates one public route per host and builds the nginx public->internal
    map from this list.
    """
    seen: list[str] = []
    for tab in tabs:
        for host in tab.get("proxyHosts", []):
            host = (host or "").strip()
            if host and host not in seen:
                seen.append(host)
    return seen


def app_proxy_public_host(project_id: str, internal_host: str, apps_domain: str) -> str:
    """Deterministic public hostname for an internal app host, matching the nginx
    app-proxy server_name regex: troshka-pf-<pid8>-<label>.<apps_domain>."""
    label = internal_host.split(".")[0]
    return f"troshka-pf-{project_id[:8]}-{label}.{apps_domain}"


def derive_apps_domain(route_hostname: str) -> str:
    """Cluster apps wildcard domain from any admitted route hostname
    (<label>.apps.<cluster> -> apps.<cluster>)."""
    if "." not in (route_hostname or ""):
        return ""
    return route_hostname.split(".", 1)[1]


def fill_app_proxy_tab_urls(ui_yaml: str, project_id: str, apps_domain: str) -> str:
    """Replace __TROSHKA_APP_PROXY__<internal>__ placeholders in the rendered
    ui-config with the deterministic public https URL (deploy-time substitution)."""

    def _repl(m: re.Match[str]) -> str:
        internal = m.group(1)
        return "https://" + app_proxy_public_host(project_id, internal, apps_domain)

    return re.sub(r"__TROSHKA_APP_PROXY__([a-z0-9.-]+)__", _repl, ui_yaml)


def _resolve_name_based_proxy(tab: dict[str, Any]) -> dict[str, Any]:
    """Resolve a proxy tab whose upstream is a hostname (Host + SNI)."""
    host = tab["proxyHost"]
    proxy_path = tab.get("proxyPath") or f"/{_slugify(host)}/"
    if not proxy_path.endswith("/"):
        proxy_path = f"{proxy_path}/"
    port = int(tab.get("proxyPort") or 80)
    scheme = "https" if tab.get("proxyTls") else "http"
    return {
        "tab": tab,
        "proxyPath": proxy_path,
        "proxyTarget": f"{scheme}://{host}:{port}",
        "proxyTls": bool(tab.get("proxyTls")),
        "proxyHost": host,
    }


def resolve_showroom_tabs(
    tabs: list[dict[str, Any]],
    vms_def: dict[str, Any],
    vm_name_to_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Resolve tabs to wetty/proxy targets (mirrors frontend resolveShowroomTabs)."""
    id_to_name = {v: k for k, v in vm_name_to_id.items()}
    wetty_port = WETTY_BASE_PORT
    resolved: list[dict[str, Any]] = []

    for tab in tabs:
        tab_type = tab.get("type")
        if tab_type == "external":
            resolved.append({"tab": tab})
            continue

        # App-proxy: multiple internal hosts (console + oauth) served by the
        # deploy-time vhost. Marked here; no inline location is emitted.
        if tab_type == "proxy" and tab.get("proxyHosts"):
            resolved.append({"tab": tab, "appProxyHosts": list(tab["proxyHosts"])})
            continue

        # Name-based proxy: target a hostname resolved via the showroom's
        # internal DNS (Host header + TLS SNI = the hostname). No VM/IP needed.
        if tab_type == "proxy" and tab.get("proxyHost"):
            resolved.append(_resolve_name_based_proxy(tab))
            continue

        # Cluster terminal: a LOCAL oc shell (not SSH-into-a-VM). Served by a
        # wetty container that runs a shell in-container with oc + every deployed
        # cluster's kubeconfig on the shared showroom disk. No vmId/host.
        if tab_type == "terminal" and tab.get("target") == "clusters":
            resolved.append(
                {
                    "tab": tab,
                    "wettyPath": "/wetty_clusters",
                    "wettyPort": wetty_port,
                    "ocTerminal": True,
                }
            )
            wetty_port += 1
            continue

        vm_id = tab.get("vmId", "")
        vm_name = id_to_name.get(vm_id, "")
        if not vm_name or vm_name not in vms_def:
            resolved.append({"tab": tab, "warning": "Select a VM for this tab"})
            continue

        network_name = tab.get("network", "")
        if not network_name:
            resolved.append({"tab": tab, "warning": "Select a network for this tab"})
            continue

        vm_ip = _vm_ip_on_network(vms_def, vm_name, network_name)
        if not vm_ip:
            resolved.append(
                {"tab": tab, "warning": f"{vm_name} has no IP on {network_name}"}
            )
            continue

        if tab_type == "terminal":
            wetty_path = f"/wetty_{_slugify(vm_name)}"
            resolved.append(
                {
                    "tab": tab,
                    "wettyPath": wetty_path,
                    "wettyPort": wetty_port,
                    "wettyHost": vm_ip,
                }
            )
            wetty_port += 1
            continue

        proxy_path = tab.get("proxyPath") or f"/{_slugify(vm_name)}/"
        if not proxy_path.endswith("/"):
            proxy_path = f"{proxy_path}/"
        port = int(tab.get("proxyPort") or 80)
        scheme = "https" if tab.get("proxyTls") else "http"
        resolved.append(
            {
                "tab": tab,
                "proxyPath": proxy_path,
                "proxyTarget": f"{scheme}://{vm_ip}:{port}",
                "proxyTls": bool(tab.get("proxyTls")),
            }
        )

    return resolved


def _yaml_name(name: str) -> str:
    return json.dumps(name)[1:-1]


def build_ui_config_yaml(
    resolved: list[dict[str, Any]], external_port: int = 443
) -> str:
    """Build showroom UI config (format expected by quay.io/rhpds/showroom-content)."""
    lines = [
        "---",
        "type: showroom",
        "",
        "default_width: 30",
        "persist_url_state: true",
        "",
        "view_switcher:",
        "  enabled: true",
        "  default_mode: split",
        "",
        "antora:",
        "  name: modules",
        "  dir: www",
        "",
        "tabs:",
    ]
    for item in resolved:
        tab = item["tab"]
        tab_type = tab.get("type")
        name = _yaml_name(tab.get("name", ""))
        if tab_type == "external":
            lines.extend(
                [
                    f"  - name: {name}",
                    f"    url: {tab.get('url', '')}",
                    "    external: true",
                ]
            )
            continue
        if tab_type == "terminal" and item.get("wettyPath"):
            lines.extend(
                [
                    f"  - name: {name}",
                    f"    path: {item['wettyPath']}",
                    f"    port: {external_port}",
                ]
            )
            continue
        if tab_type == "proxy" and item.get("appProxyHosts"):
            # url is filled at deploy time with the public host for proxyHosts[0].
            target = item["appProxyHosts"][0]
            lines.extend(
                [
                    f"  - name: {name}",
                    f"    url: '__TROSHKA_APP_PROXY__{target}__'",
                ]
            )
            continue
        if tab_type == "proxy" and item.get("proxyPath"):
            proxy_path = item["proxyPath"]
            lines.extend(
                [
                    f"  - name: {name}",
                    f"    url: '{proxy_path}'",
                ]
            )
    return "\n".join(lines) + "\n"


def build_nginx_config(resolved: list[dict[str, Any]]) -> str:
    blocks = [
        "user root;",
        "events {}",
        "http {",
        "  include /etc/nginx/mime.types;",
        "  proxy_cache off;",
        "  map $http_upgrade $connection_upgrade {",
        "    default upgrade;",
        "    '' close;",
        "  }",
        "  server {",
        "    listen 80;",
        "    location / {",
        "      proxy_pass http://127.0.0.1:8000;",
        "      proxy_set_header Host $host;",
        "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "      proxy_set_header X-Forwarded-Proto $scheme;",
        "    }",
    ]
    for item in resolved:
        tab = item["tab"]
        if (
            tab.get("type") == "terminal"
            and item.get("wettyPath")
            and item.get("wettyPort")
        ):
            path = item["wettyPath"].rstrip("/")
            blocks.extend(
                [
                    f"    location ^~ {path} {{",
                    f"      proxy_pass http://127.0.0.1:{item['wettyPort']}{path};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Upgrade $http_upgrade;",
                    "      proxy_set_header Connection $connection_upgrade;",
                    "      proxy_set_header Host $host;",
                    "      proxy_read_timeout 43200000;",
                    "    }",
                ]
            )
        if (
            tab.get("type") == "proxy"
            and item.get("proxyPath")
            and item.get("proxyTarget")
        ):
            loc = item["proxyPath"]
            proxy_host = item.get("proxyHost")
            host_value = proxy_host if proxy_host else "$host"
            blocks.extend(
                [
                    f"    location {loc} {{",
                    f"      proxy_pass {item['proxyTarget']};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Upgrade $http_upgrade;",
                    "      proxy_set_header Connection $connection_upgrade;",
                    f"      proxy_set_header Host {host_value};",
                    "      proxy_read_timeout 86400;",
                ]
            )
            if item.get("proxyTls"):
                blocks.append("      proxy_ssl_verify off;")
                # Send SNI matching the backend vhost so a router can route it.
                if proxy_host:
                    blocks.append("      proxy_ssl_server_name on;")
                    blocks.append(f"      proxy_ssl_name {proxy_host};")
            blocks.append("    }")
    blocks.append("  }")  # close main server
    app_hosts: list[str] = []
    for item in resolved:
        for host in item.get("appProxyHosts", []):
            if host not in app_hosts:
                app_hosts.append(host)
    if app_hosts:
        blocks.append(build_app_proxy_config(app_hosts).rstrip("\n"))
    blocks.append("}")  # close http
    return "\n".join(blocks) + "\n"


def build_app_proxy_config(internal_hosts: list[str]) -> str:
    """nginx server blocks that embed OAuth-protected cluster apps (console, oauth)
    in the showroom iframe, baked at scaffold time.

    Each internal host (``<label>.apps.<cluster>``) gets one server block
    matched by its deterministic public hostname
    ``troshka-pf-<pid>-<label>.apps.<cluster>``. ``pid`` and the cluster suffix are
    captured from the request ``Host`` (server_name regex), so the config is
    project- and cluster-agnostic. The upstream is a literal internal host, so no
    ``resolver`` is needed. A single generic ``proxy_redirect`` rewrites any
    redirect ``Location`` host -> the public equivalent, while leaving
    the OAuth ``redirect_uri`` query param ``.local`` so the untouched cluster
    OAuthClient still validates. The apps domain is derived per host via
    ``derive_apps_domain()``.
    """
    # Skip blank hosts: an empty entry would emit `proxy_pass https://;`, which
    # nginx rejects and the showroom proxy would fail to start.
    internal_hosts = [h.strip() for h in internal_hosts if (h or "").strip()]
    # Body rewrites: the app (e.g. the console's window.SERVER_FLAGS) embeds
    # absolute .local host URLs that the browser can't resolve, so rewrite each
    # to its public equivalent. Applied in every block so console pages fix oauth
    # refs and vice versa.
    sub_filters: list[str] = [
        # sub_filter needs an uncompressed upstream response.
        '    proxy_set_header Accept-Encoding "";',
        "    sub_filter_once off;",
        "    sub_filter_types *;",
    ]
    for h in internal_hosts:
        # Match the "//" from https:// so SERVER_FLAGS host refs are rewritten but
        # the URL-encoded redirect_uri (https%3A%2F%2F...) is left .local, keeping
        # the OAuthClient validation intact.
        sub_filters.append(
            f'    sub_filter "//{h}" '
            f'"//troshka-pf-$troshka_pid-{h.split(".")[0]}.$troshka_suffix";'
        )

    blocks: list[str] = []
    for host in internal_hosts:
        label = host.split(".")[0]
        # Derive the apps domain for this host (e.g., apps.<cluster>)
        host_apps = derive_apps_domain(host)
        host_apps_re = re.escape(host_apps)  # Escape dots for nginx regex
        blocks.extend(
            [
                "server {",
                "  listen 80;",
                # Quote the regex: nginx treats bare {8} as block syntax.
                f'  server_name "~^troshka-pf-(?<troshka_pid>[0-9a-f]{{8}})-{label}'
                '\\.(?<troshka_suffix>apps\\..+)$";',
                "  location / {",
                f"    proxy_pass https://{host};",
                "    proxy_ssl_server_name on;",
                f"    proxy_ssl_name {host};",
                "    proxy_ssl_verify off;",
                f"    proxy_set_header Host {host};",
                "    proxy_set_header X-Forwarded-Proto https;",
                "    proxy_http_version 1.1;",
                "    proxy_set_header Upgrade $http_upgrade;",
                "    proxy_set_header Connection $connection_upgrade;",
                "    proxy_read_timeout 86400;",
                # Allow the app to render inside the showroom iframe.
                "    proxy_hide_header X-Frame-Options;",
                # Rewrite redirect Location host -> public (redirect_uri
                # query params are left .local so the OAuthClient still validates).
                f"    proxy_redirect ~^https://(?<troshka_h>[^.]+)"
                f"\\.{host_apps_re}(?<troshka_rest>.*)$ "
                "https://troshka-pf-$troshka_pid-$troshka_h.$troshka_suffix$troshka_rest;",
                *sub_filters,
                f"    proxy_cookie_domain .{host_apps} $host;",
                "  }",
                "}",
            ]
        )
    return "\n".join(blocks) + "\n" if blocks else ""


def _oc_terminal_container(item: dict[str, Any], disk_id: str) -> dict[str, Any]:
    """A LOCAL-shell wetty container: runs the privilege-dropping cluster-shell
    wrapper (unprivileged, no sudo) with oc + the merged kubeconfig on the shared
    showroom disk. No SSH — the shell is in-container."""
    base_path = (item.get("wettyPath") or "/wetty_clusters").lstrip("/")
    return {
        "name": "wetty-clusters",
        "image": WETTY_IMAGE,
        "cpus": 1,
        "memory": 256,
        "envVars": [],
        "ports": [
            {"containerPort": item["wettyPort"], "hostPort": None, "protocol": "tcp"}
        ],
        "command": [
            f"--base=/{base_path}/",
            f"--port={item['wettyPort']}",
            "--command",
            _CLUSTER_SHELL_PATH,
        ],
        "mounts": [{"diskNodeId": disk_id, "mountPath": "/showroom"}],
    }


def _build_oc_fetch_init(disk_id: str) -> dict[str, Any]:
    """Init container: fetch `oc` onto the shared showroom disk and write the
    cluster-shell wrapper. Runs before the terminal container starts."""
    script_b64 = base64.b64encode(_CLUSTER_SHELL_SCRIPT.encode()).decode()
    cmd = (
        "set -e; mkdir -p /showroom/bin /showroom/kube; cd /tmp; "
        f"curl -fsSL {OC_CLIENT_URL} -o oc.tgz; "
        f"curl -fsSL {OC_SHA256SUM_URL} -o sums.txt; "
        "got=$(sha256sum oc.tgz | cut -d' ' -f1); "
        'if ! grep -qi "^$got " sums.txt; then '
        'echo "oc checksum $got not in published sha256sum.txt"; exit 1; fi; '
        "tar xzf oc.tgz -C /showroom/bin oc; "
        "chmod 0755 /showroom/bin/oc; "
        f"echo {script_b64} | base64 -d > {_CLUSTER_SHELL_PATH}; "
        f"chmod 0755 {_CLUSTER_SHELL_PATH}"
    )
    return {
        "name": "oc-fetch",
        "image": OC_FETCH_IMAGE,
        "cpus": 1,
        "memory": 256,
        "envVars": [],
        "ports": [],
        "command": cmd,
        "mounts": [{"diskNodeId": disk_id, "mountPath": "/showroom"}],
    }


def _build_wetty_containers(
    resolved: list[dict[str, Any]],
    tabs: list[dict[str, Any]],
    vms_def: dict[str, Any],
    vm_name_to_id: dict[str, str],
    disk_id: str,
) -> list[dict[str, Any]]:
    id_to_name = {v: k for k, v in vm_name_to_id.items()}
    containers: list[dict[str, Any]] = []
    for item in resolved:
        tab = item["tab"]
        if item.get("ocTerminal") and item.get("wettyPort"):
            containers.append(_oc_terminal_container(item, disk_id))
            continue
        if (
            tab.get("type") != "terminal"
            or not item.get("wettyPort")
            or not item.get("wettyHost")
        ):
            continue
        vm_id = tab.get("vmId", "")
        vm_name = id_to_name.get(vm_id, "vm")
        vm_cfg = vms_def.get(vm_name, {})
        ssh_user = tab.get("sshUser") or vm_cfg.get("login_user") or "cloud-user"
        ssh_pass = tab.get("sshPass") or vm_cfg.get("cloud_user_password") or ""
        ssh_port = tab.get("sshPort") or 22
        base_path = (item.get("wettyPath") or f"/wetty_{_slugify(vm_name)}").lstrip("/")
        cmd = [
            f"--base=/{base_path}/",
            f"--port={item['wettyPort']}",
            f"--ssh-host={item['wettyHost']}",
            f"--ssh-port={ssh_port}",
            f"--ssh-user={ssh_user}",
            "--ssh-auth=password",
            f"--ssh-pass={ssh_pass}",
        ]
        containers.append(
            {
                "name": f"wetty-{_slugify(vm_name)}",
                "image": WETTY_IMAGE,
                "cpus": 1,
                "memory": 256,
                "envVars": [],
                "ports": [
                    {
                        "containerPort": item["wettyPort"],
                        "hostPort": None,
                        "protocol": "tcp",
                    }
                ],
                "command": cmd,
                "mounts": [],
            }
        )
    return containers


def _build_init_containers(
    content_repo: str,
    content_ref: str,
    nginx_b64: str,
    ui_config_b64: str,
    disk_id: str,
) -> list[dict[str, Any]]:
    mount = {"diskNodeId": disk_id, "mountPath": "/showroom"}
    return [
        {
            "name": "git-cloner",
            "image": "quay.io/rhpds/git-cloner:v1.1.4",
            "cpus": 1,
            "memory": 256,
            "envVars": [
                {"key": "GIT_REPO_URL", "value": content_repo},
                {"key": "GIT_REPO_REF", "value": content_ref},
                {"key": "CLONE_DIR", "value": "/showroom/repo"},
            ],
            "ports": [],
            "command": None,
            "mounts": [mount],
        },
        {
            "name": "nginx-config",
            "image": "docker.io/library/busybox:1.36",
            "cpus": 1,
            "memory": 64,
            "envVars": [
                {"key": "NGINX_B64", "value": nginx_b64},
                {"key": "UI_CONFIG_B64", "value": ui_config_b64},
            ],
            "ports": [],
            "command": (
                'mkdir -p /showroom/nginx /showroom/repo && echo "$NGINX_B64" | base64 -d '
                '> /showroom/nginx/nginx.conf && echo "$UI_CONFIG_B64" | base64 -d '
                "> /showroom/repo/ui-config.yml"
            ),
            "mounts": [mount],
        },
        {
            "name": "antora-builder",
            "image": "quay.io/rhpds/antora:v1.2.2",
            "cpus": 1,
            "memory": 512,
            "envVars": [
                {"key": "FILES_DIR", "value": "/showroom/repo"},
                {"key": "OUTPUT_DIR", "value": "/showroom/www"},
                {"key": "ANTORA_PLAYBOOK", "value": "site.yml"},
                {"key": "ZT_UI_ENABLED", "value": "true"},
                {"key": "ZT_BUNDLE", "value": NOOKBAG_BUNDLE},
            ],
            "ports": [],
            "command": None,
            "mounts": [mount],
        },
    ]


def _build_pod_containers(disk_id: str) -> list[dict[str, Any]]:
    mount = {"diskNodeId": disk_id, "mountPath": "/showroom"}
    return [
        {
            "name": "proxy",
            "image": "quay.io/rhpds/nginx:1.25",
            "cpus": 1,
            "memory": 256,
            "envVars": [],
            "ports": [{"containerPort": 80, "hostPort": None, "protocol": "tcp"}],
            "command": [
                "nginx",
                "-c",
                "/showroom/nginx/nginx.conf",
                "-g",
                "daemon off;",
            ],
            "mounts": [mount],
        },
        {
            "name": "content",
            "image": "quay.io/rhpds/showroom-content:v1.4.1",
            "cpus": 1,
            "memory": 256,
            "envVars": [
                {"key": "ANTORA_PLAYBOOK", "value": "site.yml"},
                {"key": "ZT_BUNDLE", "value": NOOKBAG_BUNDLE},
                {"key": "ZT_UI_ENABLED", "value": "true"},
                {"key": "GUID", "value": "workshop"},
                {"key": "DOMAIN", "value": "workshop.local"},
            ],
            "ports": [{"containerPort": 8000, "hostPort": None, "protocol": "tcp"}],
            "mounts": [mount],
        },
    ]


def build_showroom_from_config(
    showroom_cfg: dict[str, Any],
    vm_name_to_id: dict[str, str],
    vms_def: dict[str, Any],
    net_ids: dict[str, str],
    vm_x: int,
    vm_row_y: int,
    clusters: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict], list[dict], list[dict], dict[str, Any]]:
    """Return showroom container node, disks, edges, and topology.showroom metadata."""
    disk_gb = int(showroom_cfg.get("disk_gb", 5))
    content_repo = str(showroom_cfg.get("content_repo", ""))
    content_ref = str(showroom_cfg.get("content_ref", "main"))
    build_content = showroom_cfg.get("build_content", True)
    dns_network = str(showroom_cfg.get("dns_network") or "").strip()

    tabs = parse_template_tabs(
        showroom_cfg.get("tabs") or [], vm_name_to_id, vms_def, net_ids, clusters
    )
    resolved = resolve_showroom_tabs(tabs, vms_def, vm_name_to_id)
    nginx_b64 = base64.b64encode(build_nginx_config(resolved).encode()).decode()
    ui_config_b64 = base64.b64encode(build_ui_config_yaml(resolved).encode()).decode()

    ctr_id = _id()
    disk_id = _id()
    disk_name = "showroom-vol0"

    disk_node = {
        "id": disk_id,
        "type": "storageNode",
        "position": {"x": vm_x - 190, "y": vm_row_y + _SHOWROOM_DISK_Y_OFFSET},
        "data": {
            "label": disk_name,
            "name": disk_name,
            "size": disk_gb,
            "format": "raw",
            "icon": "\U0001f6e2",
        },
    }
    disk_edge = {
        "id": _id(),
        "source": disk_id,
        "target": ctr_id,
        "sourceHandle": "right",
        "targetHandle": f"mnt-{disk_id}-left",
        "type": "smoothstep",
        "style": _STORAGE_EDGE_STYLE,
    }

    nic_edges: list[dict] = []
    ctr_nics: list[dict] = []

    init_containers = _build_init_containers(
        content_repo, content_ref, nginx_b64, ui_config_b64, disk_id
    )
    # Cluster terminal present -> fetch oc + write the shell wrapper before start.
    if any(item.get("ocTerminal") for item in resolved):
        init_containers.append(_build_oc_fetch_init(disk_id))
    pod_containers = _build_pod_containers(disk_id) + _build_wetty_containers(
        resolved, tabs, vms_def, vm_name_to_id, disk_id
    )

    ctr_node = {
        "id": ctr_id,
        "type": "containerNode",
        "position": {"x": vm_x, "y": vm_row_y},
        "data": {
            "label": "showroom",
            "name": "showroom",
            "image": "",
            "registryCredentialId": None,
            "cpus": 1,
            "memory": 512,
            "status": "stopped",
            "icon": "\U0001f4d6",
            "isPod": True,
            "isShowroom": True,
            "infraNetworking": True,
            "buildContent": bool(build_content),
            "contentRepo": content_repo,
            "contentRef": content_ref,
            "dnsNetwork": dns_network,
            "showroomTabs": tabs,
            "nics": ctr_nics,
            "envVars": [],
            "ports": [],
            "command": None,
            "restartPolicy": "always",
            "privileged": False,
            "mounts": [{"diskNodeId": disk_id, "mountPath": "/showroom"}],
            "initContainers": init_containers,
            "podContainers": pod_containers,
        },
    }

    showroom_meta: dict[str, Any] = {
        "enabled": showroom_cfg.get("enabled", True),
        "content_repo": content_repo,
        "content_ref": content_ref,
        "build_content": bool(build_content),
        "disk_gb": disk_gb,
        "tabs": tabs,
    }
    if dns_network:
        showroom_meta["dns_network"] = dns_network

    return ctr_node, [disk_node], [disk_edge], nic_edges, showroom_meta


def export_showroom_section(
    topology: dict[str, Any],
    container_nodes: list[dict[str, Any]],
    id_to_name: dict[str, str],
    edges: list[dict],
    net_nodes: dict[str, dict],
) -> dict[str, Any] | None:
    """Export readable showroom YAML (no base64, vm names not IDs)."""
    showroom_node = next(
        (n for n in container_nodes if n.get("data", {}).get("isShowroom")), None
    )
    if not showroom_node:
        return topology.get("showroom")

    cd = showroom_node["data"]
    exported: dict[str, Any] = {
        "enabled": True,
        "content_repo": cd.get("contentRepo", ""),
        "content_ref": cd.get("contentRef", "main"),
        "build_content": cd.get("buildContent", True),
    }
    dns_network = str(cd.get("dnsNetwork") or "").strip()
    if not dns_network:
        showroom_meta = topology.get("showroom") or {}
        dns_network = str(showroom_meta.get("dns_network") or "").strip()
    if dns_network:
        exported["dns_network"] = dns_network

    for nic in cd.get("nics", []):
        if nic.get("ip"):
            exported["ip"] = nic["ip"]

    mounts = cd.get("mounts", [])
    if mounts:
        exported.setdefault("disk_gb", 5)

    cluster_names = {
        c.get("id"): c.get("name") for c in (topology.get("clusters") or [])
    }
    tabs_out: list[dict[str, Any]] = []
    for tab in cd.get("showroomTabs", []):
        entry: dict[str, Any] = {
            "name": tab.get("name", ""),
            "type": tab.get("type", "terminal"),
        }
        # Cluster-managed console tab: export as ``cluster: <name>`` so it
        # re-imports as managed (name + hosts re-derived), not as static hosts.
        cluster_id = tab.get("clusterId")
        if cluster_id and cluster_names.get(cluster_id):
            entry["cluster"] = cluster_names[cluster_id]
        vm_id = tab.get("vmId")
        if vm_id:
            entry["vm"] = id_to_name.get(vm_id, "")
        if tab.get("network"):
            entry["network"] = tab["network"]
        elif tab.get("networkId") and tab["networkId"] in net_nodes:
            net_data = net_nodes[tab["networkId"]].get("data", {})
            entry["network"] = net_data.get("name") or net_data.get("label")
        if tab.get("sshUser"):
            entry["ssh_user"] = tab["sshUser"]
        if tab.get("sshPass"):
            entry["ssh_pass"] = tab["sshPass"]
        if tab.get("sshPort") is not None:
            entry["ssh_port"] = tab["sshPort"]
        if tab.get("proxyPath"):
            entry["proxy_path"] = tab["proxyPath"]
        if tab.get("proxyPort") is not None:
            entry["proxy_port"] = tab["proxyPort"]
        if tab.get("proxyTls"):
            entry["proxy_tls"] = True
        if tab.get("url"):
            entry["url"] = tab["url"]
        tabs_out.append(entry)
    if tabs_out:
        exported["tabs"] = tabs_out

    return exported
