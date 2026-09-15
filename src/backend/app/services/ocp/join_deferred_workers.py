"""Join canvas worker VMs after a true-SNO agent install (SNO + deferred workers).

OpenShift agent install only supports 1 control-plane + 0 workers at install time.
Clusters with ``type: sno`` and canvas ``workers`` > 0 install the control plane
first; worker VMs (``deferOcpInstall``) are joined here via ``oc adm node-image
create`` once the cluster API is up.
"""

from __future__ import annotations

import re
import shlex

import yaml

from app.services.ocp.agent_template import (
    _cluster_members_for,
    _redfish_eject_media_cmd,
    _redfish_insert_media_cmd,
    deferred_worker_cluster_nic,
)

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")

# Approve kubelet client/serving CSRs so nodes lose the console "Approval required"
# badge and stay Ready after ISO eject.
_APPROVE_PENDING_CSRS = (
    "oc get csr --no-headers 2>/dev/null | awk '/Pending/{print $1}' "
    "| xargs -r -n 1 oc adm certificate approve >/dev/null 2>&1 || true"
)


def deferred_workers_for_cluster(topology: dict, cluster: dict) -> list[dict]:
    """Return join targets for deferred worker VMs.

    Each entry includes cluster-network addressing for ``oc adm node-image
    create --network-config-path`` so workers default-route via the cluster
    gateway (not the post-install migration L2).
    """
    entries: list[dict] = []
    for node in _cluster_members_for(topology, cluster):
        nic = deferred_worker_cluster_nic(node, cluster, topology)
        if not nic:
            continue
        data = node.get("data") or {}
        bmc_ip = str(data.get("bmcIp") or "").strip()
        name = str(data.get("name") or "").strip()
        if not name or not bmc_ip:
            continue
        entries.append({"name": name, "bmc_ip": bmc_ip, **nic})
    return entries


def _nmstate_interface(entry: dict) -> dict:
    return {
        "name": entry["iface_name"],
        "type": "ethernet",
        "state": "up",
        "identifier": "mac-address",
        "mac-address": entry["mac"],
        "ipv4": {
            "enabled": True,
            "address": [
                {
                    "ip": entry["ip"],
                    "prefix-length": entry["prefix_len"],
                }
            ],
            "dhcp": False,
        },
    }


def build_deferred_worker_nmstate(worker: dict) -> str:
    """NMState YAML for ``oc adm node-image create --network-config-path``.

    Configures every worker NIC (cluster + migration, etc.). Only the cluster
    egress interface receives the default route and DNS; auxiliary segments are
    addressed statically so CCLM live migration works without stealing egress.
    """
    egress_iface = worker["iface_name"]
    interfaces = [_nmstate_interface(worker)]
    for aux in worker.get("aux_nics") or []:
        interfaces.append(_nmstate_interface(aux))
    cfg = {
        "interfaces": interfaces,
        "dns-resolver": {"config": {"server": [worker["dns_ip"]]}},
        "routes": {
            "config": [
                {
                    "destination": "0.0.0.0/0",
                    "next-hop-address": worker["gateway"],
                    "next-hop-interface": egress_iface,
                }
            ]
        },
    }
    return yaml.dump(cfg, default_flow_style=False, sort_keys=False)


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


def _cleanup_broken_node_joiner_namespaces_cmd(indent: str, cluster_key: str) -> str:
    """Delete ``openshift-node-joiner-*`` namespaces missing SCC UID annotations."""
    i = indent
    # ``grep`` exits 1 when there are no matches; under ``set -o pipefail`` that
    # aborts the whole install subshell with no breadcrumb — use ``|| true``.
    return (
        f"{i}for _ns in $(oc get ns -o name 2>/dev/null | grep openshift-node-joiner || true); do\n"
        f"{i}  ann=$(oc get \"$_ns\" -o jsonpath='{{.metadata.annotations.openshift\\.io/sa\\.scc\\.uid-range}}' 2>/dev/null)\n"
        f'{i}  if [ -z "$ann" ]; then\n'
        f'{i}    echo "[{cluster_key}] removing stale $_ns (missing sa.scc.uid-range)"\n'
        f'{i}    oc delete "$_ns" --wait=false 2>/dev/null || true\n'
        f"{i}  fi\n"
        f"{i}done\n"
    )


