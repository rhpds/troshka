"""WebSocket VNC proxy — relays noVNC to KubeVirt VNC subresource API.

One active session per VM. If a second connection arrives for the same VM,
the old connection is closed with code 4010 ("Superseded") so the frontend
knows not to reconnect. The newest tab always wins.

Keeps the client WebSocket alive across VM restarts by reconnecting to
KubeVirt server-side, so noVNC never sees a disconnect during reboots.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
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

_active_sessions: dict[str, Any] = {}


def _get_kubevirt_vnc_url(vm_name: str) -> str:
    return (
        f"{K8S_HOST.replace('https://', 'wss://')}"
        f"/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{NAMESPACE}"
        f"/virtualmachineinstances/{vm_name}/vnc"
    )


def _read_token() -> str:
    if os.path.exists(_token_path):
        return open(_token_path).read().strip()
    return K8S_TOKEN


def _make_ssl_context() -> ssl.SSLContext:
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if os.path.exists(ca_path):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(ca_path)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # NOSONAR — dev/test only
    return ctx


async def _client_alive(ws_client: Any) -> bool:
    try:
        await asyncio.wait_for(ws_client.ping(), timeout=2)
        return True
    except Exception:
        return False


async def _proxy(ws_client: Any) -> None:
    path = ws_client.request.path if hasattr(ws_client, "request") else "/"
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        await ws_client.close(1008, "Missing VM name in path")
        return

    vm_name = parts[0]
    logger.info(f"VNC client connecting for {vm_name}")

    existing = _active_sessions.get(vm_name)
    if existing:
        logger.info(f"Rejecting connection for {vm_name} — already in use")
        try:
            await ws_client.close(4010, "Console in use by another session")
        except Exception:
            pass
        return

    _active_sessions[vm_name] = ws_client

    vnc_url = _get_kubevirt_vnc_url(vm_name)
    ssl_ctx = _make_ssl_context()

    max_retries = 30
    try:
        for attempt in range(max_retries):
            if _active_sessions.get(vm_name) is not ws_client:
                logger.info(f"Session for {vm_name} superseded, stopping")
                return

            headers = {"Authorization": f"Bearer {_read_token()}"}
            try:
                async with websockets.connect(
                    vnc_url,
                    additional_headers=headers,
                    ssl=ssl_ctx,
                    subprotocols=[websockets.Subprotocol("binary")],
                    max_size=None,
                    compression=None,
                ) as ws_kubevirt:
                    if attempt:
                        logger.info(
                            f"Reconnected to KubeVirt VNC for {vm_name} (attempt {attempt + 1})"
                        )
                    else:
                        logger.info(f"Connected to KubeVirt VNC for {vm_name}")

                    async def client_to_kv() -> None:
                        try:
                            async for msg in ws_client:
                                await ws_kubevirt.send(msg)
                        except websockets.exceptions.ConnectionClosed:
                            pass

                    async def kv_to_client() -> None:
                        try:
                            async for msg in ws_kubevirt:
                                await ws_client.send(msg)
                        except websockets.exceptions.ConnectionClosed:
                            pass

                    t1 = asyncio.create_task(client_to_kv())
                    t2 = asyncio.create_task(kv_to_client())
                    _done, pending = await asyncio.wait(
                        [t1, t2], return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

                if _active_sessions.get(vm_name) is not ws_client:
                    return

                if not await _client_alive(ws_client):
                    logger.info(f"Client disconnected for {vm_name}")
                    return

                logger.info(f"KubeVirt VNC dropped for {vm_name}, reconnecting...")
                await asyncio.sleep(2)
                continue

            except websockets.exceptions.ConnectionClosed:
                logger.info(f"Client disconnected for {vm_name}")
                return
            except Exception as e:
                if _active_sessions.get(vm_name) is not ws_client:
                    return
                if attempt < max_retries - 1:
                    logger.info(
                        f"VNC for {vm_name} not ready (attempt {attempt + 1}), retrying in 3s: {e}"
                    )
                    if not await _client_alive(ws_client):
                        logger.info(
                            f"Client disconnected while waiting for {vm_name}"
                        )
                        return
                    await asyncio.sleep(3)
                else:
                    logger.error(
                        f"VNC proxy giving up on {vm_name} after {max_retries} attempts: {e}"
                    )
                    try:
                        await ws_client.close(1011, str(e)[:120])
                    except Exception:
                        pass
    finally:
        if _active_sessions.get(vm_name) is ws_client:
            _active_sessions.pop(vm_name, None)


STATUS_PORT = int(os.environ.get("STATUS_PORT", "8081"))


async def _handle_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """HTTP endpoint: GET /active/{vm_name} → 200 if in use, 404 if free."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = request_line.decode().strip().split()
        path = parts[1] if len(parts) > 1 else "/"
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line == b"\r\n" or line == b"\n" or not line:
                break

        if path.startswith("/active/"):
            vm_name = path[8:]
            if vm_name in _active_sessions:
                body = b'{"in_use":true}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n" + body)
            else:
                body = b'{"in_use":false}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n" + body)
        elif path.startswith("/kick/"):
            vm_name = path[6:]
            ws = _active_sessions.pop(vm_name, None)
            if ws:
                try:
                    asyncio.ensure_future(ws.close(4010, "Kicked"))
                except Exception:
                    pass
                logger.info(f"Kicked session for {vm_name}")
                body = b'{"kicked":true}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n" + body)
            else:
                body = b'{"kicked":false}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n" + body)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"active\":" + str(len(_active_sessions)).encode() + b"}")
    except Exception:
        pass
    finally:
        writer.close()


async def main() -> None:
    status_server = await asyncio.start_server(_handle_status, "0.0.0.0", STATUS_PORT)
    logger.info(f"VNC status endpoint on port {STATUS_PORT}")

    async with websockets.serve(
        _proxy,
        "0.0.0.0",
        LISTEN_PORT,
        max_size=None,
        ping_interval=30,
        ping_timeout=10,
        compression=None,
    ):
        logger.info(f"VNC proxy listening on port {LISTEN_PORT}")
        async with status_server:
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
