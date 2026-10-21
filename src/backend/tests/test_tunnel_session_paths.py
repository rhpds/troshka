"""Multiplexed stream isolation, transport selection, and cleanup contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tunnel.framing import pack_frame, unpack_frame
from app.tunnel.session import MultiplexSession


def session():
    return MultiplexSession(
        AsyncMock(), project_id="project", host=object(), provider=object(), targets=[]
    )


def candidate(via):
    return SimpleNamespace(
        via=via,
        host="api.test",
        port=6443,
        tls_server_name="api.test",
        force_insecure=False,
    )


@pytest.mark.parametrize("via", ["direct", "troshkad", "kubevirt-exec"])
def test_dial_records_transport_without_opening_stream(via):
    s = session()
    target = candidate(via)
    s.targets = [target]
    result = asyncio.run(s._dial())
    assert result == {
        "type": "ready",
        "via": via,
        "tls_server_name": "api.test",
        "force_insecure": False,
        "multiplex": True,
    }
    assert s._candidate is target
    assert not s._stream_socks


def test_failed_dial_releases_previous_portforward_and_tries_next():
    s = session()
    pf = MagicMock()
    s._pf = pf
    s.targets = [candidate("unsupported"), candidate("direct")]
    assert asyncio.run(s._dial())["via"] == "direct"
    pf.close.assert_called_once_with()
    assert s._pf is None


def test_portforward_dial_tracks_handle_and_port(monkeypatch):
    s = session()
    s.targets = [candidate("kubevirt-pf")]
    pf = MagicMock()
    opener = MagicMock(return_value=(pf, 12345))
    monkeypatch.setattr(
        "app.tunnel.session.tun.open_kubevirt_portforward_handle", opener
    )
    assert asyncio.run(s._dial())["via"] == "kubevirt-pf"
    assert s._pf is pf
    assert s._pf_port == 12345


def test_failed_run_always_closes_resources():
    s = session()
    s.provider = None
    s.targets = [candidate("kubevirt-pf")]
    operation = s.run()
    with pytest.raises(RuntimeError, match="All dial paths failed"):
        asyncio.run(operation)
    s.websocket.close.assert_awaited_once()
    assert s._closed


def test_receive_loop_dispatches_controls_and_frames_and_survives_active_idle():
    s = session()
    s._streams[1] = MagicMock()
    frame = pack_frame(1, b"request")
    s.websocket.receive.side_effect = [
        TimeoutError(),
        {"text": "control"},
        {"bytes": b""},
        {"bytes": frame},
        {"type": "websocket.disconnect"},
    ]
    s._handle_control = AsyncMock()
    s._forward_frame = AsyncMock()
    asyncio.run(s._loop())
    s._handle_control.assert_awaited_once_with("control")
    s._forward_frame.assert_awaited_once_with(frame)


def test_receive_error_exits_loop():
    s = session()
    s.websocket.receive.side_effect = OSError("closed")
    asyncio.run(s._loop())
    s.websocket.receive.assert_awaited_once()


def test_frames_ignore_malformed_and_unknown_streams():
    s = session()
    sock = MagicMock()
    s._stream_socks[1] = sock
    asyncio.run(s._forward_frame(b"x"))
    asyncio.run(s._forward_frame(pack_frame(2, b"unknown")))
    sock.sendall.assert_not_called()
    asyncio.run(s._forward_frame(pack_frame(1, b"request")))
    sock.sendall.assert_called_once_with(b"request")


def test_send_failure_closes_only_failed_stream():
    s = session()
    broken = MagicMock()
    broken.sendall.side_effect = OSError("closed")
    other = MagicMock()
    s._stream_socks = {1: broken, 2: other}
    asyncio.run(s._forward_frame(pack_frame(1, b"request")))
    broken.close.assert_called_once()
    assert s._stream_socks == {2: other}
    other.close.assert_not_called()


@pytest.mark.parametrize("message", ["not json", '{"type":"ignored"}'])
def test_unknown_control_is_ignored(message):
    s = session()
    s._open_stream = AsyncMock()
    s._close_stream = AsyncMock()
    asyncio.run(s._handle_control(message))
    s._open_stream.assert_not_awaited()
    s._close_stream.assert_not_awaited()


@pytest.mark.parametrize("kind", ["open", "close"])
def test_control_dispatches_stream_id(kind):
    s = session()
    s._open_stream = AsyncMock()
    s._close_stream = AsyncMock()
    asyncio.run(s._handle_control(json.dumps({"type": kind, "stream_id": 7})))
    handler = s._open_stream if kind == "open" else s._close_stream
    handler.assert_awaited_once_with(7)


def test_stream_limit_and_duplicate_stream_return_errors(monkeypatch):
    s = session()
    s._streams[1] = MagicMock()
    monkeypatch.setattr("app.tunnel.session.MAX_STREAMS_PER_SESSION", 1)
    asyncio.run(s._open_stream(1))
    asyncio.run(s._open_stream(2))
    assert [
        call.args[0]["error"] for call in s.websocket.send_json.await_args_list
    ] == [
        "stream exists",
        "too many streams",
    ]


def test_stream_dial_failure_is_reported_without_registering_stream():
    s = session()
    s._open_stream_sock = MagicMock(side_effect=OSError("unreachable"))
    asyncio.run(s._open_stream(1))
    assert not s._streams
    assert not s._stream_socks
    s.websocket.send_json.assert_awaited_once_with(
        {"type": "error", "stream_id": 1, "error": "unreachable"}
    )


def test_open_stream_registers_and_shutdown_cancels_reader():
    async def exercise():
        s = session()
        sock = MagicMock()
        s._open_stream_sock = MagicMock(return_value=sock)
        started = asyncio.Event()

        async def reader(*_args):
            started.set()
            await asyncio.Event().wait()

        s._sock_to_ws = reader
        await s._open_stream(3)
        await started.wait()
        task = s._streams[3]
        s.websocket.send_json.assert_awaited_once_with(
            {"type": "opened", "stream_id": 3}
        )
        await s._shutdown()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        sock.close.assert_called_once()
        assert not s._streams and not s._stream_socks

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "via,helper",
    [
        ("kubevirt-pf", "open_socket_from_portforward"),
        ("kubevirt-exec", "open_kubevirt_exec_relay"),
        ("troshkad", "open_troshkad_tunnel_socket"),
    ],
)
def test_open_socket_uses_selected_transport(monkeypatch, via, helper):
    s = session()
    s._candidate = candidate(via)
    s._pf = object()
    s._pf_port = 12345
    opener = MagicMock()
    monkeypatch.setattr("app.tunnel.session.tun." + helper, opener)
    assert s._open_stream_sock() is opener.return_value
    assert opener.call_count == 1


def test_direct_socket_is_blocking(monkeypatch):
    s = session()
    s._candidate = candidate("direct")
    connect = MagicMock()
    monkeypatch.setattr("app.tunnel.session.socket.create_connection", connect)
    assert s._open_stream_sock() is connect.return_value
    assert connect.call_args.args[0] == ("api.test", 6443)
    connect.return_value.setblocking.assert_called_once_with(True)


@pytest.mark.parametrize(
    "via,error",
    [
        (None, "not dialed"),
        ("kubevirt-pf", "portforward missing"),
        ("kubevirt-exec", "No provider"),
    ],
)
def test_missing_transport_prerequisites_fail(via, error):
    s = session()
    s._candidate = candidate(via) if via else None
    s.provider = None
    with pytest.raises(RuntimeError, match=error):
        s._open_stream_sock()


@pytest.mark.parametrize("failure", [False, True])
def test_socket_reader_frames_data_and_notifies_close(failure):
    s = session()
    sock = MagicMock()
    sock.recv.side_effect = [b"response", OSError("closed") if failure else b""]
    s._stream_socks[5] = sock
    asyncio.run(s._sock_to_ws(5, sock))
    assert unpack_frame(s.websocket.send_bytes.await_args.args[0]) == (5, b"response")
    sock.close.assert_called_once()
    s.websocket.send_json.assert_awaited_once_with({"type": "close", "stream_id": 5})


def test_shutdown_tolerates_close_failures():
    s = session()
    sock = MagicMock()
    sock.close.side_effect = OSError("closed")
    s._stream_socks[1] = sock
    s._streams[1] = MagicMock()
    s._pf = MagicMock()
    s._pf.close.side_effect = OSError("closed")
    s.websocket.close.side_effect = OSError("closed")
    asyncio.run(s._shutdown())
    assert s._closed and s._pf is None
    assert not s._stream_socks and not s._streams


def test_shutdown_closes_every_stream_while_removing_registry_entries():
    s = session()
    sockets = [MagicMock(), MagicMock()]
    s._stream_socks = dict(enumerate(sockets))
    s._streams = {key: MagicMock() for key in s._stream_socks}
    asyncio.run(s._shutdown())
    assert not s._streams and not s._stream_socks
    for sock in sockets:
        sock.close.assert_called_once_with()
