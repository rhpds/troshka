"""Resolve nested OCP API dial targets and cluster metadata for local oc tunnels.

The showroom cluster terminal continues to rewrite kubeconfigs to the API VIP
via ``rewrite_kubeconfig_server_to_host`` (injected onto the showroom disk).
This module is only for laptop → Troshka → nested API tunneling.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass

from app.services.ocp.kubeconfig_merge import (
    kubeconfig_server_host,
    rewrite_kubeconfig_server,
)
from app.services.ocp.ops_pod_install import _cluster_key

logger = logging.getLogger(__name__)

API_PORT = 6443
ROUTE_PASSTHROUGH_PORT = 443
TUNNEL_IDLE_SECONDS = 3600


@dataclass(frozen=True)
class ClusterAccess:
    """One nested OCP cluster's access metadata for the CLI tunnel."""

    id: str
    name: str
    api_vip: str
    base_domain: str
    kubeconfig: str
    kubeconfig_available: bool
    tls_server_name: str
    route_hostname: str


@dataclass(frozen=True)
class DialTarget:
    """Where the backend should open a TCP connection for an API tunnel."""

    host: str
    port: int
    via: str  # "direct" | "troshkad" | "kubevirt-pf" | "kubevirt-exec"
    tls_server_name: str
    force_insecure: bool = False
    namespace: str = ""
    pod_name: str = ""
    api_vip: str = ""


def _topology(project) -> dict:
    return project.deployed_topology or project.topology or {}


def _gateway_endpoints(topology: dict) -> list[dict]:
    for node in topology.get("nodes", []) or []:
        data = node.get("data") or {}
        if data.get("subtype") == "gateway":
            return list(data.get("externalEndpoints") or [])
    return []


def _route_hostname_for_vip(topology: dict, api_vip: str) -> str:
    if not api_vip:
        return ""
    for ep in _gateway_endpoints(topology):
        if str(ep.get("vmIp") or "") != api_vip:
            continue
        if str(ep.get("type") or "") != "route":
            continue
        # API forwards use guest 6443; ext listen key may be 6443/6444/...
        port = int(ep.get("port") or 0)
        if port == 80 or port == 443:
            continue
        host = str(ep.get("hostname") or "").strip()
        if host:
            return host
    return ""


def _cp_kubeconfig(topology: dict, cluster_id: str) -> str:
    for node in topology.get("nodes", []) or []:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data") or {}
        if data.get("clusterId") != cluster_id:
            continue
        role = data.get("clusterRole") or ""
        group = (data.get("tags") or {}).get("AnsibleGroup", "")
        if role == "worker" or (not role and "workers" in group):
            continue
        kc = data.get("ocpKubeconfig") or ""
        if isinstance(kc, str) and kc.strip():
            return kc
    return ""


def list_cluster_access(project) -> list[ClusterAccess]:
    """Build per-cluster access rows from deployed/editable topology."""
    topology = _topology(project)
    rows: list[ClusterAccess] = []
    for cluster in topology.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        cid = _cluster_key(cluster)
        name = str(cluster.get("name") or cid).strip() or cid
        api_vip = str(cluster.get("apiVip") or "").strip()
        base_domain = str(cluster.get("baseDomain") or "").strip()
        kc = _cp_kubeconfig(topology, cid)
        api_host = kubeconfig_server_host(kc)
        if not api_host and name and base_domain:
            api_host = f"api.{name}.{base_domain}"
        rows.append(
            ClusterAccess(
                id=cid,
                name=name,
                api_vip=api_vip,
                base_domain=base_domain,
                kubeconfig=kc,
                kubeconfig_available=bool(kc),
                tls_server_name=api_host,
                route_hostname=_route_hostname_for_vip(topology, api_vip),
            )
        )
    return rows


def get_cluster_access(project, cluster_id: str) -> ClusterAccess | None:
    needle = (cluster_id or "").strip().lower()
    if not needle:
        return None
    for row in list_cluster_access(project):
        if row.id.lower() == needle or row.name.lower() == needle:
            return row
    return None


def localhost_kubeconfig(
    kc_yaml: str,
    local_port: int,
    *,
    tls_server_name: str = "",
    force_insecure: bool = False,
) -> str:
    """Rewrite a harvested kubeconfig for a local TCP tunnel listener."""
    return rewrite_kubeconfig_server(
        kc_yaml,
        "127.0.0.1",
        port=local_port,
        tls_server_name=tls_server_name or None,
        insecure_skip_tls_verify=force_insecure,
    )


