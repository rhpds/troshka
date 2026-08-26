"""Tests for KubeVirt serial console exec helpers."""

from unittest.mock import MagicMock, patch

from app.services.providers.kubevirt_serial import (
    _junos_clean_output,
    _junos_wrap_command,
    kubevirt_exec_serial,
)


def test_junos_wrap_command_adds_cli_prefix():
    assert _junos_wrap_command("show version") == "cli show version"
    assert _junos_wrap_command("cli show route") == "cli show route"
    assert _junos_wrap_command("") == ""


def test_junos_clean_output_strips_prompt_noise():
    raw = "\r\ncli show version\r\nHostname: rtr3\r\n"
    assert _junos_clean_output(raw, "cli show version") == "Hostname: rtr3"


def test_kubevirt_exec_serial_junos():
    mock_ws = MagicMock()

    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt_serial._create_console_ws",
            return_value=mock_ws,
        ),
        patch(
            "app.services.providers.kubevirt_serial._junos_poke_and_login",
            return_value=None,
        ),
        patch(
            "app.services.providers.kubevirt_serial._junos_exec_command",
            return_value="Hostname: rtr3",
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
    mock_ws.close.assert_called_once()


def test_kubevirt_exec_serial_ios_not_implemented():
    import pytest

    with (
        patch(
            "app.services.providers.kubevirt._get_k8s_clients",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch(
            "app.services.providers.kubevirt_serial._create_console_ws",
            return_value=MagicMock(),
        ),
    ):
        with pytest.raises(RuntimeError, match="not implemented"):
            kubevirt_exec_serial(
                provider=MagicMock(),
                project_id="proj-1",
                vm_id="281550eb-d04b-40ef-be8f-5bf8debe9fa4",
                command="show version",
                timeout=60,
                serial_exec_type="ios",
            )
