"""Shared Linux serial console helpers."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from troshka_serial.ios import strip_ansi

if TYPE_CHECKING:
    from troshka_serial.transport import SerialTransport

_LOGIN = re.compile(r"login:\s*", re.I)
_PASSWORD = re.compile(r"[Pp]assword:\s*")
_SHELL_PROMPT = re.compile(r"[#$] ")
_ALT_PROMPT = re.compile(r"[>%] ")
_LAST_LOGIN = re.compile(r"Last login")
_LOGIN_FAILED = re.compile(r"incorrect", re.I)


def parse_marker_output(raw_output: str) -> tuple[str, int | None]:
    """Extract command output between TROSHKA_BEGIN / TROSHKA_END markers."""
    clean = strip_ansi(raw_output)
    begin_idx = clean.find("TROSHKA_BEGIN")
    end_idx = clean.find("TROSHKA_END")
    if begin_idx >= 0 and end_idx >= 0:
        body = clean[begin_idx + len("TROSHKA_BEGIN") : end_idx].strip()
        end_line = clean[end_idx:].split("\n")[0]
        exit_code_match = re.search(r"TROSHKA_END\s+(\d+)", end_line)
        exit_code = int(exit_code_match.group(1)) if exit_code_match else None
        return body, exit_code
    return clean.strip(), None


def clean_linux_tempfile_output(raw: str, outf: str, marker: str) -> str:
    """Strip echoed commands and marker artifacts from legacy tempfile capture."""
    text = strip_ansi(raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "")
    out_lines: list[str] = []
    for line in text.split("\n"):
        clean = line.strip()
        if not clean:
            continue
        if (
            "__a=" in clean
            or "__b=" in clean
            or f"cat {outf}" in clean
            or marker in clean
        ):
            continue
        if re.match(r"^\S+[>#\$%]\s", clean):
            clean = re.sub(r"^\S+[>#\$%]\s+", "", clean).strip()
        if clean:
            out_lines.append(clean)
    return "\n".join(out_lines)


def linux_poke_and_login(
    transport: SerialTransport,
    username: str,
    password: str,
    timeout_secs: float,
) -> str | None:
    transport.send("stty echo 2>/dev/null\r")
    transport.read(0.3)
    transport.send("\x03\r")
    transport.read(0.5)

    deadline = time.time() + min(timeout_secs, 60)
    while time.time() < deadline:
        idx, _ = transport.expect(
            [_LOGIN, _SHELL_PROMPT, _ALT_PROMPT],
            min(3, deadline - time.time()),
        )
        if idx == 0:
            if not password:
                return "VM is at login prompt but no password provided"
            transport.send(username + "\r")
            transport.expect([_PASSWORD], 5)
            transport.send(password + "\r")
            idx2, _ = transport.expect(
                [_SHELL_PROMPT, _LAST_LOGIN, _LOGIN_FAILED, _LOGIN],
                10,
            )
            if idx2 == 1:
                transport.expect([_SHELL_PROMPT], 5)
            elif idx2 != 0:
                return "Login failed"
            return None
        if idx in (1, 2):
            return None
        transport.poke()
    return "Console not responding"


def linux_exec_command(
    transport: SerialTransport, command: str, timeout_secs: float
) -> tuple[str, int | None]:
    transport.read(0.3)
    transport.send("echo TROSHKA_BEGIN\n")
    transport.read(1)
    transport.send(f"({command}) 2>&1; echo TROSHKA_END $?\n")
    output = ""
    deadline = time.time() + min(timeout_secs, 300)
    while time.time() < deadline:
        output += transport.read(2)
        if "TROSHKA_END" in output:
            break
    return parse_marker_output(output)


def linux_serial_exec(
    transport: SerialTransport,
    command: str,
    timeout_secs: float,
    username: str,
    password: str,
) -> tuple[str, int | None]:
    err = linux_poke_and_login(transport, username, password, timeout_secs)
    if err:
        raise RuntimeError(err)
    return linux_exec_command(transport, command, timeout_secs)
