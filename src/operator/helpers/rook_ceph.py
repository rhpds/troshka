"""Manifest builders for Project Ceph in a box (Rook-Ceph in the Troshka project ns)."""

from __future__ import annotations

import json
import logging
import os

from kubernetes import client
from kubernetes.client.exceptions import ApiException

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
ROOK_OPERATOR_DEPLOYMENT = "rook-ceph-operator"
ROOK_OPERATOR_CONFIG = "rook-ceph-operator-config"
ROOK_SYSTEM_SA = "rook-ceph-system"
ROOK_OSD_SA = "rook-ceph-osd"
ROOK_MGR_SA = "rook-ceph-mgr"
ROOK_CMD_REPORTER_SA = "rook-ceph-cmd-reporter"
ROOK_DEFAULT_SA = "rook-ceph-default"

# Troshka-owned ClusterRoles (deploy/rook-clusterroles.yaml). cmd-reporter uses a
# namespace Role per upstream Rook — not a ClusterRoleBinding.
TROSHKA_ROOK_SA_CLUSTER_ROLES: dict[str, tuple[str, ...]] = {
    ROOK_SYSTEM_SA: (
        "troshka-rook-system",
        "troshka-rook-global",
        "troshka-rook-cluster-mgmt",
    ),
    ROOK_OSD_SA: ("troshka-rook-osd", "troshka-rook-cluster-mgmt"),
    ROOK_MGR_SA: ("troshka-rook-mgr",),
}
ROOK_CLUSTER_SAS = tuple(TROSHKA_ROOK_SA_CLUSTER_ROLES.keys())
ROOK_SCC_SAS = ROOK_CLUSTER_SAS + (ROOK_CMD_REPORTER_SA, ROOK_DEFAULT_SA)
ROOK_OPERAND_SAS = ROOK_SCC_SAS
ROOK_CMD_REPORTER_ROLE = "rook-ceph-cmd-reporter"
ROOK_SCC_NAME = "rook-ceph"
ODF_ROOK_OPERATOR_NS = "openshift-storage"

DEFAULT_ROOK_OPERATOR_IMAGE = os.environ.get(
    "TROSHKA_ROOK_OPERATOR_IMAGE",
    "registry.redhat.io/odf4/rook-ceph-rhel9-operator:latest",
)

DEFAULT_OSD_COUNT = 3
MIN_OSD_COUNT = 1
MAX_OSD_COUNT = 6
MIN_OSD_SIZE_GI = 50
DEFAULT_MON_STORAGE_GI = 10
MIN_MON_STORAGE_GI = 10
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


def data_dir_host_path(namespace: str) -> str:
    """Isolated host path for Rook daemon logs/crash only (not mon data — see mon PVC).

    Mon database storage uses spec.mon.volumeClaimTemplate. Rook still mounts this
    path for ancillary daemon files; it must not overlap ODF's /var/lib/rook.
    """
    suffix = namespace.removeprefix("troshka-")
    return f"/var/lib/rook-troshka-{suffix}"


def mon_storage_gi(spec: dict) -> int:
    return max(MIN_MON_STORAGE_GI, int(spec.get("monStorageGi") or DEFAULT_MON_STORAGE_GI))


def build_mon_volume_claim_template(storage_class: str, size_gi: int) -> dict:
    """PVC template for monitor database — keeps mon data off the host."""
    return {
        "spec": {
            "storageClassName": storage_class,
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gi}Gi"}},
        }
    }


def _ceph_node_affinity() -> dict:
    """Keep project Ceph pods off dedicated ODF storage nodes when labeled."""
    return {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "cluster.ocs.openshift.io/openshift-storage",
                            "operator": "DoesNotExist",
                        }
                    ]
                }
            ]
        }
    }


