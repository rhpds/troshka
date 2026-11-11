"""Pure conservative ownership evaluation; no clients, database or subprocesses."""

from collections import defaultdict

from app.schemas.kubevirt_storage import (
    AttributionResult,
    PoolAllocation,
    PoolKey,
    ProjectAllocation,
)


def _walk(start, observations, cache, depth_limit):
    seen, path, errors = set(), set(), set()
    _visit_ancestor(start, 0, observations, cache, depth_limit, seen, path, errors)
    return seen, errors


def _visit_ancestor(key, depth, observations, cache, limit, seen, path, errors):
    if key in path:
        errors.add("ancestry_cycle")
        return
    if key in seen:
        return
    seen.add(key)
    if depth > limit:
        errors.add("parent_depth_limit")
        return
    image = observations.get(key)
    if image is None:
        errors.add("missing_image")
        return
    if key in cache and image.parent and image.parent.image_key not in cache:
        errors.add("unverified_cache_ancestor")
        return
    if image.parent:
        path.add(key)
        _visit_ancestor(
            image.parent.image_key,
            depth + 1,
            observations,
            cache,
            limit,
            seen,
            path,
            errors,
        )
        path.remove(key)


def _unknown_consumers(key, observations, members, cache, limit, path=None):
    if key in cache:
        return False
    path = set() if path is None else path
    if key in path or len(path) >= limit:
        return True
    image = observations.get(key)
    if image is None:
        return True
    path = path | {key}
    for child in image.children:
        if child not in members or _unknown_consumers(
            child, observations, members, cache, limit, path
        ):
            return True
    return False


def _index_images(images):
    observations, duplicates = {}, set()
    for image in images:
        if image.key in observations and observations[image.key] != image:
            duplicates.add(image.key)
        observations[image.key] = image
    return observations, duplicates


def _ownership(inventory, observations, cache, max_parent_depth):
    ownership, project_nodes, failures = defaultdict(set), {}, defaultdict(set)
    bindings = list(
        {
            b.model_dump_json(): b
            for b in inventory.volume_bindings + inventory.retained_bindings
        }.values()
    )
    for root in inventory.project_roots:
        nodes = set()
        for binding in bindings:
            if binding.root == root:
                reached, errors = _walk(
                    binding.image_key, observations, cache, max_parent_depth
                )
                nodes |= reached
                failures[root.project_id] |= errors
        project_nodes[root.project_id] = nodes
        for key in nodes - cache:
            ownership[key].add(root.project_id)
    return ownership, project_nodes, failures


def _project_allocation(
    root, inventory, nodes, failures, observations, unresolved, cache
):
    required = nodes - cache
    reasons = failures | {
        b.code for b in inventory.blockers if b.project_id in (None, root.project_id)
    }
    if sum(x.project_id == root.project_id for x in inventory.project_roots) > 1:
        reasons.add("duplicate_project_root")
    if required & unresolved:
        reasons.add("unresolved_ownership")
    measured = [
        observations[k]
        for k in required - unresolved
        if k in observations and observations[k].measurement_status == "complete"
    ]
    if len(measured) != len(required):
        reasons.add("incomplete_measurement")
    if any(
        k not in observations or observations[k].measurement_status != "complete"
        for k in nodes & cache
    ):
        reasons.add("incomplete_cache_evidence")
    own_bindings = {
        b.pvc_uid: b
        for b in inventory.retained_bindings + inventory.volume_bindings
        if b.root == root
    }
    provisioned = (
        None
        if any(b.provisioned_bytes is None for b in own_bindings.values())
        else sum(b.provisioned_bytes for b in own_bindings.values())
    )
    known = sum(i.allocated_bytes for i in measured)
    coverage = "complete"
    if reasons:
        coverage = "partial" if measured else "unavailable"
    return ProjectAllocation(
        project_id=root.project_id,
        namespace_uid=root.namespace_uid,
        allocated_bytes=None if reasons else known,
        known_allocated_bytes=known,
        provisioned_bytes=provisioned,
        measured_images=len(measured),
        required_images=len(required),
        oldest_observed_at=min((i.collected_at for i in measured), default=None),
        coverage=coverage,
        blockers=sorted(reasons),
    )


def _platform_pools(cache, observations):
    pools = defaultdict(list)
    for key in cache:
        pools[PoolKey(ceph_fsid=key.ceph_fsid, pool_id=key.pool_id)].append(
            observations.get(key)
        )
    return [
        PoolAllocation(
            pool_key=k,
            known_allocated_bytes=sum(
                i.allocated_bytes
                for i in values
                if i and i.measurement_status == "complete"
            ),
            allocated_bytes=(
                sum(i.allocated_bytes for i in values)
                if all(i and i.measurement_status == "complete" for i in values)
                else None
            ),
        )
        for k, values in pools.items()
    ]


def attribute_graph(inventory, images, *, max_parent_depth=32):
    observations, duplicates = _index_images(images)
    cache = {b.image_key for b in inventory.cache_bindings}
    ownership, project_nodes, failures = _ownership(
        inventory, observations, cache, max_parent_depth
    )
    all_members = set().union(*project_nodes.values()) if project_nodes else set()
    unresolved = {
        k
        for k, owners in ownership.items()
        if len(owners) > 1
        or _unknown_consumers(k, observations, all_members, cache, max_parent_depth)
    } | duplicates
    projects = [
        _project_allocation(
            root,
            inventory,
            project_nodes[root.project_id],
            failures[root.project_id],
            observations,
            unresolved,
            cache,
        )
        for root in inventory.project_roots
    ]
    return AttributionResult(
        projects=projects,
        platform_pools=_platform_pools(cache, observations),
        unattributed_images=sorted(unresolved, key=lambda k: k.model_dump_json()),
    )
