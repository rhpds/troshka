"""Tests for the ops-pod network + spec builder (Plan 4, Task 4).

Pure payload-shaping tests — no live pod is created here. The shaped dict is
consumed later by `_pod_create_params`-style logic (Task 5) to actually create
the in-cluster ops pod.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.deploy_topology import showroom_infra_network
from app.services.ocp.ops_pod_scaffold import (
    OPS_POD_IMAGE,
    build_ops_pod_config,
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


def _build():
    project = SimpleNamespace(id="proj-123", name="demo")
    return build_ops_pod_config(
        project=project,
        clusters=_clusters(),
        api_url="https://troshka.example.com",
        api_key="trk_secret",
        ocp_version="4.20.0",
        pull_secret_json='{"auths":{}}',
    )


def test_build_ops_pod_config_pod_shape():
    cfg = _build()
    assert cfg["pod_name"] == "ops"
    assert cfg["restart_policy"] == "always"
    assert cfg["privileged"] is True
    assert len(cfg["containers"]) == 1


def test_build_ops_pod_config_main_container_image_and_env():
    cfg = _build()
    main = cfg["containers"][0]
    assert main["image"] == OPS_POD_IMAGE
    env = main["env"]
    assert env["TROSHKA_API_URL"] == "https://troshka.example.com"
    assert env["TROSHKA_API_KEY"] == "trk_secret"
    assert env["TROSHKA_PROJECT_ID"] == "proj-123"
    assert env["OCP_VERSION"] == "4.20.0"


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


def test_build_ops_pod_config_uses_resolved_image(monkeypatch):
    # build_ops_pod_config reads the module-level OPS_POD_IMAGE at call time,
    # so a deployment-configured ref flows straight into the pod spec.
    from app.services.ocp import ops_pod_scaffold

    monkeypatch.setattr(
        ops_pod_scaffold, "OPS_POD_IMAGE", "quay.io/example/troshka-ops-pod:pinned"
    )
    cfg = _build()
    assert cfg["containers"][0]["image"] == "quay.io/example/troshka-ops-pod:pinned"


def test_build_ops_pod_config_carries_all_cluster_configs():
    cfg = _build()
    files = cfg["files"]
    # Both clusters' install AND agent configs are present, scoped by id.
    assert files["cl-1/install-config.yaml"] == "install: prod\n"
    assert files["cl-1/agent-config.yaml"] == "agent: prod\n"
    assert files["cl-2/install-config.yaml"] == "install: edge\n"
    assert files["cl-2/agent-config.yaml"] == "agent: edge\n"


def test_build_ops_pod_config_includes_pull_secret():
    cfg = _build()
    assert cfg["files"]["pull-secret.json"] == '{"auths":{}}'


def test_build_ops_pod_config_workdir_mounted():
    cfg = _build()
    workdir = cfg["workdir"]
    assert workdir
    mounts = cfg["containers"][0]["mounts"]
    assert any(m.get("mountPath") == workdir for m in mounts)


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
    assert "exit $fail" in script
    # A bare unconditional `wait` must NOT be the terminal join (it returns 0).
    assert not script.rstrip().endswith("wait")


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
