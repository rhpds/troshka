"""Known-ID / soft-target Ceph orphan reclaim after the Project row is released."""

from app.services import native_deployment_origin as origin
from app.services.native_teardown_map import reclaim_targets


def _soft_from_keys(keys):
    """Best-effort name targets from immutable image ids (csi-vol-* form)."""
    return [
        {
            "pool": "ocs-storagecluster-cephblockpool",
            "image": key.image_id,
        }
        for key in keys
        if key.image_id
    ]


def attempt(db, work, *, core_api):
    """Reclaim sealed private images; return remaining soft targets.

    Shared images are never deleted. Empty maps finalize immediately.
    """
    private = list(work.liabilities.private_images)
    shared = set(work.liabilities.shared_images)
    private = [k for k in private if k not in shared]
    soft = list(reclaim_targets(db, work)) + _soft_from_keys(private)
    # Dedupe by (pool, image)
    seen, targets = set(), []
    for row in soft:
        token = (row.get("pool"), row.get("image"))
        if not token[1] or token in seen:
            continue
        seen.add(token)
        targets.append(row)
    if not targets:
        return []
    from app.services.providers.kubevirt import reclaim_rbd_images

    results = reclaim_rbd_images(core_api, targets)
    remaining = [
        t
        for t, r in zip(targets, results)
        if r.get("status") not in {"gone", "ok", "trash"}
    ]
    return remaining


def finalize_if_clear(db, work, remaining):
    from app.services import native_project_namespace_cleanup as cleanup

    if remaining:
        cleanup._evidence(
            db,
            work,
            "orphan-reclaim-progress",
            dict(soft_remaining=len(remaining)),
        )
        cleanup._save(
            db,
            work,
            last_error="Known storage orphans remain; retry reclaim",
        )
        db.commit()
        origin.require(False, "Known storage orphans remain; retry reclaim")
    cleanup._evidence(
        db,
        work,
        "orphan-reclaim-complete",
        dict(soft_remaining=0),
    )
    work = cleanup._save(db, work, phase="finalized", last_error=None)
    db.commit()
    return work
