"""Shared IOS-XE / Arista EOS serial console helpers."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from troshka_serial.transport import SerialTransport

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

IOS_LOGIN_PATTERNS = [
    _IOS_INIT_DIALOG,
    _IOS_ENABLE_SECRET,
    _IOS_CONFIRM_SECRET,
    _IOS_SETUP_SELECT,
    _IOS_MORE,
    _IOS_LOGIN,
    _IOS_USERNAME,
    _IOS_PASSWORD,
    _IOS_PRESS_RETURN,
    _IOS_PROMPT,
]

_DEFAULT_IOS_ENABLE_SECRET = "Admin12345!"  # pragma: allowlist secret

SERIAL_BLANK_MESSAGE = (
    "Serial console has no output — guest may be on VGA only. "
    "Use the VNC console for boot/login output."
)


class UsernameRequiredError(Exception):
    """Raised when the console asks for a username but none was provided."""


def ios_enable_secret(password: str) -> str:
    if password and len(password) >= 10:
        return password
    return _DEFAULT_IOS_ENABLE_SECRET


def split_serial_commands(command: str) -> list[str]:
    if "\n" not in (command or ""):
        return [command]
    return [
        line
        for line in command.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07", "", text or "")


def clean_ios_output(raw: str, command: str) -> str:
    text = strip_ansi(raw).replace("\r\n", "\n").replace("\r", "")
    lines: list[str] = []
    for line in text.split("\n"):
        clean = line.strip()
        if not clean or clean == command.strip():
            continue
        if re.match(r"^[\w.-]+[>#]\s*$", clean):
            continue
        if re.match(r"^[\w.-]+(?:\([^)]*\))*[>#]\s*$", clean):
            continue
        if re.match(r"^--More--", clean, re.I):
            continue
        lines.append(clean)
    return "\n".join(lines)


def ios_login_response(
    pattern_idx: int,
    *,
    username: str,
    password: str,
    enable_secret: str,
) -> str | None:
    """Return text to send, or None when an exec/user prompt is reached."""
    if pattern_idx == 0:
        return "no\r"
    if pattern_idx == 1:
        return enable_secret + "\r"
    if pattern_idx == 2:
        return enable_secret + "\r"
    if pattern_idx == 3:
        return "2\r"
    if pattern_idx == 4:
        return " "
    if pattern_idx == 5:
        return (username or "admin") + "\r"
    if pattern_idx == 6:
        if not username:
            raise UsernameRequiredError()
        return username + "\r"
    if pattern_idx == 7:
        return (password or "") + "\r"
    if pattern_idx == 8:
        return "\r"
    if pattern_idx == 9:
        return None
    return "\r"


def ios_poke_and_login(
    transport: SerialTransport,
    username: str,
    password: str,
    timeout_secs: float,
) -> str | None:
    """Reach an IOS-XE / EOS prompt (> or #). Returns error message or None."""
    enable_secret = ios_enable_secret(password)
    poke_timeout = min(timeout_secs, 45)
    blank_deadline = min(15, poke_timeout / 3)
    deadline = time.time() + poke_timeout
    started = time.time()
    saw_output = False
    while time.time() < deadline:
        if not saw_output and (time.time() - started) >= blank_deadline:
            return SERIAL_BLANK_MESSAGE
        transport.poke()
        idx, buf = transport.expect(
            IOS_LOGIN_PATTERNS, min(3, deadline - time.time())
        )
        if buf.strip():
            saw_output = True
        if idx is None:
            continue
        try:
            response = ios_login_response(
                idx,
                username=username,
                password=password,
                enable_secret=enable_secret,
            )
        except UsernameRequiredError:
            return "Username required"
        if response is None:
            return None
        transport.send(response)
    return "Console not responding"


def ios_exec_command(
    transport: SerialTransport, command: str, timeout_secs: float
) -> str:
    transport.read(0.3)
    transport.send(command + "\r")
    buf = ""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        chunk = transport.read(0.5)
        if chunk:
            buf += chunk
        if _IOS_MORE.search(buf):
            transport.send(" ")
            continue
        if _IOS_PROMPT.search(buf):
            break
    time.sleep(0.15)
    return clean_ios_output(buf, command)


def ios_exec_commands(
    transport: SerialTransport, commands: list[str], timeout_secs: float
) -> str:
    chunks: list[str] = []
    per_cmd = max(30, timeout_secs // max(len(commands), 1))
    for cmd in commands:
        chunks.append(ios_exec_command(transport, cmd, per_cmd))
        time.sleep(0.2)
    return "\n".join(chunk for chunk in chunks if chunk)


def network_serial_exec(
    transport: SerialTransport,
    command: str,
    timeout_secs: float,
    username: str,
    password: str,
) -> str:
    err = ios_poke_and_login(transport, username, password, timeout_secs)
    if err:
        raise RuntimeError(err)
    commands = split_serial_commands(command)
    if len(commands) == 1:
        return ios_exec_command(transport, commands[0], timeout_secs)
    return ios_exec_commands(transport, commands, timeout_secs)
