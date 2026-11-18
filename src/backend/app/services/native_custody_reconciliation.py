"""Reviewed append-only custody transitions. Provider I/O remains online.verify()."""

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from app.models.native_custody import (
    NativeCustodyReceipt,
    NativeCustodyReconciliationRequest,
    NativeCustodyRequest,
    NativeCustodySuccessor,
)
from app.models.project import Project
from app.models.quota import QuotaAction
from app.models.workload_run import WorkloadRun
from app.services import native_online_enrollment as online

EffectiveCustody = tuple[
    NativeCustodyReceipt | NativeCustodySuccessor, online.VerifiedCustody
]

MAX_SUCCESSORS = 32
require = online._require
digest = online.enrollment.digest


def _evidence(receipt, project_id):
    value = online._project_evidence(receipt, project_id)
    value["authority"] = (
        "accounting_only"
        if project_id in receipt.prepared.accounting_only_project_ids
        else "cleanup"
    )
    value["identity"] = next(
        p.model_dump(mode="json", exclude={"updated_at"})
        for p in receipt.prepared.request.projects
        if str(p.project_id) == project_id
    )
    value["root"] = next(
        r.model_dump(mode="json")
        for r in receipt.enrollment.pins.project_roots
        if str(r.project_id) == project_id
    )
    return value


def _immutable(before, after, *, storage_verified=False):
    keys = ("target", "domain", "identity")
    if not storage_verified:
        keys += ("root", "bindings", "ancestry")
    for key in keys:
        require(
            before[key] == after[key],
            f"Custody {key} changed; reconciliation unsupported",
        )


def _storage_values(receipt, project_id, field):
    values = []
    for item in getattr(receipt, field):
        binding = item if field == "bindings" else item.binding
        if str(binding.root.project_id) != project_id:
            continue
        values.append(_normalized_storage(item, field))
    return {digest(v): v for v in values}


def _normalized_storage(item, field):
    value = item.model_dump(mode="json")
    value.pop("verified_at", None)
    binding = value if field == "bindings" else value["binding"]
    binding["root"].pop("deployment_generation", None)
    return value


def _references(values):
    if not values:
        return []
    from app.schemas.native_binding_retirement import BindingRetirementReference

    refs = [
        BindingRetirementReference.model_validate(v).model_dump(mode="json")
        for v in values
    ]
    require(
        len({r["receipt_id"] for r in refs}) == len(refs),
        "Duplicate binding retirement selection",
    )
    return sorted(refs, key=lambda r: r["receipt_id"])


def _resolve_retirements(db, refs):
    if not refs:
        return []
    from app.services.native_binding_retirement import resolve

    return [(ref, resolve(db, ref)) for ref in refs]


def _selected_retirements(db, refs, row, after):
    proofs = _validate_retirement_observations(db, after, refs)
    selected = []
    for ref, proof in proofs:
        prepared = proof.prepared
        if str(prepared.binding.root.project_id) != row.project_id:
            continue
        require(
            str(prepared.source_custody_id) == row.id
            and prepared.source_custody_digest == row.digest,
            "Binding retirement custody predecessor changed",
        )
        require(
            prepared.root.model_dump(mode="json")
            == _evidence(after, row.project_id)["root"],
            "Binding retirement observed root changed",
        )
        selected.append((ref, proof))
    return selected


