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
    TOOLS_IMAGE,
    _APPS_API_VERSION,
    _IPV4_RE,
    _NET_ANNOTATION_KEY,
    _PREFIX_RE,
    owner_ref,
)

logger = logging.getLogger(__name__)

CEPH_CLUSTER_NAME = "troshka-ceph"
CEPH_BLOCK_POOL_NAME = "troshka-ceph-pool"
TROSHKA_CEPH_CR_NAME = "project-ceph"
CEPH_MON_PVC_NAME = "rook-ceph-mon-a"
CEPH_DEVICE_SET_NAME = "osd-set"
CEPH_OSD_TEMPLATE_NAME = "data"
ROOK_LABEL_DEVICE_SET = "ceph.rook.io/DeviceSet"
ROOK_LABEL_DEVICE_SET_PVC_ID = "ceph.rook.io/DeviceSetPVCId"
ROOK_LABEL_SET_INDEX = "ceph.rook.io/setIndex"
_GIB = 1073741824
CEPH_EXTERNAL_SECRET = "troshka-ceph-external"  # pragma: allowlist secret
RESTORE_ANNOTATION_MON_PVC = "troshka.redhat.com/ceph-restore-mon-pvc"
RESTORE_ANNOTATION_OSD_PVCS = "troshka.redhat.com/ceph-restore-osd-pvcs"
MON_BRIDGE_NAME = "troshka-ceph-mon-bridge"
MON_BRIDGE_BACKEND_SVC = "troshka-ceph-mon"
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
    return max(
        MIN_MON_STORAGE_GI, int(spec.get("monStorageGi") or DEFAULT_MON_STORAGE_GI)
    )


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
    """Pick a stable lab IP for the Ceph mon bridge.

    Reserved on the project subnet: .1 gateway, .2 dnsmasq, .3 exec.
    Ceph uses .4 so it does not collide with the exec pod.
    """
    if not cidr or "/" not in cidr:
        return ""
    octets = cidr.split("/")[0].split(".")
    octets[3] = "4"
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


def normalize_restore_spec(spec: dict) -> dict:
    """Normalize TroshkaCeph ``spec.restore`` into ``{enabled, monPvc, osdPvcs}``.

    Per the Rook PVC-adopt spike (``docs/dev/project-ceph-pattern-restore.md``),
    Rook's ``CephCluster`` CR has no field meaning "attach PVC X" — it adopts
    the mon PVC by its fixed name and OSD PVCs by the ``ceph.rook.io/DeviceSet*``
    labels that a prior capture/restore step stamps on the pre-created claims
    before this CR exists. ``spec.restore`` therefore carries mostly
    traceability metadata; the one value ``build_ceph_cluster()`` must act on
    is the *count* of already-adopted OSD PVCs, so it never asks Rook to
    provision extra "empty" ones to top up to the configured ``osdCount``.
    """
    restore = spec.get("restore") or {}
    if not restore.get("enabled"):
        return {"enabled": False, "monPvc": "", "osdPvcs": []}
    return {
        "enabled": True,
        "monPvc": str(restore.get("monPvc") or CEPH_MON_PVC_NAME),
        "osdPvcs": list(restore.get("osdPvcs") or []),
    }


def _restore_cluster_annotations(restore: dict, osd_count: int) -> tuple[dict, int]:
    """Return (annotations, effective_osd_count) for a restore-mode CephCluster.

    Raises if ``monPvc`` cannot match Rook's fixed mon PVC name — restore can
    never adopt under a different name (see ``normalize_restore_spec``).
    """
    mon_pvc = restore.get("monPvc") or CEPH_MON_PVC_NAME
    if mon_pvc != CEPH_MON_PVC_NAME:
        raise ValueError(
            f"restore.monPvc must be {CEPH_MON_PVC_NAME!r} (Rook's fixed mon "
            f"PVC name), got {mon_pvc!r}"
        )
    osd_pvcs = restore.get("osdPvcs") or []
    if osd_pvcs:
        # Cap the device-set count at the number of already-adopted OSD PVCs
        # instead of the spec-computed osdCount, so Rook's reconcile does not
        # mint new empty claims to fill the gap. If osdPvcs is empty (a
        # malformed restore block), fall back to the spec count below rather
        # than requesting zero OSDs.
        osd_count = len(osd_pvcs)
    annotations = {
        RESTORE_ANNOTATION_MON_PVC: mon_pvc,
        RESTORE_ANNOTATION_OSD_PVCS: ",".join(osd_pvcs),
    }
    return annotations, osd_count


