"""Multiplex framing for troshka-tunnel."""

from app.tunnel.framing import pack_frame, unpack_frame


def test_round_trip():
    raw = pack_frame(7, b"hello")
    sid, payload = unpack_frame(raw)
    assert sid == 7
    assert payload == b"hello"


def test_empty_payload():
    sid, payload = unpack_frame(pack_frame(1, b""))
    assert sid == 1
    assert payload == b""


def test_stream_id_zero():
    sid, payload = unpack_frame(pack_frame(0, b"x"))
    assert sid == 0
    assert payload == b"x"