def _validate_retirement_observations(db, receipt, refs, *, current=False):
    proofs = _resolve_retirements(db, refs)
    require(
        _references([v["reference"] for v in receipt.prepared.binding_retirements])
        == list(refs),
        "Prepared retirement selection changed",
    )
    require(
        _references([v.reference for v in receipt.binding_retirements]) == list(refs),
        "Verified retirement selection changed",
    )
    if not refs:
        return proofs
    from app.services.native_binding_retirement import validate_observation

    inputs = {
        v["reference"]["receipt_id"]: v["prepared"]
        for v in receipt.prepared.binding_retirements
    }
    observations = {
        str(v.reference.receipt_id): v.observation for v in receipt.binding_retirements
    }
    for ref, proof in proofs:
        require(
            digest(inputs[ref["receipt_id"]]) == digest(proof.prepared),
            "Prepared retirement evidence changed",
        )
        observation = observations[ref["receipt_id"]]
        validate_observation(proof.prepared, observation)
        if current:
            now = online.datetime.now(online.UTC)
            require(
                proof.prepared.prepared_at
                <= observation.verified_at
                <= receipt.enrollment.verified_at
                <= now
                and all(
                    p.collected_at <= receipt.enrollment.verified_at
                    for p in observation.image_absence
                ),
                "Binding retirement observation is outside the verified preparation interval",
            )
    return proofs


def _retired_storage(retirements, field):
    result = {}
    for _, proof in retirements:
        item = (
            proof.prepared.binding if field == "bindings" else proof.prepared.ancestry
        )
        if item is not None:
            value = _normalized_storage(item, field)
            result[digest(value)] = value
    return result


def _storage_changes(before, after, project_id, retirements=()):
    first, last = _evidence(before, project_id), _evidence(after, project_id)
    old_root, new_root = deepcopy(first["root"]), deepcopy(last["root"])
    old_generation = old_root.pop("deployment_generation", None)
    new_generation = new_root.pop("deployment_generation", None)
    require(old_root == new_root, "Custody root identity changed")
    if old_generation != new_generation:
        require(
            type(old_generation) is int
            and type(new_generation) is int
            and new_generation > old_generation,
            "Custody root generation must increase",
        )
    change = {"root_before": first["root"], "root_after": last["root"]}
    for field in ("bindings", "ancestry"):
        previous, current = _storage_values(before, project_id, field), _storage_values(
            after, project_id, field
        )
        retired = _retired_storage(retirements, field)
        require(
            previous.keys() - current.keys() == retired.keys(),
            f"Custody {field} cannot drop or replace retained storage without exact retirement",
        )
        change["added_" + field] = [
            current[k] for k in sorted(current.keys() - previous.keys())
        ]
        if retirements:
            change["retired_" + field] = [retired[k] for k in sorted(retired)]
    if retirements:
        change["retirements"] = [ref for ref, _ in retirements]
    return change


def _planned_enrollment(db, predecessor, before, after):
    """Verify the exact proposed extension while it still has a current predecessor."""
    if before.enrollment.pins == after.enrollment.pins:
        return
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment

    domain = db.get(NativeCollectionDomain, str(after.enrollment.pins.domain_id))
    require(domain is not None, "Custody enrollment domain missing")
    current = db.get(NativeEnrollment, domain.enrollment_id)
    manifest = online.enrollment.validated_enrollment(current)
    online.enrollment.retained_enrollment(
        db,
        domain,
        {
            "enrollment_id": predecessor.payload["enrollment_id"],
            "root": _evidence(before, predecessor.project_id)["root"],
        },
    )
    if manifest.pins == after.enrollment.pins:
        return
    extension = after.enrollment.pins.domain_extension
    require(
        extension is not None
        and str(extension.previous_enrollment_id) == current.id
        and extension.previous_enrollment_digest == current.digest,
        "Custody extension predecessor changed",
    )
    online.enrollment.validate_domain_extension(manifest, after.enrollment)


