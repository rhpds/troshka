"""Pure conservative ownership evaluation; no clients, database or subprocesses."""

from collections import defaultdict

from app.schemas.kubevirt_storage import (
    AttributionResult,
    Blocker,
    PoolAllocation,
    PoolKey,
    ProjectAllocation,
)

UNVERIFIED_IDENTITY = frozenset(
    {
        "backend_identity_changed",
        "backend_identity_recheck_unavailable",
        "backend_identity_or_capability_unavailable",
        "cluster_identity_mismatch",
    }
)


def _walk(start, observations, cache, depth_limit, authorized):
    seen, path, errors = set(), set(), set()
    _visit_ancestor(
        start, 0, observations, cache, depth_limit, seen, path, errors, authorized
    )
    return seen, errors


def _visit_ancestor(
    key, depth, observations, cache, limit, seen, path, errors, authorized
):
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
    # V1 records snapshot IDs, not their parents. A head parent (or its absence)
    # cannot prove retained ancestry after a shallow flatten.
    if image.snapshots and key not in authorized:
        errors.add("snapshot_ancestry_unverified")
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
            authorized,
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


def _ownership(inventory, observations, cache, max_parent_depth, authorized=None):
    authorized = set() if authorized is None else authorized
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
                    binding.image_key, observations, cache, max_parent_depth, authorized
                )
                nodes |= reached
                failures[root.project_id] |= errors
        reached, errors = _retiring_ownership(
            root, inventory, observations, cache, max_parent_depth, authorized
        )
        nodes |= reached
        failures[root.project_id] |= errors
        project_nodes[root.project_id] = nodes
        for key in nodes - cache:
            ownership[key].add(root.project_id)
    return ownership, project_nodes, failures


def _retiring_ownership(
    root, inventory, observations, cache, max_parent_depth, authorized
):
    nodes, failures = set(), set()
    for retiring in inventory.retiring_roots:
        if retiring.root == root:
            for key in retiring.owned_images:
                reached, errors = _walk(
                    key, observations, cache, max_parent_depth, authorized
                )
                nodes |= reached
                failures |= errors
    return nodes, failures


def _project_allocation(
    root, inventory, nodes, failures, observations, unresolved, cache
):
    required = nodes - cache
    reasons = failures | {
        b.code
        for b in inventory.blockers
        if b.project_id in (None, root.project_id)
        and (b.image_key is None or b.image_key in nodes)
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
    if reasons & UNVERIFIED_IDENTITY:
        measured = []
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


def _binding_retirement_ownership(
    retirements, observations, nodes, ownership, failures
):
    for retirement in retirements:
        project_id = retirement.root.project_id
        if project_id not in nodes:
            continue
        for key in retirement.private_images:
            nodes[project_id].add(key)
            ownership[key].add(project_id)
            image = observations.get(key)
            if (
                image is None
                or image.absence is None
                or image.measurement_status != "complete"
                or image.collected_at < retirement.verified_at
            ):
                failures[project_id].add("binding_retirement_absence_unverified")


def _pool_allocation(pool_key, keys, observations, blockers, authorized):
    applicable = [
        b
        for b in blockers
        if b.project_id is None and (b.image_key is None or b.image_key in keys)
    ]
    complete = [
        observations[k]
        for k in keys
        if k in observations and observations[k].measurement_status == "complete"
    ]
    known = sum(i.allocated_bytes for i in complete)
    if any(b.code in UNVERIFIED_IDENTITY for b in applicable):
        known = 0
    return PoolAllocation(
        pool_key=pool_key,
        known_allocated_bytes=known,
        allocated_bytes=known
        if not applicable
        and len(complete) == len(keys)
        and not any(
            image.snapshots and image.key not in authorized for image in complete
        )
        else None,
    )


def _platform_pools(cache, observations, blockers, authorized):
    pools = defaultdict(list)
    for key in cache:
        pools[PoolKey(ceph_fsid=key.ceph_fsid, pool_id=key.pool_id)].append(key)
    return [
        _pool_allocation(k, values, observations, blockers, authorized)
        for k, values in pools.items()
    ]


def attribute_graph(
    inventory,
    images,
    *,
    max_parent_depth=32,
    clone_lineages=(),
    binding_retirements=(),
    snapshot_facts=(),
    project_cleanup_scopes=(),
):
    from app.services import native_snapshot_graph as snapshot_graph
    from app.services.native_clone_graph import authorize_graph
    from app.services.native_project_retirement import verified_retirements

    observations, duplicates = _index_images(images)
    terminal, terminal_errors = verified_retirements(
        project_cleanup_scopes, inventory, images
    )
    live_snapshot_facts = snapshot_graph.live_facts(snapshot_facts, terminal)
    authorized, private, blockers = authorize_graph(
        inventory,
        {k: v for k, v in observations.items() if k not in duplicates},
        clone_lineages,
        binding_retirements,
        snapshot_edges=snapshot_graph.extra_edges(live_snapshot_facts),
    )
    snapshot_authorized, snapshot_errors = snapshot_graph.authorize(
        inventory,
        {k: v for k, v in observations.items() if k not in duplicates},
        snapshot_facts,
        binding_retirements,
        terminal,
    )
    authorized |= snapshot_authorized
    blockers += snapshot_errors + terminal_errors
    inventory = inventory.model_copy(update={"blockers": inventory.blockers + blockers})
    cache = {b.image_key for b in inventory.cache_bindings} - private
    ownership, project_nodes, failures = _ownership(
        inventory, observations, cache, max_parent_depth, authorized
    )
    _binding_retirement_ownership(
        binding_retirements, observations, project_nodes, ownership, failures
    )
    snapshot_graph.add_ownership(
        inventory,
        observations,
        live_snapshot_facts,
        ownership,
        project_nodes,
        failures,
        cache,
        max_parent_depth,
        authorized,
    )
    authorized_absence = {
        key for root in inventory.retiring_roots for key in root.owned_images
    }
    authorized_absence.update(
        key for r in binding_retirements for key in r.private_images
    )
    authorized_absence.update(snapshot_graph.retired_images(snapshot_facts))
    authorized_absence.update(
        b.image_key
        for b in inventory.absent_cache_bindings
        if b in inventory.cache_bindings
    )
    if any(i.absence is not None and i.key not in authorized_absence for i in images):
        inventory = inventory.model_copy(
            update={
                "blockers": inventory.blockers
                + [Blocker(code="unapproved_image_absence")]
            }
        )
        for root in inventory.project_roots:
            failures[root.project_id].add("unapproved_image_absence")
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
        platform_pools=_platform_pools(
            cache, observations, inventory.blockers, authorized
        ),
        unattributed_images=sorted(unresolved, key=lambda k: k.model_dump_json()),
    )
