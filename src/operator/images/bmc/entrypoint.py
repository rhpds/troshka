"""Lightweight Redfish emulator using the KubeVirt driver."""

import json
import os
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler

from kubevirt_driver import KubeVirtDriver

driver = KubeVirtDriver()

USERNAME = os.environ["SUSHY_USERNAME"]
PASSWORD = os.environ["SUSHY_PASSWORD"]
LISTEN_PORT = int(os.environ.get("SUSHY_LISTEN_PORT", "8000"))

_AUTH_REALM = 'Basic realm="Redfish"'
_SYSTEMS_PREFIX = "/redfish/v1/Systems/"
_MANAGERS_PREFIX = "/redfish/v1/Managers/"
_ERR_GENERAL = "Base.1.0.GeneralError"
_ODATA_ID = "@odata.id"
_MEMBERS_COUNT = "Members@odata.count"
_NOT_FOUND_BODY = {"error": {"code": _ERR_GENERAL, "message": "Not found"}}
_AUTH_ERROR_BODY = {
    "error": {
        "code": _ERR_GENERAL,
        "message": "Authentication required",
    }
}

_PUBLIC_PATHS = frozenset(
    ["/redfish/v1", "/redfish/v1/Systems", "/redfish/v1/Managers"]
)


def _check_auth(handler):
    import base64

    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    decoded = base64.b64decode(auth[6:]).decode()
    return decoded == f"{USERNAME}:{PASSWORD}"


def _require_auth(handler):
    """Return True if auth passes. Send 401 JSON response and return False otherwise."""
    path = handler.path.rstrip("/")
    if path in _PUBLIC_PATHS or path.startswith("/vmedia/"):
        return True
    if _check_auth(handler):
        return True
    body = json.dumps(_AUTH_ERROR_BODY).encode()
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", _AUTH_REALM)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return False