def _historical_enrollment(db, predecessor, before, successor, after):
    previous_id, current_id = (
        predecessor.payload["enrollment_id"],
        successor.payload["enrollment_id"],
    )
    if previous_id == current_id:
        require(
            before.enrollment.pins == after.enrollment.pins,
            "Custody enrollment identity changed",
        )
        return
    from app.models.kubevirt_storage import NativeEnrollment

    try:
        UUID(current_id)
    except (ValueError, TypeError) as exc:
        raise online.AccountingUnavailable(
            "Custody enrollment identity changed"
        ) from exc
    current = db.get(NativeEnrollment, current_id)
    require(
        current is not None
        and current.domain_id == str(after.enrollment.pins.domain_id),
        "Custody enrollment identity changed",
    )
    if TYPE_CHECKING:
        assert current is not None
    manifest = online.enrollment.validated_enrollment(current)
    require(
        current.digest == successor.payload.get("enrollment_digest")
        and manifest.pins == after.enrollment.pins,
        "Custody enrollment manifest changed",
    )

    def identities(m):
        return sorted(
            digest(p.model_dump(mode="json", exclude={"updated_at"}))
            for p in m.request.projects
        )

    require(
        manifest.request.target_id == after.enrollment.request.target_id
        and identities(manifest) == identities(after.enrollment),
        "Custody enrollment correspondence changed",
    )
    domain = SimpleNamespace(id=current.domain_id, enrollment_id=current.id)
    online.enrollment.retained_enrollment(
        db,
        domain,
        {
            "enrollment_id": previous_id,
            "root": _evidence(before, predecessor.project_id)["root"],
        },
    )


def _project_transition(
    db, row, before, after, selected, *, successor=None, binding_retirements=()
):
    value = {
        "predecessor": {"id": row.id, "digest": row.digest},
        "before": _evidence(before, row.project_id),
        "after": _evidence(after, row.project_id),
    }
    changed = any(
        value["before"][k] != value["after"][k]
        for k in ("root", "bindings", "ancestry")
    )
    retirements = _selected_retirements(db, binding_retirements, row, after)
    if changed or retirements:
        value["storage_transition"] = _storage_changes(
            before, after, row.project_id, retirements
        )
    if successor is None:
        _planned_enrollment(db, row, before, after)
    else:
        _historical_enrollment(db, row, before, successor, after)
    _transition(value, row.project_id, selected, storage_verified=changed)
    return value


def _request(db, request_id):
    row = db.get(NativeCustodyReconciliationRequest, request_id)
    require(
        row is not None and row.digest == digest(row.payload),
        "Protected reconciliation request missing or changed",
    )
    if TYPE_CHECKING:
        assert row is not None
    return row.payload


def _validate_successor(db, predecessor, before, successor):
    require(
        successor.project_id == predecessor.project_id
        and successor.predecessor_digest == predecessor.digest,
        "Custody predecessor identity or digest changed",
    )
    after = online._validated(successor.payload["receipt"])
    require(after.digest == successor.digest, "Stored successor digest changed")
    review = successor.payload["review"]
    require(
        digest(review) == successor.payload["review_digest"],
        "Custody reviewed transition digest changed",
    )
    request = _request(db, review["request_id"])
    project_id = successor.project_id
    expected = {"id": predecessor.id, "digest": predecessor.digest}
    require(
        request["predecessors"][project_id] == expected
        and request["prepared_digest"] == digest(after.prepared)
        and review["reason"] == request["reason"]
        and review["cleanup_authority_project_ids"]
        == request["cleanup_authority_project_ids"]
        and review["receipt_digest"] == after.digest
        and review.get("binding_retirements", [])
        == request.get("binding_retirements", [])
        and review["projects"][project_id]
        == _project_transition(
            db,
            predecessor,
            before,
            after,
            request["cleanup_authority_project_ids"],
            successor=successor,
            binding_retirements=request.get("binding_retirements", []),
        ),
        "Custody reviewed transition changed",
    )
    return after


def effective(db, project_id):
    """Resolve a bounded validated chain rooted in the immutable original row."""
    row = db.query(NativeCustodyReceipt).filter_by(project_id=project_id).one_or_none()
    if row is None:
        return None
    receipt = online._validated(row.payload["receipt"])
    require(receipt.digest == row.digest, "Stored custody digest changed")
    seen = {row.id}
    for _ in range(MAX_SUCCESSORS + 1):
        successor = (
            db.query(NativeCustodySuccessor)
            .filter_by(predecessor_id=row.id)
            .one_or_none()
        )
        if successor is None:
            return row, receipt
        require(
            len(seen) <= MAX_SUCCESSORS and successor.id not in seen,
            "Custody successor chain limit or cycle",
        )
        receipt = _validate_successor(db, row, receipt, successor)
        seen.add(successor.id)
        row = successor
    raise AssertionError("unreachable custody chain bound")


