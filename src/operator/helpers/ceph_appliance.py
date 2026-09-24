"""Troshka-owned Ceph appliance (pods) — no Rook CephCluster on the parent.

Mons and OSDs attach to the project Multus NAD. Nested CSI uses a single
monmap/public identity at labIp:3300 so many project Cephs can share OCPV
workers without hostNetwork port collisions.
"""

from __future__ import annotations

import logging

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from helpers.k8s import (
    GATEWAY_IMAGE,
    TOOLS_IMAGE,
    _APPS_API_VERSION,
    _IPV4_RE,
    _NET_ANNOTATION_KEY,
    _PREFIX_RE,
    owner_ref,
)
from helpers.rook_ceph import (
    CEPH_EXTERNAL_SECRET,
    DEFAULT_OSD_STORAGE_CLASS,
    EXPORT_JOB_NAME,
    discover_ceph_image,
    mon_storage_gi,
    normalize_ceph_counts,
    normalize_restore_spec,
    validate_lab_ip,
)

logger = logging.getLogger(__name__)

CEPH_SA = "troshka-ceph"
CEPH_POOL_NAME = "troshka-ceph-pool"
MON_NAME = "troshka-ceph-mon"
MGR_NAME = "troshka-ceph-mgr"
OSD_NAME_PREFIX = "troshka-ceph-osd"
MON_PVC_NAME = "troshka-ceph-mon"
MON_LABEL = "app=troshka-ceph-mon"
OSD_LABEL = "app=troshka-ceph-osd"
MON_APP = "troshka-ceph-mon"
OSD_APP = "troshka-ceph-osd"
IDENTITY_FSID = "troshka-ceph-fsid"
IDENTITY_ADMIN = "troshka-ceph-admin-keyring"
IDENTITY_MON = "troshka-ceph-mon-keyring"
IDENTITY_BOOTSTRAP_OSD = "troshka-ceph-bootstrap-osd-keyring"
CONF_CONFIGMAP = "troshka-ceph-conf"
# Must be an SCC the operator SA can patch (clusterrole resourceNames).
# troshka-privileged-jobs: privileged + NET_ADMIN/SYS_ADMIN for Multus + block.
APPLIANCE_SCC_NAME = "troshka-privileged-jobs"
APPLIANCE_SCC_SAS = (CEPH_SA,)


def osd_pvc_name(index: int) -> str:
    return f"{OSD_NAME_PREFIX}-{index}"


def osd_lab_ip(lab_ip: str, index: int) -> str:
    """Fallback Multus IP for OSD ``index`` when ``spec.osdIps`` is absent: the
    ``index``-th highest host in labIp's /24, skipping the gateway (``.1``),
    dnsmasq (``.2``), and the mon (``labIp``).

    The backend normally stamps collision-*checked* ``spec.osdIps``; this
    top-down fallback keeps a CR without them clear of the low/mid node band
    (unlike the old ``.20 + i`` offset, which collided with worker IPs). The
    operator has only the CR spec, so it cannot see VM NICs — top-down just
    minimizes the odds of a clash.
    """
    if not lab_ip or not _IPV4_RE.match(lab_ip):
        return ""
    base = ".".join(lab_ip.split(".")[:3])
    skip = {f"{base}.1", f"{base}.2", lab_ip}
    picked = 0
    for last in range(254, 2, -1):  # .254 down to .3
        candidate = f"{base}.{last}"
        if candidate in skip:
            continue
        if picked == index:
            return candidate
        picked += 1
    return ""


def osd_ip_for(spec: dict, index: int) -> str:
    """Resolve OSD ``index``'s Multus IP: the backend-allocated
    ``spec.osdIps[index]`` when present, else the legacy ``.20 + i`` offset."""
    osd_ips = spec.get("osdIps") or []
    if index < len(osd_ips):
        ip = str(osd_ips[index] or "").strip()
        if ip and _IPV4_RE.match(ip):
            return ip
    return osd_lab_ip(spec.get("labIp", ""), index)


def lab_cidr(lab_ip: str, prefix: str) -> str:
    if not lab_ip or not _IPV4_RE.match(lab_ip):
        return ""
    if not _PREFIX_RE.match(str(prefix)):
        prefix = "24"
    octets = lab_ip.split(".")
    # Assume /24 labs (Troshka default); for other prefixes use network .0.
    octets[3] = "0"
    return f"{'.'.join(octets)}/{prefix}"


def _security_context_privileged() -> dict:
    return {
        "privileged": True,
        "runAsUser": 0,
        "capabilities": {"add": ["NET_ADMIN", "SYS_ADMIN"]},
    }


def _multus_annotations(nad: str) -> dict:
    return {_NET_ANNOTATION_KEY: nad} if nad else {}


def _setup_multus_ip_cmd(ip: str, prefix: str) -> str:
    if not ip or not _IPV4_RE.match(ip) or not _PREFIX_RE.match(str(prefix)):
        return "true"
    return (
        f"ip addr add {ip}/{prefix} dev net1 2>/dev/null || true; "
        f"ip link set net1 up"
    )


