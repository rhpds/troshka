"""Exact current graph checks for independently resolved managed clone proofs."""

from collections import defaultdict
from typing import Any, cast

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


def _retired_clone(inventory, observations, proof, binding_retirements=()):
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
    retired = [
        r
        for r in binding_retirements
        if binding_identity(r.binding) == binding_identity(proof.binding)
        and set(r.private_images) == private
        and r.objects_absent
    ]
    if len(matches) == 1:
        cutoff = max(matches[0].observed_at, proof.verified_at)
    elif len(retired) == 1:
        cutoff = max(retired[0].verified_at, proof.verified_at)
    else:
        return False
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
    """Select owned chains while retaining cross-project identity collision checks."""
    scope = inventory.project_scope
    if scope is None:
        return proofs, False
    sources = {p.source.image_key for p in proofs if p.binding.root.project_id == scope}
    relevant = [p for p in proofs if p.binding.root.project_id == scope]
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
    inventory,
    observations,
    proof,
    expected,
    verified,
    snapshots,
    children,
    own,
    binding_retirements=(),
    source_edges=None,
):
    head, middle, source = (
        proof.binding.image_key,
        proof.intermediate,
        proof.source.image_key,
    )
    retired = [
        r
        for r in binding_retirements
        if binding_identity(r.binding) == binding_identity(proof.binding)
    ]
    cutoff = max([proof.verified_at, *[r.verified_at for r in retired]])
    valid = _record_parent(expected, verified, source, None, cutoff)
    if own and _retired_clone(inventory, observations, proof, binding_retirements):
        # Only confirmed absence of both private images retires their edges.
        # The obsolete source snapshot must disappear too.
        desired = None
    else:
        desired = middle
    source_edges = cast(dict[Any, Any], source_edges)
    edge = proof.intermediate_parent
    if edge in source_edges and source_edges[edge] != desired:
        valid = False
    source_edges[edge] = desired
    if desired is None:
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


def _source_matches(image, selected, verified_at):
    if (
        image is None
        or image.parent is not None
        or image.absence is not None
        or image.measurement_status != "complete"
        or image.collected_at < verified_at
        or image.source_snapshot_children is None
    ):
        return False
    entries = image.source_snapshot_children
    indexed = {e.snapshot: e for e in entries}
    if len(indexed) != len(entries):
        return False
    # Projection fields must agree with their complete per-snapshot child sets.
    if (
        any(
            e.snapshot.image_key != image.key
            or (not e.present and e.children)
            or len(e.children) != len(set(e.children))
            for e in entries
        )
        or set(image.snapshots) != {e.snapshot for e in entries if e.present}
        or set(image.children) != {c for e in entries for c in e.children}
        or len(image.snapshots) != len(set(image.snapshots))
        or len(image.children) != len(set(image.children))
    ):
        return False
    for snapshot, child in selected.items():
        current = indexed.get(snapshot)
        if current is None or current.present != (child is not None):
            return False
        if set(current.children) != ({child} if child is not None else set()):
            return False
    return True


def _additional_edges(expected, sources, snapshots, children, extra):
    if extra is None:
        return
    extra_snapshots, extra_children = extra
    for key in set(expected) - sources:
        snapshots[key].update(extra_snapshots.get(key, ()))
        children[key].update(extra_children.get(key, ()))


def authorize_graph(
    inventory, observations, proofs, binding_retirements=(), *, snapshot_edges=None
):
    """Return exact snapshot-bearing subjects or a conservative global blocker.

    Proofs are a separate trusted argument, never a report field. The caller must
    resolve them through the protected journal before invoking this function.
    """
    snapshots, children = defaultdict(set), defaultdict(set)
    expected, private = {}, set()
    verified, source_edges = {}, {}
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
            inventory,
            observations,
            proof,
            expected,
            verified,
            snapshots,
            children,
            own,
            binding_retirements,
            source_edges,
        )
    sources = {p.source.image_key for p in proofs}
    _additional_edges(expected, sources, snapshots, children, snapshot_edges)
    missing = False
    for key, parent in expected.items():
        if key in sources:
            image = observations.get(key)
            missing |= image is not None and image.source_snapshot_children is None
            invalid |= not _source_matches(
                image,
                {
                    snapshot: child
                    for snapshot, child in source_edges.items()
                    if snapshot.image_key == key
                },
                verified[key],
            )
            continue
        if not _current_subject_matches(
            observations.get(key),
            parent,
            snapshots[key],
            children[key],
            verified[key],
        ):
            invalid = True
    blockers = [Blocker(code="managed_clone_lineage_unverified")] if invalid else []
    if missing:
        blockers.append(Blocker(code="shared_source_snapshot_evidence_missing"))
    return (set() if invalid else set(expected)), private, blockers


def cleanup_owned_images(inventory, images, proofs, project_id, binding_retirements=()):
    """Use accounting's verified ownership, including private CDI intermediates."""
    from app.services.kubevirt_storage_graph import _ownership, attribute_graph
    from app.services.quota_accounting import AccountingUnavailable

    graph = attribute_graph(
        inventory,
        images,
        clone_lineages=proofs,
        binding_retirements=binding_retirements,
    )
    if any(p.coverage != "complete" for p in graph.projects):
        raise AccountingUnavailable("Native cleanup clone graph is incomplete")
    observations = {i.key: i for i in images}
    authorized, private, _ = authorize_graph(
        inventory, observations, proofs, binding_retirements
    )
    cache = {b.image_key for b in inventory.cache_bindings} - private
    _, nodes, _ = _ownership(inventory, observations, cache, 32, authorized)
    retired = {
        key
        for r in binding_retirements
        if r.root.project_id == project_id
        for key in r.private_images
    }
    return (nodes[project_id] | retired) - cache
