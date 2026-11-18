"""Current dependency closure proves terminal isolation, never image ownership."""


def dependencies(image):
    value = image.current_dependencies
    return [value.head, *[s.dependency for s in value.snapshots]]


def projection_matches(image, value):
    rows = {r.snapshot: r for r in value.snapshots}
    selected = image.source_snapshot_children
    if selected is None:
        return set(image.snapshots) == set(rows) and set(image.children) == {
            c for r in rows.values() for c in r.children
        }
    return (
        len(selected) == len({e.snapshot for e in selected})
        and set(image.snapshots) == {e.snapshot for e in selected if e.present}
        and set(image.children) == {c for e in selected for c in e.children}
        and all(
            e.snapshot.image_key == image.key
            and e.present == (e.snapshot in rows)
            and len(e.children) == len(set(e.children))
            and set(e.children)
            == (set(rows[e.snapshot].children) if e.present else set())
            for e in selected
        )
    )


def _complete(image, cutoff, boundary=False):
    if (
        image is None
        or image.current_dependencies is None
        or image.absence is not None
        or not _measurement_or_verified_dependency(image, cutoff, boundary)
        or image.collected_at < cutoff
        or (image.source_snapshot_children is not None and not boundary)
    ):
        return False
    rows = image.current_dependencies.snapshots
    return (
        _coverage_matches(image, boundary)
        and image.parent == image.current_dependencies.head.parent
        and len(image.snapshots) == len(set(image.snapshots))
        and len(image.children) == len(set(image.children))
        and len(rows) == len({r.snapshot for r in rows})
        and projection_matches(image, image.current_dependencies)
        and all(
            r.snapshot.image_key == image.key
            and len(r.children) == len(set(r.children))
            for r in rows
        )
        and all(
            (d.overlap_bytes > 0) == (d.parent is not None) for d in dependencies(image)
        )
    )


def _measurement_or_verified_dependency(image, cutoff, boundary):
    if image.measurement_status == "complete":
        return True
    verified = image.dependency_graph_verified_at
    return (
        not boundary
        and image.measurement_status == "timeout"
        and verified is not None
        and verified >= max(cutoff, image.collected_at)
    )


def _coverage_matches(image, boundary):
    selected = image.current_dependencies.cache_boundary
    if selected is None:
        return True
    return (
        boundary
        and len(selected) == len(set(selected))
        and image.source_snapshot_children is not None
        and set(selected) == {e.snapshot for e in image.source_snapshot_children}
    )


def _linked(parent, child, snapshot):
    return any(
        r.snapshot == snapshot and child.key in r.children
        for r in parent.current_dependencies.snapshots
    ) and any(d.parent == snapshot for d in dependencies(child))


def _edges_match(image, observations):
    for dependency in dependencies(image):
        if dependency.parent is not None:
            parent = observations.get(dependency.parent.image_key)
            if parent is None or not _linked(parent, image, dependency.parent):
                return False
    for row in image.current_dependencies.snapshots:
        if any(
            not _linked(image, observations[child], row.snapshot)
            for child in row.children
        ):
            return False
    return True


def _acyclic(parents, limit):
    # A topological reduction handles shared ancestors without mistaking the
    # reciprocal traversal of child metadata for a dependency cycle.
    pending = {k: set(v) for k, v in parents.items()}
    for _ in range(limit + 1):
        leaves = {k for k, v in pending.items() if not v}
        if not leaves:
            return not pending
        pending = {k: v - leaves for k, v in pending.items() if k not in leaves}
    return not pending


def _unseen_dependents(observations, reached, cache):
    for key, image in observations.items():
        if key in reached:
            continue
        parents = [image.parent]
        if image.current_dependencies is not None:
            parents.extend(d.parent for d in dependencies(image))
        if any(p is not None and p.image_key in reached - {cache} for p in parents):
            return True
    return False


def _binding_conflicts(reached, bindings):
    from app.services.native_clone_graph import binding_identity

    identities = {}
    for binding in bindings:
        if binding.image_key not in reached:
            continue
        identity = binding_identity(binding)
        if (
            binding.image_key in identities
            and identities[binding.image_key] != identity
        ):
            return True
        identities[binding.image_key] = identity
    return False


def isolated(observations, starts, cache, private, cutoff, limit=32, bindings=()):
    queue = [(k, 0) for k in starts]
    reached, parents = set(), {}
    while queue:
        key, depth = queue.pop(0)
        if key in reached:
            continue
        image = observations.get(key)
        if (
            key in private
            or depth > limit
            or len(reached) >= 10000
            or not _complete(image, cutoff, key == cache)
        ):
            return False
        reached.add(key)
        ancestors = {
            d.parent.image_key for d in dependencies(image) if d.parent is not None
        }
        parents[key] = ancestors
        if key == cache:
            children = {
                c for r in image.current_dependencies.snapshots for c in r.children
            } | set(image.children)
            if ancestors or children & private:
                return False
            continue
        queue.extend((k, depth + 1) for k in ancestors | set(image.children))
    return (
        not _binding_conflicts(reached, bindings)
        and not _unseen_dependents(observations, reached, cache)
        and _acyclic(parents, limit)
        and all(_edges_match(observations[k], observations) for k in reached - {cache})
    )
