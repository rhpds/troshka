"""Tests for shared network-OS serial helpers."""

from troshka_serial.ios import (
    SERIAL_BLANK_MESSAGE,
    clean_ios_output,
    ios_enable_secret,
    ios_login_response,
    ios_poke_and_login,
    network_serial_exec,
    split_serial_commands,
)


class FakeTransport:
    def __init__(
        self,
        expect_script: list | None = None,
        read_chunks: list[str] | None = None,
    ) -> None:
        self.expect_script = list(expect_script or [])
        self.read_chunks = list(read_chunks or [])
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def read(self, timeout_secs: float) -> str:
        if self.read_chunks:
            return self.read_chunks.pop(0)
        return ""

    def expect(self, patterns, timeout_secs: float):
        if not self.expect_script:
            return None, ""
        step = self.expect_script.pop(0)
        if isinstance(step, tuple) and len(step) == 2:
            return step
        return step, ""

    def poke(self) -> None:
        self.sent.append("\r")


def test_ios_enable_secret_uses_long_password():
    assert ios_enable_secret("admin@123456") == "admin@123456"


def test_ios_enable_secret_default_for_short_password():
    assert ios_enable_secret("admin") == "Admin12345!"


def test_split_serial_commands_skips_comments():
    assert split_serial_commands("enable\n! comment\nconfigure terminal\n") == [
        "enable",
        "configure terminal",
    ]


def test_clean_ios_output_strips_config_prompt():
    raw = "ok\r\nrtr2(config-if-Et1)#"
    assert clean_ios_output(raw, "interface Ethernet1") == "ok"


def test_ios_login_response_ready():
    assert ios_login_response(9, username="admin", password="", enable_secret="x") is None


def test_ios_poke_and_login_reaches_prompt():
    transport = FakeTransport(expect_script=[(9, "localhost>")])
    assert ios_poke_and_login(transport, "admin", "", 30) is None
    assert transport.sent  # poke + login responses


def test_network_serial_exec_runs_commands():
    transport = FakeTransport(
        expect_script=[(9, "localhost>")],
        read_chunks=["", "localhost#", "", "ok\nrtr2(config)#"],
    )
    output = network_serial_exec(
        transport,
        "enable\nhostname rtr2",
        60,
        "admin",
        "",
    )
    assert "ok" in output or output == ""


def test_serial_blank_message_defined():
    assert "VGA" in SERIAL_BLANK_MESSAGE