def _provider_type(host, db) -> str:
    if not host or not getattr(host, "provider_id", None):
        return "troshkad"
    from app.models.provider import Provider

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider:
        return "troshkad"
    return str(provider.type or "troshkad")


def _vm_name_for_api_vip(topology: dict, api_vip: str) -> str:
    for ep in _gateway_endpoints(topology):
        if str(ep.get("vmIp") or "") == api_vip:
            return str(ep.get("vmName") or "")
    return ""


def _gateway_listen_port_for_vip(topology: dict, api_vip: str) -> int:
    """Gateway pod listen / Service port for this cluster's API forward."""
    for ep in _gateway_endpoints(topology):
        if str(ep.get("vmIp") or "") != api_vip:
            continue
        if str(ep.get("type") or "") != "route":
            continue
        port = int(ep.get("port") or 0)
        if port in (80, 443) or port <= 0:
            continue
        return port
    return API_PORT


def _gateway_pod_name(core_api, namespace: str, project_id: str) -> str:
    label = f"app=troshka-gateway-{project_id[:8]}"
    try:
        pods = core_api.list_namespaced_pod(namespace, label_selector=label)
    except Exception:
        logger.debug(
            "gateway pod list failed in %s (project %s)",
            namespace,
            project_id[:8],
            exc_info=True,
        )
        return ""
    items = list(getattr(pods, "items", None) or [])
    for pod in items:
        phase = getattr(getattr(pod, "status", None), "phase", "") or ""
        if phase == "Running":
            return str(pod.metadata.name)
    if items:
        return str(items[0].metadata.name)
    return ""


def _kubevirt_dial_targets(
    provider, project_id: str, cluster: ClusterAccess, topology: dict
) -> list[DialTarget]:
    """Ordered dial candidates for KubeVirt-native nested APIs.

    Prefer Kubernetes port-forward into the gateway (binary-safe; needs
    pods/portforward on the provider SA). Fall back to pod-exec+socat, then
    Service ClusterIP, then public Route passthrough.
    """
    from app.services.providers.kubevirt import _get_k8s_clients, _project_ns

    targets: list[DialTarget] = []
    namespace = _project_ns(provider, project_id)
    listen_port = _gateway_listen_port_for_vip(topology, cluster.api_vip)
    try:
        _custom, core_api, _ = _get_k8s_clients(provider)
    except Exception:
        logger.debug("k8s client unavailable for dial targets", exc_info=True)
        core_api = None

    if core_api is not None and cluster.api_vip:
        pod_name = _gateway_pod_name(core_api, namespace, project_id)
        if pod_name:
            targets.append(
                DialTarget(
                    host=pod_name,
                    port=listen_port,
                    via="kubevirt-pf",
                    tls_server_name=cluster.tls_server_name,
                    force_insecure=False,
                    namespace=namespace,
                    pod_name=pod_name,
                    api_vip=cluster.api_vip,
                )
            )
            targets.append(
                DialTarget(
                    host=pod_name,
                    port=API_PORT,
                    via="kubevirt-exec",
                    tls_server_name=cluster.tls_server_name,
                    force_insecure=False,
                    namespace=namespace,
                    pod_name=pod_name,
                    api_vip=cluster.api_vip,
                )
            )
        vm_name = _vm_name_for_api_vip(topology, cluster.api_vip)
        svc_name = f"rt-{vm_name}-{API_PORT}"[:63] if vm_name else ""
        if svc_name:
            try:
                svc = core_api.read_namespaced_service(svc_name, namespace)
                cluster_ip = (
                    getattr(getattr(svc, "spec", None), "cluster_ip", None) or ""
                )
                ports = list(getattr(getattr(svc, "spec", None), "ports", None) or [])
                port = (
                    int(getattr(ports[0], "port", listen_port))
                    if ports
                    else listen_port
                )
                if cluster_ip and cluster_ip not in ("None", "none"):
                    targets.append(
                        DialTarget(
                            host=cluster_ip,
                            port=port,
                            via="direct",
                            tls_server_name=cluster.tls_server_name,
                            force_insecure=False,
                        )
                    )
            except Exception:
                logger.debug(
                    "ClusterIP lookup failed for %s/%s",
                    namespace,
                    svc_name,
                    exc_info=True,
                )

    if cluster.route_hostname:
        targets.append(
            DialTarget(
                host=cluster.route_hostname,
                port=ROUTE_PASSTHROUGH_PORT,
                via="direct",
                tls_server_name=cluster.route_hostname,
                force_insecure=True,
            )
        )
    return targets


