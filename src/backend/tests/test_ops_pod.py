"""Tests for the ops-pod network + spec builder (Plan 4, Task 4).

Pure payload-shaping tests — no live pod is created here. The shaped dict is
consumed later by `_pod_create_params`-style logic (Task 5) to actually create
the in-cluster ops pod.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.deploy_topology import showroom_infra_network
from app.services.ocp.ops_pod_scaffold import (
    OPS_POD_WORKDIR,
    ops_pod_config_files,
    ops_pod_infra_network,
)


def _vni_map() -> dict:
    # min VNI -> octet3 == 0x0A == 10
    return {"net-a": 4106, "net-b": 4200}


def test_ops_pod_infra_network_is_infra_transit_with_distinct_ip():
    nets = ops_pod_infra_network(_vni_map(), mac="52:54:00:aa:bb:cc")
    assert len(nets) == 1
    net = nets[0]
    assert net["infra_transit"] is True
    # Distinct pod IP (.4) so it coexists with the showroom pod (.3).
    assert net["ip"].endswith(".4")
    assert net["ip"] == "172.30.10.4"
    assert net["cidr"] == "172.30.10.0/24"
    assert net["gateway"] == "172.30.10.2"
    assert net["mac"] == "52:54:00:aa:bb:cc"


def test_ops_pod_ip_distinct_from_showroom_ip():
    ops = ops_pod_infra_network(_vni_map())
    show = showroom_infra_network(_vni_map())
    assert ops[0]["ip"] != show[0]["ip"]
    assert ops[0]["ip"].endswith(".4")
    assert show[0]["ip"].endswith(".3")
    # Same subnet / gateway so both pods share the transit net.
    assert ops[0]["cidr"] == show[0]["cidr"]
    assert ops[0]["gateway"] == show[0]["gateway"]


def test_ops_pod_infra_network_carries_dns_nameserver():
    nets = ops_pod_infra_network(_vni_map(), dns_nameserver="10.0.0.53")
    assert nets[0]["dns_nameserver"] == "10.0.0.53"


def test_ops_pod_infra_network_empty_when_no_vni():
    assert ops_pod_infra_network({}) == []


def _clusters() -> list[dict]:
    return [
        {
            "id": "cl-1",
            "name": "prod",
            "_generatedInstallConfig": "install: prod\n",
            "_generatedAgentConfig": "agent: prod\n",
        },
        {
            "id": "cl-2",
            "name": "edge",
            "_generatedInstallConfig": "install: edge\n",
            "_generatedAgentConfig": "agent: edge\n",
        },
    ]


def _files():
    return ops_pod_config_files(_clusters(), OPS_POD_WORKDIR, '{"auths":{}}')


def test_ops_pod_config_files_absolute_paths_scoped_by_cluster():
    # Task 8: files are mounted at their ABSOLUTE workdir paths (troshkad
    # bind-mounts each read-only at container_path), scoped by cluster id.
    files = _files()
    wd = OPS_POD_WORKDIR
    assert files[f"{wd}/cl-1/.src/install-config.yaml"] == "install: prod\n"
    assert files[f"{wd}/cl-1/.src/agent-config.yaml"] == "agent: prod\n"
    assert files[f"{wd}/cl-2/.src/install-config.yaml"] == "install: edge\n"
    assert files[f"{wd}/cl-2/.src/agent-config.yaml"] == "agent: edge\n"


def test_ops_pod_config_files_includes_pull_secret():
    files = _files()
    assert files[f"{OPS_POD_WORKDIR}/pull-secret.json"] == '{"auths":{}}'


def test_ops_pod_config_files_omits_empty_pull_secret():
    files = ops_pod_config_files(_clusters(), OPS_POD_WORKDIR, "")
    assert not any(k.endswith("pull-secret.json") for k in files)


def test_ops_pod_config_files_skips_missing_configs():
    clusters = [{"id": "cl-x", "name": "x"}]  # no generated configs
    files = ops_pod_config_files(clusters, OPS_POD_WORKDIR, "")
    assert files == {}


def test_ops_pod_image_default_points_at_configured_registry():
    # The baked fallback matches the CI-published image (build-images.yml
    # build-operator job) and the config.yaml `ocp.ops_pod_image` default.
    from app.services.ocp import ops_pod_scaffold

    assert (
        ops_pod_scaffold._OPS_POD_IMAGE_DEFAULT
        == "quay.io/redhat-gpte/troshka-ops-pod:latest"
    )


def test_ops_pod_image_resolves_from_ocp_config(monkeypatch):
    # When `ocp.ops_pod_image` is set in config, resolution returns it verbatim.
    import app.core.config as core_config
    from app.services.ocp import ops_pod_scaffold

    custom = "quay.io/example/troshka-ops-pod:v9"
    monkeypatch.setattr(
        core_config,
        "config",
        SimpleNamespace(ocp=SimpleNamespace(ops_pod_image=custom)),
        raising=False,
    )
    assert ops_pod_scaffold._resolve_ops_pod_image() == custom


def test_ops_pod_image_falls_back_when_config_empty(monkeypatch):
    import app.core.config as core_config
    from app.services.ocp import ops_pod_scaffold

    monkeypatch.setattr(
        core_config,
        "config",
        SimpleNamespace(ocp=SimpleNamespace(ops_pod_image="")),
        raising=False,
    )
    assert (
        ops_pod_scaffold._resolve_ops_pod_image()
        == ops_pod_scaffold._OPS_POD_IMAGE_DEFAULT
    )


# ---------------------------------------------------------------------------
# Task 5: per-cluster ops-pod install-runner script
# ---------------------------------------------------------------------------


def _bmc_member(name, cid, group, bmc_ip):
    return {
        "type": "vmNode",
        "data": {
            "name": name,
            "clusterId": cid,
            "tags": {"AnsibleGroup": group},
            "os": "rhcos",
            "bmcEnabled": True,
            "bmcIp": bmc_ip,
            "nics": [{"ip": "10.0.0.20", "mac": "52:54:00:aa:bb:01"}],
        },
    }


def test_bmc_for_cluster_scopes_to_members():
    from app.services.ocp.ops_pod_install import bmc_for_cluster

    topo = {
        "nodes": [
            {
                "type": "networkNode",
                "data": {"networkType": "bmc", "bmcPassword": "s3cret"},
            },
            _bmc_member("p-cp-0", "prod", "controllers", "192.168.100.10"),
            _bmc_member("p-cp-1", "prod", "controllers", "192.168.100.11"),
            _bmc_member("p-w-0", "prod", "workers", "192.168.100.12"),
            _bmc_member("d-cp-0", "dev", "controllers", "192.168.100.20"),
        ]
    }

    prod_ips, prod_pw = bmc_for_cluster(topo, {"id": "prod"})
    assert prod_ips == ["192.168.100.10", "192.168.100.11", "192.168.100.12"]
    assert prod_pw == "s3cret"

    dev_ips, dev_pw = bmc_for_cluster(topo, {"id": "dev"})
    assert dev_ips == ["192.168.100.20"]
    assert dev_pw == "s3cret"


def _install_script():
    from app.services.ocp.ops_pod_install import build_ops_pod_install_script

    clusters = [
        {"id": "prod", "name": "prod", "_generatedInstallConfig": "x"},
        {"id": "dev", "name": "dev", "_generatedInstallConfig": "y"},
    ]
    bmc_by_cluster = {
        # "s3 cret" has a space so shlex.quote must wrap it (injection-safety).
        "prod": (["192.168.100.10", "192.168.100.11"], "pw-prod"),
        "dev": (["192.168.100.20"], "s3 cret"),
    }
    return build_ops_pod_install_script(clusters, bmc_by_cluster, "4.20", "/workdir")


def test_install_script_per_cluster_workdirs():
    script = _install_script()
    assert "cd /workdir/prod" in script
    assert "cd /workdir/dev" in script


def test_install_script_agent_create_image_per_cluster():
    script = _install_script()
    assert script.count("agent create image --dir .") == 2


def test_install_script_copies_configs_before_create_image():
    """create image consumes (deletes) the configs, which are delivered read-only
    into .src; the script must copy them into the working dir first (a RO bind
    mount cannot be removed -> EBUSY) and copy them per cluster."""
    script = _install_script()
    assert script.count("cp -f .src/install-config.yaml .src/agent-config.yaml ./") == 2


def test_install_script_inits_http_pid_for_set_u():
    """HTTP_PID must be initialised before the EXIT trap so a failure before the
    ISO server starts does not abort under `set -u` with 'unbound variable'."""
    script = _install_script()
    assert script.count('HTTP_PID=""') == 2


def test_install_script_wait_for_complete_per_cluster():
    script = _install_script()
    assert script.count("wait-for install-complete") == 2


def test_install_script_redfish_insert_media_per_cluster_bmcs():
    script = _install_script()
    # Each cluster's Redfish loop targets its OWN BMC IPs.
    assert "for BMC_IP in 192.168.100.10 192.168.100.11; do" in script
    assert "for BMC_IP in 192.168.100.20; do" in script
    # One InsertMedia loop per cluster (eject uses EjectMedia, counted separately).
    assert script.count("VirtualMedia.InsertMedia") == 2
    assert script.count("VirtualMedia.EjectMedia") == 2


def test_install_script_per_cluster_bmc_password():
    import shlex

    script = _install_script()
    # No special chars -> shlex.quote leaves it bare; a space forces quoting.
    assert f"BMC_PASS={shlex.quote('pw-prod')}" in script
    assert f"BMC_PASS={shlex.quote('s3 cret')}" in script
    # Never emit the raw, unquoted password with an embedded space.
    assert "BMC_PASS=s3 cret\n" not in script


def test_install_script_bmc_password_injection_safe():
    from app.services.ocp.ops_pod_install import build_ops_pod_install_script

    clusters = [{"id": "prod", "name": "prod"}]
    # A single quote would break naive 'BMC_PASS={pw}' quoting / allow injection.
    evil = "p'w; rm -rf /"
    script = build_ops_pod_install_script(
        clusters, {"prod": (["1.2.3.4"], evil)}, "4.20", "/workdir"
    )
    import shlex

    assert f"BMC_PASS={shlex.quote(evil)}" in script
    assert "rm -rf /\n" not in script


def test_install_script_per_cluster_skip_guard():
    script = _install_script()
    # Each cluster gets its OWN kubeconfig-exists skip guard, keyed on its workdir.
    assert "[ -f /workdir/prod/auth/kubeconfig ]" in script
    assert "[ -f /workdir/dev/auth/kubeconfig ]" in script
    # The guard exits the cluster's subshell as success when already installed.
    assert "[prod] already installed, skipping" in script
    assert "[dev] already installed, skipping" in script


def test_install_script_skip_guard_before_create_image():
    script = _install_script()
    # Per cluster, the skip guard must precede `agent create image` so a
    # restarted pod skips completed clusters before re-running the installer.
    for cid in ("prod", "dev"):
        block = script.split(f"# ===== cluster {cid} =====", 1)[1]
        block = block.split("# ===== cluster", 1)[0]
        guard_idx = block.index(f"[ -f /workdir/{cid}/auth/kubeconfig ]")
        create_idx = block.index("agent create image")
        assert guard_idx < create_idx


def test_install_script_distinct_http_ports():
    script = _install_script()
    assert "http.server 8080" in script
    assert "http.server 8081" in script


def test_install_script_runs_clusters_in_parallel():
    script = _install_script()
    # Both cluster blocks are backgrounded subshells with captured PIDs.
    assert script.count(") &\n") == 2
    assert script.count("pids+=($!)") == 2


def test_install_script_propagates_cluster_failure():
    script = _install_script()
    # pipefail everywhere so awk can't mask openshift-install's non-zero exit.
    assert "set -o pipefail" in script
    # Top-level: wait per-PID, record failure, exit non-zero if any failed.
    assert 'for p in "${pids[@]}"; do wait "$p" || fail=1; done' in script
    # Failure still exits non-zero (dead-pod detection + leave pod for debugging).
    assert script.rstrip().endswith("exit 1")
    # A bare unconditional `wait` must NOT be the terminal join (it returns 0).
    assert not script.rstrip().endswith("wait")


def test_install_script_holds_container_on_success():
    """On success the ops pod must HOLD (sleep) instead of exiting: exiting would
    restart-loop (restart_policy=always + skip-guard) and make the monitor's
    credential-harvest `podman exec` race. Holding keeps it exec-able until reap."""
    script = _install_script()
    assert 'if [ "$fail" = 0 ]; then' in script
    assert "sleep infinity" in script


def test_install_script_downloads_from_same_mirror():
    script = _install_script()
    assert (
        "mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable-$OCP_VERSION"
        in script
    )


# --- Task 7: install-progress state machine (pure) -------------------------

from app.services.ocp.ops_pod_install import (  # noqa: E402
    ops_pod_install_progress,
    ops_pod_progress_items,
)


def test_progress_all_creating_image_in_progress():
    p = ops_pod_install_progress({"c1": "creating-image", "c2": "creating-image"})
    assert p["clusters"] == {"c1": "creating-image", "c2": "creating-image"}
    assert p["overall"] == "creating-image"
    assert p["done"] is False
    assert p["failed"] == []


def test_progress_one_complete_one_waiting_overall_in_progress():
    # Least-advanced cluster (waiting) drives the aggregate; not done yet.
    p = ops_pod_install_progress({"c1": "complete", "c2": "waiting"})
    assert p["clusters"]["c1"] == "complete"
    assert p["clusters"]["c2"] == "waiting"
    assert p["overall"] == "waiting"
    assert p["done"] is False
    assert p["failed"] == []


def test_progress_all_complete_is_done():
    p = ops_pod_install_progress({"c1": "complete", "c2": "complete"})
    assert p["overall"] == "complete"
    assert p["done"] is True
    assert p["failed"] == []


def test_progress_any_failed_overall_failed_and_done():
    p = ops_pod_install_progress({"c1": "complete", "c2": "failed", "c3": "waiting"})
    assert p["overall"] == "failed"
    assert p["done"] is True
    assert p["failed"] == ["c2"]


def test_progress_multiple_failed_sorted():
    p = ops_pod_install_progress({"z": "failed", "a": "failed", "m": "waiting"})
    assert p["failed"] == ["a", "z"]
    assert p["overall"] == "failed"
    assert p["done"] is True


def test_progress_parses_log_markers():
    logs = {
        "c1": "[c1] starting agent-based install\n"
        "Agent ISO created. Serving via HTTP and booting nodes...",
        "c2": "Waiting for cluster installation to complete...",
        "c3": "[c3] install complete",
    }
    p = ops_pod_install_progress(logs)
    assert p["clusters"]["c1"] == "booting"
    assert p["clusters"]["c2"] == "waiting"
    assert p["clusters"]["c3"] == "complete"
    # Least-advanced (booting) drives the aggregate.
    assert p["overall"] == "booting"
    assert p["done"] is False


def test_progress_creating_image_when_only_start_marker():
    p = ops_pod_install_progress({"c1": "[c1] starting agent-based install"})
    assert p["clusters"]["c1"] == "creating-image"
    assert p["overall"] == "creating-image"
    assert p["done"] is False


def test_progress_parses_failure_marker():
    logs = {"c1": 'level=fatal msg="install-complete command failed"'}
    p = ops_pod_install_progress(logs)
    assert p["clusters"]["c1"] == "failed"
    assert p["overall"] == "failed"
    assert p["done"] is True
    assert p["failed"] == ["c1"]


def test_progress_complete_marker_wins_over_stale_error_text():
    # A completed install may still contain earlier non-fatal 'error' noise.
    logs = {"c1": "some error retrying...\n[c1] install complete"}
    p = ops_pod_install_progress(logs)
    assert p["clusters"]["c1"] == "complete"
    assert p["done"] is True


def test_progress_empty_is_not_done():
    p = ops_pod_install_progress({})
    assert p["clusters"] == {}
    assert p["overall"] == "creating-image"
    assert p["done"] is False
    assert p["failed"] == []


def test_progress_cancelled_resolves_to_cancelled_and_done():
    # Cancellation decision logic: a cancel signal short-circuits to cancelled,
    # regardless of per-cluster phase — this is what the live monitor acts on.
    p = ops_pod_install_progress({"c1": "waiting", "c2": "booting"}, cancelled=True)
    assert p["overall"] == "cancelled"
    assert p["done"] is True
    # Per-cluster phases are still surfaced for the UI.
    assert p["clusters"]["c1"] == "waiting"
    assert p["clusters"]["c2"] == "booting"


def test_progress_cancelled_beats_failed():
    p = ops_pod_install_progress({"c1": "failed"}, cancelled=True)
    assert p["overall"] == "cancelled"
    assert p["done"] is True


def test_progress_items_pure_helper():
    p = ops_pod_install_progress({"c1": "waiting", "c2": "complete"})
    items = ops_pod_progress_items(p)
    assert "c1: waiting" in items
    assert "c2: complete" in items
    # Deterministic ordering (sorted by cluster id).
    assert items == ["c1: waiting", "c2: complete"]


# --- Task 5 (Plan 4b): dead-job → failed injection (pure) ------------------

from app.services.ocp.ops_pod_install import (  # noqa: E402
    inject_dead_pod_failures,
)


def test_inject_dead_pod_marks_non_terminal_cluster_failed():
    # Ops pod not running + a non-terminal cluster → that cluster becomes failed
    # so the state machine reports failed instead of spinning to the 2h timeout.
    out = inject_dead_pod_failures(
        {"c1": "Waiting for cluster installation to complete"}, pod_running=False
    )
    assert ops_pod_install_progress(out)["clusters"]["c1"] == "failed"
    assert ops_pod_install_progress(out)["overall"] == "failed"


def test_inject_dead_pod_creating_image_marked_failed():
    # Empty log (→ creating-image, non-terminal) + dead pod → failed.
    out = inject_dead_pod_failures({"c1": ""}, pod_running=False)
    assert ops_pod_install_progress(out)["clusters"]["c1"] == "failed"


def test_inject_running_pod_leaves_non_terminal_in_progress():
    # Pod still running → non-terminal cluster is left untouched (in-progress).
    logs = {"c1": "Waiting for cluster installation to complete"}
    out = inject_dead_pod_failures(logs, pod_running=True)
    assert out == logs
    p = ops_pod_install_progress(out)
    assert p["overall"] == "waiting"
    assert p["done"] is False


def test_inject_dead_pod_preserves_terminal_clusters():
    # A cluster that already completed is not clobbered to failed even if the pod
    # has since stopped (install already finished for that cluster).
    out = inject_dead_pod_failures(
        {"c1": "install complete", "c2": "booting nodes"}, pod_running=False
    )
    p = ops_pod_install_progress(out)
    assert p["clusters"]["c1"] == "complete"
    assert p["clusters"]["c2"] == "failed"


def test_inject_dead_pod_preserves_already_failed():
    out = inject_dead_pod_failures({"c1": "failed"}, pod_running=False)
    assert ops_pod_install_progress(out)["clusters"]["c1"] == "failed"


# ---------------------------------------------------------------------------
# Task 8b (Plan 4b): KubeVirt ops-pod Pod + Secret spec builder (pure)
# ---------------------------------------------------------------------------

from app.services.ocp.ops_pod_scaffold import (  # noqa: E402
    OPS_POD_IMAGE,
    build_ops_pod_kubevirt_manifests,
    ops_pod_network_nads,
)


def _kv_topology() -> dict:
    return {
        "nodes": [
            {
                "id": "netclust",
                "type": "networkNode",
                "data": {"subtype": "network", "networkType": "data"},
            },
            {
                "id": "netbmc00",
                "type": "networkNode",
                "data": {
                    "subtype": "network",
                    "networkType": "bmc",
                    "bmcPassword": "s3cret",
                },
            },
            # Gateway is a networkNode too, but has NO Multus NAD — it must be
            # excluded or the ops pod hangs in ContainerCreating waiting for a
            # net-<gateway>-nad that never exists.
            {
                "id": "gw012345",
                "type": "networkNode",
                "data": {"subtype": "gateway"},
            },
            {"id": "vm-1", "type": "vmNode", "data": {}},
        ]
    }


def test_ops_pod_network_nads_splits_cluster_and_bmc():
    cluster_nads, bmc_nad = ops_pod_network_nads(_kv_topology())
    # Mirrors the operator NAD naming convention: net-<id[:8]>-nad. The gateway
    # networkNode is excluded (only subtype=="network" lab nets get a NAD).
    assert cluster_nads == ["net-netclust-nad"]
    assert bmc_nad == "net-netbmc00-nad"


def test_ops_pod_network_nads_excludes_gateway_and_router():
    topo = {
        "nodes": [
            {"id": "netclust", "type": "networkNode", "data": {"subtype": "network"}},
            {"id": "gw012345", "type": "networkNode", "data": {"subtype": "gateway"}},
            {"id": "rtr01234", "type": "networkNode", "data": {"subtype": "router"}},
        ]
    }
    cluster_nads, bmc_nad = ops_pod_network_nads(topo)
    assert cluster_nads == ["net-netclust-nad"]
    assert bmc_nad is None


def test_ops_pod_network_nads_no_bmc_network():
    topo = {
        "nodes": [
            {
                "id": "netonly0",
                "type": "networkNode",
                "data": {"subtype": "network", "networkType": "data"},
            }
        ]
    }
    cluster_nads, bmc_nad = ops_pod_network_nads(topo)
    assert cluster_nads == ["net-netonly0-nad"]
    assert bmc_nad is None


def _kv_manifests():
    files = ops_pod_config_files(_clusters(), OPS_POD_WORKDIR, '{"auths":{}}')
    return build_ops_pod_kubevirt_manifests(
        namespace="troshka-abcdef12",
        project_id="abcdef12-3456-7890-1234-567890abcdef",
        command=["bash", "-c", "echo install"],
        env={
            "TROSHKA_API_URL": "https://troshka.example.com",
            "TROSHKA_API_KEY": "trk_secret",  # pragma: allowlist secret
            "TROSHKA_PROJECT_ID": "abcdef12",
            "OCP_VERSION": "4.20",
        },
        config_files=files,
        cluster_nads=["net-clu-nad"],
        bmc_nad="net-bmc-nad",
    )


def test_kv_ops_pod_manifest_is_a_pod_with_ee_image():
    pod, _secret = _kv_manifests()
    assert pod["kind"] == "Pod"
    assert pod["metadata"]["name"] == "troshka-abcdef12-ops"
    assert pod["metadata"]["namespace"] == "troshka-abcdef12"
    ctr = pod["spec"]["containers"][0]
    assert ctr["image"] == OPS_POD_IMAGE
    assert ctr["name"] == "ops"


def test_kv_ops_pod_attaches_both_cluster_and_bmc_nads():
    pod, _secret = _kv_manifests()
    networks = pod["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"]
    # BOTH the cluster NAD (reach nested VMs / serve ISO) and the BMC NAD
    # (reach sushy :8000) are attached — mirrors build_bmc_deployment.
    assert "net-clu-nad" in networks
    assert "net-bmc-nad" in networks
    assert networks == "net-clu-nad,net-bmc-nad"


def test_kv_ops_pod_command_is_install_script_not_secret_argv():
    pod, _secret = _kv_manifests()
    ctr = pod["spec"]["containers"][0]
    assert ctr["command"] == ["bash", "-c", "echo install"]


def test_kv_ops_pod_env_carries_scoped_key():
    pod, _secret = _kv_manifests()
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["TROSHKA_API_KEY"] == "trk_secret"  # pragma: allowlist secret
    assert env["TROSHKA_API_URL"] == "https://troshka.example.com"
    assert env["TROSHKA_PROJECT_ID"] == "abcdef12"
    assert env["OCP_VERSION"] == "4.20"


def test_kv_ops_pod_privileged_net_admin_net_raw():
    pod, _secret = _kv_manifests()
    sc = pod["spec"]["containers"][0]["securityContext"]
    assert sc["privileged"] is True
    assert set(sc["capabilities"]["add"]) == {"NET_ADMIN", "NET_RAW"}


def test_kv_ops_pod_restart_policy_always():
    pod, _secret = _kv_manifests()
    assert pod["spec"]["restartPolicy"] == "Always"


def test_kv_ops_pod_secret_carries_configs_and_pull_secret():
    _pod, secret = _kv_manifests()
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "troshka-abcdef12-ops-config"
    # Secret keys can't contain '/', so absolute paths are flattened.
    data = secret["stringData"]
    values = set(data.values())
    assert "install: prod\n" in values
    assert "agent: prod\n" in values
    assert '{"auths":{}}' in values


def test_kv_ops_pod_secret_mounted_at_absolute_workdir_paths():
    pod, _secret = _kv_manifests()
    ctr = pod["spec"]["containers"][0]
    mount_paths = {m["mountPath"] for m in ctr["volumeMounts"]}
    wd = OPS_POD_WORKDIR
    # Files land at the SAME absolute paths the install script reads (Task 8 parity).
    assert f"{wd}/cl-1/.src/install-config.yaml" in mount_paths
    assert f"{wd}/cl-1/.src/agent-config.yaml" in mount_paths
    assert f"{wd}/cl-2/.src/install-config.yaml" in mount_paths
    assert f"{wd}/pull-secret.json" in mount_paths
    # Each mount is read-only and references the ops-config secret volume via subPath.
    for m in ctr["volumeMounts"]:
        assert m["readOnly"] is True
        assert m["name"] == "ops-config"
        assert m["subPath"] in secret_stringdata_keys(pod)
    vol = pod["spec"]["volumes"][0]
    assert vol["name"] == "ops-config"
    assert vol["secret"]["secretName"] == "troshka-abcdef12-ops-config"


def secret_stringdata_keys(pod):
    # helper: recompute the secret keys from the pod's volumeMount subPaths.
    return {m["subPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]}


# ── Task 8c: provider-aware ops-pod monitor (log-read / running / cancel) ─────
#
# On a kubevirt host the ops pod is a plain k8s Pod (`troshka-<pid8>-ops`,
# container `ops`) in the project namespace — NOT a troshkad podman container. So
# the monitor's log-read, running-check, and cancel must branch to the k8s path
# (connect_get_namespaced_pod_exec / read_namespaced_pod / delete_namespaced_pod)
# instead of troshkad `/containers/exec` + `/pods/destroy`. Without this a
# SUCCESSFUL kubevirt install would sit until the 2h timeout and report FAILED.

_OPS_PID = "abcdef12-1111-2222-3333-444444444444"


def _kv_host():
    return SimpleNamespace(
        id="host-1", provider_id="prov-1", host_type="kubevirt-cluster"
    )


def _troshkad_host():
    return SimpleNamespace(id="host-2", provider_id="prov-2", host_type="ec2")


def _kv_clients_patches(core_v1):
    """Patch the DB + kubevirt client resolution used by the monitor helpers."""
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = MagicMock()
    return (
        patch("app.core.database.SessionLocal", return_value=session),
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(None, core_v1, None),
        ),
        patch(
            "app.services.providers.kubevirt._project_ns",
            return_value="troshka-ns",
        ),
    )


def test_read_ops_pod_logs_kubevirt_execs_cat_install_log():
    """kubevirt log-read execs `cat <workdir>/<clusterId>/install.log` into the
    ops Pod via connect_get_namespaced_pod_exec (not troshkad containers/exec)."""
    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns, patch(
        "kubernetes.stream.stream", return_value="=== install complete ===\n"
    ) as mock_stream:
        logs = ds._read_ops_pod_cluster_logs(
            _kv_host(), "ignored-ctr", ["cl-1"], "/opt/troshka", _OPS_PID
        )

    assert logs["cl-1"] == "=== install complete ===\n"
    args, kwargs = mock_stream.call_args
    assert args[0] is core_v1.connect_get_namespaced_pod_exec
    assert args[1] == f"troshka-{_OPS_PID[:8]}-ops"  # ops Pod name
    assert args[2] == "troshka-ns"  # project namespace
    assert kwargs["container"] == "ops"
    assert kwargs["command"] == ["cat", "/opt/troshka/cl-1/install.log"]


def test_read_ops_pod_logs_troshkad_path_unchanged():
    """A troshkad host still reads via troshkad `/containers/exec` (cat)."""
    from app.services import deploy_service as ds

    host = _troshkad_host()
    with patch.object(ds, "_exec_ops_pod_cat", return_value="log-text") as mock_cat:
        logs = ds._read_ops_pod_cluster_logs(
            host, "troshka-ctr", ["cl-1"], "/wd", _OPS_PID
        )

    assert logs == {"cl-1": "log-text"}
    mock_cat.assert_called_once_with(host, "troshka-ctr", "/wd/cl-1/install.log")


def test_ops_pod_running_kubevirt_running_phase_true():
    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    core_v1.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Running")
    )
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns:
        assert ds._ops_pod_running(_kv_host(), "ignored", _OPS_PID) is True

    core_v1.read_namespaced_pod.assert_called_once()
    call = core_v1.read_namespaced_pod.call_args
    assert call.kwargs.get("name") == f"troshka-{_OPS_PID[:8]}-ops"
    assert call.kwargs.get("namespace") == "troshka-ns"


def test_ops_pod_running_kubevirt_failed_phase_false():
    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    core_v1.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Failed")
    )
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns:
        assert ds._ops_pod_running(_kv_host(), "ignored", _OPS_PID) is False


def test_ops_pod_running_kubevirt_absent_false():
    """A 404 (pod gone) counts as not-running → dead-pod injection eligible."""
    from kubernetes.client.exceptions import ApiException

    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    core_v1.read_namespaced_pod.side_effect = ApiException(status=404)
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns:
        assert ds._ops_pod_running(_kv_host(), "ignored", _OPS_PID) is False


def test_ops_pod_running_kubevirt_api_error_conservative_true():
    """A transient (non-404) API error assumes running (conservative), so a
    network blip never forces a false FAILED — mirrors the troshkad path."""
    from kubernetes.client.exceptions import ApiException

    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    core_v1.read_namespaced_pod.side_effect = ApiException(status=500)
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns:
        assert ds._ops_pod_running(_kv_host(), "ignored", _OPS_PID) is True


def test_ops_pod_running_troshkad_path_unchanged():
    """A troshkad host still checks via troshkad `/containers/states`."""
    from app.services import deploy_service as ds

    with patch(
        "app.services.troshkad_client.get_all_container_states",
        return_value={"troshka-ctr": {"state": "running"}},
    ) as mock_states:
        assert ds._ops_pod_running(_troshkad_host(), "troshka-ctr", _OPS_PID) is True
    mock_states.assert_called_once()


def test_cancel_ops_pod_install_kubevirt_deletes_pod():
    """kubevirt cancel deletes the ops Pod (grace 0), not troshkad /pods/destroy."""
    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns, patch.object(
        ds, "_publish_ops_pod_progress"
    ) as mock_pub, patch.object(ds, "start_job") as mock_start:
        ds._cancel_ops_pod_install(_kv_host(), _OPS_PID, ["cl-1", "cl-2"])

    # No troshkad job issued on the kubevirt path.
    mock_start.assert_not_called()
    core_v1.delete_namespaced_pod.assert_called_once()
    call = core_v1.delete_namespaced_pod.call_args
    assert call.kwargs.get("name") == f"troshka-{_OPS_PID[:8]}-ops"
    assert call.kwargs.get("namespace") == "troshka-ns"
    assert call.kwargs.get("grace_period_seconds") == 0
    # Terminal cancelled status is still published.
    mock_pub.assert_called_once()
    assert mock_pub.call_args[0][1]["overall"] == "cancelled"


def test_cancel_ops_pod_install_kubevirt_best_effort_on_error():
    """A k8s delete failure must not raise; cancelled status still published."""
    from app.services import deploy_service as ds

    core_v1 = MagicMock()
    core_v1.delete_namespaced_pod.side_effect = Exception("boom")
    p_db, p_clients, p_ns = _kv_clients_patches(core_v1)
    with p_db, p_clients, p_ns, patch.object(
        ds, "_publish_ops_pod_progress"
    ) as mock_pub:
        ds._cancel_ops_pod_install(_kv_host(), _OPS_PID, ["cl-1"])

    mock_pub.assert_called_once()
    assert mock_pub.call_args[0][1]["overall"] == "cancelled"


def test_cancel_ops_pod_install_troshkad_path_unchanged():
    """A troshkad host still issues `/pods/destroy` for the ops pod."""
    from app.services import deploy_service as ds

    with patch.object(
        ds, "start_job", return_value="job-destroy"
    ) as mock_start, patch.object(ds, "wait_for_job") as mock_wait, patch.object(
        ds, "_publish_ops_pod_progress"
    ):
        ds._cancel_ops_pod_install(_troshkad_host(), _OPS_PID, ["cl-1"])

    mock_start.assert_called_once()
    assert mock_start.call_args[0][1] == "/pods/destroy"
    assert mock_start.call_args[0][2]["pod_name"] == f"troshka-{_OPS_PID[:8]}-ops"
    mock_wait.assert_called_once()


def test_apply_ops_pod_creds_control_plane_only():
    """kubeadmin pw + kubeconfig land on control-plane members only, not workers."""
    from app.services.deploy_service import _apply_ops_pod_creds

    topo = {
        "nodes": [
            {
                "id": "cp",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "control-plane"},
            },
            {
                "id": "wk",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "worker"},
            },
            {
                "id": "other",
                "type": "vmNode",
                "data": {"clusterId": "dev", "clusterRole": "control-plane"},
            },
        ]
    }
    changed = _apply_ops_pod_creds(topo, {"ocp": ("pw123", "KC")})
    assert changed is True
    cp = topo["nodes"][0]["data"]
    assert cp["ocpKubeadminPassword"] == "pw123" and cp["ocpKubeconfig"] == "KC"
    assert "ocpKubeadminPassword" not in topo["nodes"][1]["data"]  # worker skipped
    assert "ocpKubeadminPassword" not in topo["nodes"][2]["data"]  # other cluster
    # Idempotent: re-applying the same creds reports no change.
    assert _apply_ops_pod_creds(topo, {"ocp": ("pw123", "KC")}) is False


def test_cluster_access_returns_control_plane_creds():
    """_cluster_access surfaces the cp member's harvested kubeadmin pw +
    kubeconfig availability + vm_name (for the live status-modal poll)."""
    from app.api.projects import _cluster_access

    topo = {
        "nodes": [
            {
                "id": "cp",
                "type": "vmNode",
                "data": {
                    "clusterId": "ocp",
                    "clusterRole": "control-plane",
                    "name": "cp-0",
                    "ocpKubeadminPassword": "pw123",
                    "ocpKubeconfig": "KC",
                },
            },
            {
                "id": "wk",
                "type": "vmNode",
                "data": {
                    "clusterId": "ocp",
                    "clusterRole": "worker",
                    "ocpKubeadminPassword": "wk-should-be-ignored",
                },
            },
        ]
    }
    acc = _cluster_access(topo, "ocp")
    assert acc == {
        "kubeadmin_password": "pw123",
        "kubeconfig_available": True,
        "vm_name": "cp-0",
    }


def test_cluster_access_empty_before_harvest():
    from app.api.projects import _cluster_access

    topo = {
        "nodes": [
            {
                "id": "cp",
                "type": "vmNode",
                "data": {"clusterId": "ocp", "clusterRole": "control-plane"},
            }
        ]
    }
    assert _cluster_access(topo, "ocp") == {
        "kubeadmin_password": "",
        "kubeconfig_available": False,
        "vm_name": "",
    }


def test_showroom_with_cluster_terminal_detects_tab():
    from app.services.deploy_service import _showroom_with_cluster_terminal

    topo = {
        "nodes": [
            {
                "data": {
                    "isShowroom": True,
                    "name": "showroom",
                    "showroomTabs": [
                        {
                            "type": "terminal",
                            "target": "clusters",
                            "name": "Cluster Terminal",
                        }
                    ],
                }
            },
        ]
    }
    assert _showroom_with_cluster_terminal(topo) == "showroom"
    # no cluster-terminal tab -> None
    topo2 = {
        "nodes": [
            {
                "data": {
                    "isShowroom": True,
                    "showroomTabs": [{"type": "terminal", "vmId": "vm-1"}],
                }
            }
        ]
    }
    assert _showroom_with_cluster_terminal(topo2) is None


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job", return_value="job-1")
def test_inject_cluster_kubeconfigs_execs_into_showroom_proxy(mock_start, _mock_wait):
    import yaml as _yaml

    from app.services.deploy_service import _inject_cluster_kubeconfigs

    kc = _yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "c", "cluster": {"server": "https://api:6443"}}],
            "users": [{"name": "u", "user": {"token": "t"}}],
            "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
            "current-context": "x",
        }
    )
    topo = {
        "nodes": [
            {
                "data": {
                    "isShowroom": True,
                    "name": "showroom",
                    "showroomTabs": [{"type": "terminal", "target": "clusters"}],
                }
            }
        ]
    }
    host = SimpleNamespace(host_type="ec2", ip_address="10.0.0.1", agent_token="t")

    _inject_cluster_kubeconfigs(
        host, "abcd1234-0000", topo, {"ocp": ("pw", kc)}, [{"id": "ocp", "name": "ocp"}]
    )

    mock_start.assert_called_once()
    endpoint = mock_start.call_args[0][1]
    payload = mock_start.call_args[0][2]
    assert endpoint == "/containers/exec"
    assert payload["container_name"] == "troshka-abcd1234-showroom-proxy"
    assert payload["command"][0] == "sh"
    assert "/showroom/kube/config" in payload["command"][2]


@patch("app.services.deploy_service.start_job")
def test_inject_cluster_kubeconfigs_noop_without_tab(mock_start):
    from app.services.deploy_service import _inject_cluster_kubeconfigs

    topo = {"nodes": [{"data": {"isShowroom": True, "showroomTabs": []}}]}
    host = SimpleNamespace(host_type="ec2", ip_address="10.0.0.1", agent_token="t")
    _inject_cluster_kubeconfigs(
        host,
        "abcd1234-0000",
        topo,
        {"ocp": ("pw", "kc")},
        [{"id": "ocp", "name": "ocp"}],
    )
    mock_start.assert_not_called()


def test_stored_cluster_creds_from_control_plane_nodes():
    from app.services.deploy_service import _stored_cluster_creds

    topo = {
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "ocp",
                    "clusterRole": "control-plane",
                    "ocpKubeadminPassword": "pw",
                    "ocpKubeconfig": "KC",
                },
            },
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "ocp",
                    "clusterRole": "worker",
                    "ocpKubeadminPassword": "nope",
                },
            },
        ]
    }
    assert _stored_cluster_creds(topo) == {"ocp": ("pw", "KC")}
    # nothing harvested yet -> empty
    assert (
        _stored_cluster_creds(
            {
                "nodes": [
                    {
                        "type": "vmNode",
                        "data": {"clusterId": "ocp", "clusterRole": "control-plane"},
                    }
                ]
            }
        )
        == {}
    )


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job", return_value="job-1")
def test_inject_stored_cluster_kubeconfigs_uses_topology_creds(mock_start, _w):
    import yaml as _yaml

    from app.services.deploy_service import _inject_stored_cluster_kubeconfigs

    kc = _yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "c", "cluster": {"server": "https://api:6443"}}],
            "users": [{"name": "u", "user": {"token": "t"}}],
            "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
            "current-context": "x",
        }
    )
    topo = {
        "clusters": [{"id": "ocp", "name": "ocp"}],
        "nodes": [
            {
                "type": "vmNode",
                "data": {
                    "clusterId": "ocp",
                    "clusterRole": "control-plane",
                    "ocpKubeadminPassword": "pw",
                    "ocpKubeconfig": kc,
                },
            },
            {
                "type": "containerNode",
                "data": {
                    "isShowroom": True,
                    "name": "showroom",
                    "showroomTabs": [{"type": "terminal", "target": "clusters"}],
                },
            },
        ],
    }
    host = SimpleNamespace(host_type="ec2", ip_address="10.0.0.1", agent_token="t")
    _inject_stored_cluster_kubeconfigs(host, "abcd1234-x", topo)
    mock_start.assert_called_once()
    assert mock_start.call_args[0][1] == "/containers/exec"
    assert "/showroom/kube/config" in mock_start.call_args[0][2]["command"][2]


@patch("app.services.deploy_service._start_ops_pod_install_monitor")
@patch("app.services.template_loader.ocp_install_via", return_value="pod")
@patch("app.core.database.SessionLocal")
def test_resume_ops_pod_monitors_restarts_stuck_pod_installs(mock_sl, _via, mock_start):
    """Worker startup re-attaches the monitor for a project stuck at
    ocp_status='monitoring' (prior worker died mid-install)."""
    import app.services.deploy_service as ds
    from app.models.host import Host
    from app.models.project import Project

    proj = SimpleNamespace(
        id="abcd1234-0000-0000-0000-000000000000",
        state="active",
        host_id="h1",
        deployed_topology={"clusters": [{"id": "ocp", "name": "ocp"}]},
        topology={},
    )
    host = SimpleNamespace(id="h1")
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is Project:
            q.filter.return_value.filter.return_value.all.return_value = [proj]
        elif model is Host:
            q.filter_by.return_value.first.return_value = host
        return q

    db.query.side_effect = _query
    mock_sl.return_value = db

    ds.resume_ops_pod_monitors()

    mock_start.assert_called_once()
    assert mock_start.call_args[0][1] == proj.id  # project_id
    assert mock_start.call_args[0][2] == [{"id": "ocp", "name": "ocp"}]  # clusters


@patch("app.core.redis.is_redis_available", return_value=False)
def test_ops_monitor_lock_allows_when_redis_unavailable(_no_redis):
    """No shared Redis -> single-process fallback: acquiring the monitor lock
    always succeeds (dedup can't apply) and release/refresh are no-ops."""
    from app.services.deploy_service import (
        _acquire_ops_monitor_lock,
        _refresh_ops_monitor_lock,
        _release_ops_monitor_lock,
    )

    assert _acquire_ops_monitor_lock("p1") is True
    _refresh_ops_monitor_lock("p1")  # no-op, no raise
    _release_ops_monitor_lock("p1")  # no-op, no raise