def retirement_references(db, domain_id, project_id=None):
    """Return only retirement selections consumed by validated reachable custody."""
    references = []
    query = db.query(NativeCustodyReceipt)
    if project_id is not None:
        query = query.filter_by(project_id=str(project_id))
    for original in query:
        if original.payload["receipt"]["enrollment"]["pins"]["domain_id"] != str(
            domain_id
        ):
            continue
        effective(db, original.project_id)
        row = original
        for _ in range(MAX_SUCCESSORS):
            row = (
                db.query(NativeCustodySuccessor)
                .filter_by(predecessor_id=row.id)
                .one_or_none()
            )
            if row is None:
                break
            transition = row.payload["review"]["projects"][original.project_id]
            references.extend(
                transition.get("storage_transition", {}).get("retirements", [])
            )
    return _references(references)


def _idle(db, project, accounting_only):
    online._legacy(db, project, accounting_only=accounting_only)
    require(
        not db.query(QuotaAction)
        .filter(
            QuotaAction.project_id == project.id,
            QuotaAction.state.in_(["pending", "issued"]),
        )
        .first(),
        "Active custody action requires settlement",
    )
    require(
        not db.query(WorkloadRun)
        .filter(
            WorkloadRun.project_id == project.id,
            WorkloadRun.status.in_(["pending", "running"]),
        )
        .first(),
        "In-flight workload prevents custody reconciliation",
    )
    _queue_idle(project.id)


def _queue_idle(project_id):
    from app.core import redis

    # Do not use get_job_info: its zombie recovery mutates tracking references.
    require(redis.is_redis_available(), "Project job tracking unavailable")
    try:
        reference = redis.get_redis().get(f"job:project:{project_id}")
    except Exception as exc:
        raise online.AccountingUnavailable("Project job tracking unavailable") from exc
    require(not reference, "In-flight project work prevents custody reconciliation")


def prepare(
    db, project_ids, *, cleanup_authority_project_ids=(), reason, binding_retirements=()
):
    online._disabled()
    selected = sorted(set(cleanup_authority_project_ids))
    require(
        bool(reason.strip()) and len(reason) <= 4096, "Reconciliation reason required"
    )
    require(
        set(selected) <= set(project_ids),
        "Cleanup selection names an unprepared project",
    )
    predecessors, accounting_only = {}, []
    for project_id in project_ids:
        project = db.get(Project, project_id)
        previous = online.retained(db, project, require_topology=False)
        require(previous is not None, "Reconciliation requires existing custody")
        if TYPE_CHECKING:
            assert previous is not None
        row, receipt, _ = previous
        restricted = (
            project_id in receipt.prepared.accounting_only_project_ids
            and project_id not in selected
        )
        _idle(db, project, restricted)
        if restricted:
            accounting_only.append(project_id)
        predecessors[project_id] = {"id": row.id, "digest": row.digest}
    refs = _references(binding_retirements)
    retirement_inputs = []
    for _, proof in _resolve_retirements(db, refs):
        prepared_proof = proof.prepared
        expected = predecessors.get(str(prepared_proof.binding.root.project_id))
        require(
            expected
            == {
                "id": str(prepared_proof.source_custody_id),
                "digest": prepared_proof.source_custody_digest,
            },
            "Binding retirement selection is outside prepared custody",
        )
    for ref, proof in _resolve_retirements(db, refs):
        retirement_inputs.append(
            {"reference": ref, "prepared": proof.prepared.model_dump(mode="json")}
        )
    prepared = online.prepare(
        db,
        project_ids,
        accounting_only_project_ids=accounting_only,
        binding_retirements=retirement_inputs,
    )
    payload = {
        "predecessors": predecessors,
        "cleanup_authority_project_ids": selected,
        "reason": reason,
        "prepared_digest": digest(prepared),
    }
    if refs:
        payload["binding_retirements"] = refs
    db.add(
        NativeCustodyReconciliationRequest(
            id=prepared["request"]["request_id"],
            digest=digest(payload),
            payload=payload,
        )
    )
    db.flush()
    return prepared