def _ensure_lab_iface_snippet(ip_env: str = "LAB_IP") -> str:
    """Best-effort Multus IP bring-up for ceph containers that lack ``ip``.

    Primary setup is the gateway-image ``setup-net`` init; this is a no-op when
    ``ip`` is missing (rhceph images) so ``set -e`` scripts do not crash.
    """
    return (
        f"if command -v ip >/dev/null 2>&1; then "
        f'ip addr add "${{{ip_env}}}/${{PREFIX}}" dev net1 2>/dev/null || true; '
        f"ip link set net1 up 2>/dev/null || true; "
        f"fi"
    )


def build_mon_pvc(ceph_cr: dict) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    size_gi = mon_storage_gi(spec)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": MON_PVC_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-mon"},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": f"{size_gi}Gi"}},
        },
    }


def build_osd_pvcs(ceph_cr: dict) -> list[dict]:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    osd_count, _, per_osd_gi = normalize_ceph_counts(spec)
    storage_class = spec.get("osdStorageClass") or DEFAULT_OSD_STORAGE_CLASS
    restore = normalize_restore_spec(spec)
    if restore["enabled"]:
        # Restore path pre-creates PVCs; skip minting empty claims.
        return []
    pvcs = []
    for i in range(osd_count):
        pvcs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": osd_pvc_name(i),
                    "namespace": namespace,
                    "ownerReferences": [owner_ref(ceph_cr)],
                    "labels": {
                        "app": "troshka-ceph",
                        "troshka-role": "ceph-osd",
                        "troshka-ceph-osd-index": str(i),
                    },
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


def build_conf_configmap(ceph_cr: dict, fsid: str) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    prefix = str(spec.get("labPrefixLength") or 24)
    cidr = lab_cidr(lab_ip, prefix)
    conf = (
        "[global]\n"
        f"fsid = {fsid}\n"
        "mon initial members = a\n"
        f"mon host = [v2:{lab_ip}:3300/0]\n"
        f"public_network = {cidr}\n"
        f"cluster_network = {cidr}\n"
        "auth cluster required = cephx\n"
        "auth service required = cephx\n"
        "auth client required = cephx\n"
        "osd pool default size = 1\n"
        "osd pool default min size = 1\n"
        "\n"
        "[mon.a]\n"
        f"public addr = {lab_ip}:3300\n"
    )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONF_CONFIGMAP,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph"},
        },
        "data": {"ceph.conf": conf},
    }


def build_placeholder_identity_secrets(ceph_cr: dict, fsid: str) -> list[dict]:
    """Empty keyring secrets created before mon bootstrap fills them."""
    namespace = ceph_cr["metadata"]["namespace"]
    refs = [owner_ref(ceph_cr)]
    labels = {"app": "troshka-ceph", "troshka-role": "ceph-identity"}
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": IDENTITY_FSID,
                "namespace": namespace,
                "ownerReferences": refs,
                "labels": labels,
            },
            "stringData": {"fsid": fsid},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": IDENTITY_ADMIN,
                "namespace": namespace,
                "ownerReferences": refs,
                "labels": labels,
            },
            "stringData": {"keyring": ""},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": IDENTITY_MON,
                "namespace": namespace,
                "ownerReferences": refs,
                "labels": labels,
            },
            "stringData": {"keyring": ""},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": IDENTITY_BOOTSTRAP_OSD,
                "namespace": namespace,
                "ownerReferences": refs,
                "labels": labels,
            },
            "stringData": {"keyring": ""},
        },
    ]


def build_external_secret(ceph_cr: dict, fsid: str = "") -> dict:
    """ODF-oriented external details stub; export Job fills keyrings."""
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    mon_endpoint = f"{lab_ip}:3300" if lab_ip else ""
    config_entry = {
        "cluster_id": fsid or "troshka-ceph",
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
            "config": __import__("json").dumps([config_entry]),
        },
    }


def _mon_bootstrap_script() -> str:
    return rf"""
set -euo pipefail
LAB_IP="${{LAB_IP:?}}"
FSID="${{FSID:?}}"
PREFIX="${{LAB_PREFIX:-24}}"
MON_DIR=/var/lib/ceph/mon/ceph-a
CONF=/etc/ceph/ceph.conf

{_ensure_lab_iface_snippet("LAB_IP")}

mkdir -p /etc/ceph /var/lib/ceph/mon /var/lib/ceph/bootstrap-osd /var/lib/ceph/mgr

if [ -f "${{MON_DIR}}/kv_backend" ] || [ -f "${{MON_DIR}}/keyring" ]; then
  echo "mon data present — skip mkfs"
  exit 0
fi

# Prefer restore/pre-seeded keyrings when non-empty.
if [ -s /seed/mon.keyring ] && [ -s /seed/admin.keyring ]; then
  cp /seed/mon.keyring /tmp/ceph.mon.keyring
  cp /seed/admin.keyring /etc/ceph/ceph.client.admin.keyring
  if [ -s /seed/bootstrap-osd.keyring ]; then
    cp /seed/bootstrap-osd.keyring /var/lib/ceph/bootstrap-osd/ceph.keyring
  fi
else
  ceph-authtool --create-keyring /tmp/ceph.mon.keyring --gen-key -n mon. \
    --cap mon 'allow *'
  ceph-authtool --create-keyring /etc/ceph/ceph.client.admin.keyring --gen-key \
    -n client.admin --cap mon 'allow *' --cap osd 'allow *' \
    --cap mds 'allow *' --cap mgr 'allow *'
  ceph-authtool /tmp/ceph.mon.keyring --import-keyring \
    /etc/ceph/ceph.client.admin.keyring
  ceph-authtool --create-keyring /var/lib/ceph/bootstrap-osd/ceph.keyring \
    --gen-key -n client.bootstrap-osd \
    --cap mon 'profile bootstrap-osd' --cap mgr 'allow r'
fi

monmaptool --create --addv a "[v2:${{LAB_IP}}:3300/0]" --fsid "${{FSID}}" /tmp/monmap
mkdir -p "${{MON_DIR}}"
ceph-mon --mkfs -i a --monmap /tmp/monmap --keyring /tmp/ceph.mon.keyring

# Persist keyrings into seed secrets via shared emptyDir exported by sidecar later.
cp /tmp/ceph.mon.keyring /shared/mon.keyring
cp /etc/ceph/ceph.client.admin.keyring /shared/admin.keyring
cp /var/lib/ceph/bootstrap-osd/ceph.keyring /shared/bootstrap-osd.keyring
echo "${{FSID}}" > /shared/fsid
echo "mon mkfs complete"
"""


