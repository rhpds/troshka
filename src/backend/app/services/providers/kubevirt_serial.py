"""KubeVirt serial console exec — shares troshka_serial session code with troshkad."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from troshka_serial.exec import exec_serial_on_transport  # noqa: E402
from troshka_serial.ios import (  # noqa: E402
    SERIAL_BLANK_MESSAGE,
    clean_ios_output,
    split_serial_commands,
)
from troshka_serial.junos import (  # noqa: E402
    junos_clean_output,
    junos_wrap_command,
)
from troshka_serial.pexpect_session import run_stream_pexpect_session  # noqa: E402

from app.services.providers.kubevirt import (  # noqa: E402
    _get_k8s_clients,
    _open_virt_launcher_serial_stream,
    _project_ns,
)

# Backward-compat aliases for tests
_SERIAL_BLANK_MESSAGE = SERIAL_BLANK_MESSAGE
_ios_clean_output = clean_ios_output
_split_serial_commands = split_serial_commands
_junos_clean_output = junos_clean_output
_junos_wrap_command = junos_wrap_command


def _serial_result(body: str, method: str, exit_code: int | None = 0) -> dict:
    return {
        "output": body,
        "error": "",
        "exit_code": exit_code,
        "method": method,
    }


def kubevirt_exec_serial(
    provider,
    project_id: str,
    vm_id: str,
    command: str,
    timeout: int = 600,
    serial_exec_type: str = "linux",
    username: str = "root",
    password: str = "",
):
    """Execute a command via the KubeVirt serial console (pexpect + virt-launcher)."""
    _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    def open_stream():
        return _open_virt_launcher_serial_stream(provider, namespace, vm_name, timeout)

    def work(transport):
        output, exit_code, method = exec_serial_on_transport(
            transport,
            serial_exec_type,
            command,
            timeout,
            username=username,
            password=password,
        )
        return _serial_result(output, method, exit_code if exit_code is not None else 0)

    return run_stream_pexpect_session(open_stream, timeout, work)