def build_ceph_cluster(
    ceph_cr: dict, ceph_image: str | None = None, restore: dict | None = None
) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    osd_count, _, per_osd_gi = normalize_ceph_counts(spec)
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    mon_gi = mon_storage_gi(spec)
    image = ceph_image or spec.get("cephImage") or "quay.io/ceph/ceph:v19"

    restore = restore if restore is not None else normalize_restore_spec(spec)
    annotations: dict = {}
    if restore.get("enabled"):
        annotations, osd_count = _restore_cluster_annotations(restore, osd_count)

    metadata = {
        "name": CEPH_CLUSTER_NAME,
        "namespace": namespace,
        "ownerReferences": [owner_ref(ceph_cr)],
    }
    if annotations:
        metadata["annotations"] = annotations

    return {
        "apiVersion": _ROOK_API,
        "kind": "CephCluster",
        "metadata": metadata,
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


def build_mon_backend_service(ceph_cr: dict) -> dict:
    """ClusterIP for hostNetwork mon daemons (v1:6789 + msgr2:3300)."""
    namespace = ceph_cr["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": MON_BRIDGE_BACKEND_SVC,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-mon-backend"},
        },
        "spec": {
            "selector": {"app": "rook-ceph-mon", "rook_cluster": namespace},
            "ports": [
                {"name": "msgr1", "port": 6789, "targetPort": 6789, "protocol": "TCP"},
                {"name": "msgr2", "port": 3300, "targetPort": 3300, "protocol": "TCP"},
            ],
        },
    }


def build_mon_bridge_deployment(ceph_cr: dict) -> dict:
    """TCP bridge on lab L2 → rook mon (6789/3300) + mgr metrics (9283).

    Nested ODF CSI must NOT use this endpoint for krbd: export stamps the
    hostNetwork mon IP instead. Bridge remains for Multus-local tools/metrics.
    """
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    nad = spec.get("networkNad", "")
    lab_ip = spec.get("labIp", "")
    prefix = str(spec.get("labPrefixLength") or 24)

    annotations = {_NET_ANNOTATION_KEY: nad} if nad else {}
    labels = {"app": "troshka-ceph-mon-bridge", "troshka-role": "ceph-mon-bridge"}

    setup_cmd = "true"
    if lab_ip and _IPV4_RE.match(lab_ip) and _PREFIX_RE.match(prefix):
        setup_cmd = f"ip addr add {lab_ip}/{prefix} dev net1 && ip link set net1 up"

    mon_target = f"{MON_BRIDGE_BACKEND_SVC}.{namespace}.svc.cluster.local"
    mgr_target = f"rook-ceph-mgr.{namespace}.svc.cluster.local"
    # ODF external mode uses msgr2 (3300) + health-checks MonitoringPort 9283.
    proxy_cmd = (
        f"socat TCP-LISTEN:6789,bind={lab_ip},fork,reuseaddr "
        f"TCP:{mon_target}:6789 & "
        f"socat TCP-LISTEN:3300,bind={lab_ip},fork,reuseaddr "
        f"TCP:{mon_target}:3300 & "
        f"exec socat TCP-LISTEN:9283,bind={lab_ip},fork,reuseaddr "
        f"TCP:{mgr_target}:9283"
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
                            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                        }
                    ],
                    "containers": [
                        {
                            "name": "mon-bridge",
                            "image": GATEWAY_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", proxy_cmd],
                            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                        }
                    ],
                },
            },
        },
    }


def build_external_secret(ceph_cr: dict, fsid: str = "") -> dict:
    """ODF-oriented external details stub; full keyring filled by export job when possible.

    Placeholder mon-host uses labIp (Multus bridge). Export job overwrites with the
    hostNetwork mon IP on :3300 for nested CSI/krbd.
    """
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    # Placeholder until export discovers hostNetwork mon; prefer msgr2 port.
    mon_endpoint = f"{lab_ip}:3300" if lab_ip else ""
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


def ceph_external_details_exported(core_api, namespace: str) -> bool:
    """True when troshka-ceph-external carries ODF external_cluster_details."""
    try:
        secret = core_api.read_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET, namespace=namespace
        )
    except ApiException:
        return False
    data = secret.data or {}
    return bool(data.get("external_cluster_details"))