def _osd_pod_anti_affinity() -> dict:
    return {
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


def _ceph_placement() -> dict:
    affinity = _ceph_node_affinity()
    return {
        "all": {"nodeAffinity": affinity},
        "mon": {"nodeAffinity": affinity},
        "mgr": {"nodeAffinity": affinity},
        "osd": {
            "nodeAffinity": affinity,
            "podAntiAffinity": _osd_pod_anti_affinity(),
        },
    }


def rook_crb_name(namespace: str, sa_name: str, cluster_role: str) -> str:
    """ClusterRoleBinding name for a project-scoped rook SA (max 63 chars)."""
    base = f"troshka-rook-{sa_name}-{cluster_role}-{namespace}"
    return base[:63].rstrip("-")


def discover_rook_operator_image(apps_api) -> str:
    """Prefer the cluster's ODF Rook image when openshift-storage is present."""
    try:
        dep = apps_api.read_namespaced_deployment(
            name=ROOK_OPERATOR_DEPLOYMENT,
            namespace=ODF_ROOK_OPERATOR_NS,
        )
        return dep.spec.template.spec.containers[0].image
    except ApiException as e:
        if e.status != 404:
            logger.warning("Could not read ODF rook operator image: %s", e)
        return DEFAULT_ROOK_OPERATOR_IMAGE


def discover_ceph_image(custom_api) -> str:
    """Match the cluster ODF Ceph image so operator CLI and mons share cephx/msgr."""
    try:
        clusters = custom_api.list_namespaced_custom_object(
            group="ceph.rook.io",
            version="v1",
            namespace=ODF_ROOK_OPERATOR_NS,
            plural="cephclusters",
        )
        items = clusters.get("items") or []
        if items:
            return items[0]["spec"]["cephVersion"]["image"]
    except ApiException as e:
        if e.status != 404:
            logger.warning("Could not read ODF ceph image: %s", e)
    return "quay.io/ceph/ceph:v19"


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


def build_ceph_cluster(ceph_cr: dict, ceph_image: str | None = None) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    osd_count, _, per_osd_gi = normalize_ceph_counts(spec)
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    mon_gi = mon_storage_gi(spec)
    image = ceph_image or spec.get("cephImage") or "quay.io/ceph/ceph:v19"
    return {
        "apiVersion": _ROOK_API,
        "kind": "CephCluster",
        "metadata": {
            "name": CEPH_CLUSTER_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "spec": {
            "cephVersion": {"image": image},
            "dataDirHostPath": data_dir_host_path(namespace),
            "skipUpgradeChecks": True,
            "continueUpgradeAfterChecksEvenIfNotHealthy": True,
            "mon": {
                "count": 1,
                "allowMultiplePerNode": True,
                "volumeClaimTemplate": build_mon_volume_claim_template(
                    storage_class, mon_gi
                ),
            },
            "mgr": {"count": 1},
            "dashboard": {"enabled": False},
            "network": {"provider": "host"},
            "crashCollector": {"disable": True},
            "placement": _ceph_placement(),
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
                        "placement": _ceph_placement()["osd"],
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


def build_rook_operator_config(ceph_cr: dict) -> dict:
    """Operator config — namespace-scoped reconcile, no CSI (ODF owns cluster CSI)."""
    namespace = ceph_cr["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ROOK_OPERATOR_CONFIG,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "rook-ceph-operator"},
        },
        "data": {
            "ROOK_CURRENT_NAMESPACE_ONLY": "true",
            "ROOK_CSI_DISABLE_DRIVER": "true",
            "ROOK_DISABLE_DEVICE_HOTPLUG": "true",
            "ROOK_HOSTPATH_REQUIRES_PRIVILEGED": "true",
            "ROOK_CEPH_MON_RUN_AS_ROOT": "true",
            "ROOK_LOG_LEVEL": "INFO",
        },
    }


def build_rook_operator_service_account(ceph_cr: dict) -> dict:
    namespace = ceph_cr["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": ROOK_SYSTEM_SA,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "rook-ceph-operator"},
        },
    }


def build_rook_cluster_role_binding(
    ceph_cr: dict, sa_name: str, cluster_role: str
) -> dict:
    """Bind a project rook SA to a Troshka-owned cluster-scoped Rook ClusterRole."""
    namespace = ceph_cr["metadata"]["namespace"]
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": rook_crb_name(namespace, sa_name, cluster_role),
            "labels": {"app": "troshka-ceph", "troshka-project-ns": namespace},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": cluster_role,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": sa_name,
                "namespace": namespace,
            }
        ],
    }


def build_rook_cmd_reporter_rbac(ceph_cr: dict) -> tuple[dict, dict]:
    """Namespace Role + RoleBinding for rook-ceph-cmd-reporter jobs."""
    namespace = ceph_cr["metadata"]["namespace"]
    labels = {"app": "troshka-ceph", "troshka-role": "rook-cmd-reporter"}
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": ROOK_CMD_REPORTER_ROLE,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["pods", "configmaps"],
                "verbs": [
                    "get",
                    "list",
                    "watch",
                    "create",
                    "update",
                    "delete",
                ],
            }
        ],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": ROOK_CMD_REPORTER_ROLE,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": ROOK_CMD_REPORTER_ROLE,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": ROOK_CMD_REPORTER_SA,
                "namespace": namespace,
            }
        ],
    }
    return role, binding


