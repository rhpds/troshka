"""Bounded one-shot maintenance PATCH/Pod DELETE using static client setup."""

import json
from urllib.parse import quote

from urllib3 import Timeout

from app.services.native_workload_observations import _ENDPOINTS, WorkloadReader

_READ_ENDPOINTS = {
    **_ENDPOINTS,
    "TroshkaVM": ("/apis/troshka.redhat.com/v1alpha1", "troshkavms"),
    "TroshkaNetwork": ("/apis/troshka.redhat.com/v1alpha1", "troshkanetworks"),
    "PersistentVolume": ("/api/v1", "persistentvolumes"),
}


class HaltClient(WorkloadReader):
    def delete_pod(self, namespace, name, options, *, dry_run):
        return self._pod_request("DELETE", namespace, name, options, dry_run=dry_run)

    def read_pod_after_delete(self, namespace, name):
        return self._pod_request("GET", namespace, name)

    def _pod_request(self, method, namespace, name, options=None, *, dry_run=False):
        path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(name, safe='')}"
        if dry_run:
            path += "?dryRun=All"
        headers = {
            **self.api.default_headers,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.api.update_params_for_auth(headers, [], ["BearerToken"])
        response = self.api.rest_client.pool_manager.request(
            method,
            self.api.configuration.host + path,
            headers=headers,
            body=json.dumps(options).encode() if options is not None else None,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=self._timeout()),
        )
        try:
            payload = self._pod_payload(response)
            if method == "GET" and response.status == 404:
                self._verify_pod_not_found(payload, name)
                return None
            if response.status not in {200, 202}:
                raise ValueError("Pod request outcome unverified")
            return payload
        finally:
            response.close()

    def _pod_payload(self, response):
        data = response.read(self.max_response_bytes + 1)
        if len(data) > self.max_response_bytes:
            raise ValueError("response limit")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("invalid response")
        return payload

    @staticmethod
    def _verify_pod_not_found(payload, name):
        details = payload.get("details", {})
        if not (
            payload.get("apiVersion") == "v1"
            and payload.get("kind") == "Status"
            and payload.get("status") == "Failure"
            and payload.get("reason") == "NotFound"
            and payload.get("code") == 404
            and details.get("name") == name
            and details.get("kind") == "pods"
            and details.get("group", "") == ""
        ):
            raise ValueError("Pod absence unverified")

    def get(self, kind, namespace, name):
        prefix, resource = _READ_ENDPOINTS[kind]
        scope = (
            "/namespaces/" + quote(namespace, safe="") if namespace is not None else ""
        )
        return self._get(f"{prefix}{scope}/{resource}/{quote(name, safe='')}")

    def patch_vm(self, namespace, name, patch, *, dry_run):
        return self._patch("VirtualMachine", namespace, name, patch, dry_run=dry_run)

    def patch_controller(self, kind, namespace, name, patch, *, dry_run):
        from app.services.native_controller_halt import CONTROLLERS

        if kind not in CONTROLLERS:
            raise ValueError("unsupported controller")
        return self._patch(kind, namespace, name, patch, dry_run=dry_run)

    def _patch(self, kind, namespace, name, patch, *, dry_run):
        prefix, resource = _READ_ENDPOINTS[kind]
        path = (
            prefix
            + "/namespaces/"
            + quote(namespace, safe="")
            + "/"
            + resource
            + "/"
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