def _transition(value, project_id, selected, *, storage_verified=False):
    before, after = value["before"], value["after"]
    _immutable(before, after, storage_verified=storage_verified)
    if before["authority"] != after["authority"]:
        require(
            project_id in selected and after["authority"] == "cleanup",
            "Cleanup authority requires explicit selected promotion",
        )
    if project_id in selected:
        require(
            after["authority"] == "cleanup",
            "Accounting-only verification cannot grant cleanup",
        )


def review(db, verified):
    receipt = online._validated(verified)
    request_id = str(receipt.prepared.request.request_id)
    request = _request(db, request_id)
    require(
        request["prepared_digest"] == digest(receipt.prepared),
        "Prepared reconciliation changed",
    )
    projects = {}
    for identity in receipt.prepared.request.projects:
        project_id = str(identity.project_id)
        previous = online.retained(
            db, db.get(Project, project_id), require_topology=False
        )
        require(previous is not None, "Custody predecessor missing")
        if TYPE_CHECKING:
            assert previous is not None
        row, before, _ = previous
        predecessor = {"id": row.id, "digest": row.digest}
        require(
            predecessor == request["predecessors"][project_id],
            "Custody predecessor changed; prepare again",
        )
        value = _project_transition(
            db,
            row,
            before,
            receipt,
            request["cleanup_authority_project_ids"],
            binding_retirements=request.get("binding_retirements", []),
        )
        projects[project_id] = value
    value = {
        "request_id": request_id,
        "receipt_digest": receipt.digest,
        "cleanup_authority_project_ids": request["cleanup_authority_project_ids"],
        "reason": request["reason"],
        "projects": projects,
    }
    if request.get("binding_retirements"):
        value["binding_retirements"] = request["binding_retirements"]
    return {"review": value, "digest": digest(value), "review_digest": digest(value)}


def apply(
    db,
    verified,
    *,
    expected_target_id,
    cleanup_authority_project_ids,
    review_digest,
    binding_retirements=(),
):
    online._disabled()
    receipt = online._validated(verified)
    require(
        0
        <= (
            online.datetime.now(online.UTC) - receipt.enrollment.verified_at
        ).total_seconds()
        <= 60,
        "Online verification must be fresh; verify again",
    )
    online.enrollment._validate_manifest(db, receipt.enrollment, expected_target_id)
    prepared = db.get(NativeCustodyRequest, str(receipt.prepared.request.request_id))
    require(
        prepared is not None and prepared.digest == digest(receipt.prepared),
        "Online custody was not independently prepared",
    )
    reviewed = review(db, receipt)
    value = reviewed["review"]
    require(reviewed["digest"] == review_digest, "Exact custody review digest required")
    refs = _references(binding_retirements)
    require(
        refs == value.get("binding_retirements", []),
        "Explicit binding retirement selection changed",
    )
    _validate_retirement_observations(db, receipt, refs, current=True)
    require(
        sorted(set(cleanup_authority_project_ids))
        == value["cleanup_authority_project_ids"],
        "Explicit cleanup authority selection changed",
    )
    require(
        any(p["before"] != p["after"] for p in value["projects"].values()),
        "No custody transition to apply",
    )
    for project_id in value["projects"]:
        project = db.get(Project, project_id)
        _idle(db, project, project_id in receipt.prepared.accounting_only_project_ids)
        require(
            online.operations.retained_topology(project)
            == receipt.prepared.topologies[project_id],
            "Retained topology changed; prepare again",
        )
    domain_id = str(receipt.enrollment.pins.domain_id)
    prior_observations = _retirement_observation_state(db, domain_id) if refs else {}
    enrollment_id = online._storage_enrollment(
        db, receipt.enrollment, expected_target_id
    )
    result = [
        _append(db, receipt, project_id, transition, reviewed, enrollment_id)
        for project_id, transition in value["projects"].items()
    ]
    db.flush()
    if refs:
        _invalidate_retirement_observations(db, domain_id, prior_observations)
    return result


