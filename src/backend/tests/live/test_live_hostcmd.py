from unittest.mock import MagicMock, patch

import live_hostcmd as hc
import pytest


def _ok(stdout=""):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def test_host_ssh_builds_command():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("ok")) as run:
        out = hc.host_ssh("a1b2", "podman", "ps")
    assert out == "ok"
    argv = run.call_args[0][0]
    assert argv[0].endswith("scripts/host-ssh.sh")
    assert argv[1:] == ["a1b2", "podman", "ps"]


def test_host_podman_prepends_sudo():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("ok")) as run:
        out = hc.host_podman("a1b2", "ps", "--format", "{{.Names}}")
    assert out == "ok"
    argv = run.call_args[0][0]
    assert argv[0].endswith("scripts/host-ssh.sh")
    assert argv[1:] == ["a1b2", "sudo", "podman", "ps", "--format", "{{.Names}}"]


def test_oc_injects_kubeconfig():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("pods")) as run:
        hc.oc("get", "pod", kubeconfig="/tmp/kc")
    argv = run.call_args[0][0]
    assert argv[0] == "oc"
    assert "--kubeconfig=/tmp/kc" in argv
    assert argv[-2:] == ["get", "pod"]


def test_host_cmd_raises_on_nonzero():
    with patch(
        "live_hostcmd.subprocess.run",
        return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
    ):
        with pytest.raises(hc.HostCmdError):
            hc.host_ssh("a1b2", "false")


def test_ops_pod_apikey_row_parses_json():
    payload = '{"is_active": true, "scopes": ["topology:read", "vm:exec"]}'
    with patch("live_hostcmd.subprocess.run", return_value=_ok(payload)):
        row = hc.ops_pod_apikey_row("proj-1234")
    assert row == {"is_active": True, "scopes": ["topology:read", "vm:exec"]}


def test_ops_pod_apikey_row_none_when_absent():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("null")):
        assert hc.ops_pod_apikey_row("proj-1234") is None


def test_repo_root_points_at_scripts():
    assert (hc.REPO_ROOT / "scripts" / "host-ssh.sh").exists()
    assert (hc.REPO_ROOT / "scripts" / "host-db.sh").exists()


def test_ops_pod_apikey_row_rejects_injection():
    with pytest.raises(ValueError):
        hc.ops_pod_apikey_row("x'; import os; os.system('rm -rf /')  #")
