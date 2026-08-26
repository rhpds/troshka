"""KubeVirt serial console exec (WebSocket), with network-OS CLI handlers."""

from __future__ import annotations

import re
import time

from app.services.providers.kubevirt import (
    _console_ws_read,
    _console_ws_send,
    _create_console_ws,
    _parse_console_output,
    _project_ns,
)

_FREEBSD_SHELL = re.compile(r"(?:root@|admin@)[^\r\n]+#\s*|login:\s*", re.I)
_FREEBSD_SHELL_PROMPT = re.compile(r"root@[^\r\n]+#\s*", re.MULTILINE)
_JUNOS_CLI_PROMPT = re.compile(r"[>%]\s*")
_JUNOS_BOOT_MORE = re.compile(r"---\(more \d+%\)---")
_JUNOS_CLI_MORE = re.compile(r"---\(more\)---")
_IOS_PROMPT = re.compile(r"[>#]\s*")
_IOS_MORE = re.compile(r" --More-- ")


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text or "")


def _ws_expect(
    ws, patterns: list[re.Pattern[str]], timeout_secs: float
) -> tuple[int | None, str]:
    """Read from serial WS until one pattern matches or timeout."""
    import websocket

    buf = ""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            ws.settimeout(0.5)
            data = ws.recv()
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            buf += data
        except websocket.WebSocketTimeoutException:
            pass
        for idx, pat in enumerate(patterns):
            if pat.search(buf):
                return idx, buf
        _console_ws_send(ws, "\r")
    return None, buf


def _junos_wrap_command(command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return cmd
    if cmd.startswith("cli "):
        return cmd
    return f"cli {cmd}"


def _junos_clean_output(raw: str, command: str) -> str:
    text = _strip_ansi(raw)
    for token in (command, "\r", "\n"):
        text = text.replace(token, "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines).strip()


def _junos_poke_and_login(ws, timeout_secs: float) -> str | None:
    patterns = [
        _JUNOS_BOOT_MORE,
        re.compile(r"login:\s*", re.I),
        re.compile(r"Password:\s*", re.I),
        _FREEBSD_SHELL,
        _JUNOS_CLI_PROMPT,
    ]
    deadline = time.time() + min(timeout_secs, 120)
    while time.time() < deadline:
        idx, _buf = _ws_expect(ws, patterns, min(3, deadline - time.time()))
        if idx is None:
            continue
        if idx == 0:
            _console_ws_send(ws, " ")
        elif idx == 1:
            _console_ws_send(ws, "root\r")
        elif idx == 2:
            _console_ws_send(ws, "\r")
        elif idx == 3:
            return None
        elif idx == 4:
            _console_ws_send(ws, "exit\r")
    return "Console not responding"


def _junos_exec_command(ws, command: str, timeout_secs: float) -> str:
    wrapped = _junos_wrap_command(command)
    _console_ws_send(ws, wrapped + "\r")
    chunks: list[str] = []
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        idx, buf = _ws_expect(
            ws,
            [_JUNOS_BOOT_MORE, _JUNOS_CLI_MORE, _FREEBSD_SHELL_PROMPT],
            min(10, deadline - time.time()),
        )
        if buf:
            chunks.append(buf)
        if idx in (0, 1):
            _console_ws_send(ws, " ")
        elif idx == 2:
            break
        elif idx is None:
            break
    return _junos_clean_output("".join(chunks), wrapped)


def _linux_poke_and_login(
    ws, username: str, password: str, timeout_secs: float
) -> str | None:
    combined = _console_ws_read(ws, 2)
    _console_ws_send(ws, "\n")
    combined += _console_ws_read(ws, 2)
    lower = combined.lower()
    if "login:" in lower:
        _console_ws_send(ws, f"{username}\n")
        _console_ws_read(ws, 2)
        _console_ws_send(ws, f"{password}\n")
        resp = _console_ws_read(ws, 3)
        if "login incorrect" in resp.lower():
            return "Console login failed"
    elif "password:" in lower:
        _console_ws_send(ws, f"{password}\n")
        resp = _console_ws_read(ws, 3)
        if "login incorrect" in resp.lower():
            return "Console login failed"
    return None


def _linux_exec_command(
    ws, command: str, timeout_secs: float
) -> tuple[str, int | None]:
    _console_ws_send(ws, "echo TROSHKA_BEGIN\n")
    _console_ws_read(ws, 1)
    _console_ws_send(ws, f"({command}) 2>&1; echo TROSHKA_END $?\n")
    output = ""
    deadline = time.time() + min(timeout_secs, 300)
    while time.time() < deadline:
        output += _console_ws_read(ws, 2)
        if "TROSHKA_END" in output:
            break
    return _parse_console_output(output)


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
    """Execute a command via the KubeVirt serial console WebSocket."""
    from app.services.providers.kubevirt import _get_k8s_clients

    _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"
    serial_type = (serial_exec_type or "linux").lower()

    ws = None
    try:
        ws = _create_console_ws(provider, namespace, vm_name, timeout)

        if serial_type in ("junos", "juniper_junos"):
            err = _junos_poke_and_login(ws, timeout)
            if err:
                raise RuntimeError(err)
            body = _junos_exec_command(ws, command, timeout)
            return {
                "output": body,
                "error": "",
                "exit_code": 0,
                "method": "serial-junos",
            }

        if serial_type in ("ios", "iosxe", "cisco_iosxe", "eos", "arista_eos"):
            raise RuntimeError(
                f"serial exec type {serial_type!r} is not implemented for KubeVirt yet"
            )

        effective_user = username or "root"
        err = _linux_poke_and_login(ws, effective_user, password, timeout)
        if err:
            raise RuntimeError(err)
        body, exit_code = _linux_exec_command(ws, command, timeout)
        return {
            "output": body,
            "error": "",
            "exit_code": exit_code,
            "method": "serial",
        }
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