def _uses_kubevirt_native_dial(host, db) -> bool:
    """True for KubeVirt-native projects (in-cluster VMs, no troshkad).

    ``ocpvirt`` is nested virt on a host VM with troshkad — dial via the agent
    netns, not via pods/portforward into a management-cluster project NS.
    """
    if getattr(host, "host_type", None) == "kubevirt-cluster":
        return True
    return _provider_type(host, db) == "kubevirt"


def resolve_dial_targets(project, cluster: ClusterAccess, host, db) -> list[DialTarget]:
    """Ordered ways the backend can reach nested API :6443."""
    topology = _topology(project)
    if _uses_kubevirt_native_dial(host, db):
        from app.models.provider import Provider

        provider = (
            db.query(Provider).filter_by(id=host.provider_id).first()
            if host and host.provider_id
            else None
        )
        targets: list[DialTarget] = []
        if provider:
            targets.extend(
                _kubevirt_dial_targets(provider, project.id, cluster, topology)
            )
        elif cluster.route_hostname:
            targets.append(
                DialTarget(
                    host=cluster.route_hostname,
                    port=ROUTE_PASSTHROUGH_PORT,
                    via="direct",
                    tls_server_name=cluster.route_hostname,
                    force_insecure=True,
                )
            )
        if targets:
            return targets
    if not cluster.api_vip:
        raise RuntimeError(f"Cluster {cluster.name} has no apiVip")
    return [
        DialTarget(
            host=cluster.api_vip,
            port=API_PORT,
            via="troshkad",
            tls_server_name=cluster.tls_server_name,
            force_insecure=False,
        )
    ]


def resolve_dial_target(project, cluster: ClusterAccess, host, db) -> DialTarget:
    """First dial candidate (tests / simple callers)."""
    targets = resolve_dial_targets(project, cluster, host, db)
    if not targets:
        raise RuntimeError(f"No dial path for cluster {cluster.name}")
    return targets[0]


async def open_direct_connection(host: str, port: int) -> tuple:
    """Open a TCP connection to host:port; returns (reader, writer)."""
    return await asyncio.open_connection(host, port)


class _ExecRelaySocket:
    """Socket-like adapter over a kubernetes pod-exec WSClient (socat relay)."""

    def __init__(self, ws_client):
        self._ws = ws_client

    def sendall(self, data: bytes) -> None:
        if isinstance(data, str):
            data = data.encode()
        self._ws.write_stdin(data)

    def recv(self, n: int = 65536) -> bytes:
        # Drain until we get data or the stream closes.
        deadline = time.time() + 60
        while time.time() < deadline:
            self._ws.update(timeout=1)
            chunk = self._ws.read_stdout()
            if chunk:
                if isinstance(chunk, str):
                    return chunk.encode("utf-8", errors="replace")
                return chunk
            if not self._ws.is_open():
                return b""
        return b""

    def shutdown(self, _how: int) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def open_kubevirt_portforward_socket(provider, target: DialTarget):
    """Port-forward to the gateway pod listen port; returns a connected socket."""
    from kubernetes.stream import portforward

    from app.services.providers.kubevirt import _get_k8s_clients

    if not target.pod_name or not target.namespace:
        raise RuntimeError("kubevirt-pf target missing pod/namespace")
    _custom, core_api, _ = _get_k8s_clients(provider)
    pf = portforward(
        core_api.connect_get_namespaced_pod_portforward,
        target.pod_name,
        target.namespace,
        ports=str(target.port),
    )
    sock = pf.socket(target.port)
    sock.setblocking(True)
    sock._troshka_portforward = pf  # type: ignore[attr-defined]
    return sock


