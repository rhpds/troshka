"""Discover KubeVirt cluster limits and publish them for the Troshka UI."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from kubernetes.client.exceptions import ApiException

from helpers.kubevirt import (
    _KV_VIDEO_MODELS,
    _get_kubevirt_feature_gates,
    is_video_config_enabled,
    parse_admission_api_warnings,
)

logger = logging.getLogger("troshka-operator")

CAPABILITIES_CONFIGMAP = "troshka-cluster-capabilities"
CAPABILITIES_LABEL = "troshka.redhat.com/cluster-capabilities"
CAPABILITIES_VERSION = 1

_ALL_DISK_BUSES = ("virtio", "scsi", "sata", "ide", "usb")
_ALL_INPUT_MODELS = ("virtio", "usb", "ps2")


def _operator_namespace() -> str:
    pod_ns = os.environ.get("POD_NAMESPACE", "").strip()
    return pod_ns or "troshka-operator"


def _probe_vm_body(namespace: str, domain_patch: dict) -> dict:
    domain = {
        "cpu": {"cores": 1},
        "resources": {"requests": {"memory": "1Gi"}},
        "devices": {
            "disks": [{"name": "disk0", "disk": {"bus": "virtio"}}],
            "interfaces": [{"name": "default", "masquerade": {}}],
        },
    }
    domain.update(domain_patch)
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": "troshka-capability-probe",
            "namespace": namespace,
            "labels": {"app": "troshka", "troshka-role": "capability-probe"},
        },
        "spec": {
            "runStrategy": "Halted",
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": "troshka-capability-probe"}},
                "spec": {
                    "domain": domain,
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {
                            "name": "disk0",
                            "containerDisk": {
                                "image": "quay.io/kubevirt/cirros-container-disk-demo",
                            },
                        }
                    ],
                },
            },
        },
    }


def _admission_allows(custom_api, namespace: str, body: dict) -> bool:
    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=body,
            dry_run="All",
        )
        return True
    except ApiException as exc:
        if exc.status in (400, 422):
            warnings = parse_admission_api_warnings(exc)
            logger.debug("Capability probe rejected: %s", warnings)
            return False
        raise


def _probe_disk_buses(custom_api, namespace: str) -> list[str]:
    supported: list[str] = []
    for bus in _ALL_DISK_BUSES:
        body = _probe_vm_body(
            namespace,
            {
                "devices": {
                    "disks": [{"name": "disk0", "disk": {"bus": bus}}],
                    "interfaces": [{"name": "default", "masquerade": {}}],
                }
            },
        )
        if _admission_allows(custom_api, namespace, body):
            supported.append(bus)
    return supported


def _probe_video_models(custom_api, namespace: str) -> list[str]:
    supported: list[str] = []
    for model in sorted(_KV_VIDEO_MODELS):
        body = _probe_vm_body(
            namespace,
            {
                "devices": {
                    "disks": [{"name": "disk0", "disk": {"bus": "virtio"}}],
                    "interfaces": [{"name": "default", "masquerade": {}}],
                    "video": {"type": model},
                }
            },
        )
        if _admission_allows(custom_api, namespace, body):
            supported.append(model)
    return supported


def collect_kubevirt_cluster_capabilities(custom_api, namespace: str) -> dict:
    """Probe admission and feature gates; return a capabilities document."""
    feature_gates = sorted(_get_kubevirt_feature_gates(custom_api))
    video_config_enabled = is_video_config_enabled(custom_api)
    disk_buses = _probe_disk_buses(custom_api, namespace)
    video_models = (
        _probe_video_models(custom_api, namespace) if video_config_enabled else []
    )
    return {
        "version": CAPABILITIES_VERSION,
        "updatedAt": datetime.now(UTC).isoformat(),
        "kubevirt": {
            "featureGates": feature_gates,
            "videoConfigEnabled": video_config_enabled,
            "diskBuses": disk_buses,
            "videoModels": video_models,
            "inputModels": list(_ALL_INPUT_MODELS),
        },
    }


def upsert_capabilities_configmap(core_api, namespace: str, capabilities: dict) -> None:
    """Write or update the cluster capabilities ConfigMap."""
    payload = json.dumps(capabilities, sort_keys=True)
    metadata = {
        "name": CAPABILITIES_CONFIGMAP,
        "namespace": namespace,
        "labels": {
            "app": "troshka-operator",
            CAPABILITIES_LABEL: "true",
        },
    }
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": metadata,
        "data": {"capabilities.json": payload},
    }
    try:
        core_api.replace_namespaced_config_map(
            name=CAPABILITIES_CONFIGMAP,
            namespace=namespace,
            body=body,
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        core_api.create_namespaced_config_map(namespace=namespace, body=body)


def refresh_cluster_capabilities(custom_api, core_api, namespace: str | None = None) -> dict:
    """Probe the cluster and publish capabilities to the operator ConfigMap."""
    ns = namespace or _operator_namespace()
    capabilities = collect_kubevirt_cluster_capabilities(custom_api, ns)
    upsert_capabilities_configmap(core_api, ns, capabilities)
    kv = capabilities.get("kubevirt", {})
    logger.info(
        "Published cluster capabilities: diskBuses=%s videoModels=%s",
        kv.get("diskBuses"),
        kv.get("videoModels"),
    )
    return capabilities
