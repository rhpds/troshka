"""Retain real storage custody before teardown, including fenced legacy work."""

import json
from copy import deepcopy
from typing import cast

from app.models.native_deployment_origin import NativeDeploymentOriginActive
from app.models.project import Project
from app.services import native_cleanup_authority as authority
from app.services import native_deployment_origin as origin
from app.services import native_origin_custody as custody
from app.services.kubevirt_storage_enrollment import digest


def require_coverage(work):
    """Require a sealed teardown map; collector enrollment is no longer a gate.

    Soft coverage debts must already be cleared by ``native_teardown_map.seal``.
    Hard PVC/producer debts still block destructive NS delete.
    """
    from app.services.native_teardown_map import hard_debts, map_sealed

    origin.require(map_sealed(work), "Teardown map required before destructive cleanup")
    origin.require(
        not hard_debts(work.liabilities),
        "Verified storage coverage required before destructive cleanup",
    )


def validate_state(db, intent, mode, expected):
    """Only the exact pre-delete cancelled request may acquire initial custody."""
    from app.services import native_project_namespace_cleanup as cleanup

    origin.require(mode == "complete", "Cleanup custody requires complete verification")
    custody._active_identity(db, intent)
    # Publication already holds all enrollment identities in canonical order.
    # Do not re-enter cleanup.fence here: it would lock the target identity
    # ahead of other enrolled roots (and operation locks after identities).
    work = cleanup.load(
        db.query(cleanup.State)
        .filter_by(id=expected["id"])
        .populate_existing()
        .one_or_none()
    )
    project = (
        db.query(Project).filter_by(id=str(intent.project_id)).populate_existing().one()
    )
    origin.require(
        work.model_dump(mode="json") == expected,
        "Cleanup retention predecessor changed",
    )
    selected = work.authority
    resolved = authority.resolve(db, project)
    origin.require(
        resolved is not None and resolved[0] == selected,
        "Cleanup retained authority changed",
    )
    pointer = (
        db.query(NativeDeploymentOriginActive)
        .filter_by(project_id=str(intent.project_id))
        .populate_existing()
        .one()
    )
    origin.require(
        selected.source_kind == "origin"
        and selected.source_id == intent.id
        and selected.project_id == intent.project_id
        and selected.domain_id is None
        and selected.enrollment_id is None
        and selected.root is None
        and custody._current(db, intent.id) is None,
        "Cleanup recovery requires original missing custody",
    )
    origin.require(
        work.phase == "fencing"
        and not work.vm_targets
        and not work.cr_targets
        and work.namespace_delete_at is None
        and work.namespace_resource_version is None
        and work.marker is not None,
        "Cleanup retention requires pre-destructive producer fence",
    )
    origin.require(
        project.state == "cleanup_pending"
        and pointer.cancelled
        and pointer.io_phase is None
        and pointer.io_token is None,
        "Cleanup cancellation or outstanding I/O changed",
    )
    authority.local_writers_closed(db, selected)


def _inventory(db, api, work):
    from app.services import native_project_namespace_cleanup as cleanup

    rows = cleanup._required_inventory(api, work.authority.namespace)
    origin.require(
        all(
            execution.get("state") == "complete"
            for row in rows
            for execution in row.get("status", {}).get("originExecutions", {}).values()
        ),
        "Cleanup original operator execution remains unresolved",
    )
    current = {
        (row["kind"], row["metadata"]["name"]): cleanup._diagnostic(row) for row in rows
    }
    history = db.query(cleanup.Evidence).filter_by(cleanup_id=str(work.id)).all()
    for evidence in history:
        if not evidence.phase.startswith("inventory:"):
            continue
        origin.require(
            evidence.digest == digest(evidence.payload),
            "Cleanup retained inventory changed",
        )
        for old in evidence.payload:
            live = current.get((old["kind"], old["name"]), {})
            origin.require(
                all(
                    live.get(field) == old.get(field)
                    for field in ("uid", "volume_name")
                ),
                "Cleanup original inventoried resource missing or replaced",
            )
    return rows


