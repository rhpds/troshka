"""Seal a VM→PVC→RBD teardown map before namespace drain.

Prefer retained custody liabilities. Otherwise inventory only the project
namespace — never a domain-wide collector scan. When CSI journal resolve is
unavailable, seal soft reclaim targets from PV volumeAttributes/handles.
"""

from app.schemas.kubevirt_storage import VolumeBinding
from app.schemas.native_project_cleanup import StorageLiabilities
from app.services import native_deployment_origin as origin
from app.services.kubevirt_storage_enrollment import digest
from app.services.kubevirt_storage_inventory import BindingError, _pvc_binding

# Soft collector debts cleared once a live map is sealed (possibly empty).
_SOFT_COVERAGE = frozenset(
    {"storage_coverage_unavailable", "storage_coverage_incomplete"}
)


def map_sealed(work):
    """True when soft coverage debts no longer block NS teardown."""
    return not (set(work.liabilities.unresolved) & _SOFT_COVERAGE)


def hard_debts(liabilities):
    """Any non-coverage debt still blocks destructive NS delete."""
    return sorted(d for d in liabilities.unresolved if d not in _SOFT_COVERAGE)


def _root_for(work):
    selected = work.authority
    if selected.root is not None:
        return selected.root
    from app.schemas.kubevirt_storage import ProjectRoot

    return ProjectRoot(
        project_id=selected.project_id,
        cluster_key=selected.cluster_uid,
        namespace=selected.namespace,
        namespace_uid=selected.namespace_uid,
        project_cr_uid=selected.root_uid or selected.namespace_uid,
        deployment_generation=None,
    )


def _pv_index(api):
    try:
        return {pv["metadata"]["name"]: pv for pv in api.rows("PersistentVolume", None)}
    except Exception:
        return {}


def _soft_target(pv):
    """Name-based reclaim target from CSI PV attrs (no journal)."""
    if not isinstance(pv, dict):
        return None
    csi = (pv.get("spec") or {}).get("csi") or {}
    driver = csi.get("driver") or ""
    if "rbd.csi.ceph.com" not in driver:
        return None
    import re

    attrs = csi.get("volumeAttributes") or {}
    handle = csi.get("volumeHandle") or ""
    image = attrs.get("imageName") or ""
    if not image:
        match = re.search(
            r"(csi-vol-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            handle,
        )
        image = match.group(1) if match else ""
    if not image:
        return None
    return {
        "pool": attrs.get("pool") or "ocs-storagecluster-cephblockpool",
        "image": image,
        "pv": pv.get("metadata", {}).get("name") or "",
    }


def _live_bindings(api, work):
    """PVC→ImageKey bindings when api.resolve_csi is available."""
    if not hasattr(api, "resolve_csi"):
        return [], [], []
    namespace = work.authority.namespace
    root = _root_for(work)
    pvs = _pv_index(api)
    bindings, debts, soft = [], [], []
    for pvc in api.rows("PersistentVolumeClaim", namespace):
        try:
            fields, size = _pvc_binding(api, pvc, pvs, namespace)
            owners = [
                o["uid"] for o in pvc.get("metadata", {}).get("ownerReferences", [])
            ]
            bindings.append(
                VolumeBinding(
                    root=root,
                    **fields,
                    provisioned_bytes=size,
                    owner_reference_uids=owners,
                )
            )
        except BindingError as error:
            volume = (pvc.get("spec") or {}).get("volumeName")
            target = _soft_target(pvs.get(volume or "", {}))
            if target:
                soft.append(target)
            else:
                debts.append(f"pending_pvc:{pvc['metadata']['uid']}:{error.args[0]}")
        except (ValueError, KeyError, TypeError, AttributeError):
            volume = (pvc.get("spec") or {}).get("volumeName")
            target = _soft_target(pvs.get(volume or "", {}))
            if target:
                soft.append(target)
            else:
                debts.append(f"pending_pvc:{pvc['metadata']['uid']}:unresolved")
    return bindings, debts, soft


