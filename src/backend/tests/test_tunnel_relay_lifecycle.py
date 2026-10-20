"""Relay framing, response draining, and cleanup on transport failure."""

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.services.ocp.api_tunnel import (
    bridge_websocket_to_socket,
    bridge_websocket_to_tcp,
)


def websocket():
    return AsyncMock(
        receive=AsyncMock(
            side_effect=[
                {"bytes": b"request"},
                {"text": "text"},
                {"bytes": b""},
                {"type": "websocket.disconnect"},
            ]
        )
    )


def test_tcp_drains_response_after_client_disconnect():
    async def scenario():
        ws = websocket()
        closed = asyncio.Event()
        writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
        writer.close.side_effect = closed.set

        async def delayed_response(_size):
            await closed.wait()
            return b"last response"

        reader = AsyncMock()
        reader.read.side_effect = delayed_response

        async def finish_response(_data):
            reader.read.side_effect = None
            reader.read.return_value = b""

        ws.send_bytes.side_effect = finish_response
        await asyncio.wait_for(bridge_websocket_to_tcp(ws, reader, writer), 2)
        assert writer.write.call_args_list == [call(b"request"), call(b"text")]
        ws.send_bytes.assert_awaited_once_with(b"last response")
        writer.wait_closed.assert_awaited_once()
        ws.close.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [TimeoutError, OSError])
def test_tcp_transport_failure_still_closes_writer(failure):
    ws = websocket()
    writer = MagicMock(
        drain=AsyncMock(side_effect=failure), wait_closed=AsyncMock(side_effect=OSError)
    )
    reader = AsyncMock(read=AsyncMock(return_value=b""))
    asyncio.run(bridge_websocket_to_tcp(ws, reader, writer))
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    ws.close.assert_awaited_once()


@pytest.mark.parametrize("receive_failure", [False, True])
def test_socket_prefetch_and_cleanup_even_when_portforward_close_fails(receive_failure):
    ws = websocket()
    sock = MagicMock()
    sock._troshka_prefetch = b"prefetched"
    sock.recv.side_effect = TimeoutError if receive_failure else [b"response", b""]
    sock._troshka_portforward.close.side_effect = OSError("already closed")
    asyncio.run(bridge_websocket_to_socket(ws, sock))
    assert sock.sendall.call_args_list == [call(b"request"), call(b"text")]
    expected = [call(b"prefetched")]
    if not receive_failure:
        expected.append(call(b"response"))
    assert ws.send_bytes.await_args_list == expected
    sock.shutdown.assert_called_once_with(socket.SHUT_WR)
    sock._troshka_portforward.close.assert_called_once()
    sock.close.assert_called_once()
    ws.close.assert_awaited_once()


def test_socket_cancellation_releases_resources():
    async def scenario():
        ws = websocket()
        started = asyncio.Event()

        async def blocked_receive():
            started.set()
            await asyncio.Future()

        ws.receive.side_effect = blocked_receive
        sock = MagicMock()
        sock._troshka_prefetch = b""
        sock.recv.return_value = b""
        task = asyncio.create_task(bridge_websocket_to_socket(ws, sock))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sock.shutdown.assert_called_once_with(socket.SHUT_WR)
        sock.close.assert_called_once()
        sock._troshka_portforward.close.assert_called_once()

    asyncio.run(scenario())
