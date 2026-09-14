"""Join canvas worker VMs after a true-SNO agent install (SNO + deferred workers).

OpenShift agent install only supports 1 control-plane + 0 workers at install time.
Clusters with ``type: sno`` and canvas ``workers`` > 0 install the control plane
first; worker VMs (``deferOcpInstall``) are joined here via ``oc adm node-image
create`` once the cluster API is up.
"""

from __future__ import annotations

import re
import shlex

from app.services.ocp.agent_template import (
    _cluster_members_for,
    _redfish_eject_media_cmd,
    _redfish_insert_media_cmd,
    member_defers_ocp_install,
)

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def deferred_workers_for_cluster(topology: dict, cluster: dict) -> list[dict]:
    """Return join targets: ``{name, mac, bmc_ip}`` for deferred worker VMs."""
    entries: list[dict] = []
    for node in _cluster_members_for(topology, cluster):
        if not member_defers_ocp_install(cluster, node, topology):
            continue
        data = node.get("data") or {}
        nics = data.get("nics") or []
        mac = (nics[0].get("mac") if nics else "") or ""
        bmc_ip = str(data.get("bmcIp") or "").strip()
        name = str(data.get("name") or "").strip()
        if not name or not _MAC_RE.match(mac) or not bmc_ip:
            continue
        entries.append({"name": name, "mac": mac, "bmc_ip": bmc_ip})
    return entries


def _serve_node_iso_cmd(
    indent: str,
    serve_dir: str,
    port: int,
    iso_basename: str,
    serving_ip: str | None,
) -> str:
    """Start a directory HTTP server and set ``ISO_URL`` for Redfish."""
    i = indent
    if serving_ip:
        ip_line = f"{i}BASTION_IP={serving_ip}\n"
    else:
        ip_line = f"{i}BASTION_IP=$(hostname -I | awk '{{print $1}}')\n"
    return (
        f"{i}sudo firewall-cmd --add-port={port}/tcp --permanent 2>/dev/null && "
        "sudo firewall-cmd --reload 2>/dev/null || true\n"
        f"{i}cd {serve_dir}\n"
        f"{i}nohup python3 -m http.server {port} > /tmp/http-server-{port}.log 2>&1 &\n"
        f"{i}HTTP_PID=$!\n"
        + ip_line
        + f'{i}ISO_URL="http://${{BASTION_IP}}:{port}/{iso_basename}"\n'
        + f'{i}echo "Node ISO URL: $ISO_URL"\n'
    )


def _wait_for_worker_nodes_cmd(indent: str, cluster_key: str, expected: int) -> str:
    """Poll until ``expected`` worker nodes are Ready (approve CSRs along the way)."""
    i = indent
    return (
        f'{i}echo "[{cluster_key}] waiting for {expected} worker node(s) to become Ready"\n'
        f"{i}ready=0\n"
        f"{i}for _try in $(seq 1 120); do\n"
        f"{i}  oc get csr --no-headers 2>/dev/null | awk '/Pending/{{print $1}}' "
        f"| xargs -r -n 1 oc adm certificate approve >/dev/null 2>&1 || true\n"
        f"{i}  ready=$(oc get nodes -l node-role.kubernetes.io/worker --no-headers 2>/dev/null "
        f"| awk '$2==\"Ready\"{{c++}} END{{print c+0}}')\n"
        f'{i}  echo "[{cluster_key}] worker nodes Ready: $ready/{expected}"\n'
        f'{i}  [ "$ready" -ge {expected} ] && break\n'
        f"{i}  sleep 30\n"
        f"{i}done\n"
        f'{i}[ "$ready" -ge {expected} ] || {{ echo "[{cluster_key}] worker join timed out"; exit 1; }}\n'
    )


def build_join_deferred_workers_cmd(
    indent: str,
    cluster_key: str,
    workers: list[dict],
    bmc_password: str,
    base_port: int,
    serving_ip: str | None,
) -> str:
    """Bash fragment: ``oc adm node-image create`` + Redfish boot per deferred worker."""
    if not workers:
        return ""
    i = indent
    lines = [
        f"{i}if [ -f .deferred-workers-joined ]; then",
        f'{i}  echo "[{cluster_key}] deferred workers already joined, skipping"',
        f"{i}else",
        f'{i}  echo "[{cluster_key}] joining {len(workers)} deferred worker(s)"',
        f'{i}  export KUBECONFIG="$(pwd)/auth/kubeconfig"',
        f"{i}  BMC_PASS={shlex.quote(bmc_password)}",
    ]
    for idx, worker in enumerate(workers):
        name = worker["name"]
        mac = worker["mac"]
        bmc_ip = worker["bmc_ip"]
        node_dir = f"nodes/{name}"
        port = base_port + 100 + idx
        iso_name = "node.iso"
        lines.extend(
            [
                f'{i}  echo "[{cluster_key}] node-image create for {name}"',
                f"{i}  mkdir -p {node_dir}",
                f"{i}  (cd {node_dir} && oc adm node-image create "
                f"--mac-address={shlex.quote(mac)} 2>&1 | tee create.log)",
                f"{i}  ISO_SRC=$(find {node_dir} -maxdepth 2 -name '*.iso' | head -1)",
                f'{i}  if [ -z "$ISO_SRC" ]; then echo "[{cluster_key}] no ISO for {name}"; exit 1; fi',
                f'{i}  cp -f "$ISO_SRC" {node_dir}/{iso_name}',
                f'{i}  HTTP_PID=""',
                f"{i}  trap 'kill $HTTP_PID 2>/dev/null || true' EXIT",
            ]
        )
        lines.append(
            _serve_node_iso_cmd(f"{i}  ", node_dir, port, iso_name, serving_ip).rstrip()
        )
        lines.append(_redfish_insert_media_cmd(f"{i}  ", bmc_ip))
        lines.append(f'{i}  echo "[{cluster_key}] net-booting worker {name}"')
        lines.append(f"{i}  kill $HTTP_PID 2>/dev/null || true")
        lines.append(_redfish_eject_media_cmd(f"{i}  ", bmc_ip))
    lines.append(_wait_for_worker_nodes_cmd(i, cluster_key, len(workers)))
    lines.append(f"{i}  touch .deferred-workers-joined")
    lines.append(f"{i}  echo '[{cluster_key}] deferred workers joined'")
    lines.append(f"{i}fi")
    return "\n".join(lines) + "\n"
