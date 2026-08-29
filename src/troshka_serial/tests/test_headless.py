from troshka_serial.headless import serial_exec_needs_headless


def test_explicit_headless():
    assert serial_exec_needs_headless(headless=True) is True
    assert serial_exec_needs_headless(headless=False) is False


def test_serial_exec_type_eos():
    assert serial_exec_needs_headless(serial_exec_type="eos") is True
    assert serial_exec_needs_headless(serial_exec_type="linux") is False
    assert serial_exec_needs_headless(serial_exec_type="junos") is False
    assert serial_exec_needs_headless(serial_exec_type="ios") is False