def open_kubevirt_exec_relay(provider, target: DialTarget):
    """Exec ``socat`` in the gateway pod to dial the nested API VIP.

    Fallback when ``pods/portforward`` is unavailable. Returns a socket-like
    object compatible with ``bridge_websocket_to_socket``.
    """
    from kubernetes.stream import stream as k8s_stream

    from app.services.providers.kubevirt import _get_k8s_clients

    if not target.pod_name or not target.namespace or not target.api_vip:
        raise RuntimeError("kubevirt-exec target missing pod/namespace/api_vip")
    _custom, core_api, _ = _get_k8s_clients(provider)
    # socat is baked into the gateway image; python3 is not.
    cmd = [
        "socat",
        "-",
        f"TCP:{target.api_vip}:{target.port or API_PORT},connect-timeout=10",
    ]
    ws = k8s_stream(
        core_api.connect_get_namespaced_pod_exec,
        target.pod_name,
        target.namespace,
        command=cmd,
        stderr=True,
        stdin=True,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    return _ExecRelaySocket(ws)


def open_troshkad_tunnel_socket(
    host_row, project_id: str, api_host: str, api_port: int
):
    """Open a raw bidirectional stream to nested API via troshkad /tcp-tunnel.

    Returns a connected ``ssl``-wrapped socket whose peer has already finished
    the HTTP handshake and is relaying bytes to ``api_host:api_port`` inside
    the project netns.
    """
    import json
    import ssl

    from app.services.troshkad_client import TROSHKAD_PORT, _get_pool

    # Ensure cert/fingerprint config is valid before dialing.
    _get_pool(host_row)

    fingerprint = (host_row.agent_cert_fingerprint or "").replace(":", "").upper()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Agents with client_ca require mTLS (same certs as troshkad_client pools).
    try:
        from app.services.agent_ca_service import get_client_cert_paths

        cert_path, key_path = get_client_cert_paths()
        if cert_path and key_path:
            ctx.load_cert_chain(cert_path, key_path)
    except Exception:
        logger.debug("troshkad tunnel: no client cert available", exc_info=True)

    raw = socket.create_connection((host_row.ip_address, TROSHKAD_PORT), timeout=30)
    ssock = ctx.wrap_socket(raw, server_hostname=host_row.ip_address)
    # Optional fingerprint pin (urllib3 pool already validates on other calls).
    if fingerprint:
        peercert = ssock.getpeercert(binary_form=True)
        if peercert:
            import hashlib

            digest = hashlib.sha256(peercert).hexdigest().upper()
            if digest != fingerprint:
                ssock.close()
                raise RuntimeError("troshkad cert fingerprint mismatch")

    body = json.dumps(
        {
            "project_id": project_id,
            "host": api_host,
            "port": int(api_port),
        }
    ).encode()
    req = (
        f"POST /tcp-tunnel HTTP/1.1\r\n"
        f"Host: {host_row.ip_address}\r\n"
        f"Authorization: Bearer {host_row.agent_token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    ).encode() + body
    ssock.sendall(req)

    # Read response headers until CRLFCRLF.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = ssock.recv(4096)
        if not chunk:
            ssock.close()
            raise RuntimeError("troshkad tcp-tunnel closed before headers")
        buf += chunk
        if len(buf) > 65536:
            ssock.close()
            raise RuntimeError("troshkad tcp-tunnel headers too large")
    header_blob, remainder = buf.split(b"\r\n\r\n", 1)
    status_line = header_blob.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 200 " not in status_line and not status_line.endswith(" 200"):
        ssock.close()
        raise RuntimeError(f"troshkad tcp-tunnel failed: {status_line}")
    # Stuff any body bytes already read back by wrapping.
    ssock._troshka_prefetch = remainder  # type: ignore[attr-defined]
    return ssock


async def bridge_websocket_to_tcp(websocket, reader, writer) -> None:
    """Relay binary WebSocket frames ↔ TCP until either side closes."""

    async def ws_to_tcp():
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is None and message.get("text") is not None:
                    data = message["text"].encode()
                if not data:
                    continue
                writer.write(data)
                await writer.drain()
        except Exception:
            logger.debug("ws→tcp closed", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            logger.debug("tcp→ws closed", exc_info=True)

    # Drain both directions; FIRST_COMPLETED closed the peer mid-response.
    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)
    try:
        await websocket.close()
    except Exception:
        pass


async def bridge_websocket_to_socket(websocket, ssock) -> None:
    """Relay WebSocket ↔ a blocking SSL/socket (troshkad tunnel)."""
    loop = asyncio.get_running_loop()
    prefetch = getattr(ssock, "_troshka_prefetch", b"") or b""

    async def ws_to_sock():
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is None and message.get("text") is not None:
                    data = message["text"].encode()
                if not data:
                    continue
                await loop.run_in_executor(None, ssock.sendall, data)
        except Exception:
            logger.debug("ws→sock closed", exc_info=True)
        finally:
            try:
                ssock.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    async def sock_to_ws():
        try:
            if prefetch:
                await websocket.send_bytes(prefetch)
            while True:
                data = await loop.run_in_executor(None, ssock.recv, 65536)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            logger.debug("sock→ws closed", exc_info=True)

    try:
        await asyncio.gather(ws_to_sock(), sock_to_ws(), return_exceptions=True)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        try:
            pf = getattr(ssock, "_troshka_portforward", None)
            if pf is not None:
                pf.close()
        except Exception:
            pass
        try:
            ssock.close()
        except Exception:
            pass
