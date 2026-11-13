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
        and len(image.snapshots) == len(snapshots)
        and len(image.children) == len(children)
        and image.measurement_status == "complete"
        and image.absence is None
        and image.collected_at >= verified_at
    )


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
    invalid = False
    for proof in proofs:
        head, middle, source = (
            proof.binding.image_key,
            proof.intermediate,
            proof.source.image_key,
        )
        private.add(middle)
        if (
            binding_identity(proof.binding) not in bindings
            or binding_identity(proof.source) not in caches
        ):
            invalid = True
        for key, parent in (
            (head, proof.head_parent),
            (middle, proof.intermediate_parent),
            (source, None),
        ):
            if key in expected and expected[key] != parent:
                invalid = True
            expected[key] = parent
            verified[key] = max(verified.get(key, proof.verified_at), proof.verified_at)
        snapshots[middle].add(proof.head_parent)
        snapshots[source].add(proof.intermediate_parent)
        snapshots[head]  # Head must stay snapshot-free.
        children[middle].add(head)
        children[source].add(middle)
        children[head]
    for key, parent in expected.items():
        if not _current_subject_matches(
            observations.get(key), parent, snapshots[key], children[key], verified[key]
        ):
            invalid = True
    blockers = [Blocker(code="managed_clone_lineage_unverified")] if invalid else []
    return (set() if invalid else set(snapshots)), private, blockers
