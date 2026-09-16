"""Manifest builders for Project Ceph in a box (Rook-Ceph in the Troshka project ns)."""

from __future__ import annotations

import json
import logging
import os

from helpers.k8s import (
    CRD_GROUP,
    CRD_VERSION,
    GATEWAY_IMAGE,
    _APPS_API_VERSION,
    _IPV4_RE,
    _NET_ANNOTATION_KEY,
    _PREFIX_RE,
    owner_ref,
)

logger = logging.getLogger(__name__)

CEPH_CLUSTER_NAME = "troshka-ceph"
CEPH_BLOCK_POOL_NAME = "troshka-ceph-pool"
CEPH_EXTERNAL_SECRET = "troshka-ceph-external"  # pragma: allowlist secret
MON_BRIDGE_NAME = "troshka-ceph-mon-bridge"
EXPORT_JOB_NAME = "troshka-ceph-export"

DEFAULT_OSD_COUNT = 3
MIN_OSD_COUNT = 1
MAX_OSD_COUNT = 6
MIN_OSD_SIZE_GI = 50
DEFAULT_OSD_STORAGE_CLASS = os.environ.get(
    "TROSHKA_CEPH_OSD_STORAGE_CLASS",
    "ocs-storagecluster-ceph-rbd-virtualization",
)
_ROOK_API = "ceph.rook.io/v1"


def normalize_ceph_counts(spec: dict) -> tuple[int, int, int]:
    """Return (osd_count, replicate_size, per_osd_gi) from a TroshkaCeph spec."""
    osd_count = int(spec.get("osdCount") or DEFAULT_OSD_COUNT)
    osd_count = max(MIN_OSD_COUNT, min(MAX_OSD_COUNT, osd_count))
    replicate_size = int(spec.get("replicateSize") or min(osd_count, 3))
    replicate_size = max(1, min(replicate_size, min(osd_count, 3)))
    total_gi = int(spec.get("capacityGi") or osd_count * MIN_OSD_SIZE_GI)
    min_total = osd_count * MIN_OSD_SIZE_GI
    total_gi = max(total_gi, min_total)
    per_osd = max(MIN_OSD_SIZE_GI, total_gi // osd_count)
    return osd_count, replicate_size, per_osd


def default_lab_ip_from_cidr(cidr: str) -> str:
    if not cidr or "/" not in cidr:
        return ""
    octets = cidr.split("/")[0].split(".")
    octets[3] = "3"
    return ".".join(octets)


def _osd_pvc_name(index: int) -> str:
    return f"troshka-ceph-osd-{index}"


def build_osd_pvcs(ceph_cr: dict) -> list[dict]:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    osd_count, _, per_osd_gi = normalize_ceph_counts(spec)
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    pvcs = []
    for i in range(osd_count):
        pvcs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": _osd_pvc_name(i),
                    "namespace": namespace,
                    "ownerReferences": [owner_ref(ceph_cr)],
                    "labels": {"app": "troshka-ceph", "role": "osd"},
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Block",
                    "storageClassName": storage_class,
                    "resources": {"requests": {"storage": f"{per_osd_gi}Gi"}},
                },
            }
        )
    return pvcs


