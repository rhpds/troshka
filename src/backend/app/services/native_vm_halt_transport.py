"""Bounded one-shot maintenance PATCH, sharing the read-only static client setup."""

import json
from urllib.parse import quote

from urllib3 import Timeout

from app.services.native_workload_observations import _ENDPOINTS, WorkloadReader

_READ_ENDPOINTS = {
    **_ENDPOINTS,
    "TroshkaVM": ("/apis/troshka.redhat.com/v1alpha1", "troshkavms"),
    "PersistentVolume": ("/api/v1", "persistentvolumes"),
}


class HaltClient(WorkloadReader):
    def get(self, kind, namespace, name):
        prefix, resource = _READ_ENDPOINTS[kind]
        scope = (
            "/namespaces/" + quote(namespace, safe="") if namespace is not None else ""
        )
        return self._get(f"{prefix}{scope}/{resource}/{quote(name, safe='')}")

    def patch_vm(self, namespace, name, patch, *, dry_run):
        path = (
            "/apis/kubevirt.io/v1/namespaces/"
            + quote(namespace, safe="")
            + "/virtualmachines/"
            + quote(name, safe="")
        )
        if dry_run:
            path += "?dryRun=All"
        headers = {
            **self.api.default_headers,
            "Accept": "application/json",
            "Content-Type": "application/json-patch+json",
        }
        self.api.update_params_for_auth(headers, [], ["BearerToken"])
        response = self.api.rest_client.pool_manager.request(
            "PATCH",
            self.api.configuration.host + path,
            headers=headers,
            body=json.dumps(patch).encode(),
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=self._timeout()),
        )
        try:
            if not 200 <= response.status <= 299:
                raise ValueError("PATCH outcome unverified")
            data = response.read(self.max_response_bytes + 1)
            if len(data) > self.max_response_bytes:
                raise ValueError("response limit")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            return payload
        finally:
            response.close()
