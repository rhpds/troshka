"""Bounded native observations; failure is nullable evidence, never zero."""

import time
from uuid import uuid4

from app.schemas.kubevirt_storage import (
    Blocker,
    ImageKey,
    ImageObservation,
    NativeStorageReport,
    PoolKey,
    ReadResult,
    SnapshotKey,
)
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_inventory import inventory_digest
from app.services.kubevirt_storage_transport import parse_usage


class _ReadFailure(Exception):  # noqa: N818 -- private typed read failure
    def __init__(self, status):
        self.status = status


def _call(method, *args):
    try:
        result = method(*args)
    except Exception:
        raise _ReadFailure("unavailable") from None
    if not isinstance(result, ReadResult) or result.status != "complete":
        raise _ReadFailure(
            result.status if isinstance(result, ReadResult) else "invalid_output"
        )
    return result


def _image_key(raw, source, pools):
    pool = raw.get("pool")
    matches = [p for p in pools if p["poolname"] == pool]
    if len(matches) != 1 or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise ValueError("unresolved backend identity")
    return ImageKey(
        ceph_fsid=source.ceph_fsid,
        pool_id=matches[0]["poolnum"],
        rbd_namespace=raw.get("pool_namespace", ""),
        image_id=raw["id"],
    )


def _parent(info, source, pools, reader):
    raw = info.get("parent")
    if not raw:
        return None
    parent_key = _image_key(raw, source, pools)
    if "snap_id" in raw:
        return SnapshotKey(image_key=parent_key, snapshot_id=str(raw["snap_id"]))
    result = _call(reader.image_snapshots, parent_key)
    snapshots, names = _snapshot_rows(result.value, parent_key)
    matches = [s for s, name in zip(snapshots, names) if name == raw.get("snapshot")]
    if len(matches) != 1:
        raise _ReadFailure("unsupported")
    return matches[0]


def _snapshot_rows(raw, key):
    if not isinstance(raw, list):
        raise ValueError("invalid snapshot inventory")
    snapshots, names = [], []
    for row in raw:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), int)
            or isinstance(row["id"], bool)
            or row["id"] < 0
            or not isinstance(row.get("name"), str)
        ):
            raise ValueError("invalid snapshot identity")
        namespace = row.get("namespace")
        if not isinstance(namespace, dict) or namespace.get("type") != "user":
            raise _ReadFailure("unsupported")
        snapshots.append(SnapshotKey(image_key=key, snapshot_id=str(row["id"])))
        names.append(row["name"])
    if len(set(snapshots)) != len(snapshots):
        raise ValueError("duplicate snapshot identity")
    return snapshots, names


def _children(reader, key, names, pools, timestamps, object_limit):
    children = []
    for name in names:
        result = _call(reader.image_children, key, name)
        if not isinstance(result.value, list):
            raise ValueError("invalid child inventory")
        children.extend(_image_key(v, key, pools) for v in result.value)
        if len(children) > object_limit:
            raise _ReadFailure("scan_limit")
        timestamps.append(result.collected_at)
    return children


def _observe(reader, key, pools, capability_ok, version, clock, limits):
    parent, snapshots, children, timestamps = None, [], [], []
    try:
        before = _call(reader.image_info, key)
        timestamps.append(before.collected_at)
        if not isinstance(before.value, dict):
            raise ValueError("invalid image metadata")
        if before.value.get("id") != key.image_id:
            raise _ReadFailure("identity_changed")
        parent = _parent(before.value, key, pools, reader)
        snap_before = _call(reader.image_snapshots, key)
        snapshots, names = _snapshot_rows(snap_before.value, key)
        if len(snapshots) > limits.max_objects:
            raise _ReadFailure("scan_limit")
        timestamps.append(snap_before.collected_at)
        children = _children(
            reader, key, names, pools, timestamps, limits.max_objects - len(snapshots)
        )
        if not capability_ok:
            raise _ReadFailure("unsupported")
        usage = _call(reader.image_usage, key)
        timestamps.append(usage.collected_at)
        allocated, provisioned = parse_usage(usage.value, key.image_id)
        after = _call(reader.image_info, key)
        snap_after = _call(reader.image_snapshots, key)
        timestamps.extend([after.collected_at, snap_after.collected_at])
        if (
            not isinstance(after.value, dict)
            or after.value.get("id") != key.image_id
            or _parent(after.value, key, pools, reader) != parent
            or snap_after.value != snap_before.value
        ):
            raise _ReadFailure("identity_changed")
        # Recheck children to invalidate consumers added during du.
        final_children = _children(
            reader, key, names, pools, timestamps, limits.max_objects - len(snapshots)
        )
        if set(final_children) != set(children):
            raise _ReadFailure("identity_changed")
        status = "complete"
    except _ReadFailure as error:
        status, allocated, provisioned = error.status, None, None
    except (ValueError, KeyError, TypeError, AttributeError):
        status, allocated, provisioned = "invalid_output", None, None
    # Original source age survives graph traversal and report serialization.
    observed = min(timestamps, default=clock())
    try:
        return ImageObservation(
            key=key,
            parent=parent,
            snapshots=snapshots,
            children=list(dict.fromkeys(children)),
            collected_at=observed,
            allocated_bytes=allocated,
            provisioned_bytes=provisioned,
            measurement_status=status,
            command_version=version,
            measurement_semantics_version=(
                "kubevirt-rbd-allocated-v1" if status == "complete" else None
            ),
        )
    except ValueError:
        return ImageObservation(
            key=key,
            collected_at=clock(),
            allocated_bytes=None,
            provisioned_bytes=None,
            measurement_status="invalid_output",
            command_version=version,
            measurement_semantics_version=None,
        )