def _worker_join_preflight_cmd(indent: str, cluster_key: str) -> str:
    """Verify SCC UID allocation works before ``oc adm node-image create``.

    Fresh SNO clusters can report install-complete while the apiserver SCC
    allocator is still settling; ``node-image create`` then fails with
    ``unable to find annotation openshift.io/sa.scc.uid-range`` on a stale or
    half-created ``openshift-node-joiner-*`` namespace.
    """
    i = indent
    return (
        f'{i}echo "[{cluster_key}] preflight: checking SCC UID allocation for worker join"\n'
        f"{i}for _try in $(seq 1 60); do\n"
        f"{i}  oc get co openshift-apiserver -o jsonpath='{{.status.conditions[?(@.type==\"Available\")].status}}' 2>/dev/null | grep -q True && break\n"
        f"{i}  sleep 10\n"
        f"{i}done\n"
        f'{i}echo "[{cluster_key}] preflight: removing stale openshift-node-joiner namespaces"\n'
        + _cleanup_broken_node_joiner_namespaces_cmd(i, cluster_key)
        + f'{i}PROBE_NS="troshka-scc-probe-$(date +%s)"\n'
        + f'{i}echo "[{cluster_key}] preflight: probing SCC UID allocator ($PROBE_NS)"\n'
        + f'{i}oc create namespace "$PROBE_NS" 2>/dev/null || true\n'
        + f'{i}uid_range=""\n'
        + f"{i}for _try in $(seq 1 36); do\n"
        + f"{i}  uid_range=$(oc get ns \"$PROBE_NS\" -o jsonpath='{{.metadata.annotations.openshift\\.io/sa\\.scc\\.uid-range}}' 2>/dev/null)\n"
        + f'{i}  [ -n "$uid_range" ] && break\n'
        + f"{i}  sleep 5\n"
        + f"{i}done\n"
        + f'{i}oc delete namespace "$PROBE_NS" --wait=false 2>/dev/null || true\n'
        + f'{i}[ -n "$uid_range" ] || {{ echo "[{cluster_key}] SCC UID range not available for worker join"; exit 1; }}\n'
        + f'{i}echo "[{cluster_key}] preflight: SCC UID allocator ready (range $uid_range)"\n'
    )


def _wait_before_worker_join_cmd(indent: str, cluster_key: str) -> str:
    """Wait for the API (and image-registry operator) before node-image create.

    Fresh nested SNO clusters often report install-complete while admission
    webhooks (ImagePolicy) are still settling; joining immediately flakes.
    """
    i = indent
    return (
        f'{i}echo "[{cluster_key}] waiting for API before worker join"\n'
        f"{i}for _try in $(seq 1 60); do\n"
        f"{i}  oc get --raw /healthz >/dev/null 2>&1 "
        f"&& oc get co image-registry >/dev/null 2>&1 && break\n"
        f"{i}  sleep 10\n"
        f"{i}done\n"
        f"{i}oc get --raw /healthz >/dev/null 2>&1 || "
        f'{{ echo "[{cluster_key}] API not ready for worker join"; exit 1; }}\n'
        f"{i}sleep 30\n"
    )


def _node_image_create_cmd(
    indent: str, cluster_key: str, worker: dict, node_dir: str
) -> str:
    """``oc adm node-image create`` with bounded retries for admission flakes."""
    i = indent
    name = worker["name"]
    mac = worker["mac"]
    nmstate = build_deferred_worker_nmstate(worker)
    net_cfg = f"{node_dir}/network-config.yaml"
    return (
        f'{i}echo "[{cluster_key}] node-image create for {name}"\n'
        f"{i}mkdir -p {node_dir}\n"
        f"{i}cat > {net_cfg} <<'NMEOF'\n{nmstate}NMEOF\n"
        f"{i}created=0\n"
        f"{i}for _try in $(seq 1 8); do\n"
        f"{i}  if (cd {node_dir} && oc adm node-image create "
        f"--mac-address={shlex.quote(mac)} "
        f"--network-config-path=network-config.yaml 2>&1 | tee create.log); then\n"
        f"{i}    if find {node_dir} -maxdepth 2 -name '*.iso' | grep -q .; then "
        f"created=1; break; fi\n"
        f"{i}  fi\n"
        f'{i}  if grep -qi "sa\\.scc\\.uid-range" create.log 2>/dev/null; then\n'
        + _cleanup_broken_node_joiner_namespaces_cmd(f"{i}    ", cluster_key)
        + f"{i}  fi\n"
        + f'{i}  echo "[{cluster_key}] node-image create failed for {name} '
        f'(attempt $_try), retrying in 60s..."\n'
        f"{i}  sleep 60\n"
        f"{i}done\n"
        f'{i}[ "$created" = 1 ] || {{ echo "[{cluster_key}] node-image create failed '
        f'for {name}"; exit 1; }}\n'
    )


