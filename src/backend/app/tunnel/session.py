"""Multiplexed tunnel session: one dial transport, many TCP streams."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from app.services.ocp import api_tunnel as tun
from app.tunnel.framing import pack_frame, unpack_frame
from app.tunnel.limits import (
    DIAL_TIMEOUT_S,
    MAX_STREAMS_PER_SESSION,
    STREAM_IDLE_S,
)

logger = logging.getLogger(__name__)


class MultiplexSession:
    """Bridge a WebSocket to nested API TCP via multiplexed streams."""

    def __init__(
        self,
        websocket,
        *,
        project_id: str,
        host,
        provider,
        targets: list,
    ) -> None:
        self.websocket = websocket
        self.project_id = project_id
        self.host = host
        self.provider = provider
        self.targets = targets
        self._via = ""
        self._tls_server_name = ""
        self._force_insecure = False
        self._pf = None
        self._pf_port = 0
        self._candidate = None
        self._streams: dict[int, asyncio.Task] = {}
        self._stream_socks: dict[int, Any] = {}
        self._closed = False

    async def run(self) -> None:
        try:
            ready = await self._dial()
            await self.websocket.send_json(ready)
            await self._loop()
        finally:
            await self._shutdown()

    async def _dial(self) -> dict:
        last_err: Exception | None = None
        for candidate in self.targets:
            try:
                if candidate.via == "kubevirt-pf":
                    if not self.provider:
                        raise RuntimeError("No provider for kubevirt tunnel")
                    pf, port = await asyncio.wait_for(
                        asyncio.to_thread(
                            tun.open_kubevirt_portforward_handle,
                            self.provider,
                            candidate,
                        ),
                        timeout=DIAL_TIMEOUT_S,
                    )
                    self._pf = pf
                    self._pf_port = port
                elif candidate.via in ("kubevirt-exec", "troshkad", "direct"):
                    # Per-stream dial; just remember the winning candidate.
                    pass
                else:
                    raise RuntimeError(f"unknown via {candidate.via}")
                self._candidate = candidate
                self._via = candidate.via
                self._tls_server_name = candidate.tls_server_name or ""
                self._force_insecure = bool(candidate.force_insecure)
                return {
                    "type": "ready",
                    "via": self._via,
                    "tls_server_name": self._tls_server_name,
                    "force_insecure": self._force_insecure,
                    "multiplex": True,
                }
            except Exception as exc:
                last_err = exc
                logger.info(
                    "tunnel dial %s:%s via=%s failed (%s); trying next",
                    candidate.host,
                    candidate.port,
                    candidate.via,
                    exc,
                )
                await self._close_pf()
        raise RuntimeError(f"All dial paths failed: {last_err}")

    async def _loop(self) -> None:
        while not self._closed:
            try:
                message = await asyncio.wait_for(
                    self.websocket.receive(), timeout=STREAM_IDLE_S
                )
            except TimeoutError:
                # Idle session — keep waiting (streams have their own traffic).
                if not self._streams:
                    break
                continue
            except Exception:
                break
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            if message.get("text") is not None:
                await self._handle_control(message["text"])
                continue
            data = message.get("bytes")
            if not data:
                continue
            try:
                stream_id, payload = unpack_frame(data)
            except ValueError:
                continue
            sock = self._stream_socks.get(stream_id)
            if sock is None:
                continue
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, sock.sendall, payload
                )
            except Exception:
                await self._close_stream(stream_id)

    async def _handle_control(self, text: str) -> None:
        import json

        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        typ = msg.get("type")
        stream_id = int(msg.get("stream_id") or 0)
        if typ == "open":
            await self._open_stream(stream_id)
        elif typ == "close":
            await self._close_stream(stream_id)

    async def _open_stream(self, stream_id: int) -> None:
        if stream_id in self._streams:
            await self.websocket.send_json(
                {"type": "error", "stream_id": stream_id, "error": "stream exists"}
            )
            return
        if len(self._streams) >= MAX_STREAMS_PER_SESSION:
            await self.websocket.send_json(
                {
                    "type": "error",
                    "stream_id": stream_id,
                    "error": "too many streams",
                }
            )
            return
        try:
            sock = await asyncio.wait_for(
                asyncio.to_thread(self._open_stream_sock),
                timeout=DIAL_TIMEOUT_S,
            )
        except Exception as exc:
            await self.websocket.send_json(
                {
                    "type": "error",
                    "stream_id": stream_id,
                    "error": str(exc)[:200],
                }
            )
            return
        self._stream_socks[stream_id] = sock
        task = asyncio.create_task(self._sock_to_ws(stream_id, sock))
        self._streams[stream_id] = task
        await self.websocket.send_json({"type": "opened", "stream_id": stream_id})

    def _open_stream_sock(self):
        candidate = self._candidate
        if candidate is None:
            raise RuntimeError("not dialed")
        if candidate.via == "kubevirt-pf":
            if self._pf is None:
                raise RuntimeError("portforward missing")
            return tun.open_socket_from_portforward(self._pf, self._pf_port)
        if candidate.via == "kubevirt-exec":
            if not self.provider:
                raise RuntimeError("No provider")
            return tun.open_kubevirt_exec_relay(self.provider, candidate)
        if candidate.via == "troshkad":
            return tun.open_troshkad_tunnel_socket(
                self.host,
                self.project_id,
                candidate.host,
                candidate.port,
            )
        # direct — blocking connect
        raw = socket.create_connection(
            (candidate.host, candidate.port), timeout=DIAL_TIMEOUT_S
        )
        raw.setblocking(True)
        return raw

    async def _sock_to_ws(self, stream_id: int, sock) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, sock.recv, 65536)
                if not data:
                    break
                await self.websocket.send_bytes(pack_frame(stream_id, data))
        except Exception:
            logger.debug("stream %s closed", stream_id, exc_info=True)
        finally:
            await self._close_stream(stream_id, notify=True)

    async def _close_stream(self, stream_id: int, *, notify: bool = False) -> None:
        task = self._streams.pop(stream_id, None)
        sock = self._stream_socks.pop(stream_id, None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if task and task is not asyncio.current_task():
            task.cancel()
        if notify:
            try:
                await self.websocket.send_json(
                    {"type": "close", "stream_id": stream_id}
                )
            except Exception:
                pass

    async def _close_pf(self) -> None:
        pf = self._pf
        self._pf = None
        if pf is not None:
            try:
                await asyncio.to_thread(pf.close)
            except Exception:
                pass

    async def _shutdown(self) -> None:
        self._closed = True
        for sid in list(self._streams):
            await self._close_stream(sid)
        await self._close_pf()
        try:
            await self.websocket.close()
        except Exception:
            pass