def _decode_secret_field(secret, field: str) -> str:
    import base64

    data = secret.data or {}
    raw = data.get(field)
    if not raw:
        return ""
    return base64.b64decode(raw).decode()


def nested_mon_host_from_secret(core_api, namespace: str) -> str:
    """Return exported mon-host (hostNetwork IP:3300) for nested CSI, if present."""
    try:
        secret = core_api.read_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET, namespace=namespace
        )
    except ApiException:
        return ""
    return _decode_secret_field(secret, "mon-host")


def ceph_export_needs_nested_mon_refresh(core_api, namespace: str, lab_ip: str) -> bool:
    """True when export still points nested CSI at the Multus labIp bridge.

    Kernel RBD (krbd) fails when monmap/CSI advertise labIp while the mon peer
    identity is the hostNetwork IP (or a dual addrvec). Nested clusters reach the
    hostNetwork mon directly; export must use that IP on msgr2 :3300.
    """
    if not ceph_external_details_exported(core_api, namespace):
        return True
    mon_host = nested_mon_host_from_secret(core_api, namespace)
    if not mon_host:
        return True
    if lab_ip and mon_host.startswith(f"{lab_ip}:"):
        return True
    if not mon_host.endswith(":3300"):
        return True
    return False


def clear_ceph_external_details(core_api, namespace: str) -> None:
    """Drop ODF blob so ensure_ceph_export_job will relaunch."""
    try:
        core_api.patch_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET,
            namespace=namespace,
            body={"data": {"external_cluster_details": None}},
        )
    except ApiException as e:
        if e.status != 404:
            raise