def _wait_for_worker_node_cmd(indent: str, cluster_key: str, name: str) -> str:
    """Poll until a named deferred worker node is Ready (approve CSRs along the way)."""
    i = indent
    return (
        f'{i}echo "[{cluster_key}] waiting for worker {name} to become Ready"\n'
        f'{i}node_ready=""\n'
        f"{i}for _try in $(seq 1 120); do\n"
        f"{i}  {_APPROVE_PENDING_CSRS}\n"
        f"{i}  node_ready=$(oc get node {shlex.quote(name)} -o jsonpath="
        f"'{{.status.conditions[?(@.type==\"Ready\")].status}}' 2>/dev/null || true)\n"
        f'{i}  [ "$node_ready" = "True" ] && break\n'
        f"{i}  sleep 30\n"
        f"{i}done\n"
        f'{i}[ "$node_ready" = "True" ] || {{ echo "[{cluster_key}] worker {name} join timed out"; exit 1; }}\n'
        f'{i}echo "[{cluster_key}] worker {name} Ready"\n'
    )


def _worker_join_and_boot_block(
    indent: str,
    cluster_key: str,
    worker: dict,
    node_dir: str,
    port: int,
    serving_ip: str | None,
) -> str:
    """Create node ISO, net-boot one worker, wait for join, then eject ISO."""
    i = indent
    inner = indent + "  "
    iso_name = "node.iso"
    name = worker["name"]
    bmc_ip = worker["bmc_ip"]
    return (
        f"{i}(\n"
        f"{inner}set -e\n"
        f"{inner}set -o pipefail\n"
        + _node_image_create_cmd(inner, cluster_key, worker, node_dir)
        + f"{inner}ISO_SRC=$(find {node_dir} -maxdepth 2 -name '*.iso' | head -1)\n"
        + f'{inner}if [ -z "$ISO_SRC" ]; then echo "[{cluster_key}] no ISO for {name}"; exit 1; fi\n'
        + f'{inner}cp -f "$ISO_SRC" {node_dir}/{iso_name}\n'
        + f'{inner}HTTP_PID=""\n'
        + f"{inner}trap 'kill $HTTP_PID 2>/dev/null || true' EXIT\n"
        + _serve_node_iso_cmd(inner, node_dir, port, iso_name, serving_ip)
        + _redfish_insert_media_cmd(inner, bmc_ip)
        + f'{inner}echo "[{cluster_key}] net-booting worker {name}"\n'
        + _wait_for_worker_node_cmd(inner, cluster_key, name)
        + f"{inner}kill $HTTP_PID 2>/dev/null || true\n"
        + _redfish_eject_media_cmd(inner, bmc_ip)
        + f'{inner}echo "[{cluster_key}] worker {name} joined (ISO ejected)"\n'
        + f"{i}) &\n"
        + f"{i}worker_pids+=($!)\n"
    )