def _soft_only(api, work):
    """When resolve_csi is unavailable, seal name-based reclaim targets."""
    namespace = work.authority.namespace
    pvs = _pv_index(api)
    soft, debts = [], []
    try:
        pvcs = api.rows("PersistentVolumeClaim", namespace)
    except Exception:
        pvcs = []
    for pvc in pvcs:
        volume = (pvc.get("spec") or {}).get("volumeName")
        target = _soft_target(pvs.get(volume or "", {})) if volume else None
        if target:
            soft.append(target)
            continue
        if not pvs:
            # Namespace client has no PV inventory; CSI reclaim follows NS delete.
            continue
        if not volume:
            # Still unbound: NS delete cannot orphan a backend image yet.
            continue
        debts.append(f"pending_pvc:{pvc['metadata']['uid']}:unresolved")
    return soft, debts


def _clear_soft(unresolved):
    return [d for d in unresolved if d not in _SOFT_COVERAGE]


def _from_custody(work):
    liabilities = work.liabilities
    if not liabilities.bindings and not liabilities.private_images:
        return None
    return liabilities.model_copy(
        update={"unresolved": sorted(_clear_soft(liabilities.unresolved))}
    )


def _from_live(api, work):
    bindings, debts, soft = _live_bindings(api, work)
    if not hasattr(api, "resolve_csi"):
        soft, debts = _soft_only(api, work)
        bindings = []
    private = {b.image_key for b in bindings}
    shared = set(work.liabilities.shared_images)
    private -= shared
    kept = [
        d
        for d in work.liabilities.unresolved
        if d not in _SOFT_COVERAGE and not d.startswith("pending_pvc:")
    ]
    liabilities = StorageLiabilities(
        bindings=bindings,
        ancestry=list(work.liabilities.ancestry),
        private_images=sorted(private, key=lambda k: k.model_dump_json()),
        shared_images=sorted(shared, key=lambda k: k.model_dump_json()),
        unresolved=sorted(set(kept) | set(debts)),
        producers=list(work.liabilities.producers),
        snapshots=list(work.liabilities.snapshots),
        snapshot_restores=list(work.liabilities.snapshot_restores),
        snapshot_retirements=list(work.liabilities.snapshot_retirements),
        support_boundaries=list(work.liabilities.support_boundaries),
    )
    return liabilities, soft


def seal(db, api, work):
    """Persist a teardown map on the cleanup row before VM/NS deletion."""
    from app.services import native_project_namespace_cleanup as cleanup

    if map_sealed(work) and not hard_debts(work.liabilities):
        return work
    soft = []
    liabilities = _from_custody(work)
    source = "custody"
    if liabilities is None:
        liabilities, soft = _from_live(api, work)
        source = "live-namespace"
    # Empty NS / only soft coverage → empty map is valid.
    if not hard_debts(liabilities):
        liabilities = liabilities.model_copy(
            update={"unresolved": sorted(_clear_soft(liabilities.unresolved))}
        )
    origin.require(
        not hard_debts(liabilities),
        "Verified storage coverage required before destructive cleanup",
    )
    payload = dict(
        source=source,
        private_images=[k.model_dump(mode="json") for k in liabilities.private_images],
        shared_images=[k.model_dump(mode="json") for k in liabilities.shared_images],
        binding_count=len(liabilities.bindings),
        reclaim_targets=soft,
        unresolved=list(liabilities.unresolved),
    )
    work = cleanup._save(
        db,
        work,
        liabilities=liabilities,
        liability_digest=digest(liabilities.model_dump(mode="json")),
        last_error=None,
    )
    cleanup._evidence(db, work, "teardown-map", payload)
    if hasattr(db, "flush"):
        db.flush()
    return work


def reclaim_targets(db, work):
    """Soft name-based targets sealed on the teardown-map evidence row."""
    from app.models.native_project_cleanup import (
        NativeProjectNamespaceEvidence as Evidence,
    )

    row = (
        db.query(Evidence)
        .filter_by(cleanup_id=str(work.id), phase="teardown-map")
        .one_or_none()
    )
    if row is None:
        return []
    return list(row.payload.get("reclaim_targets") or [])