def _backend_profile(inventory, rbd_reader, errors):
    pools, version, capability_ok = [], "unavailable", False
    try:
        identity = _call(rbd_reader.cluster_identity)
        pools_result = _call(rbd_reader.pool_identity)
        pools = pools_result.value
        actual_version = _call(rbd_reader.command_version).value
        expected_fsids = {
            b.image_key.ceph_fsid
            for b in inventory.volume_bindings
            + inventory.retained_bindings
            + inventory.cache_bindings
        }
        if expected_fsids != {identity.value}:
            raise _ReadFailure("identity_changed")
        profile = rbd_reader.capability
        version = (
            actual_version
            if actual_version == profile.command_version
            else "unavailable"
        )
        capability_ok = bool(
            profile.merge_snapshots_validated
            and profile.validation_reference
            and profile.command_version == version
            and profile.semantics_version == "kubevirt-rbd-allocated-v1"
        )
    except (AttributeError, _ReadFailure, ValueError, TypeError):
        errors.append(Blocker(code="backend_identity_or_capability_unavailable"))
    return pools, version, capability_ok


def _trash_ids(reader, key, errors):
    try:
        result = _call(reader.trash_inventory, key)
        if not isinstance(result.value, list):
            raise _ReadFailure("invalid_output")
        ids = {row["id"] for row in result.value}
        if any(not isinstance(image_id, str) for image_id in ids):
            raise _ReadFailure("invalid_output")
        return ids
    except (_ReadFailure, ValueError, TypeError, KeyError):
        errors.append(Blocker(code="trash_inventory_unavailable"))
        return set()


def _collect_images(
    inventory,
    rbd_reader,
    pools,
    version,
    capability_ok,
    limits,
    clock,
    errors,
    deadline,
):
    images, seen = [], set()
    queue = [
        (b.image_key, 0)
        for b in inventory.volume_bindings
        + inventory.retained_bindings
        + inventory.cache_bindings
    ]
    checked_pools = {}
    while queue:
        key, depth = queue.pop(0)
        if key in seen:
            continue
        if len(seen) >= limits.max_objects or time.monotonic() >= deadline:
            errors.append(Blocker(code="scan_limit"))
            break
        seen.add(key)
        if depth > limits.max_parent_depth:
            errors.append(Blocker(code="parent_depth_limit", image_key=key))
            continue
        pool_key = (key.ceph_fsid, key.pool_id, key.rbd_namespace)
        if pool_key not in checked_pools:
            checked_pools[pool_key] = _trash_ids(rbd_reader, key, errors)
        image = _observe(rbd_reader, key, pools, capability_ok, version, clock, limits)
        if key.image_id in checked_pools[pool_key]:
            image = image.model_copy(
                update={
                    "measurement_status": "unsupported",
                    "allocated_bytes": None,
                    "measurement_semantics_version": None,
                }
            )
        images.append(image)
        if image.parent:
            queue.append((image.parent.image_key, depth + 1))
        queue.extend((child, depth + 1) for child in image.children)
    return images


