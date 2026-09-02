"""Tier-1 security: scoped ops-pod key least-privilege, active-then-revoked."""

import pytest
from live_hostcmd import host_ssh, ops_pod_apikey_row
from live_scopedkey import client_for_key, scoped_key_from_pod
from test_tier1_ops_pod import _wait_ops_pod_kubevirt, _wait_ops_pod_troshkad


def _params():
    return [
        pytest.param("troshkad", marks=pytest.mark.live_troshkad, id="troshkad"),
        pytest.param("kubevirt", marks=pytest.mark.live_kubevirt, id="kubevirt"),
    ]


def _wait_ops(provider, cfg, pid):
    if provider == "kubevirt":
        _wait_ops_pod_kubevirt(cfg, pid)
    else:
        _wait_ops_pod_troshkad(cfg, pid)


@pytest.mark.live_env
@pytest.mark.parametrize("provider", _params())
def test_scoped_key_least_privilege(
    provider, live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(
        live_config.kubevirt_host
        if provider == "kubevirt"
        else live_config.troshkad_host
    )
    pid = project_factory(
        template_id=ocp_template, name=f"live-sec-{provider}", host_id=host_id
    )
    _wait_ops(provider, live_config, pid)

    raw = scoped_key_from_pod(live_config, pid, provider=provider)
    scoped = client_for_key(live_config.url, raw)

    # allowlisted + own project: 200
    assert scoped.raw.get(f"/api/v1/projects/{pid}").status_code == 200
    # non-allowlisted routes on own project: 403
    assert scoped.raw.post(f"/api/v1/projects/{pid}/deploy").status_code == 403
    assert scoped.raw.delete(f"/api/v1/projects/{pid}").status_code == 403
    # someone else's project (any other id): 403
    other = "00000000-0000-0000-0000-000000000000"
    assert scoped.raw.get(f"/api/v1/projects/{other}").status_code == 403


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_scoped_key_active_then_revoked_on_destroy(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-sec-revoke", host_id=host_id
    )
    _wait_ops_pod_troshkad(live_config, pid)

    row = ops_pod_apikey_row(pid)
    assert row is not None and row["is_active"] is True
    assert row["scopes"] == ["topology:read", "vm:exec"]

    assert client.delete(f"/api/v1/projects/{pid}").status_code in (200, 204)
    # After DELETE the project (and cascade) is gone → row absent, or is_active False.
    row2 = ops_pod_apikey_row(pid)
    assert row2 is None or row2["is_active"] is False


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_cancel_deletes_ops_pod(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-sec-cancel", host_id=host_id
    )
    name = _wait_ops_pod_troshkad(live_config, pid)
    assert client.post_json(f"/api/v1/projects/{pid}/undeploy", {}).status_code in (
        200,
        204,
    )
    out = host_ssh(
        live_config.troshkad_host, "podman", "ps", "-a", "--format", "{{.Names}}"
    )
    assert name not in out.split(), f"ops pod {name} still present after undeploy"
