"""Shared Juniper Junos (vSRX) serial console helpers."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from troshka_serial.ios import SERIAL_BLANK_MESSAGE, split_serial_commands, strip_ansi

if TYPE_CHECKING:
    from troshka_serial.transport import SerialTransport

_FREEBSD_SHELL = re.compile(
    r"(?:root@|admin@)[^\r\n]+#\s*|login:\s*|[%#$]\s", re.I
)
_FREEBSD_SHELL_PROMPT = re.compile(r"root@[^\r\n]+#\s*", re.MULTILINE)
_JUNOS_CLI_PROMPT = re.compile(r"[>#]\s*")
_JUNOS_EDIT_PROMPT = re.compile(r"\[edit\]")
_JUNOS_BOOT_MORE = re.compile(r"---\(more \d+%\)---")
_JUNOS_CLI_MORE = re.compile(r"---\(more\)---")

JUNOS_LOGIN_PATTERNS = [
    _JUNOS_BOOT_MORE,
    re.compile(r"login:\s*", re.I),
    re.compile(r"Password:\s*", re.I),
    _FREEBSD_SHELL,
    _JUNOS_CLI_PROMPT,
]

JUNOS_EXEC_PATTERNS = [
    _JUNOS_BOOT_MORE,
    _JUNOS_CLI_MORE,
    _FREEBSD_SHELL_PROMPT,
]

JUNOS_CONFIGURE_PATTERNS = [
    _JUNOS_BOOT_MORE,
    _JUNOS_CLI_MORE,
    _JUNOS_EDIT_PROMPT,
    _JUNOS_CLI_PROMPT,
]


def junos_wrap_command(command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return cmd
    if cmd.startswith("cli "):
        return cmd
    return f"cli {cmd}"


def junos_clean_output(raw: str, command: str) -> str:
    text = strip_ansi(raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "")
    lines: list[str] = []
    for line in text.split("\n"):
        clean = line.strip()
        if not clean or clean == command.strip():
            continue
        if clean.startswith("root@") and clean.endswith("#"):
            continue
        if re.match(r"^---\(more(?: \d+%)?\)---$", clean):
            continue
        lines.append(clean)
    return "\n".join(lines)


def junos_login_response(pattern_idx: int) -> str | None:
    if pattern_idx == 0:
        return " "
    if pattern_idx == 1:
        return "root\r"
    if pattern_idx == 2:
        return "\r"
    if pattern_idx == 3:
        return None
    if pattern_idx == 4:
        return "exit\r"
    return "\r"


def junos_poke_and_login(transport: SerialTransport, timeout_secs: float) -> str | None:
    poke_timeout = min(timeout_secs, 45)
    blank_deadline = min(15, poke_timeout / 2)
    deadline = time.time() + poke_timeout
    started = time.time()
    saw_output = False
    while time.time() < deadline:
        if not saw_output and (time.time() - started) >= blank_deadline:
            return SERIAL_BLANK_MESSAGE
        transport.poke()
        idx, buf = transport.expect(
            JUNOS_LOGIN_PATTERNS, min(3, deadline - time.time())
        )
        if buf.strip():
            saw_output = True
        if idx is None:
            continue
        response = junos_login_response(idx)
        if response is None:
            return None
        transport.send(response)
    return "Console not responding"


def junos_exec_command(
    transport: SerialTransport, command: str, timeout_secs: float
) -> str:
    wrapped = junos_wrap_command(command)
    transport.read(0.3)
    transport.send(wrapped + "\r")
    chunks: list[str] = []
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        idx, buf = transport.expect(
            JUNOS_EXEC_PATTERNS, max(1, min(10, deadline - time.time()))
        )
        if buf:
            chunks.append(buf)
        if idx in (0, 1):
            transport.send(" ")
            continue
        if idx == 2 or idx is None:
            break
    return junos_clean_output("".join(chunks), wrapped)


def junos_needs_configure_session(commands: list[str]) -> bool:
    for cmd in commands:
        stripped = cmd.strip()
        if stripped.startswith(
            ("configure", "set ", "delete ", "activate ", "deactivate ")
        ):
            return True
    return False


def junos_exec_configure(
    transport: SerialTransport, commands: list[str], timeout_secs: float
) -> str:
    transport.send("cli\r")
    transport.expect(
        [_JUNOS_EDIT_PROMPT, _JUNOS_CLI_PROMPT, _JUNOS_BOOT_MORE, _JUNOS_CLI_MORE],
        30,
    )
    chunks: list[str] = []
    deadline = time.time() + timeout_secs
    per_cmd = max(60, timeout_secs // max(len(commands), 1))
    for cmd in commands:
        transport.send(cmd.strip() + "\r")
        cmd_deadline = time.time() + per_cmd
        while time.time() < cmd_deadline:
            idx, buf = transport.expect(
                JUNOS_CONFIGURE_PATTERNS,
                max(1, min(15, cmd_deadline - time.time())),
            )
            if buf:
                chunks.append(buf)
            if idx in (2, 3):
                break
            if idx in (0, 1):
                transport.send(" ")
    transport.send("exit\r")
    transport.expect([_FREEBSD_SHELL], 10)
    return junos_clean_output("".join(chunks), "configure")


def junos_exec_commands(
    transport: SerialTransport, commands: list[str], timeout_secs: float
) -> str:
    if junos_needs_configure_session(commands):
        return junos_exec_configure(transport, commands, timeout_secs)
    chunks: list[str] = []
    per_cmd = max(30, timeout_secs // max(len(commands), 1))
    for cmd in commands:
        chunks.append(junos_exec_command(transport, cmd, per_cmd))
    return "\n".join(chunk for chunk in chunks if chunk)


def network_junos_serial_exec(
    transport: SerialTransport, command: str, timeout_secs: float
) -> str:
    err = junos_poke_and_login(transport, timeout_secs)
    if err:
        raise RuntimeError(err)
    commands = split_serial_commands(command)
    if len(commands) == 1:
        return junos_exec_command(transport, commands[0], timeout_secs)
    return junos_exec_commands(transport, commands, timeout_secs)
