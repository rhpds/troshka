"""WebSocket VNC proxy — multiplexes one upstream KubeVirt VNC connection
across multiple browser clients for shared console viewing.

Late-joining clients get a cached RFB handshake replay, then receive
the same framebuffer broadcast as all other viewers. Input from any
client is forwarded upstream (if two people type simultaneously, that's
their problem).

When the upstream drops (VM reboot/stop), all clients are closed and
the frontend reconnects naturally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import struct
from typing import Any

import websockets
from kubernetes import client, config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vnc-proxy")

NAMESPACE = os.environ.get("NAMESPACE", "default")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

config.load_incluster_config()
_cfg = client.Configuration.get_default_copy()
K8S_HOST = (
    _cfg.host
    or f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', '172.30.0.1')}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
)
_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_TOKEN = open(_token_path).read().strip() if os.path.exists(_token_path) else ""


def _get_kubevirt_vnc_url(vm_name):
    return (
        f"{K8S_HOST.replace('https://', 'wss://')}"
        f"/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{NAMESPACE}"
        f"/virtualmachineinstances/{vm_name}/vnc"
    )


def _read_token():
    if os.path.exists(_token_path):
        return open(_token_path).read().strip()
    return K8S_TOKEN


def _make_ssl_context():
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if os.path.exists(ca_path):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(ca_path)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # NOSONAR — dev/test only
    return ctx


async def _recv_bytes(ws) -> bytes:
    msg = await ws.recv()
    return msg if isinstance(msg, bytes) else msg.encode()


async def _send_bytes(ws, data):
    await ws.send(data if isinstance(data, bytes) else data.encode())


class _SharedSession:
    """Single upstream VNC connection shared by multiple browser clients."""

    def __init__(self, vm_name: str):
        self.vm_name = vm_name
        self.upstream = None
        self.clients: dict[int, Any] = {}
        self._next_id = 0
        self._server_handshake: list[bytes] = []
        self._fb_width = 0
        self._fb_height = 0
        self._handshake_done = asyncio.Event()
        self._broadcast_task: asyncio.Task | None = None
        self._closing = False

    async def join(self, ws_client) -> None:
        """Add a client to this session. Blocks until client disconnects."""
        cid = self._next_id
        self._next_id += 1

        if not self.upstream:
            try:
                await self._connect_and_handshake(ws_client)
            except Exception:
                self._closing = True
                self._handshake_done.set()
                raise
            self.clients[cid] = ws_client
            self._broadcast_task = asyncio.create_task(self._broadcast_loop())
            logger.info(f"First client {cid} joined session for {self.vm_name}")
        else:
            await self._handshake_done.wait()
            if self._closing:
                raise ConnectionError("Session closed")
            await self._replay_handshake(ws_client)
            self.clients[cid] = ws_client
            await self._request_full_update()
            logger.info(
                f"Client {cid} joined session for {self.vm_name} "
                f"({len(self.clients)} viewers)"
            )

        try:
            async for msg in ws_client:
                if isinstance(msg, bytes) and self.upstream and not self._closing:
                    try:
                        await self.upstream.send(msg)
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            self.clients.pop(cid, None)
            logger.info(
                f"Client {cid} left session for {self.vm_name} "
                f"({len(self.clients)} remaining)"
            )
            if not self.clients:
                await self._teardown()

    async def _connect_and_handshake(self, first_client):
        """Establish upstream and relay real RFB handshake with the first client."""
        headers = {"Authorization": f"Bearer {_read_token()}"}
        url = _get_kubevirt_vnc_url(self.vm_name)

        self.upstream = await websockets.connect(
            url,
            additional_headers=headers,
            ssl=_make_ssl_context(),
            subprotocols=[websockets.Subprotocol("binary")],
            max_size=None,
            compression=None,
        )
        logger.info(f"Upstream VNC connected for {self.vm_name}")

        # Step 1: Version exchange
        srv_version = await _recv_bytes(self.upstream)
        self._server_handshake.append(srv_version)
        await first_client.send(srv_version)
        cli_version = await first_client.recv()
        await _send_bytes(self.upstream, cli_version)

        # Step 2: Security types
        srv_security = await _recv_bytes(self.upstream)
        self._server_handshake.append(srv_security)
        await first_client.send(srv_security)
        cli_security = await first_client.recv()
        await _send_bytes(self.upstream, cli_security)

        # Step 3: Security result
        srv_result = await _recv_bytes(self.upstream)
        self._server_handshake.append(srv_result)
        await first_client.send(srv_result)

        # Step 4: Shared flag (client → server only)
        cli_shared = await first_client.recv()
        await _send_bytes(self.upstream, cli_shared)

        # Step 5: ServerInit
        srv_init = await _recv_bytes(self.upstream)
        self._server_handshake.append(srv_init)
        await first_client.send(srv_init)

        if len(srv_init) >= 4:
            self._fb_width, self._fb_height = struct.unpack(">HH", srv_init[:4])

        self._handshake_done.set()

    async def _replay_handshake(self, ws_client):
        """Replay cached RFB handshake for a late-joining client."""
        hs = self._server_handshake

        # Version exchange
        await ws_client.send(hs[0])
        await ws_client.recv()

        # Security types
        await ws_client.send(hs[1])
        await ws_client.recv()

        # Security result
        await ws_client.send(hs[2])

        # Shared flag
        await ws_client.recv()

        # ServerInit
        await ws_client.send(hs[3])

    async def _request_full_update(self):
        """Ask QEMU for a non-incremental framebuffer update so the new client sees something."""
        if self.upstream and self._fb_width and self._fb_height:
            msg = struct.pack(">BBHHHH", 3, 0, 0, 0, self._fb_width, self._fb_height)
            try:
                await self.upstream.send(msg)
            except Exception:
                pass

    async def _broadcast_loop(self):
        """Read upstream VNC frames and send to all connected clients."""
        if not self.upstream:
            return
        try:
            async for msg in self.upstream:
                dead = []
                for cid, ws in list(self.clients.items()):
                    try:
                        await asyncio.wait_for(ws.send(msg), timeout=5)
                    except Exception:
                        dead.append(cid)
                for cid in dead:
                    self.clients.pop(cid, None)
                    logger.info(f"Dropped slow/dead client {cid} for {self.vm_name}")
        except Exception as e:
            logger.info(f"Upstream VNC dropped for {self.vm_name}: {e}")
        finally:
            self._closing = True
            for ws in list(self.clients.values()):
                try:
                    await ws.close(1001, "VNC upstream closed")
                except Exception:
                    pass

    async def _teardown(self):
        self._closing = True
        if self._broadcast_task:
            self._broadcast_task.cancel()
        if self.upstream:
            try:
                await self.upstream.close()
            except Exception:
                pass
            self.upstream = None
        logger.info(f"Session for {self.vm_name} torn down")


_sessions: dict[str, _SharedSession] = {}
_sessions_lock = asyncio.Lock()


async def _proxy(ws_client):
    path = ws_client.request.path if hasattr(ws_client, "request") else "/"
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        await ws_client.close(1008, "Missing VM name in path")
        return

    vm_name = parts[0]
    logger.info(f"VNC client connecting for {vm_name}")

    async with _sessions_lock:
        session = _sessions.get(vm_name)
        if not session or session._closing:
            session = _SharedSession(vm_name)
            _sessions[vm_name] = session

    try:
        await session.join(ws_client)
    except Exception as e:
        logger.error(f"VNC session error for {vm_name}: {e}")
        try:
            await ws_client.close(1011, str(e)[:120])
        except Exception:
            pass
    finally:
        async with _sessions_lock:
            if _sessions.get(vm_name) is session and not session.clients:
                _sessions.pop(vm_name, None)


async def main():
    async with websockets.serve(
        _proxy,
        "0.0.0.0",
        LISTEN_PORT,
        max_size=None,
        ping_interval=30,
        ping_timeout=10,
        compression=None,
    ):
        logger.info(f"VNC proxy (multiplexer) listening on port {LISTEN_PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
