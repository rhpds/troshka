"""Optional exact dependency reads for terminal cleanup graph isolation."""

from app.schemas.kubevirt_storage import CurrentImageDependencies, SnapshotKey


def metadata(reader, image, pools, limits, boundary=None):
    from app.services.kubevirt_storage_collector import _call, _children
    from app.services.native_snapshot_isolation import projection_matches

    args = () if boundary is None else ([s.snapshot_id for s in image.snapshots],)
    result = _call(reader.image_dependencies, image.key, limits.max_objects, *args)
    raw = result.value
    if not isinstance(raw, dict) or raw.get("image_id") != image.key.image_id:
        raise ValueError("dependency image identity changed")
    rows = raw["snapshots"]
    if not isinstance(rows, list) or len(rows) > limits.max_objects:
        raise ValueError("dependency limit")
    timestamps, snapshots = [image.collected_at, result.collected_at], []
    remaining = limits.max_objects - len(rows)
    for row in rows:
        snapshot = SnapshotKey(image_key=image.key, snapshot_id=row["snapshot_id"])
        children = _children(
            reader, image.key, [snapshot], pools, timestamps, remaining
        )
        remaining -= len(children)
        snapshots.append(
            dict(snapshot=snapshot, dependency=row["dependency"], children=children)
        )
    value = CurrentImageDependencies(
        head=raw["head"], snapshots=snapshots, cache_boundary=boundary
    )
    if (
        len(value.snapshots)
        != len({s.snapshot.model_dump_json() for s in value.snapshots})
        or not projection_matches(image, value)
        or value.head.parent != image.parent
    ):
        raise ValueError("dependency graph changed")
    return value, min(timestamps)


def enrich(reader, image, pools, limits, boundary=None):
    from app.services.kubevirt_storage_collector import _ReadFailure

    if (
        image.measurement_status not in {"complete", "timeout"}
        or image.absence is not None
        or (boundary is not None and image.measurement_status != "complete")
    ):
        return image
    try:
        value, observed = metadata(reader, image, pools, limits, boundary)
        return image.model_copy(
            update={"current_dependencies": value, "collected_at": observed}
        )
    except (_ReadFailure, ValueError, KeyError, TypeError, AttributeError):
        # Optional evidence cannot be fabricated from a failed read. Legacy
        # exact-shape proofs remain possible only when no new evidence exists.
        return image


def related(image):
    if image.current_dependencies is None:
        return set()
    from app.services.native_snapshot_isolation import dependencies

    return set(image.children) | {
        d.parent.image_key for d in dependencies(image) if d.parent is not None
    }


def cache_boundaries(
    reader, images, caches, selected, pools, capability, version, clock, limits
):
    from app.services.kubevirt_storage_collector import _observe_source
    from app.services.native_snapshot_isolation import dependencies

    required = {key: set(selected.get(key, ())) for key in caches}
    for image in images.values():
        if image.current_dependencies is None:
            continue
        for dependency in dependencies(image):
            parent = dependency.parent
            if parent is not None and parent.image_key in caches:
                required[parent.image_key].add(parent)
    for key, snapshots in required.items():
        previous = images.get(key)
        if previous is None or previous.measurement_status != "complete":
            continue
        # Keep any already observed edges in the boundary instead of dropping
        # child evidence when a full observation is supplied by another path.
        snapshots.update(previous.snapshots)
        boundary = sorted(snapshots, key=lambda s: s.snapshot_id)
        image = _observe_source(
            reader, key, snapshots, pools, capability, version, clock, limits
        )
        images[key] = enrich(reader, image, pools, limits, boundary)
