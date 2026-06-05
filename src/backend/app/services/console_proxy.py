"""
Console proxy — tunnels VNC from hosts to browser via WebSocket.

For each VM console request:
1. Creates an SSH tunnel from a local port to the VNC port on the host
2. Runs websockify to bridge that local port to a WebSocket
3. Returns the WebSocket URL for noVNC to connect to

Processes are tracked and cleaned up after a timeout.
"""
import logging
import os
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

_active_proxies: dict[str, dict] = {}
_lock = threading.Lock()

PROXY_TIMEOUT = 3600  # 1 hour


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _cleanup_proxy(key: str):
    """Clean up SSH tunnel and websockify processes."""
    with _lock:
        proxy = _active_proxies.pop(key, None)
    if not proxy:
        return
    for proc in proxy.get("processes", []):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    key_path = proxy.get("key_path")
    if key_path and os.path.exists(key_path):
        os.unlink(key_path)
    logger.info("Cleaned up console proxy for %s", key)


def _schedule_cleanup(key: str, timeout: int = PROXY_TIMEOUT):
    def _cleanup():
        time.sleep(timeout)
        _cleanup_proxy(key)
    threading.Thread(target=_cleanup, daemon=True).start()


def get_or_create_proxy(
    vm_full_name: str,
    host_ip: str,
    private_key: str,
    vnc_port: int,
) -> dict:
    """Get an existing proxy or create a new one for a VM console."""
    key = f"{host_ip}:{vnc_port}:{vm_full_name}"

    with _lock:
        existing = _active_proxies.get(key)
        if existing:
            # Check if processes are still alive
            if all(p.poll() is None for p in existing.get("processes", [])):
                return {
                    "ws_port": existing["ws_port"],
                    "ws_url": f"ws://localhost:{existing['ws_port']}",
                }
            # Processes died, clean up
            _active_proxies.pop(key, None)

    # Write SSH key to temp file
    kf = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    kf.write(private_key)
    kf.close()
    os.chmod(kf.name, 0o600)

    tunnel_port = _find_free_port()
    ws_port = _find_free_port()

    # Start SSH tunnel: local tunnel_port → host VNC port
    ssh_proc = subprocess.Popen(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ExitOnForwardFailure=yes",
            "-N",
            "-L", f"{tunnel_port}:127.0.0.1:{vnc_port}",
            "-i", kf.name,
            f"ec2-user@{host_ip}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait briefly for tunnel to establish
    time.sleep(1)
    if ssh_proc.poll() is not None:
        os.unlink(kf.name)
        return {"error": "SSH tunnel failed to establish"}

    # Start websockify: ws_port → tunnel_port
    ws_proc = subprocess.Popen(
        [
            "websockify",
            "--heartbeat=30",
            str(ws_port),
            f"localhost:{tunnel_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(0.5)
    if ws_proc.poll() is not None:
        ssh_proc.terminate()
        os.unlink(kf.name)
        return {"error": "websockify failed to start"}

    proxy_info = {
        "ws_port": ws_port,
        "tunnel_port": tunnel_port,
        "processes": [ssh_proc, ws_proc],
        "key_path": kf.name,
        "created_at": time.time(),
    }

    with _lock:
        _active_proxies[key] = proxy_info

    _schedule_cleanup(key)

    logger.info(
        "Console proxy for %s: ws://localhost:%d → tunnel:%d → %s:%d",
        vm_full_name, ws_port, tunnel_port, host_ip, vnc_port,
    )

    return {
        "ws_port": ws_port,
        "ws_url": f"ws://localhost:{ws_port}",
    }


def close_proxy(vm_full_name: str, host_ip: str, vnc_port: int):
    key = f"{host_ip}:{vnc_port}:{vm_full_name}"
    _cleanup_proxy(key)


def close_all_proxies():
    with _lock:
        keys = list(_active_proxies.keys())
    for key in keys:
        _cleanup_proxy(key)
