"""Resolve protected retirement authority separately from untrusted scan evidence."""

from app.schemas.kubevirt_storage import (
    BindingRetirementEvidence,
    BindingRetirementReference,
)
from app.services.kubevirt_storage_enrollment import root_identity, root_key, utc


def protected_inputs(db, manifest, *, project_id=None, lineages=()):
    from app.services import native_binding_retirement as retirement
    from app.services.native_custody_reconciliation import retirement_references
    from app.services.native_origin_custody import (
        retirement_references as origin_references,
    )
    from app.services.quota_cleanup import settled_root_keys

    settled = settled_root_keys(db)
    values = []
    references = retirement_references(
        db, str(manifest.pins.domain_id)
    ) + origin_references(db, str(manifest.pins.domain_id))
    seen = set()
    for raw in references:
        reference = BindingRetirementReference.model_validate(raw)
        if reference.receipt_id in seen:
            raise ValueError("Conflicting duplicate retirement selection")
        seen.add(reference.receipt_id)
        value = retirement.resolve(db, reference)
        if root_key(value.prepared.root) not in settled:
            values.append((reference, value))
    return _scoped_values(values, project_id, lineages)


def _scoped_values(values, project_id, lineages):
    if project_id is None:
        return values
    sources = {
        p.source.image_key
        for p in lineages
        if str(p.binding.root.project_id) == str(project_id)
    }
    sources.update(
        v.prepared.ancestry.source.image_key
        for _, v in values
        if str(v.prepared.root.project_id) == str(project_id)
        and v.prepared.ancestry is not None
    )
    return [
        (ref, value)
        for ref, value in values
        if str(value.prepared.root.project_id) == str(project_id)
        or (
            value.prepared.ancestry is not None
            and value.prepared.ancestry.source.image_key in sources
        )
    ]


def _whole_root(inventory, prepared):
    from app.services.native_binding_retirement import private_images

    private = set(private_images(prepared))
    return any(
        root_identity(r.root) == root_identity(prepared.root)
        and r.objects_absent
        and private <= set(r.owned_images)
        and (
            prepared.ancestry is None
            or prepared.ancestry.source.image_key not in r.owned_images
        )
        for r in inventory.retiring_roots
    )


def scan_evidence(values, inventory, kube, backend, roots):
    from app.services.native_binding_retirement import verify_many

    selected = [
        (ref, value)
        for ref, value in values
        if not _whole_root(inventory, value.prepared)
    ]
    if not selected:
        return []
    # A retired binding remains a liability on every subsequent scan.
    verified = verify_many(
        [value.prepared for _, value in selected],
        kube,
        backend,
        roots={str(root.project_id): root for root in roots},
    )
    return [
        BindingRetirementEvidence(reference=reference, observation=value.observation)
        for (reference, _), value in zip(selected, verified, strict=True)
    ]


def report_authority(db, manifest, report, *, lineages=()):
    from app.services.native_binding_retirement import validate_observation

    values = protected_inputs(
        db, manifest, project_id=report.project_scope, lineages=lineages
    )
    supplied = {str(e.reference.receipt_id): e for e in report.binding_retirements}
    if len(supplied) != len(report.binding_retirements):
        raise ValueError("Duplicate binding retirement evidence")
    observations, lineages, required = [], [], set()
    for reference, value in values:
        prepared = value.prepared
        if prepared.ancestry is not None:
            lineages.append(prepared.ancestry)
        if _whole_root(report, prepared):
            continue
        key = str(reference.receipt_id)
        required.add(key)
        evidence = supplied.get(key)
        if evidence is None or evidence.reference != reference:
            raise ValueError("Selected binding retirement evidence missing or changed")
        observation = evidence.observation
        validate_observation(prepared, observation)
        if observation.root not in manifest.pins.project_roots:
            raise ValueError("Binding retirement root is not currently pinned")
        if (
            not utc(report.started_at)
            <= utc(observation.verified_at)
            <= utc(report.completed_at)
        ):
            raise ValueError("Binding retirement evidence is outside this scan")
        if any(
            utc(p.collected_at) > utc(report.completed_at)
            for p in observation.image_absence
        ):
            raise ValueError("Binding retirement absence is after scan completion")
        observations.append(observation)
    if set(supplied) != required:
        raise ValueError("Unselected binding retirement evidence")
    return observations, lineages
