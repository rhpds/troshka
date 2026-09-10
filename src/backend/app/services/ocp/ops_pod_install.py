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
    "recert failed",  # recert-mode block's fail-closed exit (kubeconfig/gate)
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


def inject_dead_pod_failures(
    per_cluster_log_or_status: dict[str, str], pod_running: bool
) -> dict[str, str]:
    """Pure: force non-terminal clusters to ``failed`` when the ops pod is dead.

    If the ops pod/container is no longer running (``pod_running`` False), any
    cluster whose log/status is NOT already terminal (``complete``/``failed``)
    can never finish, so it is replaced with the exact ``"failed"`` phase. This
    feeds the state machine a terminal signal — a crashed install reports
    ``failed`` immediately instead of spinning to the install timeout. When the
    pod is still running the mapping is returned unchanged; terminal clusters are
    always preserved (a cluster that already completed is never clobbered).
    """
    if pod_running:
        return per_cluster_log_or_status
    result: dict[str, str] = {}
    for cid, value in per_cluster_log_or_status.items():
        if _phase_from_input(value) in (PHASE_COMPLETE, PHASE_FAILED):
            result[cid] = value
        else:
            result[cid] = PHASE_FAILED
    return result


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
    serving_ip: str | None = None,
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
        # Truncate once on (re)start, then reopen in APPEND mode: the kubeconfig
        # delivery thread appends breadcrumbs with '>>' (O_APPEND) concurrently, and
        # a non-append 'exec >' here would overwrite its bytes at our stale offset.
        f"  : > {cluster_dir}/install.log\n"
        f"  exec >> {cluster_dir}/install.log 2>&1\n"
        "  set -e\n"
        "  set -o pipefail\n"
        f'  echo "[{cluster_key}] starting agent-based install"\n'
        f"  cd {cluster_dir}\n"
        # Idempotency guard: a restarted pod (restart_policy=always) must not
        # re-run the installer for a cluster whose install ACTUALLY completed.
        # Key on the post-install sentinel (written only after `wait-for
        # install-complete` succeeds), NOT auth/kubeconfig — `agent create image`
        # writes auth/kubeconfig up front, before any node boots, so a failure
        # after create-image (e.g. an unreachable BMC) would otherwise latch a
        # permanent fake "already installed" skip and hang forever. `exit 0` here
        # exits ONLY this cluster's subshell as success (the block is `( ... ) &`),
        # so the top-level per-PID join sees it as a success.
        f"  if [ -f {cluster_dir}/.install-complete ]; then "
        f'echo "[{cluster_key}] already installed, skipping"; exit 0; fi\n'
        # `agent create image` (--dir .) CONSUMES install-config/agent-config, so
        # they must be regular, deletable files. They are delivered read-only into
        # `.src` (a bind mount cannot be removed -> EBUSY); copy them into the
        # working dir each run so a restart restores them after a prior consume.
        "  cp -f .src/install-config.yaml .src/agent-config.yaml ./\n"
        f"  BMC_PASS={shlex.quote(bmc_password)}\n"
        # Initialise HTTP_PID before the trap: under `set -u` a failure before the
        # ISO server starts would otherwise abort the trap with "unbound variable".
        '  HTTP_PID=""\n'
        "  trap 'kill $HTTP_PID 2>/dev/null || true' EXIT\n"
        + _agent_create_image_cmd("  ", "openshift-install", "create-image.log")
        + "  echo 'Agent ISO created. Serving via HTTP and booting nodes...'\n"
        + _serve_iso_cmd("  ", cluster_dir, port, serving_ip=serving_ip)
        + _redfish_insert_media_cmd("  ", bmc_ips_str)
        + "  echo 'Waiting for cluster installation to complete...'\n"
        + _wait_for_complete_cmd("  ", "openshift-install", ".")
        + "  echo 'Ejecting agent ISO from nodes...'\n"
        + _redfish_eject_media_cmd("  ", bmc_ips_str)
        # Completion sentinel: only reached when wait-for succeeded (set -e), so a
        # restarted pod skips ONLY a genuinely-installed cluster (see the guard).
        + f"  touch {cluster_dir}/.install-complete\n"
        + f'  echo "[{cluster_key}] install complete"\n'
        + ") &\n"
        + "pids+=($!)\n"
    )