def _wait_for_workers_converged_cmd(
    indent: str,
    cluster_key: str,
    worker_names: list[str],
    stable_checks: int = 3,
    sleep_secs: int = 15,
) -> str:
    """Wait until every deferred worker stays Ready across consecutive polls.

    Workers often flip Ready briefly during ISO install, then NotReady after
    ISO eject/reboot while OVN settles — do not treat join as done until Ready
    is stable.
    """
    i = indent
    if not worker_names:
        return ""
    names = " ".join(shlex.quote(n) for n in worker_names)
    expected = len(worker_names)
    return (
        f'{i}echo "[{cluster_key}] waiting for deferred workers to converge '
        f'(stable Ready)"\n'
        f"{i}stable=0\n"
        f"{i}for _try in $(seq 1 30); do\n"
        f"{i}  {_APPROVE_PENDING_CSRS}\n"
        f"{i}  ready=0\n"
        f"{i}  for _node in {names}; do\n"
        f'{i}    _st=$(oc get node "$_node" -o jsonpath='
        f"'{{.status.conditions[?(@.type==\"Ready\")].status}}' 2>/dev/null || true)\n"
        f'{i}    [ "$_st" = "True" ] && ready=$((ready + 1))\n'
        f"{i}  done\n"
        f'{i}  echo "[{cluster_key}] deferred workers Ready: $ready/{expected}"\n'
        f'{i}  if [ "$ready" -ge {expected} ]; then\n'
        f"{i}    stable=$((stable + 1))\n"
        f'{i}    [ "$stable" -ge {stable_checks} ] && break\n'
        f"{i}  else\n"
        f"{i}    stable=0\n"
        f"{i}  fi\n"
        f"{i}  sleep {sleep_secs}\n"
        f"{i}done\n"
        f'{i}[ "$stable" -ge {stable_checks} ] || {{ echo "[{cluster_key}] worker '
        f'convergence timed out"; exit 1; }}\n'
        f'{i}echo "[{cluster_key}] deferred workers converged"\n'
    )


def _wait_for_worker_nodes_cmd(
    indent: str, cluster_key: str, worker_names: list[str]
) -> str:
    """Quick check: every deferred worker hostname is Ready (pre-convergence)."""
    i = indent
    if not worker_names:
        return ""
    names = " ".join(shlex.quote(n) for n in worker_names)
    expected = len(worker_names)
    return (
        f'{i}echo "[{cluster_key}] verifying {expected} deferred worker(s) Ready"\n'
        f"{i}ready=0\n"
        f"{i}for _try in $(seq 1 10); do\n"
        f"{i}  {_APPROVE_PENDING_CSRS}\n"
        f"{i}  ready=0\n"
        f"{i}  for _node in {names}; do\n"
        f'{i}    _st=$(oc get node "$_node" -o jsonpath='
        f"'{{.status.conditions[?(@.type==\"Ready\")].status}}' 2>/dev/null || true)\n"
        f'{i}    [ "$_st" = "True" ] && ready=$((ready + 1))\n'
        f"{i}  done\n"
        f'{i}  echo "[{cluster_key}] deferred workers Ready: $ready/{expected}"\n'
        f'{i}  [ "$ready" -ge {expected} ] && break\n'
        f"{i}  sleep 10\n"
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
    lines.append(_wait_before_worker_join_cmd(f"{i}  ", cluster_key).rstrip())
    lines.append(_worker_join_preflight_cmd(f"{i}  ", cluster_key).rstrip())
    lines.append(f'{i}  echo "[{cluster_key}] joining workers in parallel"')
    lines.append(f"{i}  worker_pids=()")
    for idx, worker in enumerate(workers):
        lines.append(
            _worker_join_and_boot_block(
                f"{i}  ",
                cluster_key,
                worker,
                f"nodes/{worker['name']}",
                base_port + 100 + idx,
                serving_ip,
            ).rstrip()
        )
    lines.extend(
        [
            f"{i}  join_fail=0",
            f'{i}  for p in "${{worker_pids[@]}}"; do wait "$p" || join_fail=1; done',
            f'{i}  [ "$join_fail" = 0 ] || {{ echo "[{cluster_key}] worker join failed"; exit 1; }}',
        ]
    )
    worker_names = [w["name"] for w in workers]
    lines.append(
        _wait_for_worker_nodes_cmd(f"{i}  ", cluster_key, worker_names).rstrip()
    )
    lines.append(f"{i}  echo '[{cluster_key}] deferred workers joined'")
    lines.append(
        _wait_for_workers_converged_cmd(f"{i}  ", cluster_key, worker_names).rstrip()
    )
    lines.append(f"{i}  touch .deferred-workers-joined")
    lines.append(f"{i}fi")
    return "\n".join(lines) + "\n"
