"""Lightweight Redfish emulator using the KubeVirt driver."""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

from kubevirt_driver import KubeVirtDriver

driver = KubeVirtDriver()

USERNAME = os.environ["SUSHY_USERNAME"]
PASSWORD = os.environ["SUSHY_PASSWORD"]
LISTEN_PORT = int(os.environ.get("SUSHY_LISTEN_PORT", "8000"))

_AUTH_REALM = 'Basic realm="Redfish"'
_SYSTEMS_PREFIX = "/redfish/v1/Systems/"
_NOT_FOUND_BODY = {"error": "Not found"}


def _check_auth(handler):
    import base64

    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    decoded = base64.b64decode(auth[6:]).decode()
    return decoded == f"{USERNAME}:{PASSWORD}"


def _send_json(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class RedfishHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", _AUTH_REALM)
            self.end_headers()
            return

        path = self.path.rstrip("/")

        if path == "/redfish/v1":
            _send_json(
                self,
                {
                    "@odata.type": "#ServiceRoot.v1_0_0.ServiceRoot",
                    "Id": "RootService",
                    "Name": "Troshka Redfish Service",
                    "Systems": {"@odata.id": "/redfish/v1/Systems"},
                },
            )
            return

        if path == _SYSTEMS_PREFIX.rstrip("/"):
            systems = driver.get_systems()
            members = [{"@odata.id": f"{_SYSTEMS_PREFIX}{s}"} for s in systems]
            _send_json(
                self,
                {
                    "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
                    "Name": "Computer System Collection",
                    "Members": members,
                    "Members@odata.count": len(members),
                },
            )
            return

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

        _send_json(self, _NOT_FOUND_BODY, 404)

    def do_PATCH(self):
        if not _check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", _AUTH_REALM)
            self.end_headers()
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
        if not _check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", _AUTH_REALM)
            self.end_headers()
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

    _configure_network()

    # HTTP server on existing port (default 8000)
    http_server = HTTPServer(("0.0.0.0", LISTEN_PORT), RedfishHandler)

    # HTTPS server on port 8443
    ssl_port = int(os.environ.get("SUSHY_SSL_PORT", "8443"))
    cert_path = "/tmp/sushy.crt"
    key_path = "/tmp/sushy.key"
    _generate_self_signed_cert(cert_path, key_path)

    https_server = HTTPServer(("0.0.0.0", ssl_port), RedfishHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    https_server.socket = ctx.wrap_socket(https_server.socket, server_side=True)

    ssl_thread = threading.Thread(target=https_server.serve_forever, daemon=True)
    ssl_thread.start()
    print(
        f"Redfish emulator listening on port {LISTEN_PORT} (HTTP) and {ssl_port} (HTTPS)"
    )
    http_server.serve_forever()