def _recert_cluster_block(cluster_key: str, workdir: str, mode: str) -> str:
    """One cluster's RECERT (pattern-deploy) steps, backgrounded.

    NO fresh install — the disks are already installed and were recerted offline
    (guestfish kubelet-PKI wipe). This block:

    - waits for the backend to deliver the node's live ``lb-ext.kubeconfig`` at
      ``<dir>/auth/kubeconfig`` (pulled via an offline snapshot+guestfish read).
      Both the captured kubeconfig's server AND client CAs roll on recert and
      oauth (ingress :443) is typically down, so the delivered admin cert — which
      authenticates to :6443 with NO oauth — is the only usable credential. Waits
      for it to appear AND authenticate; FAILS CLOSED (no fallback) if it never
      arrives;
    - approves pending CSRs until nodes go Ready (kubelets re-bootstrap after the
      PKI wipe);
    - forces a kube-apiserver redeploy (ALL clusters, SNO included) to recover
      post-recert API-aggregation trust;
    - FAIL-CLOSED readiness gate: every cluster operator Available=True /
      Degraded=False INCLUDING ``authentication`` (oauth) and ``console`` — an
      empty/failed ``oc`` is NEVER treated as healthy;
    - only on genuine success writes ``<dir>/auth/kubeadmin-password`` and emits
      ``install complete`` (so ``_store_ops_pod_creds`` harvests the FRESH
      kubeconfig into the showroom terminal); otherwise exits non-zero.

    Writes to ``<dir>/install.log`` (same as the install path) so the live
    monitor tails it unchanged.
    """
    cluster_dir = f"{workdir}/{cluster_key}"
    _ = mode  # (was multinode-only; the redeploy is now needed for SNO too)
    # Force a kube-apiserver redeploy on EVERY recert (SNO included). Post-recert
    # the API-aggregation trust (requestheader CA / extension-apiserver-
    # authentication) needs the kube-apiserver kicked to repopulate; without it
    # route.openshift.io stays flaky, the router can't list routes (has-synced
    # fails), :443 never serves, and the console/oauth hang at 503 forever. SNO
    # skipping this was THE recert instability (a stuck SNO recovered the instant
    # the redeploy was forced).
    redeploy = (
        "  echo 'Forcing kube-apiserver redeploy "
        "(recover API-aggregation trust + fresh serving)...'\n"
        "  oc patch kubeapiserver cluster --type=merge "
        '-p "{\\"spec\\":{\\"forceRedeploymentReason\\":'
        '\\"recert-$(date +%s)\\"}}" >/dev/null 2>&1 || true\n'
    )
    head = (
        f"# ===== cluster {cluster_key} =====\n"
        "(\n"
        # Truncate once on (re)start, then reopen in APPEND mode: the kubeconfig
        # delivery thread appends breadcrumbs with '>>' (O_APPEND) concurrently, and
        # a non-append 'exec >' here would overwrite its bytes at our stale offset.
        f"  : > {cluster_dir}/install.log\n"
        f"  exec >> {cluster_dir}/install.log 2>&1\n"
        f'  echo "[{cluster_key}] Waiting for cluster installation to complete (recert)"\n'
        f"  mkdir -p {cluster_dir}/auth\n"
        # oauth (ingress :443) is typically down on a freshly-recert'd cluster, so
        # we CANNOT oc-login — we need a client-cert admin kubeconfig. Two arrive:
        # the CAPTURED one ({dir}/kubeconfig, long-lived admin-kubeconfig-signer
        # client cert) and the DELIVERED lb-ext one ({dir}/auth/kubeconfig, pulled
        # via offline snapshot+guestfish). NEITHER is universally valid: server and
        # client CAs roll differently per provider (on ocpvirt the captured one
        # works while lb-ext is stale; on KubeVirt it's the reverse). So try BOTH
        # and use whichever authenticates. Strip the embedded CA + set insecure so
        # a rotated SERVER cert never fails selection (auth rests on the client
        # cert; the nested endpoint is trusted). The winner is written to
        # auth/kubeconfig so _store_ops_pod_creds harvests it for the terminal.
        f"  export KUBECONFIG={cluster_dir}/auth/kubeconfig\n"
        "  li=''\n"
        "  for i in $(seq 1 180); do "
        f"for src in {cluster_dir}/kubeconfig {cluster_dir}/auth/kubeconfig; do "
        '[ -s "$src" ] || continue; '
        f'cp "$src" {cluster_dir}/.recert-try 2>/dev/null || continue; '
        f"cl=$(KUBECONFIG={cluster_dir}/.recert-try oc config view "
        "-o jsonpath='{.clusters[0].name}' 2>/dev/null); "
        '[ -n "$cl" ] || continue; '
        f"KUBECONFIG={cluster_dir}/.recert-try oc config unset "
        '"clusters.$cl.certificate-authority-data" >/dev/null 2>&1; '
        f'KUBECONFIG={cluster_dir}/.recert-try oc config set-cluster "$cl" '
        "--insecure-skip-tls-verify=true >/dev/null 2>&1; "
        f"KUBECONFIG={cluster_dir}/.recert-try oc get nodes >/dev/null 2>&1 && "
        f'{{ mv {cluster_dir}/.recert-try "$KUBECONFIG"; li=1; break; }}; '
        "done; "
        '[ -n "$li" ] && break; '
        "sleep 10; done\n"
        f"  rm -f {cluster_dir}/.recert-try\n"
        # No fallback: if NEITHER kubeconfig ever authenticates, fail closed.
        f'  [ -n "$li" ] || {{ echo "[{cluster_key}] recert failed: no working '
        'admin kubeconfig (captured or delivered)"; exit 1; }\n'
        f'  echo "[{cluster_key}] admin kubeconfig received; approving pending CSRs"\n'
        # Approve CSRs until all nodes are Ready (kubelet re-bootstrap after wipe).
        "  for i in $(seq 1 120); do "
        "oc get csr -o name 2>/dev/null | xargs -r oc adm certificate approve "
        ">/dev/null 2>&1 || true; "
        "total=$(oc get nodes --no-headers 2>/dev/null | wc -l); "
        "notready=$(oc get nodes --no-headers 2>/dev/null | grep -vc ' Ready'); "
        '[ "$total" -gt 0 ] && [ "$notready" = 0 ] && '
        f'{{ echo "[{cluster_key}] all $total node(s) Ready"; break; }}; '
        f'echo "[{cluster_key}] approving CSRs: $notready/$total node(s) not Ready"; '
        "sleep 10; done\n"
        # POD REAPER: pods restored from the captured etcd are ZOMBIES after the
        # cluster's post-recert crypto rotation — still Running but holding stale
        # in-memory SA tokens / cert trust / API connections (router won't bind
        # :443, kubelet :10250 logs fail, console down). They are NOT crashlooping,
        # so a health-based reap misses them: recreate ALL workload pods ONCE so
        # they re-read fresh creds. Skip static control-plane namespaces
        # (kubelet-owned; deleting via API blips the apiserver), Completed pods,
        # and image-pull failures (no catalog mirror in a lab). `--force
        # --grace-period=0` avoids the hostNetwork router Pending<->Terminating
        # deadlock (a stuck-Terminating pod holds :443 so the new one can't bind).
        f'  echo "[{cluster_key}] recreating pods to clear stale post-recert state (zombies)"\n'
        "  oc get pods -A --no-headers 2>/dev/null | awk '"
        "$1 ~ /^openshift-(etcd|kube-apiserver|kube-controller-manager|kube-scheduler)$/ {next} "
        "$4 ~ /ImagePull|ErrImage|Completed/ {next} "
        '{print $1" "$2}\' | while read rns rpod; do '
        'oc delete pod "$rpod" -n "$rns" --force --grace-period=0 --wait=false '
        ">/dev/null 2>&1 || true; done\n"
        f'  echo "[{cluster_key}] pods recreated; waiting for cluster operators (incl oauth + console)"\n'
    )
    gate = (
        # FAIL-CLOSED: oc must WORK (non-empty co list) AND all operators healthy,
        # incl authentication (oauth/login) + console. Empty output != healthy.
        "  ready=''\n"
        "  for i in $(seq 1 160); do "
        # Keep approving CSRs: the pod reaper's recreated pods (e.g. monitoring)
        # and the kubelet's re-bootstrap issue fresh CSRs that must be approved
        # for their operators to go Available.
        "oc get csr -o name 2>/dev/null | xargs -r oc adm certificate approve "
        ">/dev/null 2>&1 || true; "
        "out=$(oc get co --no-headers 2>/dev/null); "
        '[ -z "$out" ] && { sleep 15; continue; }; '
        # Don't block on slow-settling operators that aren't needed for a usable
        # console/login: monitoring (prometheus/metrics-server) and OLM
        # packageserver (catalog-dependent). They converge on their own; gating on
        # them just delays "ready" for no user-visible benefit.
        'bad=$(echo "$out" | awk \'$1=="monitoring"||$1=="operator-lifecycle-manager-packageserver"{next} $3!="True"||$5=="True"{c++} END{print c+0}\'); '
        'auth=$(echo "$out" | awk \'$1=="authentication"&&$3=="True"&&$5=="False"{c++} END{print c+0}\'); '
        'con=$(echo "$out" | awk \'$1=="console"&&$3=="True"&&$5=="False"{c++} END{print c+0}\'); '
        # Verify the console route ACTUALLY HTTP-responds (:443 serving), not just
        # that the operator reports Available — a "zombie" router leaves the
        # operator Available while :443 is refused. curl code 000 == no connection.
        "chost=$(oc get route console -n openshift-console "
        "-o jsonpath='{.spec.host}' 2>/dev/null); "
        'ccode=$(curl -sk --max-time 8 -o /dev/null -w "%{http_code}" '
        '"https://$chost/" 2>/dev/null); '
        'resp=0; [ -n "$chost" ] && [ "$ccode" != "000" ] && '
        '[ "$ccode" -ge 200 ] 2>/dev/null && resp=1; '
        '[ "$bad" = 0 ] && [ "$auth" = 1 ] && [ "$con" = 1 ] && [ "$resp" = 1 ] && '
        "{ ready=1; break; }; "
        # Log which operators are still not ready + the console HTTP status so the
        # showroom log is informative. Keep them on SEPARATE lines: the frontend
        # parses everything after "waiting on operators:" as operator names, so the
        # console status must not share that line (it'd render as fake operators).
        'nr=$(echo "$out" | awk \'$1=="monitoring"||$1=="operator-lifecycle-manager-packageserver"{next} $3!="True"||$5=="True"{printf "%s ",$1}\'); '
        f'echo "[{cluster_key}] waiting on operators: ${{nr:-none}}"; '
        f'echo "[{cluster_key}] console http=$ccode"; '
        "sleep 15; done\n"
    )
    tail = (
        '  if [ -n "$li" ] && [ -n "$ready" ]; then\n'
        f"    cp {cluster_dir}/kubeadmin-password {cluster_dir}/auth/kubeadmin-password "
        "2>/dev/null || true\n"
        f'    echo "[{cluster_key}] install complete"\n'
        "  else\n"
        f'    echo "[{cluster_key}] recert failed: cluster not ready '
        '(login=$li operators=$ready)"\n'
        "    exit 1\n"
        "  fi\n"
        ") &\n"
        "pids+=($!)\n"
    )
    return head + redeploy + gate + tail


