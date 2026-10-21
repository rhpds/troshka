"""Provider execution contracts at the Kubernetes streaming boundary."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.providers import kubevirt as kv


@pytest.fixture
def clients():
    provider = MagicMock()
    provider.get_credentials.return_value = {"namespace": "troshka"}
    custom, core = MagicMock(), MagicMock()
    with patch.object(kv, "_get_k8s_clients", return_value=(custom, core, MagicMock())):
        yield provider, custom, core


def pod(name, phase="Running"):
    compute = SimpleNamespace(name="compute", ready=True)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase=phase, container_statuses=[compute]),
    )


def stream_response(output):
    ws = MagicMock()
    ws.is_open.side_effect = [True, False]
    ws.peek_stdout.return_value = True
    ws.peek_stderr.return_value = True
    ws.read_stdout.return_value = output
    return ws


def guest_launcher(core):
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[pod("virt-launcher-troshka-vm-abcdefgh-xyz")]
    )


@pytest.mark.parametrize(
    "info",
    [
        '{"return":{"supported_commands":[{"name":"guest-exec","enabled":true}]}}',
        "unparsable agent info",
    ],
)
def test_guest_agent_runs_command_and_decodes_completed_output(clients, info):
    provider, _, core = clients
    guest_launcher(core)
    result = {
        "return": {
            "exited": True,
            "exitcode": 7,
            "out-data": base64.b64encode(b"output\n").decode(),
            "err-data": base64.b64encode(b"failure\n").decode(),
        }
    }
    responses = [
        stream_response(info),
        stream_response('{"return":{"pid":42}}'),
        stream_response('{"return":{"exited":false}}'),
        stream_response(json.dumps(result)),
    ]
    with patch(
        "kubernetes.stream.stream", side_effect=["domain\n", *responses]
    ) as stream, patch.object(kv.time, "sleep"):
        actual = kv.kubevirt_exec_guest_agent(
            provider, "project1", "abcdefgh-long", "exit 7", timeout=10
        )
    assert actual == {
        "output": "output\n",
        "error": "failure\n",
        "exit_code": 7,
        "method": "guest-agent",
    }
    payload = json.loads(stream.call_args_list[2].kwargs["command"][3])
    assert payload["arguments"]["arg"] == ["-c", "exit 7"]
    for ws in responses:
        ws.close.assert_called_once()


@pytest.mark.parametrize(
    "responses,message",
    [
        ([""], "No libvirt domain"),
        (["domain", "error: guest agent disconnected"], "Guest agent not available"),
        (
            [
                "domain",
                '{"return":{"supported_commands":[{"name":"guest-exec","enabled":false}]}}',
            ],
            "guest-exec is disabled",
        ),
        (["domain", "{}", '{"return":{}}'], "No PID"),
    ],
)
def test_guest_agent_rejects_unavailable_execution_before_polling(
    clients, responses, message
):
    provider, _, core = clients
    guest_launcher(core)
    stream_values = [responses[0], *[stream_response(value) for value in responses[1:]]]
    with patch("kubernetes.stream.stream", side_effect=stream_values), pytest.raises(
        RuntimeError, match=message
    ):
        kv.kubevirt_exec_guest_agent(provider, "project1", "abcdefgh", "id")


def vnc_pods(core):
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            pod("virt-launcher-old", "Pending"),
            pod("virt-launcher-troshka-vm-abcdefgh-xyz"),
            pod("exec-project1"),
        ]
    )


@pytest.mark.parametrize(
    "pods,message",
    [
        ([], "No running virt-launcher"),
        ([pod("virt-launcher-troshka-vm-abcdefgh-xyz")], "No running exec pod"),
    ],
)
def test_vnc_requires_both_running_launcher_and_ocr_tools(clients, pods, message):
    provider, _, core = clients
    core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    with pytest.raises(RuntimeError, match=message):
        kv.kubevirt_exec_vnc(provider, "project1", "abcdefgh", "user", "secret", "id")


def test_vnc_login_handles_wakeup_username_submission_password_and_shell():
    keys, text, screenshot = MagicMock(), MagicMock(), MagicMock()
    screenshot.side_effect = [
        "",
        "host login:",
        "host login: user",
        "Password:",
        "user@host ~]$",
    ]
    with patch.object(kv.time, "sleep"):
        assert (
            kv._vnc_login(
                keys, text, screenshot, kv._detect_vnc_state, "user", "secret"
            )
            is True
        )
    assert [call.args[0] for call in text.call_args_list] == ["user\n", "secret\n"]
    assert keys.call_count == 2


def test_vnc_exec_reads_ocr_markers_and_restores_console(clients):
    provider, _, core = clients
    vnc_pods(core)
    launcher, tools = MagicMock(), MagicMock()
    launcher.return_value = "domain"
    tools.side_effect = [
        "",
        "user@host ~]$",
        "",
        "TROSHKA_BEGIN\nhello\nTROSHKA_EXIT 3",
    ]
    with patch.object(
        kv, "_make_pod_exec_fn", side_effect=[launcher, tools]
    ), patch.object(kv.time, "sleep"):
        result = kv.kubevirt_exec_vnc(
            provider, "project1", "abcdefgh", "user", "secret", "echo hello", timeout=10
        )
    assert result == {"output": "hello", "error": "", "exit_code": 3, "method": "vnc"}
    assert launcher.call_args.args[0][-3:] == ["KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F1"]
    assert tools.call_count == 4


def test_vnc_exec_reports_failed_login_without_running_command(clients):
    provider, _, core = clients
    vnc_pods(core)
    launcher, tools = MagicMock(), MagicMock()
    launcher.return_value = "domain"
    tools.return_value = "not ready"
    with patch.object(
        kv, "_make_pod_exec_fn", side_effect=[launcher, tools]
    ), patch.object(kv.time, "sleep"):
        result = kv.kubevirt_exec_vnc(
            provider, "project1", "abcdefgh", "user", "secret", "id"
        )
    assert result["exit_code"] is None
    assert "Could not reach shell" in result["error"]
    assert tools.call_count == 12


def test_vnc_no_domain_fails_before_sending_credentials(clients):
    provider, _, core = clients
    vnc_pods(core)
    execute = MagicMock(return_value="")
    with patch.object(kv, "_make_pod_exec_fn", return_value=execute), pytest.raises(
        RuntimeError, match="No libvirt domain"
    ):
        kv.kubevirt_exec_vnc(provider, "project1", "abcdefgh", "user", "secret", "id")
    execute.assert_called_once()


def test_empty_vnc_screenshot_does_not_invoke_ocr():
    launcher, tools = MagicMock(return_value=""), MagicMock()
    assert kv._vnc_screenshot_ocr(launcher, tools, "domain") == ""
    tools.assert_not_called()
    assert launcher.call_args.args[0] == ["rm", "-f", "/tmp/troshka-screen.ppm"]


def test_vnc_stream_closes_when_remote_read_fails():
    ws = MagicMock()
    ws.is_open.return_value = True
    ws.update.side_effect = RuntimeError("stream lost")
    with patch("kubernetes.stream.stream", return_value=ws):
        execute = kv._make_pod_exec_fn(MagicMock(), "launcher", "lab", "compute")
        with pytest.raises(RuntimeError, match="stream lost"):
            execute(["virsh", "list"])
    ws.close.assert_called_once()


def test_console_exec_collects_chunks_and_always_closes_session(clients):
    provider, _, _ = clients
    ws = MagicMock()
    with (
        patch.object(kv, "_create_console_ws", return_value=ws),
        patch.object(
            kv,
            "_console_ws_read",
            side_effect=["root#", "", "", "TROSHKA_BEGIN\nhello\n", "TROSHKA_END 0\n"],
        ),
        patch.object(kv, "_console_ws_send") as send,
    ):
        result = kv.kubevirt_exec_console(
            provider, "project1", "abcdefgh", "root", "secret", "echo hello"
        )
    assert result == {
        "output": "hello",
        "error": "",
        "exit_code": 0,
        "method": "console",
    }
    assert send.call_args.args[1] == "(echo hello) 2>&1; echo TROSHKA_END $?\n"
    ws.close.assert_called_once()


def test_console_preserves_read_error_when_close_also_fails(clients):
    provider, _, _ = clients
    ws = MagicMock()
    ws.close.side_effect = RuntimeError("close failed")
    with patch.object(kv, "_create_console_ws", return_value=ws), patch.object(
        kv, "_console_ws_read", side_effect=RuntimeError("read failed")
    ):
        with pytest.raises(RuntimeError, match="read failed"):
            kv.kubevirt_exec_console(
                provider, "project1", "abcdefgh", "root", "secret", "id"
            )
    ws.close.assert_called_once()


def test_proxy_route_preserves_source_service_tls_and_port(clients):
    provider, custom, _ = clients
    spec = {
        "to": {"kind": "Service", "name": "showroom"},
        "port": {"targetPort": 8080},
        "tls": {"termination": "edge"},
    }
    custom.get_namespaced_custom_object.return_value = {"spec": spec}
    driver = kv.KubeVirtDriver()
    with patch.object(
        kv, "_route_apps_domain", return_value="apps.example.com"
    ), patch.object(kv, "_create_or_update_route") as create:
        name = driver.create_app_proxy_route(
            provider, "project1", "proxy-name", "showroom"
        )
    assert name == "proxy-name"
    body = create.call_args.args[2]
    assert body["spec"]["to"] == spec["to"]
    assert body["spec"]["port"] == spec["port"]
    assert body["spec"]["tls"] == spec["tls"]
    assert body["spec"]["host"].endswith(".apps.example.com")
    assert body["metadata"]["labels"]["troshka-project"] == "project1"


def test_proxy_route_missing_source_does_not_create_route(clients):
    provider, custom, _ = clients
    custom.get_namespaced_custom_object.side_effect = RuntimeError("API unavailable")
    with patch.object(kv, "_create_or_update_route") as create:
        assert (
            kv.KubeVirtDriver().create_app_proxy_route(
                provider, "project1", "proxy", "missing"
            )
            == ""
        )
    create.assert_not_called()


def test_showroom_route_lookup_falls_back_to_legacy_port(clients):
    provider, custom, _ = clients
    custom.get_namespaced_custom_object.side_effect = [
        RuntimeError("missing"),
        {"spec": {"host": "legacy.example.com"}},
    ]
    result = kv.KubeVirtDriver().find_showroom_route(
        provider, "project1", "showroom", 80
    )
    assert result == {"hostname": "legacy.example.com", "route_name": "rt-showroom-80"}
    assert [
        call.kwargs["name"]
        for call in custom.get_namespaced_custom_object.call_args_list
    ] == ["showroom", "rt-showroom-80"]


def test_showroom_route_lookup_deduplicates_candidates_when_all_missing(clients):
    provider, custom, _ = clients
    custom.get_namespaced_custom_object.side_effect = RuntimeError("missing")
    assert (
        kv.KubeVirtDriver().find_showroom_route(provider, "project1", "showroom", 80)
        is None
    )
    assert [
        call.kwargs["name"]
        for call in custom.get_namespaced_custom_object.call_args_list
    ] == ["showroom", "rt-showroom-80", "rt-showroom-443"]
