"""Snapshot-based file extraction from a VM disk (provider-agnostic core).

Pulls a single file out of a (possibly running) VM by reading a point-in-time
snapshot of its root disk OFFLINE with guestfish — never touching the live disk.
The guestfish script + path logic here are shared: the KubeVirt operator runs it
in a privileged pod against a VolumeSnapshot clone; the troshkad agent runs the
same guestfish invocation on the host against a libvirt snapshot. This keeps
``pull_file`` behavior identical across providers.

RHCOS/ostree note: an OS path like ``/etc/kubernetes/...`` does NOT live at the
physical-disk root — ``/etc`` and ``/usr`` are under the booted deployment
(``/ostree/deploy/<stateroot>/deploy/<hash>/``) while ``/var`` is shared per
stateroot (``/ostree/deploy/<stateroot>/var/``). :func:`candidate_paths` returns
both the direct path (normal VMs / inspected root) and the ostree-translated
glob, and the reader tries each.
"""

import base64

from helpers.k8s import TOOLS_IMAGE


def candidate_paths(guest_path: str) -> list[str]:
    """In-disk paths to try for a logical guest path, most-specific-first.

    ``[0]`` is the path as-is (a normal VM's root, or an inspected mountpoint).
    ``[1]`` is the ostree translation: ``/var`` is shared per stateroot; every
    other tree (``/etc``, ``/usr``, ...) lives under the deployment checksum.
    """
    gp = (guest_path or "").strip()
    if not gp.startswith("/"):
        gp = "/" + gp
    if gp == "/var" or gp.startswith("/var/"):
        return [gp, "/ostree/deploy/*" + gp]
    return [gp, "/ostree/deploy/*/deploy/*" + gp]


def parse_filepull_output(log: str) -> bytes | None:
    """Decode the base64 payload the pull pod prints between B64 markers.

    Returns ``None`` if the markers are absent or the payload is empty/invalid —
    the caller treats that as "file not found / pull failed"."""
    if not log or "B64_BEGIN" not in log or "B64_END" not in log:
        return None
    seg = log.split("B64_BEGIN", 1)[1].split("B64_END", 1)[0].strip()
    if not seg:
        return None
    try:
        return base64.b64decode(seg)
    except Exception:
        return None


def build_filepull_script(guest_path: str) -> str:
    """Bash that mounts each Linux fs on ``/disk/disk.img`` read-only and tries
    every :func:`candidate_paths` glob until one downloads, then base64s it.

    ``glob download`` runs zero times on no match (not an error), so chaining the
    candidates in one guestfish invocation per partition costs one appliance boot
    per partition regardless of which candidate hits."""
    globs = " ".join(
        f': glob download "{c}" /tmp/out' for c in candidate_paths(guest_path)
    )
    return (
        "set +e\n"
        "export LIBGUESTFS_BACKEND=direct\n"
        "IMG=/disk/disk.img\n"
        "rm -f /tmp/out\n"
        'PARTS=$(guestfish --ro -a "$IMG" run : list-filesystems 2>/dev/null | '
        "awk -F': ' '$2 ~ /^(xfs|ext[234])$/{print $1}')\n"
        "for p in $PARTS; do\n"
        f'  guestfish --ro -a "$IMG" run : mount-ro "$p" / {globs} 2>/dev/null\n'
        '  [ -s /tmp/out ] && { echo "ROOT=$p"; break; }\n'
        "done\n"
        '[ -s /tmp/out ] || { echo "FILEPULL_ERROR: file not found"; exit 1; }\n'
        'echo "SIZE=$(stat -c%s /tmp/out)"\n'
        "echo B64_BEGIN\n"
        "base64 -w0 /tmp/out\n"
        "echo\n"
        "echo B64_END\n"
    )


def build_filepull_pod(
    name: str,
    namespace: str,
    temp_pvc_name: str,
    guest_path: str,
    *,
    image: str = TOOLS_IMAGE,
    service_account: str = "troshka-recert",
) -> dict:
    """A privileged pod that guestfish-reads ``guest_path`` from a snapshot clone
    PVC (``temp_pvc_name``) and prints it base64 for the caller to harvest via
    ``oc logs``. Mirrors the recert job's SA/securityContext so it schedules."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"troshka-role": "filepull"},
        },
        "spec": {
            "serviceAccountName": service_account,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "filepull",
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["bash", "-c", build_filepull_script(guest_path)],
                    "securityContext": {"privileged": True},
                    "volumeMounts": [{"name": "disk", "mountPath": "/disk"}],
                    "resources": {
                        "requests": {"cpu": "1", "memory": "2Gi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                }
            ],
            "volumes": [
                {
                    "name": "disk",
                    "persistentVolumeClaim": {"claimName": temp_pvc_name},
                }
            ],
        },
    }