def build_ops_pod_recert_script(
    clusters: list[dict],
    workdir: str,
    mode_by_cluster: dict[str, str],
    net_ip_assignments: list[tuple[str, str]] | None = None,
) -> str:
    """Ops-pod script for a PATTERN (recert) deploy — recert, never reinstall.

    Mirrors :func:`build_ops_pod_install_script`'s structure (parallel per-cluster
    subshells, per-PID join, hold-on-success for credential handling) but each
    block runs :func:`_recert_cluster_block` instead of a fresh agent install.
    ``mode_by_cluster`` maps a cluster key to ``"sno"`` or ``"multinode"`` — only
    multi-node forces the kube-apiserver redeploy. The admin kubeconfig is
    injected per cluster at ``<workdir>/<key>/kubeconfig``.
    """
    parts: list[str] = [
        "#!/bin/bash\n",
        "# Per-cluster OCP recert runner (ops pod) — pattern deploy, no reinstall.\n",
        "set -u\n",
        "\n",
        _self_assign_net_ips(net_ip_assignments),
        _ensure_installers_cmd(),
        "\n",
        "pids=()\n",
    ]
    for cluster in clusters:
        key = _cluster_key(cluster)
        parts.append(
            _recert_cluster_block(key, workdir, mode_by_cluster.get(key, "multinode"))
        )
    parts.append("\n")
    parts.append("fail=0\n")
    parts.append('for p in "${pids[@]}"; do wait "$p" || fail=1; done\n')
    # Hold on success (like the install path) so the monitor can mark ready and
    # reap the pod without racing a restart loop.
    parts.append('if [ "$fail" = 0 ]; then\n')
    parts.append('  echo "All clusters recerted. Holding..."\n')
    parts.append("  sleep infinity\n")
    parts.append("fi\n")
    parts.append("exit 1\n")
    return "".join(parts)


