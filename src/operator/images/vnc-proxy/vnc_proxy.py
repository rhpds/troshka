"""WebSocket VNC proxy — relays noVNC to KubeVirt VNC subresource API."""

import asyncio
import logging
import os
import ssl

import websockets
from kubernetes import client, config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vnc-proxy")

NAMESPACE = os.environ.get("NAMESPACE", "default")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

config.load_incluster_config()
_cfg = client.Configuration.get_default_copy()
K8S_HOST = _cfg.host or f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', '172.30.0.1')}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_TOKEN = open(_token_path).read().strip() if os.path.exists(_token_path) else ""


def _get_kubevirt_vnc_url(vm_name):
    return (
        f"{K8S_HOST.replace('https://', 'wss://')}"
        f"/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{NAMESPACE}"
        f"/virtualmachineinstances/{vm_name}/vnc"
    )


async def _proxy(ws_client):
    path = ws_client.request.path if hasattr(ws_client, "request") else "/"
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        await ws_client.close(1008, "Missing VM name in path")
        return

    vm_name = parts[0]
    logger.info(f"VNC proxy request for {vm_name}")

    vnc_url = _get_kubevirt_vnc_url(vm_name)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    max_retries = 20
    for attempt in range(max_retries):
        token = open(_token_path).read().strip() if os.path.exists(_token_path) else K8S_TOKEN
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with websockets.connect(
                vnc_url,
                additional_headers=headers,
                ssl=ssl_ctx,
                subprotocols=["binary"],
            ) as ws_kubevirt:
                logger.info(f"Connected to KubeVirt VNC for {vm_name}")

                async def client_to_kv():
                    try:
                        async for msg in ws_client:
                            await ws_kubevirt.send(msg)
                    except websockets.exceptions.ConnectionClosed:
                        pass

                async def kv_to_client():
                    try:
                        async for msg in ws_kubevirt:
                            await ws_client.send(msg)
                    except websockets.exceptions.ConnectionClosed:
                        pass

                await asyncio.gather(client_to_kv(), kv_to_client())
                return

        except websockets.exceptions.ConnectionClosed:
            return
        except Exception as e:
            if attempt < max_retries - 1:
                logger.info(f"VNC for {vm_name} not ready (attempt {attempt + 1}), retrying in 3s: {e}")
                try:
                    pong = await asyncio.wait_for(ws_client.ping(), timeout=5)
                except Exception:
                    logger.info(f"Client disconnected while waiting for {vm_name}")
                    return
                await asyncio.sleep(3)
            else:
                logger.error(f"VNC proxy giving up on {vm_name} after {max_retries} attempts: {e}")
                try:
                    await ws_client.close(1011, str(e))
                except Exception:
                    pass


async def main():
    async with websockets.serve(_proxy, "0.0.0.0", LISTEN_PORT):
        logger.info(f"VNC proxy listening on port {LISTEN_PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
