"""Exact current graph checks for independently resolved managed clone proofs."""

from collections import defaultdict

from app.schemas.kubevirt_storage import Blocker


def binding_identity(binding):
    value = binding.model_dump(
        mode="json", exclude={"provisioned_bytes", "owner_reference_uids"}
    )
    if "root" in value:
        value["root"].pop("deployment_generation", None)
    return value


def _current_subject_matches(image, parent, snapshots, children, verified_at):
    if image is None:
        return False
    return (
        image.parent == parent
        and set(image.snapshots) == snapshots
        and set(image.children) == children
        and len(image.snapshots) == len(set(image.snapshots))
        and len(image.children) == len(children)
        and image.measurement_status == "complete"
        and image.absence is None
        and image.collected_at >= verified_at
    )


def _retired_clone(inventory, observations, proof):
    private = {proof.binding.image_key, proof.intermediate}
    root = proof.binding.root.model_dump(exclude={"deployment_generation"})
    matches = [
        r
        for r in inventory.retiring_roots
        if r.root.model_dump(exclude={"deployment_generation"}) == root
        and r.objects_absent
        and private <= set(r.owned_images)
        and proof.source.image_key not in r.owned_images
    ]
    if len(matches) != 1:
        return False
    cutoff = max(matches[0].observed_at, proof.verified_at)
    return all(
        (image := observations.get(key)) is not None
        and image.absence is not None
        and image.measurement_status == "complete"
        and image.collected_at >= cutoff
        for key in private
    )


def _record_parent(expected, verified, key, parent, verified_at):
    compatible = key not in expected or expected[key] == parent
    expected[key] = parent
    verified[key] = max(verified.get(key, verified_at), verified_at)
    return compatible


def _scoped_proofs(inventory, proofs):
    """Keep sibling source edges, without requiring sibling private observations."""
    scope = inventory.project_scope
    if scope is None:
        return proofs, False
    sources = {p.source.image_key for p in proofs if p.binding.root.project_id == scope}
    relevant = [p for p in proofs if p.source.image_key in sources]
    owners = defaultdict(set)
    for proof in proofs:
        for key in (proof.binding.image_key, proof.intermediate):
            owners[key].add(proof.binding.root.project_id)
    invalid = any(
        scope in projects and len(projects) != 1 for projects in owners.values()
    )
    invalid |= bool(sources & owners.keys())
    invalid |= any(scope in owners.get(p.source.image_key, set()) for p in proofs)
    return relevant, invalid


def _lineage_expectations(
    inventory, observations, proof, expected, verified, snapshots, children, own
):
    head, middle, source = (
        proof.binding.image_key,
        proof.intermediate,
        proof.source.image_key,
    )
    valid = _record_parent(expected, verified, source, None, proof.verified_at)
    if own and _retired_clone(inventory, observations, proof):
        # Only confirmed absence of both private images retires their edges.
        # The obsolete source snapshot must disappear too.
        return valid
    snapshots[source].add(proof.intermediate_parent)
    children[source].add(middle)
    if own:
        for key, parent in (
            (head, proof.head_parent),
            (middle, proof.intermediate_parent),
        ):
            valid &= _record_parent(expected, verified, key, parent, proof.verified_at)
        snapshots[middle].add(proof.head_parent)
        children[middle].add(head)
        snapshots[head]  # Head must stay snapshot-free.
        children[head]
    return valid


def authorize_graph(inventory, observations, proofs):
    """Return exact snapshot-bearing subjects or a conservative global blocker.

    Proofs are a separate trusted argument, never a report field. The caller must
    resolve them through the protected journal before invoking this function.
    """
    snapshots, children = defaultdict(set), defaultdict(set)
    expected, private = {}, set()
    verified = {}
    bindings = [
        binding_identity(b)
        for b in inventory.volume_bindings + inventory.retained_bindings
    ]
    caches = [binding_identity(b) for b in inventory.cache_bindings]
    proofs, invalid = _scoped_proofs(inventory, proofs)
    for proof in proofs:
        own = inventory.project_scope in (None, proof.binding.root.project_id)
        if own:
            private.add(proof.intermediate)
        if (
            own and binding_identity(proof.binding) not in bindings
        ) or binding_identity(proof.source) not in caches:
            invalid = True
        invalid |= not _lineage_expectations(
            inventory, observations, proof, expected, verified, snapshots, children, own
        )
    for key, parent in expected.items():
        if not _current_subject_matches(
            observations.get(key),
            parent,
            snapshots[key],
            children[key],
            verified[key],
        ):
            invalid = True
    blockers = [Blocker(code="managed_clone_lineage_unverified")] if invalid else []
    return (set() if invalid else set(expected)), private, blockers


def cleanup_owned_images(inventory, images, proofs, project_id):
    """Use accounting's verified ownership, including private CDI intermediates."""
    from app.services.kubevirt_storage_graph import _ownership, attribute_graph
    from app.services.quota_accounting import AccountingUnavailable

    graph = attribute_graph(inventory, images, clone_lineages=proofs)
    if any(p.coverage != "complete" for p in graph.projects):
        raise AccountingUnavailable("Native cleanup clone graph is incomplete")
    observations = {i.key: i for i in images}
    authorized, private, _ = authorize_graph(inventory, observations, proofs)
    cache = {b.image_key for b in inventory.cache_bindings} - private
    _, nodes, _ = _ownership(inventory, observations, cache, 32, authorized)
    return nodes[project_id] - cache
