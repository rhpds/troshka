#!/usr/bin/env python3
"""
troshka-vncd — WebSocket-to-VNC relay daemon.

Listens on port 443 with TLS, validates JWT tokens, and proxies
binary WebSocket frames to the local QEMU VNC socket.

Dependencies: websockets (pip install websockets)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import ssl
import subprocess
import time

VERSION = "dev"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vncd")

_consumed: set[str] = set()
_consumed_expiry: dict[str, float] = {}

CERT_CHECK_INTERVAL = 3600


def _load_config() -> dict:
    conf_path = os.environ.get("VNCD_CONFIG", "/opt/troshka/troshkad.conf")
    with open(conf_path) as f:
        return json.load(f)


def _verify_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(secret.encode(), sig_input, hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        if not hmac.compare_digest(expected, parts[2]):
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _get_vnc_port(domain_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["virsh", "dumpxml", domain_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        import xml.etree.ElementTree as ET

        root = ET.fromstring(result.stdout)
        for gfx in root.iter("graphics"):
            if gfx.get("type") == "vnc":
                port = gfx.get("port")
                if port and port != "-1":
                    return int(port)
        return None
    except Exception:
        return None


def _prune_consumed():
    now = time.time()
    expired = [t for t, exp in _consumed_expiry.items() if exp < now]
    for t in expired:
        _consumed.discard(t)
        _consumed_expiry.pop(t, None)


def _build_ssl_context(conf: dict) -> ssl.SSLContext:
    console_domain = conf.get("console_domain", "")
    if console_domain:
        cert = f"/etc/letsencrypt/live/{console_domain}/fullchain.pem"
        key = f"/etc/letsencrypt/live/{console_domain}/privkey.pem"
    else:
        cert = conf.get("tls_cert", "/opt/troshka/tls/server.crt")
        key = conf.get("tls_key", "/opt/troshka/tls/server.key")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _handle_connection(websocket, conf: dict):
    path = websocket.request.path if hasattr(websocket, "request") else ""
    if not path.startswith("/ws/"):
        await websocket.close(4000, "Invalid path")
        return
    token = path[4:]

    secret = conf["token"]
    claims = _verify_jwt(token, secret)
    if not claims:
        await websocket.close(4001, "Invalid or expired token")
        return

    if token in _consumed:
        await websocket.close(4001, "Token already used")
        return
    _consumed.add(token)
    _consumed_expiry[token] = claims["exp"]

    domain_name = claims.get("domain_name")
    if not domain_name:
        await websocket.close(4002, "Missing domain_name")
        return

    vnc_port = _get_vnc_port(domain_name)
    if not vnc_port:
        await websocket.close(4003, "VNC not available")
        return

    logger.info("Console: %s -> 127.0.0.1:%d", domain_name, vnc_port)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", vnc_port)
    except Exception:
        await websocket.close(4004, "Cannot connect to VNC")
        return

    async def _ws_to_vnc():
        try:
            async for msg in websocket:
                if isinstance(msg, bytes):
                    writer.write(msg)
                    await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _vnc_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send(data)
        except Exception:
            pass

    try:
        await asyncio.gather(_ws_to_vnc(), _vnc_to_ws())
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Console closed: %s", domain_name)


async def _cert_reload_loop(ssl_context_holder: list, conf: dict):
    console_domain = conf.get("console_domain", "")
    if not console_domain:
        return
    cert_path = f"/etc/letsencrypt/live/{console_domain}/fullchain.pem"
    last_mtime = 0.0
    while True:
        await asyncio.sleep(CERT_CHECK_INTERVAL)
        try:
            mtime = os.path.getmtime(cert_path)
            if mtime > last_mtime:
                ssl_context_holder[0] = _build_ssl_context(conf)
                last_mtime = mtime
                logger.info("Reloaded TLS certificate")
        except Exception:
            pass


async def _prune_loop():
    while True:
        await asyncio.sleep(60)
        _prune_consumed()


async def main():
    import argparse

    import websockets

    parser = argparse.ArgumentParser(description="troshka-vncd WebSocket-to-VNC relay")
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Listen for plain WebSocket (TLS handled externally by OCP router)",
    )
    parser.add_argument(
        "--plain-port",
        type=int,
        default=8080,
        help="Port for plain WebSocket when --no-tls is set (default: 8080)",
    )
    args = parser.parse_args()

    conf = _load_config()
    bind_ip = conf.get("bind_ip", "0.0.0.0")

    if args.no_tls:
        port = args.plain_port
        ssl_ctx = None
        ssl_holder = [None]
        logger.info(
            "troshka-vncd %s starting on %s:%d (plain, TLS handled externally)",
            VERSION,
            bind_ip,
            port,
        )
    else:
        port = 443
        ssl_ctx = _build_ssl_context(conf)
        ssl_holder = [ssl_ctx]
        logger.info("troshka-vncd %s starting on %s:%d", VERSION, bind_ip, port)

    async def handler(websocket):
        await _handle_connection(websocket, conf)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(
        handler,
        bind_ip,
        port,
        ssl=ssl_ctx,
        max_size=None,
        ping_interval=30,
        ping_timeout=10,
    ):
        asyncio.create_task(_prune_loop())
        if not args.no_tls:
            asyncio.create_task(_cert_reload_loop(ssl_holder, conf))
        logger.info("troshka-vncd ready")
        await stop.wait()

    logger.info("troshka-vncd shutting down")


if __name__ == "__main__":
    asyncio.run(main())
