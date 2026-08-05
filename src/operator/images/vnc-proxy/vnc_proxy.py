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
_background_tasks: set[asyncio.Task] = set()


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
        ctx = ssl.SSLContext(  # NOSONAR — in-cluster CA may not match hostname
            ssl.PROTOCOL_TLS_CLIENT
        )
        ctx.load_verify_locations(ca_path)
    else:
        ctx = ssl.create_default_context()  # NOSONAR — dev/test fallback
        ctx.check_hostname = False  # NOSONAR
        ctx.verify_mode = ssl.CERT_NONE  # NOSONAR
    return ctx


async def _client_alive(ws_client: Any) -> bool:
    try:
        await asyncio.wait_for(ws_client.ping(), timeout=2)
        return True
    except Exception:
        return False


async def _parse_ws_path(ws_client: Any) -> tuple[str | None, bool]:
    """Parse WebSocket path for VM name and force flag. Returns (vm_name, force)."""
    path = ws_client.request.path if hasattr(ws_client, "request") else "/"
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        await ws_client.close(1008, "Missing VM name in path")
        return None, False
    force = parts[0] == "force" and len(parts) > 1
    vm_name = parts[1] if force else parts[0]
    return vm_name, force


async def _evict_or_reject(vm_name: str, force: bool, ws_client: Any) -> bool:
    """Handle existing session. Returns True if the caller should stop."""
    existing = _active_sessions.get(vm_name)
    if existing and force:
        logger.info(f"Force-kicking existing session for {vm_name}")
        try:
            await existing.close(4010, "Kicked")
        except Exception:
            pass
        _active_sessions.pop(vm_name, None)
        return False
    if existing:
        logger.info(f"Rejecting connection for {vm_name} — already in use")
        try:
            await ws_client.close(4010, "Console in use by another session")
        except Exception:
            pass
        return True
    return False


async def _relay_bidirectional(ws_client: Any, ws_kubevirt: Any) -> None:
    """Relay WebSocket messages bidirectionally until one side closes."""

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
    _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


async def _handle_connect_error(
    ws_client: Any, vm_name: str, attempt: int, max_retries: int, error: Exception
) -> bool:
    """Handle a connection error during VNC relay. Returns True to stop retrying."""
    if _active_sessions.get(vm_name) is not ws_client:
        return True
    if attempt < max_retries - 1:
        logger.info(
            f"VNC for {vm_name} not ready (attempt {attempt + 1}), retrying in 3s: {error}"
        )
        if not await _client_alive(ws_client):
            logger.info(f"Client disconnected while waiting for {vm_name}")
            return True
        await asyncio.sleep(3)
        return False
    logger.error(
        f"VNC proxy giving up on {vm_name} after {max_retries} attempts: {error}"
    )
    try:
        await ws_client.close(1011, str(error)[:120])
    except Exception:
        pass
    return True


async def _connect_and_relay(
    ws_client: Any,
    vm_name: str,
    vnc_url: str,
    ssl_ctx: ssl.SSLContext,
    headers: dict[str, str],
    attempt: int,
) -> bool:
    """Connect to KubeVirt VNC, relay traffic, and handle post-relay state.

    Returns True if the caller should continue retrying, False to stop.
    """
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

        await _relay_bidirectional(ws_client, ws_kubevirt)

    if _active_sessions.get(vm_name) is not ws_client:
        return False
    if not await _client_alive(ws_client):
        logger.info(f"Client disconnected for {vm_name}")
        return False
    logger.info(f"KubeVirt VNC dropped for {vm_name}, reconnecting...")
    await asyncio.sleep(2)
    return True


async def _vnc_connection_loop(
    ws_client: Any, vm_name: str, vnc_url: str, ssl_ctx: ssl.SSLContext
) -> None:
    """Retry loop: connect to KubeVirt VNC and relay until client disconnects."""
    max_retries = 30
    for attempt in range(max_retries):
        if _active_sessions.get(vm_name) is not ws_client:
            logger.info(f"Session for {vm_name} superseded, stopping")
            return

        headers = {"Authorization": f"Bearer {_read_token()}"}
        try:
            if not await _connect_and_relay(
                ws_client, vm_name, vnc_url, ssl_ctx, headers, attempt
            ):
                return
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client disconnected for {vm_name}")
            return
        except Exception as e:
            if await _handle_connect_error(ws_client, vm_name, attempt, max_retries, e):
                return


async def _proxy(ws_client: Any) -> None:
    vm_name, force = await _parse_ws_path(ws_client)
    if vm_name is None:
        return

    logger.info(f"VNC client connecting for {vm_name}{' (force)' if force else ''}")

    if await _evict_or_reject(vm_name, force, ws_client):
        return

    _active_sessions[vm_name] = ws_client
    vnc_url = _get_kubevirt_vnc_url(vm_name)
    ssl_ctx = _make_ssl_context()

    try:
        await _vnc_connection_loop(ws_client, vm_name, vnc_url, ssl_ctx)
    finally:
        if _active_sessions.get(vm_name) is ws_client:
            _active_sessions.pop(vm_name, None)


STATUS_PORT = int(os.environ.get("STATUS_PORT", "8081"))


def _write_json(writer: asyncio.StreamWriter, body: bytes) -> None:
    """Write a JSON HTTP 200 response."""
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


def _handle_active_path(writer: asyncio.StreamWriter, vm_name: str) -> None:
    """Handle GET /active/{vm_name}."""
    in_use = vm_name in _active_sessions
    _write_json(writer, f'{{"in_use":{str(in_use).lower()}}}'.encode())


def _handle_kick_path(writer: asyncio.StreamWriter, vm_name: str) -> None:
    """Handle GET /kick/{vm_name}."""
    ws = _active_sessions.pop(vm_name, None)
    if ws:
        try:
            task = asyncio.ensure_future(ws.close(4010, "Kicked"))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except Exception:
            pass
        logger.info(f"Kicked session for {vm_name}")
    _write_json(writer, f'{{"kicked":{str(bool(ws)).lower()}}}'.encode())


async def _read_http_path(reader: asyncio.StreamReader) -> str:
    """Read HTTP request line and drain headers. Returns the path."""
    request_line = await asyncio.wait_for(reader.readline(), timeout=5)
    parts = request_line.decode().strip().split()
    path = parts[1] if len(parts) > 1 else "/"
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if line == b"\r\n" or line == b"\n" or not line:
            break
    return path


async def _handle_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """HTTP endpoint: GET /active/{vm_name} → 200 if in use, 404 if free."""
    try:
        path = await _read_http_path(reader)

        if path.startswith("/active/"):
            _handle_active_path(writer, path[8:])
        elif path.startswith("/kick/"):
            _handle_kick_path(writer, path[6:])
        else:
            _write_json(writer, f'{{"active":{len(_active_sessions)}}}'.encode())
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
