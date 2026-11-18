"""Graph authority for separately retained CSI snapshot images and restores."""

from collections import defaultdict
from collections.abc import Hashable
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from app.schemas.kubevirt_storage import Blocker, ImageKey
from app.services.native_clone_graph import (
    _current_subject_matches,
    _record_parent,
    binding_identity,
)
from app.services.native_snapshot_custody import retired_ids


def proofs(facts):
    return [p for f in facts for p in f.snapshots]


def retired_images(facts):
    return {
        p.image for f in facts for p in f.snapshots if p.producer_id in retired_ids(f)
    }


def _retirements_match(facts, observations):
    for f in facts:
        by_id = {p.producer_id: p for p in f.snapshots}
        for retired in f.snapshot_retirements:
            proof = by_id.get(retired.producer_id)
            image = observations.get(proof.image) if proof else None
            if (
                image is None
                or image.absence is None
                or image.measurement_status != "complete"
            ):
                return False
            if TYPE_CHECKING:
                assert proof is not None
            if (
                image.absence.model_dump(include=set(ImageKey.model_fields))
                != proof.image.model_dump()
                or not image.absence.excludes(proof.image)
                or image.collected_at < retired.verified_at
            ):
                return False
    return True


def extra_edges(facts):
    snapshots, children = defaultdict(set), defaultdict(set)
    for f in facts:
        for proof in f.snapshots:
            if proof.producer_id not in retired_ids(f) and proof.parent is not None:
                snapshots[proof.source.image_key].add(proof.parent)
                children[proof.source.image_key].add(proof.image)
    return snapshots, children


def evidence_matches(evidence, proof):
    return (
        evidence.root == proof.source.root
        and evidence.snapshot_uid == proof.snapshot_uid
        and evidence.snapshot_name == proof.snapshot_name
        and evidence.content_uid == proof.content_uid
        and evidence.content_name == proof.content_name
        and evidence.csi_driver == proof.csi_driver
        and evidence.source_pvc_name == proof.source_pvc_name
        and evidence.deletion_policy == "Delete"
        and evidence.reference_snapshot_uid == proof.snapshot_uid
        and evidence.reference_snapshot_name == proof.snapshot_name
        and evidence.reference_namespace == proof.source.root.namespace
        and evidence.source_snapshot_handle_sha256 is None
        and evidence.source_volume_handle_sha256
        == sha256(proof.source.volume_handle.encode()).hexdigest()
        and evidence.snapshot_handle_sha256 == sha256(proof.handle.encode()).hexdigest()
    )


def _expected(facts, retired_bindings):
    parents, times = {}, {}
    snapshots, children = defaultdict(set), defaultdict(set)
    valid = True
    for f in facts:
        for p in f.snapshots:
            valid &= _record_parent(
                parents, times, p.source.image_key, p.source_parent, p.verified_at
            )
            if p.producer_id in retired_ids(f):
                continue
            valid &= _record_parent(parents, times, p.image, p.parent, p.verified_at)
            _snapshot_edges(p, snapshots, children)
        for p in f.snapshot_restores:
            if p.binding.pvc_uid in retired_bindings:
                continue
            valid &= _record_parent(
                parents, times, p.binding.image_key, p.parent, p.verified_at
            )
            if p.parent is not None:
                children[p.parent.image_key].add(p.binding.image_key)
    return parents, times, snapshots, children, valid


def _snapshot_edges(proof, snapshots, children):
    snapshots[proof.image].add(proof.snapshot)
    if proof.parent is not None:
        snapshots[proof.source.image_key].add(proof.parent)
        children[proof.source.image_key].add(proof.image)


def live_facts(facts, project_retirements):
    """Only a typed, independently verified terminal project proof removes edges."""
    from app.schemas.native_project_cleanup import ProjectRetirement

    # Evidence is frozen through Pydantic's ConfigDict and supplies __hash__.
    retired = {
        cast(Hashable, r.root)
        for r in project_retirements
        if isinstance(r, ProjectRetirement)
    }
    result = []
    for value in facts:
        root = getattr(getattr(value, "publication", None), "root", None)
        if root in retired:
            continue
        result.append(value)
    return result


def _unmatched_evidence_errors(inventory, all_proofs):
    return [
        Blocker(
            code="origin_snapshot_authority_unverified",
            project_id=e.root.project_id if e.root in inventory.project_roots else None,
        )
        for e in inventory.snapshot_evidence
        if not any(evidence_matches(e, p) for p in all_proofs)
    ]


def unresolved_producers(facts):
    for value in facts:
        resolved = {p.producer_id for p in value.snapshots}
        for producer in value.producers:
            if producer.kind == "VolumeSnapshot" and producer.id not in resolved:
                yield value, producer


def _producer_owners_match(facts):
    from app.services.kubevirt_storage_enrollment import root_identity

    return all(
        p.source_binding is None
        or root_identity(p.source_binding.root) == root_identity(f.publication.root)
        for f, p in unresolved_producers(facts)
    )


def _source_lineage(inventory, facts, producer):
    from app.services.kubevirt_storage_enrollment import root_identity

    source = producer.source_binding
    root = facts.publication.root
    # resolve() already pins every receipt root to the full enrollment. A
    # project-scoped inventory deliberately contains only the retiring root.
    if (
        source is None
        or root_identity(source.root) != root_identity(root)
        or binding_identity(source) not in [binding_identity(b) for b in facts.bindings]
    ):
        return None
    matches = [
        p
        for p in facts.ancestry
        if binding_identity(p.binding) == binding_identity(source)
    ]
    if len(matches) != 1 or binding_identity(matches[0].source) not in [
        binding_identity(b) for b in inventory.cache_bindings
    ]:
        return None
    return matches[0]


