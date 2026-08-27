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
_IOS_USERNAME = re.compile(r"(?i)Username:\s*")
_IOS_LOGIN = re.compile(r"(?i)login:\s*")
_IOS_PASSWORD = re.compile(r"(?i)Password:\s*")
_IOS_PRESS_RETURN = re.compile(r"(?i)Press RETURN to get started")
_IOS_INIT_DIALOG = re.compile(r"(?i)initial configuration dialog\?")
_IOS_ENABLE_SECRET = re.compile(r"(?i)Enter enable secret:")
_IOS_CONFIRM_SECRET = re.compile(r"(?i)Confirm enable secret:")
_IOS_SETUP_SELECT = re.compile(r"Enter your selection \[2\]:")
_IOS_MORE = re.compile(r"--More--", re.I)
_IOS_USER_PROMPT = re.compile(r"[\w.-]+>\s*")
_IOS_EXEC_PROMPT = re.compile(r"[\w.-]+#\s*")
_DEFAULT_IOS_ENABLE_SECRET = "Admin12345!"  # pragma: allowlist secret


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07", "", text or "")


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
            if data:
                buf += data
        except websocket.WebSocketTimeoutException:
            pass
        for idx, pat in enumerate(patterns):
            if pat.search(buf):
                return idx, buf
        _console_ws_send(ws, "\r")
    return None, buf


_SERIAL_BLANK_MESSAGE = (
    "Serial console has no output — guest may be on VGA only. "
    "Use the VNC console for boot/login output."
)


def _split_serial_commands(command: str) -> list[str]:
    if "\n" not in (command or ""):
        return [command]
    return [
        line
        for line in command.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def _ios_enable_secret(password: str) -> str:
    if password and len(password) >= 10:
        return password
    return _DEFAULT_IOS_ENABLE_SECRET


def _ios_clean_output(raw: str, command: str) -> str:
    text = _strip_ansi(raw).replace("\r\n", "\n").replace("\r", "")
    lines: list[str] = []
    for line in text.split("\n"):
        clean = line.strip()
        if not clean or clean == command.strip():
            continue
        if re.match(r"^[\w.-]+[>#]\s*$", clean):
            continue
        if re.match(r"^--More--", clean, re.I):
            continue
        lines.append(clean)
    return "\n".join(lines)


def _ios_poke_and_login(
    ws,
    username: str,
    password: str,
    timeout_secs: float,
) -> str | None:
    """Reach an IOS-XE / EOS exec prompt (> or #)."""
    enable_secret = _ios_enable_secret(password)
    patterns = [
        _IOS_INIT_DIALOG,
        _IOS_ENABLE_SECRET,
        _IOS_CONFIRM_SECRET,
        _IOS_SETUP_SELECT,
        _IOS_MORE,
        _IOS_LOGIN,
        _IOS_USERNAME,
        _IOS_PASSWORD,
        _IOS_PRESS_RETURN,
        _IOS_EXEC_PROMPT,
        _IOS_USER_PROMPT,
    ]
    poke_timeout = min(timeout_secs, 30)
    blank_deadline = min(15, poke_timeout / 2)
    deadline = time.time() + poke_timeout
    started = time.time()
    saw_output = False
    while time.time() < deadline:
        if not saw_output and (time.time() - started) >= blank_deadline:
            return _SERIAL_BLANK_MESSAGE
        idx, buf = _ws_expect(ws, patterns, min(3, deadline - time.time()))
        if buf.strip():
            saw_output = True
        if idx is None:
            continue
        if idx == 0:
            _console_ws_send(ws, "no\r")
        elif idx == 1:
            _console_ws_send(ws, enable_secret + "\r")
        elif idx == 2:
            _console_ws_send(ws, enable_secret + "\r")
        elif idx == 3:
            _console_ws_send(ws, "2\r")
        elif idx == 4:
            _console_ws_send(ws, " ")
        elif idx == 5:
            user = username or "admin"
            _console_ws_send(ws, user + "\r")
        elif idx == 6:
            if not username:
                return "Username required"
            _console_ws_send(ws, username + "\r")
        elif idx == 7:
            _console_ws_send(ws, (password or "") + "\r")
        elif idx == 8:
            _console_ws_send(ws, "\r")
        elif idx == 9:
            return None
        elif idx == 10:
            _console_ws_send(ws, "enable\r")
    return "Console not responding"


def _ios_exec_command(ws, command: str, timeout_secs: float) -> str:
    _console_ws_send(ws, command + "\r")
    buf = ""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        _, chunk = _ws_expect(ws, [_IOS_MORE], min(3, deadline - time.time()))
        if chunk:
            buf += chunk
        if _IOS_MORE.search(buf):
            _console_ws_send(ws, " ")
            continue
        if command not in buf:
            continue
        tail = buf.split(command, 1)[1]
        if _IOS_EXEC_PROMPT.search(tail):
            break
    return _ios_clean_output(buf, command)


def _ios_exec_commands(ws, commands: list[str], timeout_secs: float) -> str:
    chunks: list[str] = []
    per_cmd = max(30, timeout_secs // max(len(commands), 1))
    for cmd in commands:
        chunks.append(_ios_exec_command(ws, cmd, per_cmd))
    return "\n".join(chunk for chunk in chunks if chunk)


def _network_serial_exec(
    ws,
    command: str,
    timeout_secs: float,
    username: str,
    password: str,
    method: str,
) -> dict:
    err = _ios_poke_and_login(ws, username, password, timeout_secs)
    if err:
        raise RuntimeError(err)
    commands = _split_serial_commands(command)
    if len(commands) == 1:
        body = _ios_exec_command(ws, commands[0], timeout_secs)
    else:
        body = _ios_exec_commands(ws, commands, timeout_secs)
    return {
        "output": body,
        "error": "",
        "exit_code": 0,
        "method": method,
    }


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
    poke_timeout = min(timeout_secs, 45)
    blank_deadline = min(15, poke_timeout / 2)
    deadline = time.time() + poke_timeout
    started = time.time()
    saw_output = False
    while time.time() < deadline:
        if not saw_output and (time.time() - started) >= blank_deadline:
            return _SERIAL_BLANK_MESSAGE
        idx, buf = _ws_expect(ws, patterns, min(3, deadline - time.time()))
        if buf.strip():
            saw_output = True
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

        if serial_type in ("ios", "iosxe", "cisco_iosxe"):
            return _network_serial_exec(
                ws,
                command,
                timeout,
                username or "admin",
                password,
                "serial-ios",
            )

        if serial_type in ("eos", "arista_eos"):
            return _network_serial_exec(
                ws,
                command,
                timeout,
                username or "admin",
                password,
                "serial-eos",
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
