"""Per-cluster ops-pod install-runner script generator (Plan 4, Task 5).

The in-cluster *ops pod* replaces the bastion for bastionless / multi-cluster OCP
installs. This module generates the bash script the pod runs: it reproduces the
bastion's exact OpenShift agent-based install steps — download the client tools,
``openshift-install agent create image``, serve the ISO over HTTP, drive each
node's BMC over Redfish (InsertMedia + ForceRestart), ``agent wait-for
install-complete``, then eject — but once *per cluster*, in parallel, each block
consuming the per-cluster ``install-config``/``agent-config`` already materialised
into ``<workdir>/<clusterId>/`` by the pod-create runner (Task 4).

The Redfish/serve/wait-for/create-image command strings are shared with the
bastion installer (:mod:`app.services.ocp.agent_template`) so behavior stays one
source of truth. Everything here is pure string generation and unit-testable via
the produced script text; actual execution is a live-environment concern.
"""

from __future__ import annotations

import ipaddress
import shlex

from app.services.ocp.agent_template import (
    _agent_create_image_cmd,
    _cluster_members_for,
    _installer_tarball_url,
    _node_role,
    _redfish_eject_media_cmd,
    _redfish_insert_media_cmd,
    _serve_iso_cmd,
    _wait_for_complete_cmd,
)

# Base HTTP port for serving each cluster's agent ISO; incremented per cluster so
# parallel installs never collide on the same listen port.
_BASE_ISO_PORT = 8080

# ── Task 7: per-cluster install-progress state machine ─────────────────────
#
# The install-runner script (:func:`build_ops_pod_install_script`) writes a
# per-cluster ``<workdir>/<clusterId>/install.log``. The live monitor
# (``deploy_service._monitor_ops_pod_install``) tails those logs — that timing /
# exec loop is a live-environment concern — but the parsing of a cluster's log
# text into an install *phase*, and the aggregation of per-cluster phases into an
# overall status + done/failed decision, is PURE and unit-tested here.

# Ordered install phases (ranked). ``failed``/``cancelled`` are terminal and sit
# outside the linear rank.
PHASE_CREATING_IMAGE = "creating-image"
PHASE_BOOTING = "booting"
PHASE_WAITING = "waiting"
PHASE_COMPLETE = "complete"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"

_PHASE_RANK = {
    PHASE_CREATING_IMAGE: 0,
    PHASE_BOOTING: 1,
    PHASE_WAITING: 2,
    PHASE_COMPLETE: 3,
}
_RANK_TO_PHASE = {rank: phase for phase, rank in _PHASE_RANK.items()}

# Any exact phase string is passed through as-is (the caller may supply an
# authoritative status — e.g. the troshkad job failed → "failed").
_KNOWN_PHASES = set(_PHASE_RANK) | {PHASE_FAILED}

# Log markers → phase, scanned furthest-progressed first (see the install
# script's per-cluster ``echo`` breadcrumbs). "install complete" wins outright.
_LOG_MARKERS = (
    ("install complete", PHASE_COMPLETE),
    ("Waiting for cluster installation to complete", PHASE_WAITING),
    ("Agent ISO created", PHASE_BOOTING),
    ("booting nodes", PHASE_BOOTING),
    ("starting agent-based install", PHASE_CREATING_IMAGE),
)

# Fatal-failure markers (only consulted when the log has NOT reached "complete").
_FAILURE_MARKERS = (
    "level=fatal",
    "install-complete command failed",
    "installation failed",
    "failed to wait for install",
)


def _phase_from_input(value: str) -> str:
    """Map one cluster's raw log text (or an exact phase string) to a phase.

    Priority: an exact known-phase string passes through; otherwise a
    ``complete`` marker wins outright, then a fatal-failure marker, then the
    furthest-progressed log marker; default ``creating-image`` (started but no
    breadcrumb yet).
    """
    text = value or ""
    if text in _KNOWN_PHASES:
        return text
    lowered = text.lower()
    if "install complete" in lowered:
        return PHASE_COMPLETE
    if any(marker in lowered for marker in _FAILURE_MARKERS):
        return PHASE_FAILED
    for marker, phase in _LOG_MARKERS:
        if marker.lower() in lowered:
            return phase
    return PHASE_CREATING_IMAGE


def _aggregate_in_progress(clusters: dict[str, str]) -> str:
    """Overall phase while still in progress: the least-advanced cluster.

    One cluster ``complete`` + another ``waiting`` → overall ``waiting`` (the
    aggregate can't be ahead of its slowest cluster).
    """
    if not clusters:
        return PHASE_CREATING_IMAGE
    min_rank = min(_PHASE_RANK.get(phase, 0) for phase in clusters.values())
    return _RANK_TO_PHASE[min_rank]