def _mon_run_script() -> str:
    return rf"""
set -euo pipefail
LAB_IP="${{LAB_IP:?}}"
PREFIX="${{LAB_PREFIX:-24}}"
{_ensure_lab_iface_snippet("LAB_IP")}
mkdir -p /var/lib/ceph/bootstrap-osd
# Prefer freshly generated shared keyrings, else seeded secrets (restarts/restore).
if [ -s /shared/admin.keyring ]; then
  cp /shared/admin.keyring /etc/ceph/ceph.client.admin.keyring
elif [ -s /seed/admin.keyring ]; then
  cp /seed/admin.keyring /etc/ceph/ceph.client.admin.keyring
fi
if [ -s /shared/bootstrap-osd.keyring ]; then
  cp /shared/bootstrap-osd.keyring /var/lib/ceph/bootstrap-osd/ceph.keyring
elif [ -s /seed/bootstrap-osd.keyring ]; then
  cp /seed/bootstrap-osd.keyring /var/lib/ceph/bootstrap-osd/ceph.keyring
fi
exec ceph-mon -f -i a --public-addr "${{LAB_IP}}:3300"
"""


def _mgr_run_script() -> str:
    return rf"""
set -euo pipefail
LAB_IP="${{LAB_IP:?}}"
PREFIX="${{LAB_PREFIX:-24}}"
{_ensure_lab_iface_snippet("LAB_IP")}
if [ -s /shared/admin.keyring ]; then
  cp /shared/admin.keyring /etc/ceph/ceph.client.admin.keyring
elif [ -s /seed/admin.keyring ]; then
  cp /seed/admin.keyring /etc/ceph/ceph.client.admin.keyring
fi
for i in $(seq 1 60); do
  if ceph --conf /etc/ceph/ceph.conf -s >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
mkdir -p /var/lib/ceph/mgr/ceph-a
if [ ! -f /var/lib/ceph/mgr/ceph-a/keyring ]; then
  ceph --conf /etc/ceph/ceph.conf auth get-or-create mgr.a \
    mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
    -o /var/lib/ceph/mgr/ceph-a/keyring
fi
exec ceph-mgr -f -i a
"""


def _keyring_sync_script() -> str:
    """Sidecar: push generated keyrings into K8s secrets, then stay alive."""
    return r"""
set -euo pipefail
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
# Wait for bootstrap to write shared keyrings.
for i in $(seq 1 90); do
  if [ -s /shared/admin.keyring ] && [ -s /shared/mon.keyring ]; then
    break
  fi
  # Restore path: seed already populated secrets — nothing to sync.
  if [ -s /seed/admin.keyring ]; then
    echo "seed keyrings present — skip sync"
    sleep infinity
  fi
  sleep 2
done
if [ ! -s /shared/admin.keyring ]; then
  echo "no keyrings to sync"
  sleep infinity
fi
kubectl -n "$NS" create secret generic troshka-ceph-admin-keyring \
  --from-file=keyring=/shared/admin.keyring --dry-run=client -o yaml \
  | kubectl -n "$NS" apply -f -
kubectl -n "$NS" create secret generic troshka-ceph-mon-keyring \
  --from-file=keyring=/shared/mon.keyring --dry-run=client -o yaml \
  | kubectl -n "$NS" apply -f -
if [ -s /shared/bootstrap-osd.keyring ]; then
  kubectl -n "$NS" create secret generic troshka-ceph-bootstrap-osd-keyring \
    --from-file=keyring=/shared/bootstrap-osd.keyring --dry-run=client -o yaml \
    | kubectl -n "$NS" apply -f -
fi
echo "keyrings synced"
# Stay idle so the pod keeps running (sidecar).
sleep infinity
"""