def _retry_progress(value):
    origin.require(isinstance(value, dict), "Cleanup handler progress invalid")
    # This handler keeps retrying while the namespace fence rejects production.
    # Only its retry schedule/counter are volatile; lifecycle/outcome stays pinned.
    if (
        value.get("purpose") == "resume"
        and value.get("message") == "namespace_cleanup_production_fenced"
        and value.get("success") is False
        and value.get("failure") is False
    ):
        return {
            key: item
            for key, item in value.items()
            if key not in {"delayed", "retries"}
        }
    return value


def _comparison_object(row):
    value = deepcopy(row)
    meta = value["metadata"]
    meta.pop("resourceVersion", None)
    meta.pop("managedFields", None)
    handler = {
        "TroshkaProject": "project_managed_power_resume",
        "TroshkaNetwork": "network_resume",
    }.get(value["kind"])
    if handler is None:
        return value
    annotation = "kopf.zalando.org/" + handler
    annotations = meta.get("annotations", {})
    if annotation in annotations:
        annotations[annotation] = _retry_progress(json.loads(annotations[annotation]))
    progress = value.get("status", {}).get("kopf", {}).get("progress", {})
    if handler in progress:
        progress[handler] = _retry_progress(progress[handler])
    return value


def _same_inventory(before, after):
    # LIST order and API write bookkeeping are not custody identities. All
    # remaining fields, including every storage/producer status fact, stay exact.
    return sorted(digest(_comparison_object(row)) for row in before) == sorted(
        digest(_comparison_object(row)) for row in after
    )


def recover(db, api, work):
    """Called only after live ACK. Publish custody and adoption in one transaction."""
    from app.services import native_project_namespace_cleanup as cleanup

    if work.authority.domain_id is not None:
        return work
    expected = work.model_dump(mode="json")
    inventory = _inventory(db, api, work)
    prepared = custody.prepare(
        db, str(work.authority.source_id), mode="complete", cleanup_read_state=expected
    )
    db.commit()
    verified = custody.verify_configured(prepared)
    # Storage verification owns a separate, potentially longer deadline. Start
    # exactly one new bounded read phase for the full inventory and fence
    # recheck; never renew its budget during pagination or qualification.
    api = api.new_observation()
    origin.require(
        _same_inventory(
            inventory, cleanup._required_inventory(api, work.authority.namespace)
        ),
        "Cleanup resources changed during storage retention",
    )
    namespace = cleanup._namespace(api, work.authority)
    origin.require(
        namespace is not None
        and not namespace["metadata"].get("deletionTimestamp")
        and cleanup._acknowledged(api, work, namespace),
        "Cleanup retention producer fence changed",
    )
    # apply revalidates the exact request, canonical identity, enrollment,
    # cancellation and producer fences under target/domain/identity locks.
    row = custody.apply(db, verified)
    project = db.get(Project, str(work.authority.project_id))
    selected, liabilities = cast(
        tuple[authority.CleanupAuthority, authority.StorageLiabilities],
        authority.resolve(db, project),
    )
    evidence = dict(
        previous=expected,
        authority=selected.model_dump(mode="json"),
        liabilities=liabilities.model_dump(mode="json"),
        receipt_id=row.id,
        receipt_digest=row.digest,
    )
    cleanup._evidence(db, work, "storage_retained", evidence)
    work = cleanup._save(
        db,
        work,
        authority=selected,
        liabilities=liabilities,
        liability_digest=digest(liabilities.model_dump(mode="json")),
        enrollment_boundary=authority.enrollment_boundary(db, selected),
    )
    from app.services.native_teardown_map import seal

    work = seal(db, api, work)
    require_coverage(work)
    db.commit()
    # The old marker remains retained. advance publishes its successor and
    # requires a fresh ACK for the new authority before issuing any DELETE.
    return work
