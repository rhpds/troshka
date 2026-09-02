"""Tier-1: an install_via=bastion project bakes a bastion, creates NO ops pod."""

import time

import pytest
from live_hostcmd import host_ssh


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_bastion_project_creates_no_ops_pod(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template,
        name="live-bastion",
        install_via="bastion",
        host_id=host_id,
    )
    ops_name = f"troshka-{pid[:8]}-ops"
    # Give the deploy time to reach VM-start; the bastion path must never create
    # an ops pod. Sample a few times to be sure it doesn't appear.
    for _ in range(8):
        out = host_ssh(
            live_config.troshkad_host, "podman", "ps", "-a", "--format", "{{.Names}}"
        )
        assert ops_name not in out.split(), "bastion project must not create an ops pod"
        time.sleep(15)