def build_ceph_cluster(ceph_cr: dict) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    osd_count, _, per_osd_gi = normalize_ceph_counts(spec)
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    return {
        "apiVersion": _ROOK_API,
        "kind": "CephCluster",
        "metadata": {
            "name": CEPH_CLUSTER_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "spec": {
            "cephVersion": {"image": "quay.io/ceph/ceph:v19"},
            "dataDirHostPath": "/var/lib/rook",
            "skipUpgradeChecks": True,
            "continueUpgradeAfterChecksEvenIfNotHealthy": True,
            "removeOSDsIfOutOfSafeRange": False,
            "mon": {"count": 1, "allowMultiplePerNode": True},
            "mgr": {"count": 1},
            "dashboard": {"enabled": False},
            "network": {"provider": "host"},
            "crashCollector": {"disable": True},
            "cleanupPolicy": {
                "confirmation": "",
                "sanitizeDisks": False,
            },
            "storage": {
                "useAllNodes": False,
                "useAllDevices": False,
                "storageClassDeviceSets": [
                    {
                        "name": "osd-set",
                        "count": osd_count,
                        "portable": True,
                        "tuneDeviceClass": False,
                        "tuneFastDeviceClass": False,
                        "placement": {
                            "podAntiAffinity": {
                                "preferredDuringSchedulingIgnoredDuringExecution": [
                                    {
                                        "weight": 100,
                                        "podAffinityTerm": {
                                            "labelSelector": {
                                                "matchExpressions": [
                                                    {
                                                        "key": "app",
                                                        "operator": "In",
                                                        "values": ["rook-ceph-osd"],
                                                    }
                                                ]
                                            },
                                            "topologyKey": "kubernetes.io/hostname",
                                        },
                                    }
                                ]
                            }
                        },
                        "volumeClaimTemplates": [
                            {
                                "metadata": {"name": "data"},
                                "spec": {
                                    "resources": {
                                        "requests": {"storage": f"{per_osd_gi}Gi"}
                                    },
                                    "storageClassName": storage_class,
                                    "volumeMode": "Block",
                                    "accessModes": ["ReadWriteOnce"],
                                },
                            }
                        ],
                    }
                ],
            },
        },
    }


def build_ceph_block_pool(ceph_cr: dict) -> dict:
    namespace = ceph_cr["metadata"]["namespace"]
    _, replicate_size, _ = normalize_ceph_counts(ceph_cr["spec"])
    return {
        "apiVersion": _ROOK_API,
        "kind": "CephBlockPool",
        "metadata": {
            "name": CEPH_BLOCK_POOL_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "spec": {
            "failureDomain": "host",
            "replicated": {"size": replicate_size},
        },
    }


def build_mon_bridge_deployment(ceph_cr: dict) -> dict:
    """TCP bridge on lab L2 (labIp:6789) → rook mon service in the project ns."""
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    nad = spec.get("networkNad", "")
    lab_ip = spec.get("labIp", "")
    prefix = str(spec.get("labPrefixLength") or 24)

    annotations = {_NET_ANNOTATION_KEY: nad} if nad else {}
    labels = {"app": "troshka-ceph-mon-bridge", "troshka-role": "ceph-mon-bridge"}

    setup_cmd = "true"
    if (
        lab_ip
        and _IPV4_RE.match(lab_ip)
        and _PREFIX_RE.match(prefix)
    ):
        setup_cmd = (
            f"ip addr add {lab_ip}/{prefix} dev net1 && ip link set net1 up"
        )

    mon_target = f"rook-ceph-mon-a.{namespace}.svc.cluster.local"
    proxy_cmd = (
        f"exec socat TCP-LISTEN:6789,bind={lab_ip},fork,reuseaddr "
        f"TCP:{mon_target}:6789"
    )

    return {
        "apiVersion": _APPS_API_VERSION,
        "kind": "Deployment",
        "metadata": {
            "name": MON_BRIDGE_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "serviceAccountName": "troshka-network",
                    "initContainers": [
                        {
                            "name": "setup-ip",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", setup_cmd],
                            "securityContext": {
                                "capabilities": {"add": ["NET_ADMIN"]}
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "mon-bridge",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", proxy_cmd],
                            "securityContext": {
                                "capabilities": {"add": ["NET_ADMIN"]}
                            },
                        }
                    ],
                },
            },
        },
    }


def build_external_secret(ceph_cr: dict, fsid: str = "") -> dict:
    """ODF-oriented external details stub; full keyring filled by export job when possible."""
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    mon_endpoint = f"{lab_ip}:6789" if lab_ip else ""
    config_entry = {
        "cluster_id": fsid or CEPH_CLUSTER_NAME,
        "mon_host": mon_endpoint,
        "mon_host_override": mon_endpoint,
    }
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": CEPH_EXTERNAL_SECRET,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph"},
        },
        "stringData": {
            "mon-host": mon_endpoint,
            "fsid": fsid or "",
            "config": json.dumps([config_entry]),
        },
    }


def build_export_job(ceph_cr: dict) -> dict:
    """Job that attempts to enrich troshka-ceph-external from the live rook cluster."""
    namespace = ceph_cr["metadata"]["namespace"]
    script = r"""
set -e
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
SECRET=troshka-ceph-external
if ! kubectl -n "$NS" get secret "$SECRET" >/dev/null 2>&1; then
  echo "external secret missing"; exit 1
fi
TOOLBOX=$(kubectl -n openshift-storage get pod -l app=rook-ceph-tools -o name 2>/dev/null | head -1)
if [ -z "$TOOLBOX" ]; then
  echo "host rook toolbox not found; keeping stub secret"
  exit 0
fi
FSID=$(kubectl -n openshift-storage exec "$TOOLBOX" -- ceph fsid 2>/dev/null || true)
if [ -n "$FSID" ]; then
  kubectl -n "$NS" patch secret "$SECRET" --type merge -p "{\"stringData\":{\"fsid\":\"$FSID\"}}"
fi
echo "export job complete"
"""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": EXPORT_JOB_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph-export"},
        },
        "spec": {
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"app": "troshka-ceph-export"}},
                "spec": {
                    "serviceAccountName": "troshka-ceph",
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "export",
                            "image": "quay.io/openshift/origin-cli:4.14",
                            "command": ["sh", "-c", script],
                        }
                    ],
                },
            },
        },
    }


def build_ceph_rbac(ceph_cr: dict) -> tuple[dict, dict]:
    """Role + RoleBinding so export job can patch secrets in the project ns."""
    namespace = ceph_cr["metadata"]["namespace"]
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": "troshka-ceph",
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["get", "patch", "create", "update"],
            }
        ],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "troshka-ceph",
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "troshka-ceph",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "troshka-ceph",
                "namespace": namespace,
            }
        ],
    }
    return role, binding


def ceph_cluster_phase(custom_api, namespace: str) -> tuple[str, str]:
    """Return (phase, fsid) from the rook CephCluster status, if present."""
    try:
        cluster = custom_api.get_namespaced_custom_object(
            group="ceph.rook.io",
            version="v1",
            namespace=namespace,
            plural="cephclusters",
            name=CEPH_CLUSTER_NAME,
        )
    except Exception:
        return "", ""
    status = cluster.get("status") or {}
    phase = str(status.get("phase") or "")
    fsid = str(status.get("cephFSID") or status.get("fsid") or "")
    return phase, fsid


def is_ceph_ready(phase: str) -> bool:
    return phase.lower() in ("ready", "connected")


def validate_lab_ip(lab_ip: str) -> bool:
    return bool(lab_ip and _IPV4_RE.match(lab_ip))
