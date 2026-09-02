"""Tier-2: a real (pod) SNO install reaches ready on troshkad and kubevirt.

Slow (~30-60 min). Gated by TROSHKA_LIVE_TIER2=1 and a configured pull secret.
"""

import pytest
from live_api import poll_ocp


def _params():
    return [
        pytest.param("troshkad", marks=pytest.mark.live_troshkad, id="troshkad"),
        pytest.param("kubevirt", marks=pytest.mark.live_kubevirt, id="kubevirt"),
    ]


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.parametrize("provider", _params())
def test_pod_sno_install_reaches_ready(
    provider, live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(
        live_config.kubevirt_host
        if provider == "kubevirt"
        else live_config.troshkad_host
    )
    pid = project_factory(
        template_id=ocp_template, name=f"live-t2-{provider}", host_id=host_id
    )
    final = poll_ocp(
        client,
        pid,
        until={"ready", "warning"},
        timeout_s=live_config.timeout_s,
        interval_s=30,
    )
    assert final["ocp_status"] in ("ready", "warning")
    assert (final.get("ocp_install_elapsed") or 0) > 0


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_monitor_reports_progress(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-t2-progress", host_id=host_id
    )
    # Sample once early: status should be monitoring with a detail string before ready.
    import time

    time.sleep(120)
    mid = client.status(pid)
    assert mid["ocp_status"] in ("monitoring", "ready", "warning")
    final = poll_ocp(
        client,
        pid,
        until={"ready", "warning"},
        timeout_s=live_config.timeout_s,
        interval_s=30,
    )
    assert final["ocp_status"] in ("ready", "warning")
