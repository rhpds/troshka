"""Fixed toolbox program: bounded read-only dependency metadata, no writes.

This file is shipped verbatim to python3 through the pinned transport. It must
depend only on the standard library and the toolbox's qualified rados/rbd API.
"""

import json
import shutil
import subprocess
import sys

QUALIFIED_CLI = "ceph version 19.2.1-423.el9cp (cb76ca76a78ea7346ab574122ab21a208cf75df0) squid (stable)"
QUALIFIED_RELEASE = "19.2.1-423.el9cp.x86_64"


def _qualified_runtime(rbd):
    if tuple(rbd.RBD().version()) != (1, 19, 0):
        raise ValueError("unqualified librbd API")
    executable = shutil.which("rbd")
    if executable is None:
        raise ValueError("missing qualified CLI")
    checks = [
        ([executable, "--version"], QUALIFIED_CLI),
        (["rpm", "-qf", rbd.__file__], "python3-rbd-" + QUALIFIED_RELEASE),
        (["rpm", "-qf", executable], "ceph-common-" + QUALIFIED_RELEASE),
        (["rpm", "-q", "librbd1"], "librbd1-" + QUALIFIED_RELEASE),
    ]
    for command, expected in checks:
        observed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=5
        )
        if observed.stdout.strip() != expected:
            raise ValueError("unqualified dependency runtime")


def _snapshots(image, limit, selected=None):
    rows = []
    for row in image.list_snaps():
        sid = row["id"]
        if selected is not None and str(sid) not in selected:
            continue
        rows.append(_snapshot_identity(row))
        if len(rows) > limit:
            raise ValueError("snapshot limit")
    if len({sid for sid, _ in rows}) != len(rows):
        raise ValueError("duplicate snapshot")
    if selected is not None and {str(sid) for sid, _ in rows} != set(selected):
        raise ValueError("missing boundary snapshot")
    return sorted(rows)


def _snapshot_identity(row):
    sid, namespace = row["id"], row["namespace"]
    if namespace not in (0, 2) or type(sid) is not int or sid <= 0:
        raise ValueError("unsupported snapshot")
    if namespace == 2 and row["trash"]["original_namespace_type"] != 0:
        raise ValueError("unsupported snapshot namespace")
    return sid, namespace


def _dependency(image, fsid):
    overlap = image.overlap()
    if type(overlap) is not int or overlap < 0:
        raise ValueError("invalid overlap")
    if overlap == 0:
        # Successful overlap, never a parent lookup exception, proves this.
        return {"overlap_bytes": 0, "parent": None}
    parent = image.get_parent_image_spec()
    if parent["snap_namespace_type"] not in (0, 2) or parent["trash"]:
        raise ValueError("unsupported dependency")
    if type(parent["snap_id"]) is not int or parent["snap_id"] <= 0:
        raise ValueError("invalid parent snapshot")
    return {
        "overlap_bytes": overlap,
        "parent": {
            "image_key": {
                "ceph_fsid": fsid,
                "pool_id": parent["pool_id"],
                "rbd_namespace": parent["pool_namespace"],
                "image_id": parent["image_id"],
            },
            "snapshot_id": str(parent["snap_id"]),
        },
    }


def observe(image, fsid, image_id, limit, migrating_feature, selected=None):
    if image.id() != image_id or image.features() & migrating_feature:
        raise ValueError("unsupported image identity or migration")
    before = _snapshots(image, limit, selected)
    head = _dependency(image, fsid)
    snapshots = []
    for sid, _ in before:
        image.set_snap_by_id(sid)
        snapshots.append(
            {"snapshot_id": str(sid), "dependency": _dependency(image, fsid)}
        )
    image.set_snap(None)
    if (
        image.id() != image_id
        or image.features() & migrating_feature
        or _snapshots(image, limit, selected) != before
        or _dependency(image, fsid) != head
    ):
        raise ValueError("dependency identity changed")
    return {"image_id": image_id, "head": head, "snapshots": snapshots}


def main():
    import rados
    import rbd

    fsid, pool, pool_id, namespace, image_id, limit, selected, conf, keyring = sys.argv[
        1:
    ]
    _qualified_runtime(rbd)
    # No encryption loader or skip-parent open is used: overlap must describe
    # the normally opened image, including its selected immutable snapshot.
    config = {"keyring": keyring} if keyring else {}
    with rados.Rados(conffile=conf, conf=config) as cluster:
        if cluster.get_fsid() != fsid:
            raise ValueError("cluster changed")
        with cluster.open_ioctx(pool) as ioctx:
            if ioctx.get_pool_id() != int(pool_id):
                raise ValueError("pool changed")
            ioctx.set_namespace(namespace)
            with rbd.Image(ioctx, image_id=image_id, read_only=True) as image:
                result = observe(
                    image,
                    fsid,
                    image_id,
                    int(limit),
                    rbd.RBD_FEATURE_MIGRATING,
                    json.loads(selected),
                )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