def _ceph_export_job_script() -> str:
    """Shell wrapper that builds ODF external_cluster_details via in-cluster Python."""
    return r"""
set -euo pipefail
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
SECRET=troshka-ceph-external
POOL=troshka-ceph-pool
python3 <<'PY'
import base64
import json
import re
import subprocess
import sys

ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
secret_name = "troshka-ceph-external"  # pragma: allowlist secret
pool_name = "troshka-ceph-pool"


def kubectl(args: list[str]) -> str:
    return subprocess.check_output(
        ["kubectl", "-n", ns, *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def kubectl_json(args: list[str]):
    return json.loads(kubectl(args))


def secret_field(name: str, field: str) -> str:
    secret = kubectl_json(["get", "secret", name, "-o", "json"])
    raw = (secret.get("data") or {}).get(field)
    if raw:
        return base64.b64decode(raw).decode()
    return (secret.get("stringData") or {}).get(field, "")


def parse_keyring(keyring: str) -> str:
    match = re.search(r"key\s*=\s*(\S+)", keyring)
    if not match:
        raise RuntimeError("no key found in rook admin keyring")
    return match.group(1)


def discover_hostnetwork_mon_ip() -> str:
    # hostNetwork mon hostIP; nested CSI reaches this without Multus socat.
    pods = kubectl_json(
        ["get", "pods", "-l", "app=rook-ceph-mon", "-o", "json"]
    )
    for pod in pods.get("items") or []:
        status = pod.get("status") or {}
        if status.get("phase") != "Running":
            continue
        host_ip = status.get("hostIP") or status.get("podIP") or ""
        if host_ip:
            return host_ip
    raise RuntimeError("no running rook-ceph-mon pod with hostIP")


def collapse_monmap_to_host(mon_ip: str, admin_key: str) -> None:
    # Single mon addrvec - dual labIp+host breaks kernel libceph/krbd.
    mon_pods = kubectl_json(["get", "pods", "-l", "app=rook-ceph-mon", "-o", "json"])
    mon_name = ""
    for pod in mon_pods.get("items") or []:
        if (pod.get("status") or {}).get("phase") == "Running":
            mon_name = pod["metadata"]["name"]
            break
    if not mon_name:
        print("skip mon set-addrs: no running mon pod", file=sys.stderr)
        return
    keyring = f"[client.admin]\n\tkey = {admin_key}\n"
    addrs = f"[v2:{mon_ip}:3300/0,v1:{mon_ip}:6789/0]"
    script = (
        "cat >/tmp/admin.keyring <<'KEY'\n"
        f"{keyring}"
        "KEY\n"
        "ceph --conf /dev/null --mon-host=127.0.0.1:3300 "
        "--keyring=/tmp/admin.keyring -n client.admin "
        f"mon set-addrs a '{addrs}' || "
        "ceph --conf /dev/null --mon-host=$ROOK_CEPH_MON_HOST "
        "--keyring=/tmp/admin.keyring -n client.admin "
        f"mon set-addrs a '{addrs}'\n"
    )
    try:
        subprocess.check_call(
            [
                "kubectl",
                "-n",
                ns,
                "exec",
                mon_name,
                "-c",
                "mon",
                "--",
                "bash",
                "-c",
                script,
            ]
        )
        print(f"monmap collapsed to {addrs}")
    except subprocess.CalledProcessError as exc:
        print(f"mon set-addrs warning: {exc}", file=sys.stderr)


lab_mon_host = secret_field(secret_name, "mon-host")
lab_ip = lab_mon_host.split(":", 1)[0] if lab_mon_host else ""

fsid = kubectl(
    [
        "get",
        "cephcluster",
        "troshka-ceph",
        "-o",
        "jsonpath={.status.ceph.fsid}{.status.cephFSID}{.status.fsid}",
    ]
)
if not fsid:
    fsid = secret_field("rook-ceph-mon", "fsid")

admin_key = parse_keyring(secret_field("rook-ceph-admin-keyring", "keyring"))
mon_key = secret_field("rook-ceph-mon", "mon-secret")
csi_node_key = secret_field("rook-csi-rbd-node", "userKey")
csi_prov_key = secret_field("rook-csi-rbd-provisioner", "userKey")
for label, value in (
    ("admin", admin_key),
    ("mon", mon_key),
    ("csi-node", csi_node_key),
    ("csi-provisioner", csi_prov_key),
):
    if not value:
        print(f"missing {label} key in rook secrets", file=sys.stderr)
        sys.exit(1)

# Nested ODF/CSI: hostNetwork mon on msgr2. Multus labIp bridge remains for
# in-lab tools/metrics only (see mon-bridge); do not dual-advertise both.
mon_ip = discover_hostnetwork_mon_ip()
mon_host = f"{mon_ip}:3300"
collapse_monmap_to_host(mon_ip, admin_key)
mon_endpoints = f"a={mon_host}"
# Monitoring scrape via Multus bridge when labIp known; else host mon IP.
monitor_ip = lab_ip or mon_ip
resources = [
    {
        "name": "rook-ceph-mon-endpoints",
        "kind": "ConfigMap",
        "data": {"data": mon_endpoints, "maxMonId": "0", "mapping": "{}"},
    },
    {
        "name": "rook-ceph-mon",
        "kind": "Secret",
        "data": {
            "admin-secret": admin_key,
            "fsid": fsid,
            "mon-secret": mon_key,
        },
    },
    {
        "name": "rook-ceph-operator-creds",
        "kind": "Secret",
        "data": {"userID": "client.admin", "userKey": admin_key},
    },
    {
        "name": "rook-csi-rbd-node",
        "kind": "Secret",
        "data": {"userID": "csi-rbd-node", "userKey": csi_node_key},
    },
    {
        "name": "rook-csi-rbd-provisioner",
        "kind": "Secret",
        "data": {"userID": "csi-rbd-provisioner", "userKey": csi_prov_key},
    },
    {
        "name": "ceph-rbd",
        "kind": "StorageClass",
        "data": {"pool": pool_name},
    },
    {
        "name": "monitoring-endpoint",
        "kind": "CephCluster",
        "data": {"MonitoringEndpoint": monitor_ip, "MonitoringPort": "9283"},
    },
]
blob = json.dumps(resources)
string_data = {
    "fsid": fsid,
    "mon-host": mon_host,
    "mon-host-lab": lab_mon_host or (f"{lab_ip}:3300" if lab_ip else ""),
    "external_cluster_details": blob,
}
string_data = {k: v for k, v in string_data.items() if v}
patch = json.dumps({"stringData": string_data})
subprocess.check_call(
    ["kubectl", "-n", ns, "patch", "secret", secret_name, "--type", "merge", "-p", patch]
)
print(f"export job complete mon-host={mon_host}")
PY
"""


