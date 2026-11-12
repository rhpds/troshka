"""UID-pinned ConfigMap CAS transport for the unwired issuance foundation.

Provisioning/credential management is intentionally absent. The caller supplies
the trusted namespace, object name and original UID; replacement never rebinds.
A broad Kubernetes credential does not enforce field-level issuer/worker roles.
Do not deploy this adapter as a substitute for the future mediation boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from kubernetes.client.exceptions import ApiException

from helpers.quota_fence import (
    CASConflict,
    FenceError,
    _integer,
    _text,
    validate_record,
)

MAX_RECORD_BYTES = 128 * 1024


class StoreIdentityError(FenceError):
    pass


@dataclass(frozen=True)
class Snapshot:
    uid: str
    resource_version: str
    record: dict


class CASStore(Protocol):
    """Atomic durable state only. Conflicts mean definitively not persisted.

    All other failures are ambiguous and must propagate without automatic retry.
    Implementations must reject object replacement, including during CAS.
    """

    def read(self) -> Snapshot: ...

    def compare_and_swap(self, snapshot: Snapshot, record: dict) -> None: ...


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FenceError("Duplicate persisted field")
        result[key] = value
    return result


def _kubernetes_name(value, *, namespace=False):
    _text(value, 63 if namespace else 253)
    parts = [value] if namespace else value.split(".")
    for part in parts:
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", part):
            raise FenceError("Invalid pinned Kubernetes name")
    return value


class KubernetesCASStore:
    """Operate one existing object via UID + resourceVersion JSON Patch tests."""

    def __init__(
        self, api, *, namespace: str, name: str, uid: str, max_bytes=MAX_RECORD_BYTES
    ):
        self._api = api
        self._namespace = _kubernetes_name(namespace, namespace=True)
        self._name = _kubernetes_name(name)
        self._uid = _text(uid, 128)
        self._max_bytes = _integer(max_bytes, minimum=256, maximum=MAX_RECORD_BYTES)

    def _check_size(self, encoded):
        if (
            not isinstance(encoded, str)
            or len(encoded.encode("utf-8")) > self._max_bytes
        ):
            raise FenceError("Issuance record exceeds storage bound")

    def read(self) -> Snapshot:
        try:
            obj = self._api.read_namespaced_config_map(
                name=self._name, namespace=self._namespace, _request_timeout=10
            )
        except ApiException as exc:
            if exc.status == 404:
                raise StoreIdentityError("Pinned issuance record disappeared") from None
            raise FenceError("Issuance store read failed") from None
        if obj.metadata.uid != self._uid:
            raise StoreIdentityError("Issuance record UID changed")
        version = _text(obj.metadata.resource_version, 128)
        encoded = (obj.data or {}).get("record")
        self._check_size(encoded)
        try:
            record = json.loads(encoded, object_pairs_hook=_unique_fields)
        except (ValueError, RecursionError):
            raise FenceError("Invalid issuance record encoding") from None
        validate_record(record)
        return Snapshot(self._uid, version, record)

    def compare_and_swap(self, snapshot: Snapshot, record: dict) -> None:
        if snapshot.uid != self._uid:
            raise StoreIdentityError("CAS snapshot belongs to another object")
        _text(snapshot.resource_version, 128)
        validate_record(record)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self._check_size(encoded)
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self._uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": snapshot.resource_version,
            },
            {"op": "replace", "path": "/data/record", "value": encoded},
        ]
        try:
            self._api.patch_namespaced_config_map(
                name=self._name,
                namespace=self._namespace,
                body=patch,
                _content_type="application/json-patch+json",
                _request_timeout=10,
            )
        except ApiException as exc:
            if exc.status == 409:
                raise CASConflict("Issuance CAS conflict") from None
            if exc.status == 404:
                raise StoreIdentityError("Pinned issuance record disappeared") from None
            raise FenceError("Issuance CAS failed; reconciliation required") from None
