"""Direct oc and bastion fallback contracts across host providers."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import deploy_service as deploy


@pytest.fixture
def agent(monkeypatch):
    start, wait = Mock(return_value="job"), Mock()
    monkeypatch.setattr("app.services.troshkad_client.start_job", start)
    monkeypatch.setattr("app.services.troshkad_client.wait_for_job", wait)
    monkeypatch.setattr(deploy, "start_job", start)
    monkeypatch.setattr(deploy, "wait_for_job", wait)
    return SimpleNamespace(host_type="libvirt"), start, wait


def test_direct_oc_success_preserves_output_and_timeout(agent):
    host, start, wait = agent
    wait.return_value = {
        "status": "completed",
        "result": {"exit_code": 0, "output": "nodes\n"},
    }
    assert deploy._exec_oc(host, "p", "oc get nodes", timeout=7) == "nodes\n"
    start.assert_called_once_with(
        host, "/oc-exec", {"project_id": "p", "command": "oc get nodes", "timeout": 7}
    )
    wait.assert_called_once_with(host, "job", timeout=22)


@pytest.mark.parametrize(
    "result,message",
    [
        ({"exit_code": 1, "error": "forbidden"}, "forbidden"),
        ({"exit_code": 1, "output": "connection refused"}, "connection refused"),
        ({}, "oc-exec failed"),
    ],
)
def test_direct_oc_rejects_nonzero_or_missing_exit_code(agent, result, message):
    host, _, wait = agent
    wait.return_value = {"status": "completed", "result": result}
    with pytest.raises(RuntimeError, match=message):
        deploy._exec_oc(host, "p", "oc get nodes")


def test_direct_oc_rejects_failed_job(agent):
    host, _, wait = agent
    wait.return_value = {"status": "failed", "result": {"error": "job failed"}}
    with pytest.raises(RuntimeError, match="job failed"):
        deploy._exec_oc(host, "p", "oc get nodes")


@pytest.mark.parametrize(
    "command",
    [
        "oc get nodes | head",
        "oc get nodes && echo done",
        "oc get nodes; echo done",
        "cat /etc/os-release",
    ],
)
def test_shell_compound_commands_use_bastion_ssh(agent, command):
    host, start, wait = agent
    wait.return_value = {"status": "completed", "result": {"output": "answer"}}
    assert (
        deploy._exec_on_bastion(host, "p", "10.0.0.50", "password", command) == "answer"
    )
    assert start.call_args.args[1] == "/vm/ssh-exec"
    assert start.call_args.args[2]["command"] == command
    assert start.call_args.args[2]["vm_ip"] == "10.0.0.50"


def test_simple_oc_falls_back_to_bastion_after_direct_failure(agent):
    host, start, wait = agent
    wait.side_effect = [
        {"status": "failed", "result": {"error": "direct unavailable"}},
        {"status": "completed", "result": {"output": "nodes"}},
    ]
    assert (
        deploy._exec_on_bastion(host, "p", "10.0.0.50", "password", "oc get nodes")
        == "nodes"
    )
    assert [call.args[1] for call in start.call_args_list] == [
        "/oc-exec",
        "/vm/ssh-exec",
    ]


def test_simple_oc_does_not_use_bastion_when_direct_succeeds(agent):
    host, start, wait = agent
    wait.return_value = {
        "status": "completed",
        "result": {"exit_code": 0, "output": "nodes"},
    }
    assert (
        deploy._exec_on_bastion(host, "p", "10.0.0.50", "password", "kubectl get nodes")
        == "nodes"
    )
    assert start.call_count == 1
    assert start.call_args.args[1] == "/oc-exec"


@pytest.mark.parametrize(
    "result,expected",
    [
        (
            {
                "output": "\x1b[32mready\x1b[0m\nOpenShift Console: secret\nUsername: admin\nPassword: secret\n\nnode ready"
            },
            "ready\nnode ready",
        ),
        ({"output": "", "error": "denied"}, None),
        ({"output": ""}, ""),
    ],
)
def test_bastion_agent_output_strips_banner_and_terminal_color(agent, result, expected):
    host, _, wait = agent
    wait.return_value = {"status": "completed", "result": result}
    assert (
        deploy._exec_on_bastion_troshkad(
            host, "p", "10.0.0.50", "password", "hostname", 7
        )
        == expected
    )


def test_bastion_agent_transport_error_is_unavailable(agent):
    host, start, _ = agent
    start.side_effect = deploy.TroshkadError("offline")
    assert (
        deploy._exec_on_bastion_troshkad(
            host, "p", "10.0.0.50", "password", "hostname", 7
        )
        is None
    )


@pytest.fixture
def kubevirt(monkeypatch):
    host = SimpleNamespace(host_type="kubevirt-cluster", provider_id="provider")
    db = Mock()
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        id="provider"
    )
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    core = Mock()
    monkeypatch.setattr(
        "app.services.providers.kubevirt._get_k8s_clients",
        lambda _: (Mock(), core, Mock()),
    )
    monkeypatch.setattr(
        "app.services.providers.kubevirt._project_ns", lambda *_: "namespace"
    )
    pod = Mock(return_value=SimpleNamespace(metadata=SimpleNamespace(name="exec-pod")))
    monkeypatch.setattr("app.services.providers.kubevirt._find_exec_pod", pod)
    stream = Mock(return_value=" result \n")
    monkeypatch.setattr("kubernetes.stream.stream", stream)
    return host, db, pod, stream


@pytest.mark.parametrize("output,expected", [(" result \n", "result"), (None, "")])
def test_direct_kubevirt_oc_exec_uses_mounted_config(kubevirt, output, expected):
    host, db, _, stream = kubevirt
    stream.return_value = output
    assert deploy._exec_oc(host, "p", "oc get nodes", timeout=9) == expected
    assert stream.call_args.args[1:] == ("exec-pod", "namespace")
    assert stream.call_args.kwargs["command"] == [
        "sh",
        "-c",
        "export KUBECONFIG=/root/.kube/config; oc get nodes",
    ]
    assert stream.call_args.kwargs["_request_timeout"] == 14
    db.close.assert_called_once()


@pytest.mark.parametrize("missing", ["provider", "pod"])
def test_direct_kubevirt_exec_rejects_missing_infrastructure(kubevirt, missing):
    host, db, pod, stream = kubevirt
    if missing == "provider":
        db.query.return_value.filter_by.return_value.first.return_value = None
    else:
        pod.return_value = None
    with pytest.raises(RuntimeError):
        deploy._exec_oc(host, "p", "oc get nodes")
    db.close.assert_called_once()
    stream.assert_not_called()


def test_kubevirt_bastion_ssh_uses_key_and_filters_login_banner(kubevirt):
    host, db, _, stream = kubevirt
    stream.return_value = "OpenShift Console: secret\nUsername: admin\nPassword: secret\n\x1b[32mnode ready\x1b[0m\n"
    assert (
        deploy._exec_on_bastion(
            host, "p", "10.0.0.50", "unused", "hostname", timeout=20
        )
        == "node ready"
    )
    command = stream.call_args.kwargs["command"]
    assert "cloud-user@10.0.0.50" in command
    assert "/root/.ssh/id_ed25519" in command
    assert "ConnectTimeout=10" in command
    assert "unused" not in command
    db.close.assert_called_once()


@pytest.mark.parametrize("missing", ["provider", "pod", "stream"])
def test_kubevirt_bastion_exec_returns_none_for_unavailable_infrastructure(
    kubevirt, missing
):
    host, db, pod, stream = kubevirt
    if missing == "provider":
        db.query.return_value.filter_by.return_value.first.return_value = None
    elif missing == "pod":
        pod.return_value = None
    else:
        stream.side_effect = RuntimeError("unavailable")
    assert (
        deploy._exec_on_bastion_kubevirt(
            host, "p", "10.0.0.50", "unused", "hostname", 20
        )
        is None
    )
    db.close.assert_called_once()


def test_csr_approval_only_submits_pending_well_formed_rows(agent):
    host, start, wait = agent
    wait.side_effect = [
        {
            "status": "completed",
            "result": {
                "exit_code": 0,
                "output": "csr-pending 1m signer requester Pending\ncsr-approved 1m signer requester Approved\nmalformed Pending",
            },
        },
        {"status": "completed", "result": {"exit_code": 0, "output": "approved"}},
    ]
    assert deploy._approve_pending_csrs(host, "p", "10.0.0.50", "password") == 1
    assert start.call_count == 2
    assert (
        start.call_args.args[2]["command"]
        == "oc adm certificate approve csr-pending 2>/dev/null"
    )
