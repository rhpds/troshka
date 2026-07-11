"""Lightweight Redfish emulator using the KubeVirt driver."""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

from kubevirt_driver import KubeVirtDriver

driver = KubeVirtDriver()

USERNAME = os.environ.get("SUSHY_USERNAME", "admin")
PASSWORD = os.environ.get("SUSHY_PASSWORD", "redhat")  # pragma: allowlist secret
LISTEN_PORT = int(os.environ.get("SUSHY_LISTEN_PORT", "8000"))


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
            self.send_header("WWW-Authenticate", 'Basic realm="Redfish"')
            self.end_headers()
            return

        path = self.path.rstrip("/")

        if path == "/redfish/v1":
            _send_json(self, {
                "@odata.type": "#ServiceRoot.v1_0_0.ServiceRoot",
                "Id": "RootService",
                "Name": "Troshka Redfish Service",
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
            })
            return

        if path == "/redfish/v1/Systems":
            systems = driver.get_systems()
            members = [
                {"@odata.id": f"/redfish/v1/Systems/{s}"} for s in systems
            ]
            _send_json(self, {
                "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
                "Name": "Computer System Collection",
                "Members": members,
                "Members@odata.count": len(members),
            })
            return

        if path.startswith("/redfish/v1/Systems/"):
            identity = path.split("/redfish/v1/Systems/")[1].split("/")[0]

            if path.endswith(identity):
                power = driver.get_power_state(identity)
                boot_dev = driver.get_boot_device(identity)
                boot_mode = driver.get_boot_mode(identity)
                mem = driver.get_total_memory(identity)
                cpus = driver.get_total_cpus(identity)
                _send_json(self, {
                    "@odata.type": "#ComputerSystem.v1_1_0.ComputerSystem",
                    "Id": identity,
                    "Name": identity,
                    "UUID": identity,
                    "PowerState": power,
                    "MemorySummary": {"TotalSystemMemoryGiB": mem / 1024},
                    "ProcessorSummary": {"Count": cpus},
                    "Boot": {
                        "BootSourceOverrideTarget": boot_dev,
                        "BootSourceOverrideMode": boot_mode,
                    },
                    "Actions": {
                        "#ComputerSystem.Reset": {
                            "target": f"/redfish/v1/Systems/{identity}/Actions/ComputerSystem.Reset",
                            "ResetType@Redfish.AllowableValues": [
                                "On", "ForceOff", "GracefulShutdown",
                                "ForceRestart", "ForceOn",
                            ],
                        }
                    },
                })
                return

        _send_json(self, {"error": "Not found"}, 404)

    def do_POST(self):
        if not _check_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Redfish"')
            self.end_headers()
            return

        path = self.path.rstrip("/")

        if "/Actions/ComputerSystem.Reset" in path:
            identity = path.split("/redfish/v1/Systems/")[1].split("/")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            reset_type = body.get("ResetType", "On")
            driver.set_power_state(identity, reset_type)
            self.send_response(204)
            self.end_headers()
            return

        _send_json(self, {"error": "Not found"}, 404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), RedfishHandler)
    print(f"Redfish emulator listening on port {LISTEN_PORT}")
    server.serve_forever()