def _retirement_observation_state(db, domain_id):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectAttempt

    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=domain_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    state: dict[tuple[str, str] | None, tuple[Any, bool]] = {
        None: (domain.latest_started_at, domain.conflicted)
    }
    for attempt in (
        db.query(NativeProjectAttempt)
        .filter_by(domain_id=domain_id)
        .populate_existing()
        .with_for_update()
    ):
        state[(attempt.project_id, attempt.root_key)] = (
            attempt.latest_started_at,
            attempt.conflicted,
        )
    return state


def _invalidate_retirement_observations(db, domain_id, prior_observations=None):
    """New retirement selections change report authority, never settle storage.

    Shared source scopes can include sibling retirement proofs. Invalidate the
    entire domain's cursors while retaining reports and all conflict markers.
    The next cleanup must use the ordinary fresh collection path.
    """
    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeEnrollment,
        NativeProjectAttempt,
    )

    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=domain_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    manifest = online.enrollment.validated_enrollment(
        db.get(NativeEnrollment, domain.enrollment_id)
    )
    current_roots = {
        str(root.project_id): online.enrollment.root_key(root)
        for root in manifest.pins.project_roots
    }
    cutoff = online.datetime.now(online.UTC)
    prior_observations = prior_observations or {}
    _invalidate_cursor(domain, cutoff, prior_observations.get(None))
    for attempt in (
        db.query(NativeProjectAttempt)
        .filter_by(domain_id=domain_id)
        .populate_existing()
        .with_for_update()
    ):
        _invalidate_cursor(
            attempt,
            cutoff,
            prior_observations.get((attempt.project_id, attempt.root_key)),
        )
        if current_roots.get(attempt.project_id) == attempt.root_key:
            attempt.enrollment_id = domain.enrollment_id
    db.flush()


def _invalidate_cursor(cursor, cutoff, previous):
    cursor.latest_report_id = None
    cursor.latest_started_at = _retirement_cutoff(cursor.latest_started_at, cutoff)
    if previous is not None:
        cursor.latest_started_at = _retirement_cutoff(
            previous[0], cursor.latest_started_at
        )
        cursor.conflicted = cursor.conflicted or previous[1]


def _retirement_cutoff(previous, current):
    # Keep the ordering watermark: an older in-flight report must not replace
    # this refresh requirement or clear a retained conflict marker.
    return (
        max(online.enrollment.utc(previous), current)
        if previous is not None
        else current
    )


def _append(db, receipt, project_id, transition, reviewed, enrollment_id):
    previous, _ = cast(EffectiveCustody, effective(db, project_id))
    if transition["before"] == transition["after"]:
        return previous.id
    require(
        db.query(NativeCustodySuccessor).filter_by(project_id=project_id).count()
        < MAX_SUCCESSORS,
        "Custody successor chain limit",
    )
    from app.models.kubevirt_storage import NativeEnrollment

    enrolled = db.get(NativeEnrollment, enrollment_id)
    row = NativeCustodySuccessor(
        id=str(uuid4()),
        project_id=project_id,
        predecessor_id=previous.id,
        predecessor_digest=previous.digest,
        digest=receipt.digest,
        payload={
            "receipt": receipt.model_dump(mode="json"),
            "enrollment_id": enrollment_id,
            "enrollment_digest": enrolled.digest,
            "review": reviewed["review"],
            "review_digest": reviewed["digest"],
        },
    )
    db.add(row)
    return row.id
