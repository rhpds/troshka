"""Tests for KubeVirt serial console exec helpers."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.providers.kubevirt_serial import (
    _SERIAL_BLANK_MESSAGE,
    _ios_clean_output,
    _junos_clean_output,
    _junos_wrap_command,
    _split_serial_commands,
    kubevirt_exec_serial,
)


def test_junos_wrap_command_adds_cli_prefix():
    assert _junos_wrap_command("show version") == "cli show version"
    assert _junos_wrap_command("cli show route") == "cli show route"
    assert _junos_wrap_command("") == ""


def test_junos_clean_output_strips_prompt_noise():
    raw = "\r\ncli show version\r\nHostname: rtr3\r\n"
    assert _junos_clean_output(raw, "cli show version") == "Hostname: rtr3"


def test_ios_clean_output_strips_config_prompt_noise():
    raw = "ok\r\nrtr2(config-if-Et1)#"
    assert _ios_clean_output(raw, "interface Ethernet1") == "ok"


def test_ios_clean_output_strips_prompt_noise():
    raw = "show version\r\nCisco IOS XE Software\r\nRouter>"
    assert _ios_clean_output(raw, "show version") == "Cisco IOS XE Software"


def test_split_serial_commands_skips_comments():
    assert _split_serial_commands("show version") == ["show version"]
    assert _split_serial_commands("show ip int brief\n! comment\nshow clock") == [
        "show ip int brief",
        "show clock",
    ]


def _mock_kubevirt_serial_session(return_value):
    return patch(
        "app.services.providers.kubevirt_serial.run_stream_pexpect_session",
        side_effect=lambda _open_stream, _timeout, work: return_value
        if not callable(return_value)
        else work(MagicMock()),
    )


def test_kubevirt_exec_serial_junos():
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        _mock_kubevirt_serial_session(
            {
                "output": "Hostname: rtr3",
                "error": "",
                "exit_code": 0,
                "method": "serial-junos",
            }
        ),
    ):
        result = kubevirt_exec_serial(
            provider=MagicMock(),
            project_id="proj-1",
            vm_id="281550eb-d04b-40ef-be8f-5bf8debe9fa4",
            command="show version",
            timeout=60,
            serial_exec_type="junos",
        )

    assert result["method"] == "serial-junos"
    assert result["output"] == "Hostname: rtr3"
    assert result["exit_code"] == 0


def test_kubevirt_exec_serial_ios():
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        _mock_kubevirt_serial_session(
            {
                "output": "Cisco IOS XE Software",
                "error": "",
                "exit_code": None,
                "method": "serial-ios",
            }
        ),
    ):
        result = kubevirt_exec_serial(
            provider=MagicMock(),
            project_id="proj-1",
            vm_id="281550eb-d04b-40ef-be8f-5bf8debe9fa4",
            command="show version",
            timeout=60,
            serial_exec_type="ios",
            username="admin",
            password="secret",
        )

    assert result["method"] == "serial-ios"
    assert result["output"] == "Cisco IOS XE Software"


def test_kubevirt_exec_serial_junos_blank_console():
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt_serial.run_stream_pexpect_session",
            side_effect=RuntimeError(_SERIAL_BLANK_MESSAGE),
        ),
    ):
        with pytest.raises(RuntimeError, match="VGA only"):
            kubevirt_exec_serial(
                provider=MagicMock(),
                project_id="proj-1",
                vm_id="281550eb-d04b-40ef-be8f-5bf8debe9fa4",
                command="show version",
                timeout=60,
                serial_exec_type="junos",
            )


def test_kubevirt_exec_serial_ios_blank_console():
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt_serial.run_stream_pexpect_session",
            side_effect=RuntimeError(_SERIAL_BLANK_MESSAGE),
        ),
    ):
        with pytest.raises(RuntimeError, match="VGA only"):
            kubevirt_exec_serial(
                provider=MagicMock(),
                project_id="proj-1",
                vm_id="10f3160c-d04b-40ef-be8f-5bf8debe9fa4",
                command="show version",
                timeout=60,
                serial_exec_type="ios",
            )


def test_kubevirt_exec_serial_eos():
    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        _mock_kubevirt_serial_session(
            {
                "output": "Arista EOS",
                "error": "",
                "exit_code": None,
                "method": "serial-eos",
            }
        ),
    ):
        result = kubevirt_exec_serial(
            provider=MagicMock(),
            project_id="proj-1",
            vm_id="281550eb-d04b-40ef-be8f-5bf8debe9fa4",
            command="show version",
            timeout=60,
            serial_exec_type="eos",
        )

    assert result["method"] == "serial-eos"