def build_rook_operator_deployment(ceph_cr: dict, image: str | None = None) -> dict:
    """Per-project rook operator — watches only its own namespace."""
    namespace = ceph_cr["metadata"]["namespace"]
    labels = {"app": "rook-ceph-operator", "troshka-role": "rook-operator"}
    operator_image = image or DEFAULT_ROOK_OPERATOR_IMAGE
    return {
        "apiVersion": _APPS_API_VERSION,
        "kind": "Deployment",
        "metadata": {
            "name": ROOK_OPERATOR_DEPLOYMENT,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": ROOK_SYSTEM_SA,
                    "containers": [
                        {
                            "name": "rook-ceph-operator",
                            "image": operator_image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": ["ceph", "operator"],
                            "env": [
                                {
                                    "name": "ROOK_CURRENT_NAMESPACE_ONLY",
                                    "value": "true",
                                },
                                {
                                    "name": "ROOK_CSI_DISABLE_DRIVER",
                                    "value": "true",
                                },
                                {
                                    "name": "ROOK_DISABLE_DEVICE_HOTPLUG",
                                    "value": "true",
                                },
                                {
                                    "name": "ROOK_HOSTPATH_REQUIRES_PRIVILEGED",
                                    "value": "true",
                                },
                                {
                                    "name": "ROOK_CEPH_MON_RUN_AS_ROOT",
                                    "value": "true",
                                },
                                {
                                    "name": "POD_NAMESPACE",
                                    "valueFrom": {
                                        "fieldRef": {"fieldPath": "metadata.namespace"}
                                    },
                                },
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {
                                        "fieldRef": {"fieldPath": "metadata.name"}
                                    },
                                },
                                {
                                    "name": "NODE_NAME",
                                    "valueFrom": {
                                        "fieldRef": {"fieldPath": "spec.nodeName"}
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "mountPath": "/var/lib/rook",
                                    "name": "rook-config",
                                },
                                {
                                    "mountPath": "/etc/ceph",
                                    "name": "default-config-dir",
                                },
                            ],
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 2016,
                                "runAsGroup": 2016,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "rook-config", "emptyDir": {}},
                        {"name": "default-config-dir", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def rook_service_account_ref(namespace: str, sa_name: str) -> str:
    return f"system:serviceaccount:{namespace}:{sa_name}"


def build_export_job(ceph_cr: dict) -> dict:
    """Enrich troshka-ceph-external from the project-scoped rook CephCluster status."""
    namespace = ceph_cr["metadata"]["namespace"]
    script = r"""
set -e
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
SECRET=troshka-ceph-external
if ! kubectl -n "$NS" get secret "$SECRET" >/dev/null 2>&1; then
  echo "external secret missing"; exit 1
fi
FSID=$(kubectl -n "$NS" get cephcluster troshka-ceph \
  -o jsonpath='{.status.ceph.fsid}{.status.cephFSID}{.status.fsid}' 2>/dev/null || true)
if [ -z "$FSID" ]; then
  TOOLBOX=$(kubectl -n "$NS" get pod -l app=rook-ceph-tools -o name 2>/dev/null | head -1)
  if [ -n "$TOOLBOX" ]; then
    FSID=$(kubectl -n "$NS" exec "$TOOLBOX" -- ceph fsid 2>/dev/null || true)
  fi
fi
if [ -n "$FSID" ]; then
  kubectl -n "$NS" patch secret "$SECRET" --type merge \
    -p "{\"stringData\":{\"fsid\":\"$FSID\"}}"
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


def delete_ceph_storage_pvcs(core_api, namespace: str) -> None:
    """Delete OSD/backing PVCs left after a TroshkaCeph teardown."""
    delete_opts = client.V1DeleteOptions(propagation_policy="Background")
    seen: set[str] = set()

    def _delete_pvc(name: str) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        try:
            core_api.delete_namespaced_persistent_volume_claim(
                name=name,
                namespace=namespace,
                body=delete_opts,
            )
            logger.info("Deleted ceph PVC %s in %s", name, namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete ceph PVC %s: %s", name, e)

    for selector in ("app=rook-ceph-osd", "app=troshka-ceph"):
        try:
            pvcs = core_api.list_namespaced_persistent_volume_claim(
                namespace=namespace,
                label_selector=selector,
            )
            for pvc in pvcs.items:
                _delete_pvc(pvc.metadata.name)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to list ceph PVCs (%s): %s", selector, e)

    try:
        pvcs = core_api.list_namespaced_persistent_volume_claim(namespace=namespace)
        for pvc in pvcs.items:
            name = pvc.metadata.name or ""
            if name.startswith("troshka-ceph-osd-") or name.startswith("osd-set-"):
                _delete_pvc(name)
    except ApiException as e:
        if e.status != 404:
            logger.warning("Failed to list namespace PVCs for ceph cleanup: %s", e)


def validate_lab_ip(lab_ip: str) -> bool:
    return bool(lab_ip and _IPV4_RE.match(lab_ip))


def _create_or_patch_configmap(core_api, namespace: str, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        core_api.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        core_api.patch_namespaced_config_map(
            name=name, namespace=namespace, body=body
        )


def _create_or_patch_deployment(apps_api, namespace: str, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        apps_api.patch_namespaced_deployment(
            name=name, namespace=namespace, body=body
        )


def _create_cluster_role_binding(rbac_api, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        rbac_api.create_cluster_role_binding(body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("ClusterRoleBinding %s already exists", name)


def _ensure_service_account(core_api, namespace: str, name: str, ceph_cr: dict) -> None:
    body = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph"},
        },
    }
    try:
        core_api.create_namespaced_service_account(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


def _ensure_rook_namespace_rbac(rbac_api, namespace: str, ceph_cr: dict) -> None:
    role, binding = build_rook_cmd_reporter_rbac(ceph_cr)
    for body in (role, binding):
        try:
            if body["kind"] == "Role":
                rbac_api.create_namespaced_role(namespace=namespace, body=body)
            else:
                rbac_api.create_namespaced_role_binding(
                    namespace=namespace, body=body
                )
        except ApiException as e:
            if e.status != 409:
                raise


def ensure_rook_operator(ceph_cr: dict) -> None:
    """Deploy a namespace-scoped rook operator before creating CephCluster CRs."""
    namespace = ceph_cr["metadata"]["namespace"]
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    _create_or_patch_configmap(core_api, namespace, build_rook_operator_config(ceph_cr))

    for sa_name in ROOK_OPERAND_SAS:
        _ensure_service_account(core_api, namespace, sa_name, ceph_cr)

    for sa_name, cluster_roles in TROSHKA_ROOK_SA_CLUSTER_ROLES.items():
        for cluster_role in cluster_roles:
            _create_cluster_role_binding(
                rbac_api,
                build_rook_cluster_role_binding(ceph_cr, sa_name, cluster_role),
            )

    _ensure_rook_namespace_rbac(rbac_api, namespace, ceph_cr)

    operator_image = discover_rook_operator_image(apps_api)
    _create_or_patch_deployment(
        apps_api,
        namespace,
        build_rook_operator_deployment(ceph_cr, image=operator_image),
    )


def delete_rook_operator(ceph_cr: dict | None, namespace: str) -> None:
    """Tear down per-project rook operator resources."""
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()
    core_api = client.CoreV1Api()

    for dep_name in (ROOK_OPERATOR_DEPLOYMENT,):
        try:
            apps_api.delete_namespaced_deployment(name=dep_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete %s: %s", dep_name, e)

    for sa_name, cluster_roles in TROSHKA_ROOK_SA_CLUSTER_ROLES.items():
        for cluster_role in cluster_roles:
            crb_name = rook_crb_name(namespace, sa_name, cluster_role)
            try:
                rbac_api.delete_cluster_role_binding(name=crb_name)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("Failed to delete CRB %s: %s", crb_name, e)

    for kind, delete_fn, obj_name in (
        ("RoleBinding", rbac_api.delete_namespaced_role_binding, ROOK_CMD_REPORTER_ROLE),
        ("Role", rbac_api.delete_namespaced_role, ROOK_CMD_REPORTER_ROLE),
    ):
        try:
            delete_fn(name=obj_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete rook %s %s: %s", kind, obj_name, e)

    for sa_name in ROOK_OPERAND_SAS:
        if sa_name == ROOK_SYSTEM_SA:
            continue
        try:
            core_api.delete_namespaced_service_account(
                name=sa_name, namespace=namespace
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete rook SA %s: %s", sa_name, e)

    for cm_name in (ROOK_OPERATOR_CONFIG,):
        try:
            core_api.delete_namespaced_config_map(name=cm_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete configmap %s: %s", cm_name, e)

    try:
        core_api.delete_namespaced_service_account(
            name=ROOK_SYSTEM_SA, namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("Failed to delete rook SA: %s", e)