def build_mon_deployment(ceph_cr: dict, ceph_image: str) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    prefix = str(spec.get("labPrefixLength") or 24)
    nad = spec.get("networkNad", "")
    labels = {"app": MON_APP, "troshka-role": "ceph-mon"}
    env = [
        {"name": "LAB_IP", "value": lab_ip},
        {"name": "LAB_PREFIX", "value": prefix},
        {
            "name": "FSID",
            "valueFrom": {
                "secretKeyRef": {"name": IDENTITY_FSID, "key": "fsid"},
            },
        },
    ]
    volume_mounts_conf = [
        {
            "name": "ceph-conf",
            "mountPath": "/etc/ceph/ceph.conf",
            "subPath": "ceph.conf",
        },
        {"name": "mon-data", "mountPath": "/var/lib/ceph/mon"},
        {"name": "shared", "mountPath": "/shared"},
        {"name": "seed-mon", "mountPath": "/seed/mon.keyring", "subPath": "keyring"},
        {
            "name": "seed-admin",
            "mountPath": "/seed/admin.keyring",
            "subPath": "keyring",
        },
        {
            "name": "seed-boot-osd",
            "mountPath": "/seed/bootstrap-osd.keyring",
            "subPath": "keyring",
        },
    ]
    return {
        "apiVersion": _APPS_API_VERSION,
        "kind": "Deployment",
        "metadata": {
            "name": MON_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": _multus_annotations(nad),
                },
                "spec": {
                    "serviceAccountName": CEPH_SA,
                    "initContainers": [
                        {
                            "name": "setup-net",
                            "image": GATEWAY_IMAGE,
                            "command": [
                                "sh",
                                "-c",
                                _setup_multus_ip_cmd(lab_ip, prefix),
                            ],
                            "securityContext": _security_context_privileged(),
                        },
                        {
                            "name": "bootstrap",
                            "image": ceph_image,
                            "command": ["bash", "-c", _mon_bootstrap_script()],
                            "env": env,
                            "volumeMounts": volume_mounts_conf,
                            "securityContext": _security_context_privileged(),
                        },
                    ],
                    "containers": [
                        {
                            "name": "mon",
                            "image": ceph_image,
                            "command": ["bash", "-c", _mon_run_script()],
                            "env": env,
                            "volumeMounts": volume_mounts_conf,
                            "securityContext": _security_context_privileged(),
                            "ports": [
                                {"name": "msgr2", "containerPort": 3300},
                                {"name": "msgr1", "containerPort": 6789},
                            ],
                        },
                        {
                            "name": "mgr",
                            "image": ceph_image,
                            "command": ["bash", "-c", _mgr_run_script()],
                            "env": env,
                            "volumeMounts": [
                                {
                                    "name": "ceph-conf",
                                    "mountPath": "/etc/ceph/ceph.conf",
                                    "subPath": "ceph.conf",
                                },
                                {"name": "shared", "mountPath": "/shared"},
                                {"name": "mgr-data", "mountPath": "/var/lib/ceph/mgr"},
                                {
                                    "name": "seed-admin",
                                    "mountPath": "/seed/admin.keyring",
                                    "subPath": "keyring",
                                },
                            ],
                            "securityContext": _security_context_privileged(),
                        },
                        {
                            "name": "keyring-sync",
                            "image": TOOLS_IMAGE,
                            "command": ["sh", "-c", _keyring_sync_script()],
                            "volumeMounts": [
                                {"name": "shared", "mountPath": "/shared"},
                                {
                                    "name": "seed-admin",
                                    "mountPath": "/seed/admin.keyring",
                                    "subPath": "keyring",
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {
                            "name": "ceph-conf",
                            "configMap": {"name": CONF_CONFIGMAP},
                        },
                        {
                            "name": "mon-data",
                            "persistentVolumeClaim": {"claimName": MON_PVC_NAME},
                        },
                        {"name": "shared", "emptyDir": {}},
                        {"name": "mgr-data", "emptyDir": {}},
                        {
                            "name": "seed-mon",
                            "secret": {
                                "secretName": IDENTITY_MON,
                                "optional": True,
                            },
                        },
                        {
                            "name": "seed-admin",
                            "secret": {
                                "secretName": IDENTITY_ADMIN,
                                "optional": True,
                            },
                        },
                        {
                            "name": "seed-boot-osd",
                            "secret": {
                                "secretName": IDENTITY_BOOTSTRAP_OSD,
                                "optional": True,
                            },
                        },
                    ],
                },
            },
        },
    }


def _osd_bootstrap_script() -> str:
    """Prepare an OSD without ceph-volume (no host udev in CSI RBD pods)."""
    return rf"""
set -euo pipefail
OSD_IP="${{OSD_IP:?}}"
OSD_ID="${{OSD_INDEX:?}}"
LAB_IP="${{LAB_IP:?}}"
PREFIX="${{LAB_PREFIX:-24}}"
BLOCK=/dev/osd-block
OSD_DIR=/var/lib/ceph/osd/ceph-${{OSD_ID}}

{_ensure_lab_iface_snippet("OSD_IP")}

mkdir -p /etc/ceph /var/lib/ceph/osd /var/lib/ceph/bootstrap-osd

# Wait for mon keyring sync. Directory mounts (no subPath) refresh in place.
for i in $(seq 1 120); do
  if [ -s /seed/admin/keyring ]; then
    cp /seed/admin/keyring /etc/ceph/ceph.client.admin.keyring
    break
  fi
  sleep 2
done
if [ ! -s /etc/ceph/ceph.client.admin.keyring ]; then
  echo "admin keyring not ready" >&2
  exit 1
fi
if [ -s /seed/bootstrap-osd/keyring ]; then
  cp /seed/bootstrap-osd/keyring /var/lib/ceph/bootstrap-osd/ceph.keyring
fi

for i in $(seq 1 90); do
  if ceph --conf /etc/ceph/ceph.conf -s >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! ceph --conf /etc/ceph/ceph.conf -s >/dev/null 2>&1; then
  echo "mon not reachable" >&2
  exit 1
fi

mkdir -p "${{OSD_DIR}}"

# Pattern restore / pod restart: BlueStore already lives on the block PVC
# (osd-data is emptyDir, so whoami/keyring alone are not durable).
if ceph-bluestore-tool show-label --dev "${{BLOCK}}" >/tmp/bs-label.json 2>/dev/null; then
  echo "bluestore present on ${{BLOCK}} — adopt osd.${{OSD_ID}}"
  UUID=$(sed -n 's/.*"osd_uuid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /tmp/bs-label.json | head -1)
  if [ -z "${{UUID}}" ]; then
    echo "could not parse osd_uuid from bluestore label" >&2
    exit 1
  fi
  # prime-osd-dir recreates meta files; note Ceph's "fsid" file holds the
  # OSD uuid (not the cluster fsid) — writing cluster fsid here breaks mount.
  ceph-bluestore-tool prime-osd-dir --dev "${{BLOCK}}" --path "${{OSD_DIR}}"
  ln -sfn "${{BLOCK}}" "${{OSD_DIR}}/block"
  echo "${{UUID}}" > "${{OSD_DIR}}/osd_uuid"
  # Defense in depth: prime should write fsid=OSD uuid; force it if not.
  echo "${{UUID}}" > "${{OSD_DIR}}/fsid"
  if [ ! -s "${{OSD_DIR}}/keyring" ]; then
    ceph auth get "osd.${{OSD_ID}}" -o "${{OSD_DIR}}/keyring" \
      || ceph auth get-or-create "osd.${{OSD_ID}}" \
           mon 'allow profile osd' \
           mgr 'allow profile osd' \
           osd 'allow *' \
           -o "${{OSD_DIR}}/keyring"
  fi
  exit 0
fi

if [ -f "${{OSD_DIR}}/whoami" ] || [ -f "${{OSD_DIR}}/keyring" ]; then
  echo "osd data present — skip prepare"
  exit 0
fi

UUID=$(uuidgen)
# Claim a stable OSD id matching the Deployment index.
ceph osd new "${{UUID}}" "${{OSD_ID}}" 2>/dev/null || \
  ceph osd create "${{UUID}}" "${{OSD_ID}}"

ceph auth get-or-create "osd.${{OSD_ID}}" \
  mon 'allow profile osd' \
  mgr 'allow profile osd' \
  osd 'allow *' \
  -o "${{OSD_DIR}}/keyring"

echo "${{UUID}}" > "${{OSD_DIR}}/osd_uuid"

# BlueStore on the raw block PVC — avoid ceph-volume (needs host udev).
ceph-osd -i "${{OSD_ID}}" --mkfs --osd-uuid "${{UUID}}" \
  --osd_objectstore=bluestore \
  --bluestore_block_path="${{BLOCK}}"
echo "osd prepare complete"
"""


def _osd_run_script() -> str:
    return rf"""
set -euo pipefail
OSD_IP="${{OSD_IP:?}}"
OSD_ID="${{OSD_INDEX:?}}"
PREFIX="${{LAB_PREFIX:-24}}"
BLOCK=/dev/osd-block
OSD_DIR=/var/lib/ceph/osd/ceph-${{OSD_ID}}
{_ensure_lab_iface_snippet("OSD_IP")}
if [ ! -f "${{OSD_DIR}}/keyring" ]; then
  echo "osd ${{OSD_ID}} not prepared" >&2
  exit 1
fi
if [ ! -s "${{OSD_DIR}}/osd_uuid" ]; then
  # Restore/restart safety: recover UUID from the BlueStore label.
  ceph-bluestore-tool show-label --dev "${{BLOCK}}" >/tmp/bs-label.json 2>/dev/null || true
  sed -n 's/.*"osd_uuid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /tmp/bs-label.json 2>/dev/null | head -1 > "${{OSD_DIR}}/osd_uuid" || true
fi
UUID=$(cat "${{OSD_DIR}}/osd_uuid")
if [ -z "${{UUID}}" ]; then
  echo "osd ${{OSD_ID}} missing osd_uuid" >&2
  exit 1
fi
exec ceph-osd -f -i "${{OSD_ID}}" --osd-uuid "${{UUID}}" \
  --osd_objectstore=bluestore \
  --bluestore_block_path="${{BLOCK}}"
"""


def build_osd_deployment(ceph_cr: dict, ceph_image: str, index: int) -> dict:
    spec = ceph_cr["spec"]
    namespace = ceph_cr["metadata"]["namespace"]
    lab_ip = spec.get("labIp", "")
    prefix = str(spec.get("labPrefixLength") or 24)
    nad = spec.get("networkNad", "")
    osd_ip = osd_ip_for(spec, index)
    name = osd_pvc_name(index)
    labels = {
        "app": OSD_APP,
        "troshka-role": "ceph-osd",
        "troshka-ceph-osd-index": str(index),
    }
    env = [
        {"name": "LAB_IP", "value": lab_ip},
        {"name": "OSD_IP", "value": osd_ip},
        {"name": "OSD_INDEX", "value": str(index)},
        {"name": "LAB_PREFIX", "value": prefix},
    ]
    return {
        "apiVersion": _APPS_API_VERSION,
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": labels,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": _multus_annotations(nad),
                },
                "spec": {
                    "serviceAccountName": CEPH_SA,
                    "initContainers": [
                        {
                            "name": "setup-net",
                            "image": GATEWAY_IMAGE,
                            "command": [
                                "sh",
                                "-c",
                                _setup_multus_ip_cmd(osd_ip, prefix),
                            ],
                            "securityContext": _security_context_privileged(),
                        },
                        {
                            "name": "prepare",
                            "image": ceph_image,
                            "command": ["bash", "-c", _osd_bootstrap_script()],
                            "env": env,
                            "volumeMounts": [
                                {
                                    "name": "ceph-conf",
                                    "mountPath": "/etc/ceph/ceph.conf",
                                    "subPath": "ceph.conf",
                                },
                                {
                                    "name": "osd-data",
                                    "mountPath": "/var/lib/ceph/osd",
                                },
                                # Directory mounts (no subPath) so keyring-sync
                                # updates are visible without restarting the pod.
                                {
                                    "name": "seed-admin",
                                    "mountPath": "/seed/admin",
                                },
                                {
                                    "name": "seed-boot-osd",
                                    "mountPath": "/seed/bootstrap-osd",
                                },
                            ],
                            "volumeDevices": [
                                {
                                    "name": "osd-block",
                                    "devicePath": "/dev/osd-block",
                                },
                            ],
                            "securityContext": _security_context_privileged(),
                        },
                    ],
                    "containers": [
                        {
                            "name": "osd",
                            "image": ceph_image,
                            "command": ["bash", "-c", _osd_run_script()],
                            "env": env,
                            "volumeMounts": [
                                {
                                    "name": "ceph-conf",
                                    "mountPath": "/etc/ceph/ceph.conf",
                                    "subPath": "ceph.conf",
                                },
                                {
                                    "name": "osd-data",
                                    "mountPath": "/var/lib/ceph/osd",
                                },
                            ],
                            "volumeDevices": [
                                {
                                    "name": "osd-block",
                                    "devicePath": "/dev/osd-block",
                                },
                            ],
                            "securityContext": _security_context_privileged(),
                        }
                    ],
                    "volumes": [
                        {
                            "name": "ceph-conf",
                            "configMap": {"name": CONF_CONFIGMAP},
                        },
                        {
                            "name": "osd-block",
                            "persistentVolumeClaim": {"claimName": name},
                        },
                        {"name": "osd-data", "emptyDir": {}},
                        {
                            "name": "seed-admin",
                            "secret": {
                                "secretName": IDENTITY_ADMIN,
                                "optional": True,
                            },
                        },
                        {
                            "name": "seed-boot-osd",
                            "secret": {
                                "secretName": IDENTITY_BOOTSTRAP_OSD,
                                "optional": True,
                            },
                        },
                    ],
                },
            },
        },
    }


