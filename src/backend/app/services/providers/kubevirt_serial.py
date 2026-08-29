"""KubeVirt serial console exec — shares troshka_serial session code with troshkad."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from troshka_serial.exec import (  # noqa: E402
    cap_serial_timeout,
    exec_serial_on_transport,
    serial_method_label,
)
from troshka_serial.ios import (  # noqa: E402
    SERIAL_BLANK_MESSAGE,
    clean_ios_output,
    ios_ensure_prompt,
    ios_exec_command,
    ios_poke_and_login,
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

_IOS_SERIAL_TYPES = frozenset(
    {
        "ios",
        "iosxe",
        "cisco_iosxe",
        "eos",
        "arista_eos",
    }
)

# KubeVirt pod-exec websockets drop around ~60s; keep each session under that.
_KUBEVIRT_SERIAL_SESSION_CAP = 45


def _serial_result(body: str, method: str, exit_code: int | None = 0) -> dict:
    return {
        "output": body,
        "error": "",
        "exit_code": exit_code,
        "method": method,
    }


def _ios_serial_user(username: str) -> str:
    user = username or "admin"
    if user in ("root", "cloud-user"):
        return "admin"
    return user


def _kubevirt_exec_ios_multisession(
    open_stream,
    commands: list[str],
    timeout_secs: float,
    username: str,
    password: str,
    method: str,
) -> dict:
    """Run IOS/EOS bootstrap one line per pod-exec session (avoids WS timeout)."""
    per_cmd = max(30, int(timeout_secs) // max(len(commands), 1))
    outputs: list[str] = []

    for index, line in enumerate(commands):
        first = index == 0
        session_timeout = min(
            _KUBEVIRT_SERIAL_SESSION_CAP,
            per_cmd + (25 if first else 15),
        )

        def work(transport, *, _first: bool = first, _line: str = line) -> str:
            if _first:
                err = ios_poke_and_login(
                    transport,
                    username,
                    password,
                    min(timeout_secs, 45),
                    allow_blank_abort=False,
                )
            else:
                err = ios_ensure_prompt(
                    transport,
                    min(20, session_timeout // 2),
                    username=username,
                    password=password,
                )
            if err:
                raise RuntimeError(err)
            return ios_exec_command(transport, _line, min(per_cmd, session_timeout - 5))

        outputs.append(run_stream_pexpect_session(open_stream, session_timeout, work))

    return _serial_result(
        "\n".join(chunk for chunk in outputs if chunk),
        method,
        0,
    )


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
    serial_type = (serial_exec_type or "linux").lower()
    commands = split_serial_commands(command)

    def open_stream():
        return _open_virt_launcher_serial_stream(provider, namespace, vm_name, timeout)

    if serial_type in _IOS_SERIAL_TYPES and len(commands) > 1:
        return _kubevirt_exec_ios_multisession(
            open_stream,
            commands,
            cap_serial_timeout(serial_type, timeout),
            _ios_serial_user(username),
            password or "",
            serial_method_label(serial_type),
        )

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