def ensure_ceph_export_job(batch_api, ceph_cr: dict, namespace: str) -> None:
    """Launch (or relaunch) the export job when ODF details are not yet stamped."""
    core_api = client.CoreV1Api()
    lab_ip = (ceph_cr.get("spec") or {}).get("labIp", "")
    if ceph_export_needs_nested_mon_refresh(core_api, namespace, lab_ip):
        if ceph_external_details_exported(core_api, namespace):
            logger.info(
                "Refreshing Ceph export in %s (nested mon must be hostNetwork:3300)",
                namespace,
            )
            clear_ceph_external_details(core_api, namespace)
            try:
                batch_api.delete_namespaced_job(
                    name=EXPORT_JOB_NAME,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
            except ApiException as e:
                if e.status != 404:
                    raise
        # fall through to create
    elif ceph_external_details_exported(core_api, namespace):
        return

    try:
        job = batch_api.read_namespaced_job(name=EXPORT_JOB_NAME, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        job = None

    if job is not None:
        status = job.status or client.V1JobStatus()
        if (status.succeeded or 0) >= 1:
            # Stale success while details cleared — delete and recreate below
            if not ceph_external_details_exported(core_api, namespace):
                batch_api.delete_namespaced_job(
                    name=EXPORT_JOB_NAME,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
            else:
                return
        elif (status.failed or 0) >= 1:
            batch_api.delete_namespaced_job(
                name=EXPORT_JOB_NAME,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        else:
            return

    batch_api.create_namespaced_job(namespace=namespace, body=build_export_job(ceph_cr))


def build_export_job(ceph_cr: dict) -> dict:
    """Build ODF external_cluster_details in troshka-ceph-external."""
    namespace = ceph_cr["metadata"]["namespace"]
    script = _ceph_export_job_script()
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
                            "image": TOOLS_IMAGE,
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
            },
            {
                "apiGroups": ["ceph.rook.io"],
                "resources": ["cephclusters"],
                "verbs": ["get"],
            },
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


def _storage_request_bytes(quantity: str | None) -> int:
    """Parse a Kubernetes storage quantity into bytes."""
    if not quantity:
        return 0
    q = str(quantity).strip()
    if q.endswith("Gi"):
        return int(q[:-2]) * _GIB
    if q.endswith("Ti"):
        return int(q[:-2]) * 1024 * _GIB
    if q.endswith("Mi"):
        return int(q[:-2]) * 1024 * 1024
    if q.endswith("G"):
        return max(1, round(float(q[:-1]) * 1_000_000_000))
    if q.isdigit():
        return int(q)
    return 0


def _pvc_size_bytes(pvc) -> int:
    spec = pvc.spec
    requests = (spec.resources.requests if spec and spec.resources else None) or {}
    storage = requests.get("storage")
    return _storage_request_bytes(str(storage) if storage is not None else None)


def _expected_osd_count(namespace: str, custom_api=None) -> int:
    """Return osdCount from TroshkaCeph, falling back to the CephCluster device set.

    ``custom_api`` lets callers outside the operator process (e.g. Troshka's
    backend, which talks to a remote provider cluster via an explicit
    ``ApiClient``) pass their own client instead of relying on the SDK's
    process-global default.
    """
    custom_api = custom_api or client.CustomObjectsApi()
    try:
        ceph_cr = custom_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkancephs",
            name=TROSHKA_CEPH_CR_NAME,
        )
        osd_count, _, _ = normalize_ceph_counts(ceph_cr.get("spec") or {})
        return osd_count
    except ApiException as e:
        if e.status != 404:
            raise

    try:
        cluster = custom_api.get_namespaced_custom_object(
            group="ceph.rook.io",
            version="v1",
            namespace=namespace,
            plural="cephclusters",
            name=CEPH_CLUSTER_NAME,
        )
    except ApiException as e:
        if e.status == 404:
            raise ValueError(
                f"cannot determine osdCount for namespace {namespace}: "
                "TroshkaCeph and CephCluster not found"
            ) from e
        raise

    device_sets = (
        (cluster.get("spec") or {})
        .get("storage", {})
        .get("storageClassDeviceSets")
        or []
    )
    if not device_sets:
        raise ValueError(
            f"cannot determine osdCount for namespace {namespace}: "
            "CephCluster has no storageClassDeviceSets"
        )
    return int(device_sets[0].get("count") or 0)


def _osd_index_from_pvc(pvc) -> int:
    labels = pvc.metadata.labels or {}
    set_index = labels.get(ROOK_LABEL_SET_INDEX)
    if set_index is not None and str(set_index) != "":
        return int(set_index)

    pvc_id = labels.get(ROOK_LABEL_DEVICE_SET_PVC_ID, "")
    prefix = f"{CEPH_DEVICE_SET_NAME}-{CEPH_OSD_TEMPLATE_NAME}-"
    if pvc_id.startswith(prefix):
        return int(pvc_id[len(prefix) :])

    name = pvc.metadata.name or "<unknown>"
    raise ValueError(
        f"OSD PVC {name} missing {ROOK_LABEL_SET_INDEX} or "
        f"{ROOK_LABEL_DEVICE_SET_PVC_ID} label"
    )


def discover_ceph_device_pvcs(
    core_api, namespace: str, custom_api=None
) -> list[dict]:
    """Discover mon and OSD PVCs for project Ceph capture.

    Mon PVCs are matched by fixed Rook name (``rook-ceph-mon-a``). OSD PVCs are
    matched by ``ceph.rook.io/DeviceSet=osd-set`` labels, not random suffixes.

    ``custom_api`` is forwarded to ``_expected_osd_count`` — see its docstring.
    """
    expected_osds = _expected_osd_count(namespace, custom_api=custom_api)

    try:
        mon_pvc = core_api.read_namespaced_persistent_volume_claim(
            name=CEPH_MON_PVC_NAME,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            raise ValueError(
                f"expected mon PVC {CEPH_MON_PVC_NAME}, not found in {namespace}"
            ) from e
        raise

    label_selector = f"{ROOK_LABEL_DEVICE_SET}={CEPH_DEVICE_SET_NAME}"
    osd_pvcs = core_api.list_namespaced_persistent_volume_claim(
        namespace=namespace,
        label_selector=label_selector,
    ).items

    if len(osd_pvcs) != expected_osds:
        raise ValueError(
            f"expected {expected_osds} ceph-osd PVC(s), found {len(osd_pvcs)}"
        )

    devices: list[dict] = [
        {
            "name": CEPH_MON_PVC_NAME,
            "kind": "ceph-mon",
            "index": 0,
            "size_bytes": _pvc_size_bytes(mon_pvc),
        }
    ]

    seen_indices: set[int] = set()
    for pvc in osd_pvcs:
        index = _osd_index_from_pvc(pvc)
        if index in seen_indices:
            raise ValueError(f"duplicate ceph-osd index {index}")
        seen_indices.add(index)
        expected_pvc_id = f"{CEPH_DEVICE_SET_NAME}-{CEPH_OSD_TEMPLATE_NAME}-{index}"
        labels = pvc.metadata.labels or {}
        device_set = labels.get(ROOK_LABEL_DEVICE_SET)
        if device_set != CEPH_DEVICE_SET_NAME:
            raise ValueError(
                f"OSD PVC {pvc.metadata.name} has unexpected "
                f"{ROOK_LABEL_DEVICE_SET}={device_set!r}"
            )
        pvc_id = labels.get(ROOK_LABEL_DEVICE_SET_PVC_ID)
        if pvc_id and pvc_id != expected_pvc_id:
            raise ValueError(
                f"OSD PVC {pvc.metadata.name} has "
                f"{ROOK_LABEL_DEVICE_SET_PVC_ID}={pvc_id!r}, expected {expected_pvc_id!r}"
            )
        devices.append(
            {
                "name": pvc.metadata.name,
                "kind": "ceph-osd",
                "index": index,
                "size_bytes": _pvc_size_bytes(pvc),
            }
        )

    if seen_indices != set(range(expected_osds)):
        missing = sorted(set(range(expected_osds)) - seen_indices)
        raise ValueError(f"missing ceph-osd index(es): {missing}")

    devices.sort(key=lambda d: (0 if d["kind"] == "ceph-mon" else 1, d["index"]))
    return devices


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
        core_api.patch_namespaced_config_map(name=name, namespace=namespace, body=body)


def _create_or_patch_deployment(apps_api, namespace: str, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        apps_api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)


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
                rbac_api.create_namespaced_role_binding(namespace=namespace, body=body)
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
        (
            "RoleBinding",
            rbac_api.delete_namespaced_role_binding,
            ROOK_CMD_REPORTER_ROLE,
        ),
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
