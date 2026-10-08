"""Binary multiplex framing for troshka-tunnel WebSocket sessions.

Binary frame: ``uint32_be stream_id`` + payload.
Control messages are JSON text frames (``open`` / ``opened`` / ``close`` / …).
"""

from __future__ import annotations

import struct

_HEADER = struct.Struct(">I")


def pack_frame(stream_id: int, payload: bytes) -> bytes:
    if stream_id < 0 or stream_id > 0xFFFFFFFF:
        raise ValueError(f"invalid stream_id {stream_id}")
    return _HEADER.pack(stream_id) + payload


def unpack_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < _HEADER.size:
        raise ValueError("frame too short")
    (stream_id,) = _HEADER.unpack_from(data)
    return stream_id, data[_HEADER.size :]