def _self_assign_net_ips(net_ip_assignments: list[tuple[str, str]] | None) -> str:
    """`ip addr add` lines for the ops pod's lab-net interfaces.

    KubeVirt secondary networks are OVN-L2 NADs with no IPAM, so a pod attached
    to them gets no IP unless it self-assigns one (the ops pod has NET_ADMIN,
    mirroring how the BMC/sushy pod adds SUSHY_BMC_IPS to net1). Without this the
    ops pod can't reach the node BMC or serve the agent ISO on the lab network.
    Empty for the troshkad path (its ops pod already has bridge IPs).
    """
    if not net_ip_assignments:
        return ""
    lines = ["# Self-assign lab-network IPs (OVN-L2 NADs have no IPAM).\n"]
    for iface, cidr in net_ip_assignments:
        lines.append(f"ip addr add {cidr} dev {iface} 2>/dev/null || true\n")
        lines.append(f"ip link set {iface} up 2>/dev/null || true\n")
    return "".join(lines)


def build_ops_pod_install_script(
    clusters: list[dict],
    bmc_by_cluster: dict[str, tuple[list[str], str]],
    ocp_version: str,
    workdir: str,
    net_ip_assignments: list[tuple[str, str]] | None = None,
    serving_ip: str | None = None,
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
        _self_assign_net_ips(net_ip_assignments),
        _ensure_installers_cmd(),
        "\n",
        "pids=()\n",
    ]
    for index, cluster in enumerate(clusters):
        key = _cluster_key(cluster)
        bmc_ips, bmc_password = bmc_by_cluster.get(key, ([], ""))
        parts.append(
            _cluster_install_block(
                key,
                bmc_ips,
                bmc_password,
                _BASE_ISO_PORT + index,
                workdir,
                serving_ip=serving_ip,
            )
        )
    parts.append("\n")
    parts.append(
        "# Wait on each cluster individually so a failed install exits non-zero.\n"
    )
    parts.append("fail=0\n")
    parts.append('for p in "${pids[@]}"; do wait "$p" || fail=1; done\n')
    # On success, HOLD the container running instead of exiting. The pod is
    # restart_policy=always; if we exited 0 it would restart, hit the per-cluster
    # skip-guard, exit again — a restart loop that makes `podman exec` (the
    # monitor's credential harvest of auth/kubeconfig + auth/kubeadmin-password)
    # race and intermittently fail. Holding keeps the container exec-able until
    # the monitor harvests creds and reaps the pod. On failure we still exit 1 so
    # dead-pod detection works and the pod is left for debugging.
    parts.append('if [ "$fail" = 0 ]; then\n')
    parts.append('  echo "All clusters installed. Holding for credential harvest..."\n')
    parts.append("  sleep infinity\n")
    parts.append("fi\n")
    parts.append("exit 1\n")
    return "".join(parts)
