"""Read KubeVirt cluster capabilities published by the troshka-operator."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

CAPABILITIES_CONFIGMAP = "troshka-cluster-capabilities"
_DEFAULT_KUBEVIRT_CAPABILITIES: dict[str, Any] = {
    "featureGates": [],
    "videoConfigEnabled": False,
    "diskBuses": ["virtio", "scsi", "sata"],
    "videoModels": [],
    "inputModels": ["virtio", "usb", "ps2"],
}

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CACHE_TTL_SECS = 60.0


def _operator_namespace(provider) -> str:
    creds = provider.get_credentials() or {}
    return creds.get("namespace") or "troshka-operator"


def _normalize_capabilities(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    kv = raw.get("kubevirt")
    if not isinstance(kv, dict):
        return None
    merged = dict(_DEFAULT_KUBEVIRT_CAPABILITIES)
    for key in merged:
        if key in kv and kv[key] is not None:
            merged[key] = kv[key]
    return {
        "version": raw.get("version", 1),
        "updatedAt": raw.get("updatedAt"),
        "kubevirt": merged,
    }


def fetch_cluster_capabilities(provider) -> dict[str, Any] | None:
    """Return operator-published capabilities, with a short in-process cache."""
    from app.services.providers.kubevirt import _get_k8s_clients

    now = time.monotonic()
    cached = _cache.get(provider.id)
    if cached and now - cached[0] < _CACHE_TTL_SECS:
        return cached[1]

    _, core_api, _ = _get_k8s_clients(provider)
    ns = _operator_namespace(provider)
    capabilities: dict[str, Any] | None = None
    try:
        cm = core_api.read_namespaced_config_map(CAPABILITIES_CONFIGMAP, ns)
        cm_data = getattr(cm, "data", None) or {}
        raw = json.loads(cm_data.get("capabilities.json", "{}"))
        capabilities = _normalize_capabilities(raw)
    except ApiException as exc:
        if exc.status != 404:
            logger.warning(
                "Failed to read cluster capabilities for provider %s: %s",
                provider.id[:8],
                exc,
            )
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning(
            "Invalid cluster capabilities ConfigMap for provider %s: %s",
            provider.id[:8],
            exc,
        )

    _cache[provider.id] = (now, capabilities)
    return capabilities


def get_kubevirt_capabilities(provider) -> dict[str, Any]:
    """Return kubevirt capability block, falling back to conservative defaults."""
    doc = fetch_cluster_capabilities(provider)
    if doc and doc.get("kubevirt"):
        return doc["kubevirt"]
    return dict(_DEFAULT_KUBEVIRT_CAPABILITIES)


def validate_vm_disk_buses_against_capabilities(
    current: dict,
    vm_ids: list[str],
    capabilities: dict[str, Any],
) -> str | None:
    """Return an error when a VM disk bus is unsupported on this cluster."""
    from app.services.deploy_topology import _extract_vms, build_troshkavm_vm_spec

    allowed = set(capabilities.get("diskBuses") or [])
    if not allowed:
        return None

    vms = {v["node_id"]: v for v in _extract_vms(current)}
    for vm_id in vm_ids:
        vm = vms.get(vm_id)
        if not vm:
            continue
        spec = build_troshkavm_vm_spec(vm_id, vm, current)
        for disk in spec.get("disks", []):
            bus = disk.get("bus", "virtio")
            if bus not in allowed:
                vm_name = vm.get("name", vm_id[:8])
                return (
                    f"VM {vm_name}: {bus.upper()} disk bus is not supported on this "
                    "KubeVirt cluster"
                )
    return None