def _send_json(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_pod_ip():
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


_POD_IP = _get_pod_ip()


class RedfishHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _require_auth(self):
            return

        path = self.path.rstrip("/")

        # ── Service Root ──
        if path == "/redfish/v1":
            _send_json(
                self,
                {
                    "@odata.type": "#ServiceRoot.v1_0_0.ServiceRoot",
                    "Id": "RootService",
                    "Name": "Troshka Redfish Service",
                    "Systems": {_ODATA_ID: "/redfish/v1/Systems"},
                    "Managers": {_ODATA_ID: "/redfish/v1/Managers"},
                },
            )
            return

        # ── Systems Collection ──
        if path == _SYSTEMS_PREFIX.rstrip("/"):
            systems = driver.get_systems()
            members = [{_ODATA_ID: f"{_SYSTEMS_PREFIX}{s}"} for s in systems]
            _send_json(
                self,
                {
                    "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
                    "Name": "Computer System Collection",
                    "Members": members,
                    _MEMBERS_COUNT: len(members),
                },
            )
            return

        # ── System Detail ──
        if path.startswith(_SYSTEMS_PREFIX):
            identity = path.split(_SYSTEMS_PREFIX)[1].split("/")[0]

            if path.endswith(identity):
                power = driver.get_power_state(identity)
                boot_dev = driver.get_boot_device(identity)
                boot_mode = driver.get_boot_mode(identity)
                mem = driver.get_total_memory(identity)
                cpus = driver.get_total_cpus(identity)
                boot_enabled = driver.get_boot_override_enabled(identity)
                smbios_uuid = driver.get_uuid(identity)
                _send_json(
                    self,
                    {
                        "@odata.type": "#ComputerSystem.v1_1_0.ComputerSystem",
                        "Id": identity,
                        "Name": identity,
                        "UUID": smbios_uuid,
                        "PowerState": power,
                        "MemorySummary": {"TotalSystemMemoryGiB": mem / 1024},
                        "ProcessorSummary": {"Count": cpus},
                        "Boot": {
                            "BootSourceOverrideEnabled": boot_enabled,
                            "BootSourceOverrideTarget": boot_dev,
                            "BootSourceOverrideMode": boot_mode,
                        },
                        "Links": {
                            "ManagedBy": [{_ODATA_ID: f"{_MANAGERS_PREFIX}{identity}"}]
                        },
                        "Actions": {
                            "#ComputerSystem.Reset": {
                                "target": f"{_SYSTEMS_PREFIX}{identity}/Actions/ComputerSystem.Reset",
                                "ResetType@Redfish.AllowableValues": [
                                    "On",
                                    "ForceOff",
                                    "GracefulShutdown",
                                    "ForceRestart",
                                    "ForceOn",
                                ],
                            }
                        },
                    },
                )
                return

        # ── Managers Collection ──
        if path == _MANAGERS_PREFIX.rstrip("/"):
            systems = driver.get_systems()
            members = [{_ODATA_ID: f"{_MANAGERS_PREFIX}{s}"} for s in systems]
            _send_json(
                self,
                {
                    "@odata.type": "#ManagerCollection.ManagerCollection",
                    "Name": "Manager Collection",
                    "Members": members,
                    _MEMBERS_COUNT: len(members),
                },
            )
            return

        # ── Manager Detail / VirtualMedia ──
        if path.startswith(_MANAGERS_PREFIX):
            parts = path[len(_MANAGERS_PREFIX) :].split("/")
            identity = parts[0]
            sub = "/".join(parts[1:]) if len(parts) > 1 else ""

            if not sub:
                _send_json(
                    self,
                    {
                        "@odata.type": "#Manager.v1_0_0.Manager",
                        "Id": identity,
                        "Name": f"Manager for {identity}",
                        "ManagerType": "BMC",
                        "VirtualMedia": {
                            _ODATA_ID: f"{_MANAGERS_PREFIX}{identity}/VirtualMedia"
                        },
                        "Links": {
                            "ManagerForServers": [
                                {_ODATA_ID: f"{_SYSTEMS_PREFIX}{identity}"}
                            ]
                        },
                    },
                )
                return

            if sub == "VirtualMedia":
                _send_json(
                    self,
                    {
                        "@odata.type": "#VirtualMediaCollection.VirtualMediaCollection",
                        "Name": "Virtual Media Collection",
                        "Members": [
                            {_ODATA_ID: f"{_MANAGERS_PREFIX}{identity}/VirtualMedia/Cd"}
                        ],
                        _MEMBERS_COUNT: 1,
                    },
                )
                return

            if sub == "VirtualMedia/Cd":
                state = driver.get_vmedia_state(identity)
                _send_json(
                    self,
                    {
                        "@odata.type": "#VirtualMedia.v1_0_0.VirtualMedia",
                        "Id": "Cd",
                        "Name": "Virtual CD",
                        "MediaTypes": ["CD", "DVD"],
                        "Image": state.get("url", ""),
                        "Inserted": state.get("inserted", False),
                        "WriteProtected": True,
                        "Actions": {
                            "#VirtualMedia.InsertMedia": {
                                "target": f"{_MANAGERS_PREFIX}{identity}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia"
                            },
                            "#VirtualMedia.EjectMedia": {
                                "target": f"{_MANAGERS_PREFIX}{identity}/VirtualMedia/Cd/Actions/VirtualMedia.EjectMedia"
                            },
                        },
                    },
                )
                return

        # ── Virtual Media Download Proxy ──
        if path.startswith("/vmedia/download/"):
            identity = path.split("/vmedia/download/")[1]
            state = driver.get_vmedia_state(identity)
            url = state.get("url", "") if state else ""
            if not url:
                _send_json(self, _NOT_FOUND_BODY, 404)
                return
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=300) as resp:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    length = resp.headers.get("Content-Length")
                    if length:
                        self.send_header("Content-Length", length)
                    self.end_headers()
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception as e:
                _send_json(
                    self,
                    {"error": {"code": _ERR_GENERAL, "message": str(e)}},
                    502,
                )
            return

        _send_json(self, _NOT_FOUND_BODY, 404)

    def do_PATCH(self):
        if not _require_auth(self):
            return

        path = self.path.rstrip("/")

        if path.startswith(_SYSTEMS_PREFIX):
            identity = path.split(_SYSTEMS_PREFIX)[1].split("/")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            boot = body.get("Boot", {})
            target = boot.get("BootSourceOverrideTarget")
            if target:
                boot_enabled = boot.get("BootSourceOverrideEnabled", "Continuous")
                driver.set_boot_device(identity, target, boot_enabled=boot_enabled)
            self.send_response(204)
            self.end_headers()
            return

        _send_json(self, _NOT_FOUND_BODY, 404)

    def do_POST(self):
        if not _require_auth(self):
            return

        path = self.path.rstrip("/")

        if "/Actions/ComputerSystem.Reset" in path:
            identity = path.split(_SYSTEMS_PREFIX)[1].split("/")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            reset_type = body.get("ResetType", "On")
            driver.set_power_state(identity, reset_type)
            driver.revert_boot_once(identity)
            self.send_response(204)
            self.end_headers()
            return

        # ── VirtualMedia Actions ──
        if path.startswith(_MANAGERS_PREFIX) and "VirtualMedia/Cd/Actions/" in path:
            parts = path[len(_MANAGERS_PREFIX) :].split("/")
            identity = parts[0]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if path.endswith("VirtualMedia.InsertMedia"):
                image_url = body.get("Image", "")
                if not image_url:
                    _send_json(
                        self,
                        {
                            "error": {
                                "code": _ERR_GENERAL,
                                "message": "Image URL is required",
                            }
                        },
                        400,
                    )
                    return
                proxy_base = f"http://{_POD_IP}:{LISTEN_PORT}"
                driver._vmedia_state[identity] = {
                    "url": image_url,
                    "inserted": False,
                    "dv_name": "",
                }
                try:
                    driver.insert_image(identity, image_url, proxy_base)
                except Exception as e:
                    driver._vmedia_state.pop(identity, None)
                    _send_json(
                        self,
                        {
                            "error": {
                                "code": _ERR_GENERAL,
                                "message": str(e),
                            }
                        },
                        500,
                    )
                    return
                self.send_response(204)
                self.end_headers()
                return

            if path.endswith("VirtualMedia.EjectMedia"):
                try:
                    driver.eject_image(identity)
                except Exception:
                    pass
                self.send_response(204)
                self.end_headers()
                return

        _send_json(self, _NOT_FOUND_BODY, 404)

    def log_message(self, format, *args):
        pass