def ops_pod_install_progress(
    per_cluster_log_or_status: dict[str, str], cancelled: bool = False
) -> dict:
    """Pure state machine: per-cluster log/status → aggregate install progress.

    Args:
        per_cluster_log_or_status: ``{clusterId: install.log text OR exact phase}``.
        cancelled: whether a cancel signal has fired for this project.

    Returns ``{clusters: {id: phase}, overall: phase, done: bool, failed: [...]}``.
    Decision precedence: cancelled → failed (any) → complete (all) → in-progress.
    ``done`` is True for the three terminal overalls (cancelled/failed/complete).
    """
    clusters = {
        cid: _phase_from_input(value)
        for cid, value in per_cluster_log_or_status.items()
    }
    failed = sorted(cid for cid, phase in clusters.items() if phase == PHASE_FAILED)

    if cancelled:
        overall, done = PHASE_CANCELLED, True
    elif failed:
        overall, done = PHASE_FAILED, True
    elif clusters and all(phase == PHASE_COMPLETE for phase in clusters.values()):
        overall, done = PHASE_COMPLETE, True
    else:
        overall, done = _aggregate_in_progress(clusters), False

    return {"clusters": clusters, "overall": overall, "done": done, "failed": failed}


def ops_pod_progress_items(progress: dict) -> list[str]:
    """Render a progress dict's per-cluster phases as sorted ``"id: phase"`` lines
    (the item list the deploy-progress UI shows)."""
    clusters = progress.get("clusters", {})
    return [f"{cid}: {clusters[cid]}" for cid in sorted(clusters)]


def _cluster_key(cluster: dict) -> str:
    """Workdir-relative key for a cluster (id, else name), matching the scaffold."""
    return str(cluster.get("id") or cluster.get("name") or "cluster")


def _bmc_password_from_topology(topology: dict) -> str:
    """Read the BMC password from the topology's BMC network node (else '')."""
    for tnode in topology.get("nodes", []):
        td = tnode.get("data", {})
        if td.get("networkType") == "bmc" and td.get("bmcPassword"):
            return str(td["bmcPassword"])
    return ""


def bmc_for_cluster(topology: dict, cluster: dict) -> tuple[list[str], str]:
    """Collect ``(bmc_ips, bmc_password)`` scoped to one cluster's members.

    Mirrors :func:`agent_template._collect_bmc_ips_and_password` but restricts
    the BMC IPs to the given cluster's member VM nodes (via
    :func:`agent_template._cluster_member_nodes`), so a multi-cluster topology
    never leaks one cluster's BMCs into another's Redfish loop. The password is
    read from the topology's BMC network node (shared across clusters).
    """
    members = _cluster_members_for(topology, cluster)
    bmc_ips: list[str] = []
    for node in members:
        td = node.get("data", {})
        if (
            td.get("bmcEnabled")
            and td.get("bmcIp")
            and _node_role(node) in ("control-plane", "worker")
        ):
            bmc_ips.append(str(ipaddress.IPv4Address(td["bmcIp"])))
    return bmc_ips, _bmc_password_from_topology(topology)


def _ensure_installers_cmd() -> str:
    """Ensure ``oc``/``openshift-install`` on PATH, downloading if absent.

    Uses the same OCP client mirror URL the bastion installer uses (shared via
    :func:`agent_template._installer_tarball_url`); a no-op when the tools are
    already baked into the ops-pod execution environment image.
    """
    return (
        "# Ensure oc / openshift-install present (baked into the EE image, else\n"
        "# download from the same OCP client mirror the bastion installer uses).\n"
        "if ! command -v openshift-install >/dev/null 2>&1; then\n"
        '  echo "Downloading openshift-install $OCP_VERSION..."\n'
        f"  curl -L -o /tmp/openshift-install.tar.gz {_installer_tarball_url('openshift-install-linux.tar.gz')}\n"
        "  tar xzf /tmp/openshift-install.tar.gz -C /usr/local/bin openshift-install && rm -f /tmp/openshift-install.tar.gz\n"
        "fi\n"
        "if ! command -v oc >/dev/null 2>&1; then\n"
        '  echo "Downloading oc client..."\n'
        f"  curl -L -o /tmp/openshift-client.tar.gz {_installer_tarball_url('openshift-client-linux.tar.gz')}\n"
        "  tar xzf /tmp/openshift-client.tar.gz -C /usr/local/bin oc kubectl && rm -f /tmp/openshift-client.tar.gz\n"
        "fi\n"
    )


