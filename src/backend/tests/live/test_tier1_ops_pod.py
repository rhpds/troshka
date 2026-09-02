"""Tier-1: the ops pod is created for a pod OCP project, with no secrets in argv.

These are live_env tests: they require a running Troshka + a real host. Without
TROSHKA_LIVE_* they are skipped by the conftest collection guard.
"""

import time

import pytest
from live_hostcmd import host_ssh, oc

SECRET_MARKERS = ("BEGIN CERTIFICATE", "pullSecret", '"auth"', "-----BEGIN")


def _ops_container(pid):
    return f"troshka-{pid[:8]}-ops"


def _wait_ops_pod_troshkad(cfg, pid, timeout=900):
    name = _ops_container(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = host_ssh(cfg.troshkad_host, "podman", "ps", "--format", "{{.Names}}")
        if any(name in line for line in out.splitlines()):
            return name
        time.sleep(15)
    pytest.fail(f"ops pod {name} never appeared within {timeout}s")


def _wait_ops_pod_kubevirt(cfg, pid, timeout=900):
    name = _ops_container(pid)
    ns = f"troshka-{pid[:8]}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = oc(
            "get",
            "pod",
            name,
            "-n",
            ns,
            "--no-headers",
            "--ignore-not-found",
            kubeconfig=cfg.kubeconfig,
        )
        if name in out:
            return name, ns
        time.sleep(15)
    pytest.fail(f"ops pod {name} never appeared in {ns} within {timeout}s")


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_troshkad_ops_pod_created_and_running(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-t1-troshkad", host_id=host_id
    )
    name = _wait_ops_pod_troshkad(live_config, pid)
    inspect = host_ssh(live_config.troshkad_host, "podman", "inspect", name)
    assert '"Running": true' in inspect or '"Status": "running"' in inspect


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_troshkad_no_secret_in_argv(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.troshkad_host)
    pid = project_factory(
        template_id=ocp_template, name="live-t1-nosecret", host_id=host_id
    )
    name = _wait_ops_pod_troshkad(live_config, pid)
    inspect = host_ssh(
        live_config.troshkad_host,
        "podman",
        "inspect",
        "--format",
        "{{.Config.CreateCommand}} {{.Args}} {{.Config.Cmd}}",
        name,
    )
    for marker in SECRET_MARKERS:
        assert marker not in inspect, f"secret marker {marker!r} leaked into argv"


@pytest.mark.live_env
@pytest.mark.live_kubevirt
def test_kubevirt_ops_pod_created_and_running(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.kubevirt_host)
    pid = project_factory(template_id=ocp_template, name="live-t1-kv", host_id=host_id)
    name, ns = _wait_ops_pod_kubevirt(live_config, pid)
    phase = oc(
        "get",
        "pod",
        name,
        "-n",
        ns,
        "-o",
        "jsonpath={.status.phase}",
        kubeconfig=live_config.kubeconfig,
    )
    assert phase.strip() == "Running"


@pytest.mark.live_env
@pytest.mark.live_kubevirt
def test_kubevirt_configs_are_secret_mount_not_argv(
    live_config, client, project_factory, ocp_template, resolve_host_id
):
    host_id = resolve_host_id(live_config.kubevirt_host)
    pid = project_factory(
        template_id=ocp_template, name="live-t1-kvsecret", host_id=host_id
    )
    name, ns = _wait_ops_pod_kubevirt(live_config, pid)
    # The install-config / pull-secret ride in a mounted Secret volume, not argv.
    vols = oc(
        "get",
        "pod",
        name,
        "-n",
        ns,
        "-o",
        "jsonpath={.spec.volumes[*].secret.secretName}",
        kubeconfig=live_config.kubeconfig,
    )
    assert vols.strip(), "expected a mounted secret volume on the ops pod"
    cmd = oc(
        "get",
        "pod",
        name,
        "-n",
        ns,
        "-o",
        "jsonpath={.spec.containers[*].command}",
        kubeconfig=live_config.kubeconfig,
    )
    for marker in SECRET_MARKERS:
        assert marker not in cmd