def build_ceph_rbac(ceph_cr: dict) -> tuple[dict, dict]:
    namespace = ceph_cr["metadata"]["namespace"]
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": CEPH_SA,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets", "configmaps"],
                "verbs": ["get", "list", "patch", "create", "update"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods", "pods/exec"],
                "verbs": ["get", "list", "create"],
            },
        ],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": CEPH_SA,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": CEPH_SA,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": CEPH_SA,
                "namespace": namespace,
            }
        ],
    }
    return role, binding


def _export_job_script() -> str:
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
import time

ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
secret_name = "troshka-ceph-external"  # pragma: allowlist secret
pool_name = "troshka-ceph-pool"


def kubectl(args):
    return subprocess.check_output(
        ["kubectl", "-n", ns, *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def kubectl_json(args):
    return json.loads(kubectl(args))


def secret_field(name, field):
    secret = kubectl_json(["get", "secret", name, "-o", "json"])
    raw = (secret.get("data") or {}).get(field)
    if raw:
        return base64.b64decode(raw).decode()
    return (secret.get("stringData") or {}).get(field, "")


def parse_keyring(keyring: str) -> str:
    match = re.search(r"key\s*=\s*(\S+)", keyring)
    if not match:
        raise RuntimeError("no key in admin keyring")
    return match.group(1)


lab_mon_host = secret_field(secret_name, "mon-host")
lab_ip = lab_mon_host.split(":", 1)[0] if lab_mon_host else ""
if not lab_ip:
    raise RuntimeError("mon-host missing labIp")

# Wait for admin keyring sync from mon pod.
admin_keyring = ""
for _ in range(60):
    admin_keyring = secret_field("troshka-ceph-admin-keyring", "keyring")
    if admin_keyring.strip():
        break
    time.sleep(2)
if not admin_keyring.strip():
    raise RuntimeError("admin keyring not ready")

admin_key = parse_keyring(admin_keyring)
fsid = secret_field("troshka-ceph-fsid", "fsid")
if not fsid:
    raise RuntimeError("fsid secret empty")

mon_pods = kubectl_json(["get", "pods", "-l", "app=troshka-ceph-mon", "-o", "json"])
mon_name = ""
for pod in mon_pods.get("items") or []:
    if (pod.get("status") or {}).get("phase") == "Running":
        mon_name = pod["metadata"]["name"]
        break
if not mon_name:
    raise RuntimeError("no running troshka-ceph-mon pod")


def ceph_in_mon(args: str) -> str:
    script = (
        "ceph --conf /etc/ceph/ceph.conf "
        f"-n client.admin {args}"
    )
    return subprocess.check_output(
        ["kubectl", "-n", ns, "exec", mon_name, "-c", "mon", "--", "bash", "-c", script],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=120,
    )


# Ensure pool exists with intended size.
osd_count = 0
try:
    osd_count = int(ceph_in_mon("osd ls | wc -l").strip() or "0")
except Exception as exc:
    print(f"osd ls warning: {exc}", file=sys.stderr)

repl = max(1, min(osd_count or 1, 3))
try:
    ceph_in_mon(f"osd pool create {pool_name} 32 32")
except Exception as exc:
    print(f"pool create: {exc}", file=sys.stderr)
try:
    ceph_in_mon(f"osd pool set {pool_name} size {repl}")
    ceph_in_mon(f"osd pool set {pool_name} min_size 1")
    ceph_in_mon(f"osd pool application enable {pool_name} rbd")
except Exception as exc:
    print(f"pool tune: {exc}", file=sys.stderr)

# CSI users — provisioner and node must be distinct Ceph entities.
# Reusing one key under both userIDs causes rados Permission denied on PVC create.
csi_caps = "mon 'profile rbd' osd 'profile rbd' mgr 'profile rbd'"
csi_node_key = admin_key
csi_prov_key = admin_key
try:
    out = ceph_in_mon(f"auth get-or-create client.csi-rbd-node {csi_caps}")
    csi_node_key = parse_keyring(out)
except Exception as exc:
    print(f"csi-rbd-node key: {exc}", file=sys.stderr)
    try:
        ceph_in_mon(f"auth caps client.csi-rbd-node {csi_caps}")
        csi_node_key = parse_keyring(ceph_in_mon("auth get client.csi-rbd-node"))
    except Exception as exc2:
        print(f"csi-rbd-node caps: {exc2}", file=sys.stderr)
try:
    out = ceph_in_mon(f"auth get-or-create client.csi-rbd-provisioner {csi_caps}")
    csi_prov_key = parse_keyring(out)
except Exception as exc:
    print(f"csi-rbd-provisioner key: {exc}", file=sys.stderr)
    try:
        ceph_in_mon(f"auth caps client.csi-rbd-provisioner {csi_caps}")
        csi_prov_key = parse_keyring(ceph_in_mon("auth get client.csi-rbd-provisioner"))
    except Exception as exc2:
        print(f"csi-rbd-provisioner caps: {exc2}", file=sys.stderr)

# Enable mgr prometheus for ODF monitoring-endpoint (port 9283).
try:
    ceph_in_mon("mgr module enable prometheus")
except Exception as exc:
    print(f"prometheus module: {exc}", file=sys.stderr)

# Lab appliance: silence single-replica .mgr warn and clear bootstrap osd flags
# so external StorageCluster can reach Ready (HEALTH_OK).
try:
    ceph_in_mon("config set global mon_warn_on_pool_no_redundancy false")
except Exception as exc:
    print(f"mon_warn_on_pool_no_redundancy: {exc}", file=sys.stderr)
for flag in ("noout", "nobackfill", "norecover", "noscrub", "nodeep-scrub"):
    try:
        ceph_in_mon(f"osd unset {flag}")
    except Exception as exc:
        print(f"osd unset {flag}: {exc}", file=sys.stderr)
try:
    if (osd_count or 0) >= 3:
        ceph_in_mon("osd pool set .mgr size 3")
        ceph_in_mon("osd pool set .mgr min_size 1")
except Exception as exc:
    print(f".mgr pool size: {exc}", file=sys.stderr)

mon_host = f"{lab_ip}:3300"
# ODF external mode expects rook-style {name,kind,data} resources (not
# flat name/value). Missing monitoring-endpoint or wrong shape leaves
# StorageCluster stuck / Error.
resources = [
    {
        "name": "rook-ceph-mon-endpoints",
        "kind": "ConfigMap",
        "data": {"data": f"a={mon_host}", "maxMonId": "0", "mapping": "{}"},
    },
    {
        "name": "rook-ceph-mon",
        "kind": "Secret",
        "data": {
            "admin-secret": admin_key,
            "fsid": fsid,
            "mon-secret": admin_key,
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
        "data": {
            "MonitoringEndpoint": lab_ip,
            "MonitoringPort": "9283",
        },
    },
]
details = resources
patch = {
    "stringData": {
        "fsid": fsid,
        "mon-host": mon_host,
        "admin-key": admin_key,
        "external_cluster_details": json.dumps(details),
        "config": json.dumps(
            [
                {
                    "cluster_id": fsid,
                    "mon_host": mon_host,
                    "mon_host_override": mon_host,
                }
            ]
        ),
    }
}
subprocess.check_call(
    [
        "kubectl",
        "-n",
        ns,
        "patch",
        "secret",
        secret_name,
        "--type",
        "merge",
        "-p",
        json.dumps(patch),
    ]
)
print(f"export job complete mon-host={mon_host}")
PY
"""


def build_export_job(ceph_cr: dict) -> dict:
    namespace = ceph_cr["metadata"]["namespace"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": EXPORT_JOB_NAME,
            "namespace": namespace,
            "ownerReferences": [owner_ref(ceph_cr)],
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-export"},
        },
        "spec": {
            "backoffLimit": 6,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app": "troshka-ceph-export"}},
                "spec": {
                    "serviceAccountName": CEPH_SA,
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "export",
                            "image": TOOLS_IMAGE,
                            "command": ["sh", "-c", _export_job_script()],
                        }
                    ],
                },
            },
        },
    }


def ensure_ceph_export_job(batch_api, ceph_cr: dict, namespace: str) -> None:
    try:
        batch_api.read_namespaced_job(name=EXPORT_JOB_NAME, namespace=namespace)
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = build_export_job(ceph_cr)
    batch_api.create_namespaced_job(namespace=namespace, body=body)


def ceph_external_details_exported(core_api, namespace: str) -> bool:
    try:
        secret = core_api.read_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET, namespace=namespace
        )
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    data = secret.data or {}
    return bool(data.get("external_cluster_details"))


def nested_mon_host_from_secret(core_api, namespace: str) -> str:
    try:
        secret = core_api.read_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET, namespace=namespace
        )
    except ApiException as e:
        if e.status == 404:
            return ""
        raise
    data = secret.data or {}
    raw = data.get("mon-host")
    if not raw:
        return ""
    import base64

    return base64.b64decode(raw).decode()


def appliance_is_ready(apps_api, namespace: str, osd_count: int) -> bool:
    """True when mon Deployment and all OSD Deployments have ready replicas."""
    try:
        mon = apps_api.read_namespaced_deployment(name=MON_NAME, namespace=namespace)
        if not (mon.status and mon.status.ready_replicas):
            return False
    except ApiException:
        return False
    for i in range(osd_count):
        name = osd_pvc_name(i)
        try:
            dep = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException:
            return False
        if not (dep.status and dep.status.ready_replicas):
            return False
    return True


def new_fsid() -> str:
    import uuid

    return str(uuid.uuid4())


def read_or_create_fsid(core_api, namespace: str, ceph_cr: dict) -> str:
    try:
        secret = core_api.read_namespaced_secret(
            name=IDENTITY_FSID, namespace=namespace
        )
        import base64

        raw = (secret.data or {}).get("fsid")
        if raw:
            return base64.b64decode(raw).decode()
    except ApiException as e:
        if e.status != 404:
            raise
    return new_fsid()


def apply_deployment(apps_api, namespace: str, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        apps_api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)


def apply_pvc(core_api, namespace: str, body: dict) -> None:
    try:
        core_api.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=body
        )
    except ApiException as e:
        if e.status != 409:
            raise


def apply_configmap(core_api, namespace: str, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        core_api.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        core_api.patch_namespaced_config_map(name=name, namespace=namespace, body=body)


def apply_secret_if_absent(core_api, namespace: str, body: dict) -> None:
    try:
        core_api.create_namespaced_secret(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise


def delete_appliance(
    apps_api, core_api, batch_api, namespace: str, osd_count: int
) -> None:
    for name in [MON_NAME, *[osd_pvc_name(i) for i in range(osd_count)]]:
        try:
            apps_api.delete_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("delete deployment %s: %s", name, e)
    try:
        batch_api.delete_namespaced_job(
            name=EXPORT_JOB_NAME,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("delete export job: %s", e)

    for name in (
        MON_PVC_NAME,
        *[osd_pvc_name(i) for i in range(max(osd_count, 6))],
        CEPH_EXTERNAL_SECRET,
        IDENTITY_FSID,
        IDENTITY_ADMIN,
        IDENTITY_MON,
        IDENTITY_BOOTSTRAP_OSD,
        CONF_CONFIGMAP,
    ):
        try:
            if name == CONF_CONFIGMAP:
                core_api.delete_namespaced_config_map(name=name, namespace=namespace)
            elif name.endswith("-keyring") or name in (
                CEPH_EXTERNAL_SECRET,
                IDENTITY_FSID,
            ):
                core_api.delete_namespaced_secret(name=name, namespace=namespace)
            else:
                core_api.delete_namespaced_persistent_volume_claim(
                    name=name, namespace=namespace
                )
        except ApiException as e:
            if e.status != 404:
                logger.warning("delete %s: %s", name, e)


def service_account_ref(namespace: str, sa_name: str = CEPH_SA) -> str:
    return f"system:serviceaccount:{namespace}:{sa_name}"