def _independent_terminal(
    inventory, observations, facts, producer, terminal, all_facts
):
    lineage = _source_lineage(inventory, facts, producer)
    if lineage is None or facts.publication.root.project_id == terminal.root.project_id:
        return False
    source = lineage.binding
    keys = {source.image_key, lineage.intermediate, lineage.source.image_key}
    if keys & set(terminal.private_images):
        return False
    # No second PVC/owner may claim a member of the retained source chain.
    bindings = (
        [b for f in all_facts for b in f.bindings]
        + inventory.volume_bindings
        + inventory.retained_bindings
    )
    if any(
        b.image_key in keys and binding_identity(b) != binding_identity(source)
        for b in bindings
    ):
        return False
    cutoff = max(terminal.observed_at, lineage.verified_at)
    from app.services.native_snapshot_isolation import isolated

    if isolated(
        observations,
        keys,
        lineage.source.image_key,
        set(terminal.private_images),
        cutoff,
        bindings=bindings,
    ):
        return True
    if any(
        getattr(observations.get(key), "current_dependencies", None) is not None
        for key in keys
    ):
        return False
    if not _current_subject_matches(
        observations.get(source.image_key), lineage.head_parent, set(), set(), cutoff
    ):
        return False
    if not _current_subject_matches(
        observations.get(lineage.intermediate),
        lineage.intermediate_parent,
        {lineage.head_parent},
        {source.image_key},
        cutoff,
    ):
        return False
    cache = observations.get(lineage.source.image_key)
    # Shared cache ancestry survives retirement; its other consumers need not retire.
    return (
        cache is not None
        and cache.measurement_status == "complete"
        and cache.absence is None
        and cache.parent is None
        and cache.collected_at >= cutoff
        and not set(cache.children) & set(terminal.private_images)
    )


def _unresolved_errors(inventory, observations, facts, terminal):
    from app.schemas.native_project_cleanup import ProjectRetirement

    unresolved = list(unresolved_producers(facts))
    if not unresolved:
        return []
    safe = {
        r.root.project_id
        for r in terminal
        if isinstance(r, ProjectRetirement)
        and all(
            _independent_terminal(inventory, observations, f, p, r, facts)
            for f, p in unresolved
        )
    }
    if not safe:
        return [Blocker(code="origin_snapshot_authority_unverified")]
    # Preserve the previous hold for every live root, including the affected owner.
    return [
        Blocker(code="origin_snapshot_authority_unverified", project_id=r.project_id)
        for r in inventory.project_roots
        if r.project_id not in safe
    ]


def authorize(
    inventory, observations, facts, binding_retirements=(), project_retirements=()
):
    all_proofs = proofs(facts)
    bound = inventory.volume_bindings + inventory.retained_bindings
    occupied = {b.image_key for b in bound + inventory.cache_bindings}
    owners = defaultdict(set)
    for p in all_proofs:
        owners[p.image].add(p.source.root.project_id)
    invalid = any(
        p.image in occupied or p.image == p.source.image_key for p in all_proofs
    )
    invalid |= len({p.image for p in all_proofs}) != len(all_proofs)
    invalid |= any(len(v) != 1 for v in owners.values())
    invalid |= not _producer_owners_match(facts)
    live = live_facts(facts, project_retirements)
    selected = [
        f
        for f in live
        if any(
            inventory.project_scope in (None, p.source.root.project_id)
            for p in f.snapshots
        )
    ]
    retired = {r.binding.pvc_uid for r in binding_retirements if r.objects_absent}
    parents, times, snaps, children, valid = _expected(selected, retired)
    invalid |= not valid
    invalid |= not _retirements_match(selected, observations)
    identities = [binding_identity(b) for b in bound]
    invalid |= any(
        binding_identity(p.source) not in identities for p in proofs(selected)
    )
    invalid |= any(
        not _current_subject_matches(
            observations.get(key), parent, snaps[key], children[key], times[key]
        )
        for key, parent in parents.items()
    )
    errors = _unmatched_evidence_errors(inventory, all_proofs)
    errors += _unresolved_errors(inventory, observations, live, project_retirements)
    if invalid or any(e.project_id is None for e in errors):
        return set(), [Blocker(code="origin_snapshot_authority_unverified"), *errors]
    return set(parents), errors


def add_ownership(
    inventory,
    observations,
    facts,
    ownership,
    project_nodes,
    failures,
    cache,
    limit,
    authorized,
):
    from app.services.kubevirt_storage_graph import _walk

    for p in proofs(facts):
        project = p.source.root.project_id
        if project not in project_nodes:
            continue
        nodes, errors = _walk(p.image, observations, cache, limit, authorized)
        project_nodes[project] |= nodes
        failures[project] |= errors
        for key in nodes - cache:
            ownership[key].add(project)


def resolve(db, manifest, *, retired=()):
    """Current protected facts only; uploaded reports cannot supply authority."""
    from app.models.native_origin_custody import (
        NativeOriginCustodyCurrent,
        NativeOriginCustodyReceipt,
    )
    from app.models.project import Project
    from app.services import native_origin_custody as custody
    from app.services.kubevirt_storage_enrollment import root_key

    result = []
    for pointer in db.query(NativeOriginCustodyCurrent):
        row = db.get(NativeOriginCustodyReceipt, pointer.receipt_id)
        if row.domain_id != str(manifest.pins.domain_id):
            continue
        value = custody._receipt(row)
        if not value.producers or root_key(value.publication.root) in retired:
            continue
        _, value, root = cast(
            custody.RetainedCustody,
            custody.retained(db, db.get(Project, row.project_id)),
        )
        custody.require(
            root in manifest.pins.project_roots
            and value.publication.enrollment.target_id == manifest.request.target_id,
            "Snapshot protected domain changed",
        )
        result.append(value)
    return result