def _cluster_install_block(
    cluster_key: str,
    bmc_ips: list[str],
    bmc_password: str,
    port: int,
    workdir: str,
) -> str:
    """One cluster's install steps, wrapped in a backgrounded subshell.

    The subshell isolates the per-cluster shell state (``BMC_PASS``,
    ``HTTP_PID``, ``ISO_URL``, ``SYS_ID``) so parallel clusters never clobber
    each other, redirects its output to a per-cluster log, and reuses the exact
    bastion command strings (create-image / serve / Redfish / wait-for / eject).

    ``set -e`` + ``set -o pipefail`` make a failed ``wait-for install-complete``
    fatal for THIS cluster (mirroring the bastion's ``PIPESTATUS``/``exit 1``):
    the awk pipeline no longer masks openshift-install's non-zero exit, so the
    subshell exits non-zero and its ``$!`` wait propagates the failure. A
    ``trap`` reaps the ISO HTTP server on any exit (success or failure). The
    trailing ``pids+=($!)`` records this cluster's PID for the top-level join.
    """
    cluster_dir = f"{workdir}/{cluster_key}"
    bmc_ips_str = " ".join(bmc_ips)
    return (
        f"# ===== cluster {cluster_key} =====\n"
        "(\n"
        f"  exec > {cluster_dir}/install.log 2>&1\n"
        "  set -e\n"
        "  set -o pipefail\n"
        f'  echo "[{cluster_key}] starting agent-based install"\n'
        f"  cd {cluster_dir}\n"
        # Idempotency guard: a restarted pod (restart_policy=always) must not
        # re-run the installer for a cluster that already produced a kubeconfig.
        # `exit 0` here exits ONLY this cluster's subshell as success (the block
        # is `( ... ) &`), so the top-level per-PID join sees it as a success.
        f"  if [ -f {cluster_dir}/auth/kubeconfig ]; then "
        f'echo "[{cluster_key}] already installed, skipping"; exit 0; fi\n'
        f"  BMC_PASS={shlex.quote(bmc_password)}\n"
        "  trap 'kill $HTTP_PID 2>/dev/null || true' EXIT\n"
        + _agent_create_image_cmd("  ", "openshift-install", "create-image.log")
        + "  echo 'Agent ISO created. Serving via HTTP and booting nodes...'\n"
        + _serve_iso_cmd("  ", cluster_dir, port)
        + _redfish_insert_media_cmd("  ", bmc_ips_str)
        + "  echo 'Waiting for cluster installation to complete...'\n"
        + _wait_for_complete_cmd("  ", "openshift-install", ".")
        + "  echo 'Ejecting agent ISO from nodes...'\n"
        + _redfish_eject_media_cmd("  ", bmc_ips_str)
        + f'  echo "[{cluster_key}] install complete"\n'
        + ") &\n"
        + "pids+=($!)\n"
    )


def build_ops_pod_install_script(
    clusters: list[dict],
    bmc_by_cluster: dict[str, tuple[list[str], str]],
    ocp_version: str,
    workdir: str,
) -> str:
    """Generate the ops-pod bash script that installs every cluster in parallel.

    ``clusters`` are the cluster-shaped dicts (``id``/``name``); their
    install-config/agent-config are assumed already materialised into
    ``<workdir>/<clusterId>/`` by the pod-create runner. ``bmc_by_cluster`` maps
    each cluster key to its ``(bmc_ips, bmc_password)`` (see
    :func:`bmc_for_cluster`). Each cluster gets its own HTTP port
    (``8080 + index``) so the ISO servers don't collide, and each install block
    is a backgrounded subshell whose PID is captured; a final loop waits on each
    PID individually and propagates failure, so the script exits non-zero if ANY
    cluster install failed (Task 7's monitor relies on this — a bare ``wait``
    would always return 0 and mask a failed install).
    """
    parts: list[str] = [
        "#!/bin/bash\n",
        "# Per-cluster OCP agent-based install runner (ops pod).\n",
        "# Each cluster installs in parallel; see <workdir>/<clusterId>/install.log.\n",
        "set -u\n",
        "set -o pipefail\n",
        f"OCP_VERSION={ocp_version}\n",
        "\n",
        _ensure_installers_cmd(),
        "\n",
        "pids=()\n",
    ]
    for index, cluster in enumerate(clusters):
        key = _cluster_key(cluster)
        bmc_ips, bmc_password = bmc_by_cluster.get(key, ([], ""))
        parts.append(
            _cluster_install_block(
                key, bmc_ips, bmc_password, _BASE_ISO_PORT + index, workdir
            )
        )
    parts.append("\n")
    parts.append(
        "# Wait on each cluster individually so a failed install exits non-zero.\n"
    )
    parts.append("fail=0\n")
    parts.append('for p in "${pids[@]}"; do wait "$p" || fail=1; done\n')
    parts.append("exit $fail\n")
    return "".join(parts)
