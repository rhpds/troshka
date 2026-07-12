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
api_client = client.ApiClient()
K8S_HOST = client.Configuration().host
K8S_TOKEN = client.Configuration().api_key.get(
    "authorization", ""
).replace("Bearer ", "")


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
    headers = {"Authorization": f"Bearer {K8S_TOKEN}"}

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

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

    except Exception as e:
        logger.error(f"VNC proxy error for {vm_name}: {e}")
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