def _generate_self_signed_cert(cert_path, key_path):
    """Generate a self-signed TLS certificate."""
    import subprocess

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            "/CN=troshka-bmc",
            "-keyout",
            key_path,
            "-out",
            cert_path,
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )


def _configure_network():
    """Assign BMC IPs to net1 if SUSHY_BMC_IPS is set."""
    import subprocess

    bmc_ips = os.environ.get("SUSHY_BMC_IPS", "")
    if not bmc_ips:
        return
    for ip in bmc_ips.split(","):
        ip = ip.strip()
        if not ip:
            continue
        cidr = ip if "/" in ip else f"{ip}/24"
        try:
            subprocess.run(
                ["ip", "addr", "add", cidr, "dev", "net1"],
                capture_output=True,
                timeout=5,
            )
            print(f"Assigned {cidr} to net1")
        except Exception as e:
            print(f"Failed to assign {cidr} to net1: {e}")


if __name__ == "__main__":
    import ssl
    import threading
    from http.server import ThreadingHTTPServer

    _configure_network()

    http_server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), RedfishHandler)

    ssl_port = int(os.environ.get("SUSHY_SSL_PORT", "8443"))
    cert_path = "/tmp/sushy.crt"
    key_path = "/tmp/sushy.key"
    _generate_self_signed_cert(cert_path, key_path)

    https_server = ThreadingHTTPServer(("0.0.0.0", ssl_port), RedfishHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    https_server.socket = ctx.wrap_socket(https_server.socket, server_side=True)

    ssl_thread = threading.Thread(target=https_server.serve_forever, daemon=True)
    ssl_thread.start()
    print(
        f"Redfish emulator listening on port {LISTEN_PORT} (HTTP) and {ssl_port} (HTTPS)"
    )
    http_server.serve_forever()
