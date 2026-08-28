"""Tests for shared Linux serial helpers."""

from troshka_serial.linux import clean_linux_tempfile_output, parse_marker_output


def test_parse_marker_output_extracts_body_and_exit_code():
    raw = "noise\nTROSHKA_BEGIN\nhello world\nTROSHKA_END 0\n$"
    body, code = parse_marker_output(raw)
    assert body == "hello world"
    assert code == 0


def test_clean_linux_tempfile_output_strips_artifacts():
    raw = "some output\n__a=MARKER\nreal output\ncat /tmp/out\n"
    result = clean_linux_tempfile_output(raw, "/tmp/out", "MARKER")
    assert "real output" in result
    assert "__a=" not in result
    assert "cat /tmp/out" not in result
