"""Tier-2 lifecycle: idempotent restart (troshkad), bastion install unchanged."""

import time

import pytest
from live_api import poll_ocp
from live_hostcmd import host_podman
from test_tier1_ops_pod import _wait_ops_pod_troshkad


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_idempotent_restart_skips_completed_cluster(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-t2-restart", host_id=host_id
    )
    name = _wait_ops_pod_troshkad(live_config, pid)
    # Let the install get underway, then kill the ops pod; restart_policy=always
    # brings it back and the per-cluster kubeconfig skip-guard must avoid a rerun.
    time.sleep(300)
    host_podman(live_config.troshkad_host, "restart", name)
    final = poll_ocp(
        client,
        pid,
        until={"ready", "warning"},
        timeout_s=live_config.timeout_s,
        interval_s=30,
    )
    assert final["ocp_status"] in ("ready", "warning")


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_bastion_sno_install_unchanged(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template,
        name="live-t2-bastion",
        install_via="bastion",
        host_id=host_id,
    )
    final = poll_ocp(
        client,
        pid,
        until={"ready", "warning"},
        timeout_s=live_config.timeout_s,
        interval_s=30,
    )
    assert final["ocp_status"] in ("ready", "warning")