def _final_checks(inventory, rbd_reader, errors):
    try:
        final_identity = _call(rbd_reader.cluster_identity)
        _call(rbd_reader.pool_identity)
        if final_identity.value not in {
            b.image_key.ceph_fsid
            for b in inventory.volume_bindings
            + inventory.retained_bindings
            + inventory.cache_bindings
        }:
            errors.append(Blocker(code="backend_identity_changed"))
    except Exception:
        errors.append(Blocker(code="backend_identity_recheck_unavailable"))
    try:
        rechecked = rbd_reader.recheck_inventory()
        if inventory_digest(rechecked) != inventory_digest(inventory):
            errors.append(Blocker(code="inventory_changed"))
    except Exception:
        errors.append(Blocker(code="inventory_recheck_unavailable"))


def _graph_metadata(reader, image, pools, limits):
    info = _call(reader.image_info, image.key)
    if not isinstance(info.value, dict):
        raise ValueError("invalid image metadata")
    parent = _parent(info.value, image.key, pools, reader)
    result = _call(reader.image_snapshots, image.key)
    snapshots, names = _snapshot_rows(result.value, image.key)
    if len(snapshots) > limits.max_objects:
        raise _ReadFailure("scan_limit")
    children = _children(
        reader, image.key, names, pools, [], limits.max_objects - len(snapshots)
    )
    return info.value.get("id"), parent, set(snapshots), set(children)


def _final_graph_check(reader, images, pools, limits, deadline, errors):
    verified = []
    for image in images:
        if image.measurement_status != "complete":
            verified.append(image)
            continue
        try:
            if time.monotonic() >= deadline:
                raise _ReadFailure("scan_limit")
            observed = _graph_metadata(reader, image, pools, limits)
            expected = (
                image.key.image_id,
                image.parent,
                set(image.snapshots),
                set(image.children),
            )
            if observed != expected:
                raise _ReadFailure("identity_changed")
            verified.append(image)
        except (_ReadFailure, ValueError, KeyError, TypeError, AttributeError) as error:
            status = (
                error.status if isinstance(error, _ReadFailure) else "invalid_output"
            )
            code = (
                "backend_graph_changed"
                if status == "identity_changed"
                else "backend_graph_recheck_unavailable"
            )
            errors.append(Blocker(code=code, image_key=image.key))
            verified.append(
                image.model_copy(
                    update={
                        "measurement_status": status,
                        "allocated_bytes": None,
                        "measurement_semantics_version": None,
                    }
                )
            )
    return verified


def collect_report(inventory, rbd_reader, *, limits, clock):
    started = getattr(rbd_reader, "scan_started_at", clock())
    deadline = time.monotonic() + limits.scan_timeout_seconds
    errors = list(inventory.blockers)
    pools, version, capability_ok = _backend_profile(inventory, rbd_reader, errors)
    images = _collect_images(
        inventory,
        rbd_reader,
        pools,
        version,
        capability_ok,
        limits,
        clock,
        errors,
        deadline,
    )
    _final_checks(inventory, rbd_reader, errors)
    images = _final_graph_check(rbd_reader, images, pools, limits, deadline, errors)
    working = inventory.model_copy(update={"blockers": errors})
    attributed = attribute_graph(
        working, images, max_parent_depth=limits.max_parent_depth
    )
    # Inventory was acquired before any image; include its age in every total.
    projects = [
        p.model_copy(
            update={
                "oldest_observed_at": min(
                    [inventory.collected_at, *[i.collected_at for i in images]]
                )
            }
        )
        for p in attributed.projects
    ]
    return NativeStorageReport(
        **attributed.model_dump(exclude={"projects"}),
        projects=projects,
        report_id=uuid4(),
        collector_id=inventory.collector_id,
        started_at=started,
        completed_at=clock(),
        inventory_digest=inventory_digest(inventory),
        cluster_keys=sorted(
            {r.cluster_key for r in inventory.project_roots}
            | {b.cluster_key for b in inventory.cache_bindings}
        ),
        pool_keys=[
            PoolKey(ceph_fsid=fsid, pool_id=pool_id)
            for fsid, pool_id in sorted(
                {(i.key.ceph_fsid, i.key.pool_id) for i in images}
            )
        ],
        project_roots=inventory.project_roots,
        volume_bindings=inventory.volume_bindings,
        cache_bindings=inventory.cache_bindings,
        retained_bindings=inventory.retained_bindings,
        snapshot_evidence=inventory.snapshot_evidence,
        images=images,
        collection_errors=errors,
    )
