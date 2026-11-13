"""Atomic, offline current capacity verification after fresh accounting conversion.

Reviewed observations are evidence, never dispatch authorization. This importer
does not collect from providers, migrate billing history, change rates or enable
enforcement. The caller supplies a dedicated, clean session.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.database import Base
from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeProjectRoot,
    NativeRetainedBinding,
)
from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaAllocation, QuotaReadiness, QuotaReservation
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, Resources, Scope
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_baseline as legacy
from app.services import quota_native as native
from app.services import quota_operations as operations
from app.services import quota_storage as storage
from app.services.quota_locks import lock_rows
from app.services.quota_policy import get_limits
from app.services.quota_resources import topology_resources

VERSION = "fresh-capacity-v1"
RECEIPT_ID = "unified-groups-v1"
MUTABLE = {"quota_reservations", "quota_allocations"}


def _require(condition, message):
    if not condition:
        raise AccountingUnavailable(message)


def _fields(value, allowed, required=()):
    _require(isinstance(value, dict), "Evidence must be an object")
    _require(not set(value) - set(allowed), "Unknown evidence fields are not accepted")
    _require(set(required) <= set(value), "Required evidence fields are missing")


def _rows_digest(db, names, *, protected=False):
    result = {}
    for name in sorted(names):
        rows = [
            dict(row)
            for row in db.execute(select(Base.metadata.tables[name])).mappings()
        ]
        if protected and name == "quota_readiness":
            rows = [row for row in rows if row["id"] != "baseline"]
        result[name] = sorted(rows, key=legacy._hash)
    return legacy._hash(result)


def preservation(db):
    """Every source column, including organizations, prices and billing cursors."""
    return _rows_digest(db, set(Base.metadata.tables) - MUTABLE, protected=True)


def source_digest(db):
    return legacy._hash(
        {"protected": preservation(db), "ledger": _rows_digest(db, MUTABLE)}
    )


def _receipt(db):
    row = db.get(QuotaReadiness, RECEIPT_ID)
    _require(row is not None, "Fresh accounting conversion receipt is required")
    start = accounting.accounting_started_at(db)
    if start is None:
        raise AccountingUnavailable("Fresh accounting start is required")
    return {
        "id": RECEIPT_ID,
        "accounting_started_at": start.isoformat(),
        "digest": legacy._hash(
            {
                "details": row.details,
                "checked_at": accounting.utc(row.checked_at).isoformat()
                if row.checked_at
                else None,
                "ready": row.ready,
                "version": row.baseline_version,
            }
        ),
    }


def _native_identity(db, project):
    roots = db.query(NativeProjectRoot).filter_by(project_id=project.id).all()
    _require(len(roots) == 1, "Native project needs one current enrolled root")
    root = roots[0]
    domain = db.get(NativeCollectionDomain, root.domain_id)
    _require(domain is not None, "Native collection domain is missing")
    from app.models.kubevirt_storage import NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment

    enrollment = db.get(NativeEnrollment, domain.enrollment_id)
    _require(enrollment is not None, "Native enrollment is missing")
    manifest = VerifiedEnrollment.model_validate(enrollment.payload)
    pins = [r for r in manifest.pins.project_roots if str(r.project_id) == project.id]
    _require(len(pins) == 1, "Native enrolled project identity is ambiguous")
    return {
        "domain_id": root.domain_id,
        "enrollment_id": domain.enrollment_id,
        "host_id": project.host_id,
        "provider_id": root.provider_id,
        "root": pins[0].model_dump(mode="json"),
    }


def _adapter(db, project):
    host = db.get(Host, project.host_id) if project.host_id else None
    provider = db.get(Provider, getattr(host, "provider_id", None)) if host else None
    if (host and host.host_type == "kubevirt-cluster") or (
        provider and provider.type == "kubevirt"
    ):
        return native.ADAPTER
    return "nested"


def _current(db, project):
    rows = (
        db.query(QuotaReservation)
        .filter_by(project_id=project.id)
        .order_by(QuotaReservation.operation_id)
        .all()
    )
    return operations.current_generation(rows)


def _empty(db, project, rows):
    return bool(
        project.state == "draft"
        and not project.host_id
        and not project.disks
        and not rows
        and not db.query(QuotaAllocation)
        .filter(
            QuotaAllocation.project_id == project.id,
            QuotaAllocation.state != "released",
        )
        .first()
        and not db.query(MeteringInterval)
        .filter_by(project_id=project.id, ended_at=None)
        .first()
        and not db.query(NativeProjectRoot).filter_by(project_id=project.id).first()
    )


def _project_inventory(db, project):
    rows = _current(db, project)
    adapter = _adapter(db, project)
    identity = _native_identity(db, project) if adapter == native.ADAPTER else None
    generations = sorted({operations._generation(row) for row in rows})
    proposed = (
        "fresh-capacity:"
        + project.id
        + ":"
        + legacy._hash(
            identity
            or {
                "host_id": project.host_id,
                "topology": operations.retained_topology(project),
            }
        )[:32]
    )
    owner = db.get(User, project.owner_id) if project.owner_id else None
    return {
        "project_id": project.id,
        "owner_id": project.owner_id,
        "organization_id": getattr(owner, "organization_id", None),
        "host_id": project.host_id,
        "provider_id": project.provider_id,
        "adapter": adapter,
        "empty": _empty(db, project, rows),
        "desired_digest": operations.digest(operations.retained_topology(project)),
        "generation_id": generations[0] if len(generations) == 1 else proposed,
        "retained_generations": generations,
        "operation_ids": [r.operation_id for r in rows],
        "native_storage_identity": identity,
    }


def inventory(db):
    """Complete deterministic inventory; unsupported/orphaned capacity is explicit."""
    projects = []
    for project in db.query(Project).order_by(Project.id):
        try:
            projects.append(_project_inventory(db, project))
        except (AccountingUnavailable, ValueError, KeyError, TypeError) as error:
            projects.append(
                {"project_id": project.id, "empty": False, "blocker": str(error)}
            )
    return {
        "version": VERSION,
        "inventory_digest": source_digest(db),
        "fresh_receipt": _receipt(db),
        "projects": projects,
        **_retained_inventory(db),
    }


def _retained_inventory(db):
    roots = [
        dict(row)
        for row in db.execute(
            select(NativeProjectRoot.__table__).order_by(NativeProjectRoot.key)
        ).mappings()
    ]
    retained = [
        dict(row)
        for row in db.execute(
            select(QuotaAllocation.__table__).order_by(QuotaAllocation.id)
        ).mappings()
        if row["state"] != "released" or any(row["observed"].values())
    ]
    return {
        "retained_allocations": retained,
        "retained_operations": [
            dict(row)
            for row in db.execute(
                select(QuotaReservation.__table__).order_by(QuotaReservation.id)
            ).mappings()
            if row["state"] not in {"released", "confirmed"}
            or any(row["observed"].values())
        ],
        "native_roots": roots,
        "native_retained_bindings": _binding_inventory(db, roots),
        "open_sources": [
            dict(row)
            for row in db.execute(
                select(MeteringInterval.__table__)
                .where(MeteringInterval.ended_at.is_(None))
                .order_by(MeteringInterval.id)
            ).mappings()
        ],
        "external_ips": [
            e.id
            for e in db.query(ElasticIp)
            .filter(ElasticIp.state.in_(("allocated", "associated")))
            .order_by(ElasticIp.id)
        ],
    }


def _binding_inventory(db, roots):
    by_key = {root["key"]: root for root in roots}
    entries = []
    for binding in db.query(NativeRetainedBinding).order_by(NativeRetainedBinding.key):
        root = by_key.get(binding.root_key)
        coverage = "unresolved"
        if root is not None and root["domain_id"] == binding.domain_id:
            coverage = "project"
        elif binding.root_key == "cache":
            coverage = "platform_cache"
        entries.append(
            {
                "key": binding.key,
                "root_key": binding.root_key,
                "domain_id": binding.domain_id,
                "project_id": root["project_id"] if root else None,
                "coverage": coverage,
                "payload": binding.payload,
            }
        )
    return entries


def _operation_set(db, source):
    values = {value for (value,) in db.query(QuotaReservation.operation_id)}
    values.update(value for (value,) in db.query(QuotaAllocation.operation_id))
    values.update(
        p["generation_id"]
        for p in source["projects"]
        if p.get("generation_id") and not p["empty"]
    )
    return values


def _lock(db, source):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
    identities = _operation_set(db, source)
    admission.lock_operations(db, identities)
    legacy._boundary(db)
    _require(
        _operation_set(db, inventory(db)) == identities,
        "Operation inventory changed; repeat maintenance review",
    )
    lock_rows(
        db,
        user_ids=[v for (v,) in db.query(User.id)],
        organization_ids=[v for (v,) in db.query(ManagedGroup.id)],
        project_ids=[v for (v,) in db.query(Project.id)],
    )
    for domain in (
        db.query(NativeCollectionDomain)
        .order_by(NativeCollectionDomain.id)
        .with_for_update()
        .populate_existing()
    ):
        _require(domain.id is not None, "Native domain identity missing")


def _envelope(evidence, source, now):
    _fields(
        evidence,
        {"version", "inventory_digest", "fresh_receipt", "maintenance", "projects"},
        {"version", "inventory_digest", "fresh_receipt", "maintenance", "projects"},
    )
    _require(evidence["version"] == VERSION, "Fresh capacity manifest version required")
    _require(
        evidence["fresh_receipt"] == source["fresh_receipt"],
        "Fresh accounting receipt changed",
    )
    _require(
        evidence["inventory_digest"] == source["inventory_digest"],
        "Reviewed source inventory changed",
    )
    maintenance = evidence["maintenance"]
    _fields(maintenance, {"writers_stopped", "since", "evidence"})
    _require(
        maintenance.get("writers_stopped") is True
        and bool(maintenance.get("evidence")),
        "Stopped Troshka writer and job inventory attestation required",
    )
    since = legacy._timestamp(maintenance["since"])
    _require(
        legacy._timestamp(source["fresh_receipt"]["accounting_started_at"])
        <= since
        <= now,
        "Maintenance boundary must follow the immutable fresh accounting start",
    )
    expected = {p["project_id"] for p in source["projects"] if not p["empty"]}
    _require(
        isinstance(evidence["projects"], dict)
        and set(evidence["projects"]) == expected,
        "Manifest must cover exactly every retained project",
    )
    return since


def _timing(project, entry, now, since):
    _fields(
        entry,
        {
            "adapter",
            "generation_id",
            "worker_fenced_at",
            "jobs_checked_at",
            "jobs",
            "compute_observation",
            "storage_observation",
        },
        {
            "adapter",
            "generation_id",
            "worker_fenced_at",
            "jobs_checked_at",
            "jobs",
            "compute_observation",
            "storage_observation",
        },
    )
    compute = entry["compute_observation"]
    common = {"host_id", "source", "observation_id", "observed_at"}
    details = (
        {"resources", "states"}
        if entry["adapter"] == "nested"
        else {"managed_inventory", "request_id", "started_at"}
    )
    _fields(compute, common | details, common)
    fenced = legacy._timestamp(entry["worker_fenced_at"])
    jobs = legacy._timestamp(entry["jobs_checked_at"])
    observed = legacy._timestamp(compute["observed_at"])
    _require(
        since <= fenced <= jobs <= observed <= now,
        "Invalid worker/job/observation boundary",
    )
    _require(
        (now - observed).total_seconds() <= storage.MAX_AGE,
        "Compute observation is stale",
    )
    _require(
        entry["jobs"] == [], "Pending or ambiguous jobs require ordinary reconciliation"
    )
    _require(
        compute.get("host_id") == project.host_id
        and bool(compute.get("source"))
        and bool(compute.get("observation_id")),
        "Independent compute observation identity required",
    )
    return observed, fenced, jobs


class _OfflineInventory:
    """Read-only complete resource snapshot; deliberately has no mutation methods."""

    def __init__(self, supplied, namespace):
        from app.services.native_managed_power import helpers

        module = helpers()
        _fields(supplied, module.KINDS, module.KINDS)
        self.values = supplied
        for kind, rows in supplied.items():
            _require(
                isinstance(rows, list) and len(rows) <= 1000,
                "Incomplete managed inventory",
            )
            for value in rows:
                _require(
                    value["kind"] == kind
                    and value["apiVersion"] == module.KINDS[kind][0],
                    "Managed resource kind/version changed",
                )
                if kind not in {"Namespace", "PersistentVolume"}:
                    _require(
                        value["metadata"]["namespace"] == namespace,
                        "Managed namespace changed",
                    )
        identities = [v["metadata"]["uid"] for rows in supplied.values() for v in rows]
        _require(
            len(identities) == len(set(identities)) and len(identities) <= 1000,
            "Duplicate or oversized managed inventory",
        )

    def rows(self, kind, namespace):
        return self.values[kind]

    def get(self, kind, namespace, name):
        values = [
            v
            for v in self.values[kind]
            if v["metadata"]["name"] == name
            and v["metadata"].get("namespace") == namespace
        ]
        _require(len(values) == 1, "Managed resource is missing or ambiguous")
        return values[0]


def _native_compute(project, entry, identity, observed, jobs):
    from app.services import native_managed_power as power

    compute = entry["compute_observation"]
    _require(
        "managed_inventory" in compute,
        "Native active compute profile unavailable; complete independent managed off inventory required",
    )
    started = legacy._timestamp(compute["started_at"])
    _require(
        jobs <= started <= observed, "Managed observation predates writer/job fence"
    )
    api = _OfflineInventory(compute["managed_inventory"], identity["root"]["namespace"])
    context = {"root": identity["root"], "generation": entry["generation_id"]}
    proof = power.observe_off(
        api, context, compute["request_id"], started, lambda: observed
    )
    _require(
        proof is not None, "Native managed producers have not independently settled off"
    )
    root = api.rows("TroshkaProject", identity["root"]["namespace"])[0]
    _require(
        root["spec"].get("topology", {}) == operations.retained_topology(project),
        "Managed observation topology differs from retained topology",
    )
    return Resources(projects=1)


def _nested_compute(db, project, entry):
    operations.supported(db, project, deployed=True)
    topology = operations.retained_topology(project)
    sizes = operations.virtual_sizes(db, topology)
    mapping = operations.resource_map(project, topology)
    legacy._resource_evidence(mapping, entry["compute_observation"]["resources"])
    power = operations._whole_project_power(project, entry["compute_observation"])
    _require(power is not None, "Mixed or missing nested power observation")
    target = topology_resources(
        topology, state="stopped" if power == "off" else "active", virtual_sizes=sizes
    )
    target.storage_bytes = 0
    return target, mapping, sizes


def _pair(db, row, identity):
    allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one_or_none()
    )
    _require(
        allocation is not None, "Retained reservation lacks original allocation pairing"
    )
    for item in (row, allocation):
        _require(
            (item.project_id, item.owner_id, item.organization_id)
            == (
                identity["project_id"],
                identity["owner_id"],
                identity["organization_id"],
            ),
            "Retained attribution changed; ordinary transfer/reconciliation required",
        )
    _require(
        row.requested == allocation.requested
        and row.observed == allocation.observed
        and row.observation_id == allocation.observation_id
        and bool(row.observation_id)
        and allocation.observed_at is not None,
        "Retained reservation/allocation ledger is inconsistent",
    )
    _require(
        row.state == "confirmed" and operations._settled(row),
        "Pending original worker requires reconciliation",
    )
    _require(
        allocation.state != "released" or not any(row.observed.values()),
        "Retained allocation state is inconsistent",
    )


def _retained(db, project, entry, adapter):
    identity = accounting.attribution(db, project)
    rows = _current(db, project)
    if not rows:
        return [], identity
    root = storage.measurement_row(rows)
    _require(
        operations._generation(root) == entry["generation_id"],
        "Retained generation changed",
    )
    for row in rows:
        _pair(db, row, identity)
        _require(storage.storage_adapter(row) == adapter, "Retained adapter changed")
    return rows, identity


def _new_row(
    db, project, entry, target, identity, mapping, sizes, fenced, native_identity
):
    row = QuotaReservation(
        operation_id=entry["generation_id"],
        project_id=project.id,
        owner_id=identity["owner_id"],
        organization_id=identity["organization_id"],
        requested=target.model_dump(),
        observed=Resources().model_dump(),
        state="reconciling",
        dispatch_metadata={
            "kind": "deploy",
            "adapter": entry["adapter"],
            "generation_id": entry["generation_id"],
            "desired_digest": operations.digest(operations.retained_topology(project)),
            "resources": mapping,
            "virtual_sizes": sizes,
            "known_empty": False,
            "worker_finished": True,
            "worker_finished_at": fenced.isoformat(),
            "baseline_worker_boundary": "verified existing infrastructure; no worker dispatched",
            "provenance": VERSION,
        },
    )
    if native_identity:
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "native_storage_identity": native_identity,
        }
    db.add(row)
    db.flush()
    return row


def _compute_vectors(rows, target):
    """Keep existing per-operation compute; distribute only monotonic reductions."""
    totals = admission._vector_sum(
        Resources.model_validate(r.observed).model_dump() for r in rows
    )
    _require(
        totals.projects == 1, "Retained project slot is not explained by original rows"
    )
    for name in ("cpu", "ram_bytes"):
        _require(
            getattr(target, name) <= getattr(totals, name),
            "Unreserved compute growth requires ordinary admission",
        )
        _require(
            getattr(target, name) in (0, getattr(totals, name)),
            "Partial per-operation compute reconciliation is unsupported",
        )
    return [
        {
            **r.observed,
            "cpu": r.observed["cpu"] if target.cpu else 0,
            "ram_bytes": r.observed["ram_bytes"] if target.ram_bytes else 0,
        }
        for r in rows
    ]


def _storage_observation(db, project, entry, identity, jobs, observed, now, mapping):
    supplied = entry["storage_observation"]
    if entry["adapter"] == native.ADAPTER:
        _fields(supplied, {"identity", "measurement"}, {"identity", "measurement"})
        _require(
            supplied["identity"] == identity, "Native enrollment/root identity changed"
        )
        measurement = native.storage_measurement(
            db, project, entry["generation_id"], identity, now
        )
        _require(
            supplied["measurement"] == measurement.evidence(),
            "Native latest storage evidence changed",
        )
        _require(
            jobs
            <= accounting.utc(measurement.started_at)
            <= accounting.utc(measurement.completed_at)
            <= observed,
            "Native source interval predates writer/job boundary",
        )
        return measurement.allocated_bytes, measurement
    _nested_storage_fields(supplied)
    amount = storage.validate(
        supplied, project.id, entry["generation_id"], project.host_id, now
    )
    storage.require_disks(mapping, supplied)
    _require(
        jobs <= legacy._timestamp(supplied["observed_at"]) <= observed,
        "Nested storage observation predates writer/job boundary",
    )
    return amount, supplied


def _nested_storage_fields(supplied):
    _fields(
        supplied,
        {
            "version",
            "complete",
            "project_id",
            "generation_id",
            "host_id",
            "observed_at",
            "observation_id",
            "writable_pool",
            "bases",
            "files",
            "pools",
        },
    )
    for item in supplied.get("files", []) + supplied.get("bases", []):
        _fields(item, {"path", "device", "inode", "allocated_bytes"})
    for pool in supplied.get("pools", []):
        _fields(
            pool, {"id", "capacity_bytes", "free_bytes", "platform_allocated_bytes"}
        )


def _import_project(db, project, item, entry, now, since):
    _require(not item.get("blocker"), item.get("blocker", "Unsupported project"))
    observed, fenced, jobs = _timing(project, entry, now, since)
    _require(
        entry["adapter"] == item["adapter"]
        and entry["generation_id"] == item["generation_id"],
        "Reviewed project adapter/generation changed",
    )
    rows, identity = _retained(db, project, entry, item["adapter"])
    native_identity = item.get("native_storage_identity")
    mapping, sizes = {}, {}
    if item["adapter"] == native.ADAPTER:
        target = _native_compute(project, entry, native_identity, observed, jobs)
    else:
        target, mapping, sizes = _nested_compute(db, project, entry)
        _nested_profile(project, rows, mapping, sizes)
    amount, physical = _storage_observation(
        db, project, entry, native_identity, jobs, observed, now, mapping
    )
    target.storage_bytes = amount
    if rows:
        vectors = _compute_vectors(rows, target)
        root = storage.measurement_row(rows)
        for row, vector in zip(rows, vectors):
            resulting = {**vector, "storage_bytes": amount if row == root else 0}
            _require(
                row.observation_id != entry["compute_observation"]["observation_id"]
                or row.observed == resulting,
                "Observation identity has different resources",
            )
    else:
        rows = [
            _new_row(
                db,
                project,
                entry,
                target,
                identity,
                mapping,
                sizes,
                fenced,
                native_identity,
            )
        ]
        vectors = [target.model_dump()]
    _worker_profile(rows, observed, native_identity)
    applied = storage.apply_measurement(db, project, rows, physical, now)
    _storage_agreement(rows, physical, amount, applied)
    _confirm(db, rows, vectors, entry, observed)
    return {
        "adapter": item["adapter"],
        "generation_id": entry["generation_id"],
        "operation_ids": [r.operation_id for r in rows],
        "observation_id": entry["compute_observation"]["observation_id"],
        "observed_at": observed.isoformat(),
        "storage_observation": physical.evidence() if native_identity else physical,
    }


def _storage_agreement(rows, physical, amount, applied):
    root = storage.measurement_row(rows)
    key = (
        "storage_evidence"
        if storage.storage_adapter(root) == "nested"
        else "native_storage_evidence"
    )
    expected = physical if isinstance(physical, dict) else physical.evidence()
    if not applied:
        _require(
            root.dispatch_metadata.get(key) == expected,
            "Skipped physical measurement differs from retained evidence; independently recollect",
        )
    _require(
        root.observed["storage_bytes"] == amount
        and all(row.observed["storage_bytes"] == 0 for row in rows if row != root),
        "Persisted physical allocation differs from the accepted observation",
    )


def _nested_profile(project, rows, mapping, sizes):
    for row in rows:
        _require(
            row.dispatch_metadata.get("resources") == mapping
            and row.dispatch_metadata.get("desired_digest")
            == operations.digest(operations.retained_topology(project))
            and row.dispatch_metadata.get("virtual_sizes") == sizes,
            "Retained nested resource/profile identity changed",
        )


def _worker_profile(rows, observed, native_identity):
    for row in rows:
        _require(
            operations._observation_follows_worker(row, observed),
            "Observation predates retained worker fence",
        )
        if native_identity:
            _require(
                row.dispatch_metadata.get("native_storage_identity") == native_identity,
                "Retained native enrollment identity changed",
            )


def _confirm(db, rows, vectors, entry, observed):
    for row, vector in zip(rows, vectors):
        if entry["adapter"] == native.ADAPTER:
            from app.services.native_managed_power import baseline_profile

            row.dispatch_metadata = {
                **row.dispatch_metadata,
                "native_baseline_profile": baseline_profile(
                    entry["compute_observation"]
                ),
            }
        target = Resources.model_validate(
            {**vector, "storage_bytes": row.observed["storage_bytes"]}
        )
        # Storage updates intentionally retain compute observation identity. Detect
        # conflicts against the original compute vector before physical mutation.
        admission.confirm(
            db,
            row.operation_id,
            target,
            entry["compute_observation"]["observation_id"],
            observed,
        )
        legacy._confirmed_baseline(db, row, target, observed)


def _inventory_blockers(db, source):
    _require(
        not source["external_ips"],
        "Retained external IP capacity has no supported quota adapter",
    )
    projects = {p["project_id"] for p in source["projects"]}
    for allocation in source["retained_allocations"]:
        _require(
            allocation["project_id"] in projects,
            "Deleted project retains an orphan allocation",
        )
        _require(
            db.query(QuotaReservation)
            .filter_by(operation_id=allocation["operation_id"])
            .one_or_none()
            is not None,
            "Orphan allocation has no original reservation",
        )
        _require(
            allocation["state"] != "released"
            or not any(allocation["observed"].values()),
            "Released allocation retains unexplained capacity",
        )
    for root in source["native_roots"]:
        _require(
            root["project_id"] in projects,
            "Deleted project retains an enrolled native root",
        )
    _work_coverage(source, projects)
    _binding_coverage(source)


def _work_coverage(source, projects):
    for entry in source["retained_operations"]:
        _require(
            entry["project_id"] in projects,
            "Deleted project retains unresolved operation work",
        )
    for entry in source["open_sources"]:
        _require(
            entry["project_id"] in projects,
            "Deleted project retains an open metering source",
        )


def _binding_coverage(source):
    projects = {p["project_id"]: p for p in source["projects"]}
    covered_domains = set()
    for root in source["native_roots"]:
        project = projects.get(root["project_id"], {})
        _require(
            project.get("adapter") == native.ADAPTER and not project.get("empty", True),
            "Enrolled native root lacks imported native project coverage",
        )
        covered_domains.add(root["domain_id"])
    for binding in source["native_retained_bindings"]:
        _require(
            binding["coverage"] in {"project", "platform_cache"}
            and binding["domain_id"] in covered_domains,
            "Standalone native retained binding lacks complete imported domain/root coverage",
        )


def _accounting_project(db, project, entry, now):
    """Validate current coverage without accrual, cursor writes or history import."""
    from app.services import metering_service as metering

    if entry["adapter"] == native.ADAPTER:
        from app.services.native_managed_power import billing_snapshot

        snap = billing_snapshot(db, project)
        rates = metering.load_type_rates(db)
    else:
        snap, rates = _billing_snapshot(db, project, entry["compute_observation"])
    snap = metering._quota_snapshot(project, snap, now)
    _billing_coverage(db, project, snap, rates, now)


def _billing_snapshot(db, project, compute):
    from app.services import metering_service as metering

    _require(
        metering.metering_enabled(), "Metering must be enabled for accounting readiness"
    )
    host = db.get(Host, project.host_id)
    provider = db.get(Provider, host.provider_id)
    rates = metering.load_type_rates(db)
    topology = operations.retained_topology(project)
    snap = {
        "host_id": host.id,
        "provider_type": provider.type,
        "billing_mode": "shared",
        "project_state": project.state,
        "host_rates": metering.compose_host_rates(host, provider.type, rates),
        "rate_keys": list(
            metering._configured_rate_keys(
                metering.load_catalog().get("kubevirt_unit_rates"),
                metering._typed_rate_row(provider.type, rates),
                host.metering_rates,
            )
        ),
        "observed_at": compute["observed_at"],
        "vms": metering._vm_nodes(topology, compute["states"]),
        "disks": metering._topology_disks(topology),
        "eips": [],
        "ceph": [],
    }
    return snap, rates


def _billing_coverage(db, project, snap, rates, now):
    from app.services import metering_service as metering
    from app.services.metering_math import desired_resources

    future = {
        **snap,
        "vms": [{**v, "running": True, "live_state": True} for v in snap["vms"]],
    }
    metering._validate_quota_rates(
        snap,
        desired_resources(future, quota_mode=True),
        rates,
        metering.kubevirt_unit_rates(),
    )
    rows = (
        db.query(MeteringInterval).filter_by(project_id=project.id, ended_at=None).all()
    )
    start = accounting.accounting_started_at(db)
    if start is None:
        raise AccountingUnavailable("Fresh accounting start is required")
    _require(
        all(
            start <= accounting.utc(r.started_at) <= now
            and _billing_attribution(
                r.quota_attribution, accounting.attribution(db, project)
            )
            for r in rows
        ),
        "Current billing source has pre-era or changed attribution",
    )
    actual = {
        (r.kind, r.resource_id, float(r.qty), r.host_id, r.provider_type) for r in rows
    }
    desired = desired_resources(snap, quota_mode=True)
    _require(
        actual == desired and len(rows) == len(desired),
        "Current billing coverage differs from independent observation",
    )
    _billing_rates(rows, snap, rates)


def _billing_attribution(stamp, expected):
    return bool(stamp) and all(
        stamp.get(key) == expected[key]
        for key in ("project_id", "owner_id", "organization_id")
    )


def _billing_rates(rows, snap, rates):
    from app.services import metering_service as metering
    from app.services.metering_math import resolve_rate

    for row in rows:
        current = Decimal(
            str(
                resolve_rate(
                    row.kind,
                    row.provider_type,
                    snap["host_rates"],
                    rates,
                    metering.kubevirt_unit_rates(),
                )
            )
        )
        stamped = Decimal(str(row.unit_rate))
        _require(
            stamped.is_finite()
            and stamped >= 0
            and abs(stamped - current) <= Decimal("0.000001"),
            "Current billing source rate differs from the configured rate",
        )


def _readiness(db, report, evidence, now):
    accounting_blockers = []
    runtime_blockers = []
    for project_id, entry in evidence["projects"].items():
        if entry["adapter"] == native.ADAPTER:
            try:
                from app.services.native_managed_power import current_profile

                project = db.get(Project, project_id)
                current_profile(db, project, _current(db, project))
            except (AccountingUnavailable, ValueError, KeyError, TypeError) as error:
                runtime_blockers.append(str(error))
        try:
            _accounting_project(db, db.get(Project, project_id), entry, now)
        except (AccountingUnavailable, ValueError, KeyError, TypeError) as error:
            accounting_blockers.append(
                {"project_id": project_id, "message": str(error)}
            )
    report.update(
        accounting_ready=not accounting_blockers,
        accounting_blockers=accounting_blockers,
        runtime_ready=not runtime_blockers,
        runtime_blockers=runtime_blockers,
    )
    report["ready"] = (
        report["capacity_ready"]
        and report["accounting_ready"]
        and report["runtime_ready"]
    )


def _scopes(db):
    result, overages = {}, []
    for scope, model in (
        ("project", Project),
        ("user", User),
        ("organization", ManagedGroup),
    ):
        for (identity,) in db.query(model.id).order_by(model.id):
            used, reserved = admission.usage(db, scope, identity)
            result[scope + ":" + identity] = {
                "used": used.model_dump(),
                "reserved": reserved.model_dump(),
            }
            limits = get_limits(db, cast(Scope, scope), identity)
            for name in admission.DIMENSIONS:
                limit = getattr(limits, name)
                if limit is not None and getattr(used, name) > limit:
                    overages.append(
                        {
                            "scope": scope,
                            "scope_id": identity,
                            "dimension": name,
                            "used": getattr(used, name),
                            "limit": limit,
                        }
                    )
    return result, overages


def _report(now, apply):
    return {
        "version": VERSION,
        "mode": "apply" if apply else "dry-run",
        "checked_at": now.isoformat(),
        "ready": False,
        "capacity_ready": False,
        "accounting_ready": False,
        "runtime_ready": False,
        "enforcement_enabled": accounting.quotas_enabled(),
        "blockers": [],
    }


def _replay(db, evidence, actual):
    marker = db.get(QuotaReadiness, "baseline")
    if (
        marker
        and marker.baseline_version == VERSION
        and marker.details.get("evidence_digest") == legacy._hash(evidence)
    ):
        _require(
            marker.details.get("post_import_digest") == actual,
            "Exact replay source/ledger drift; independently recollect evidence",
        )
        return dict(marker.details)
    return None


def run(db, *, evidence=None, now=None, apply=False):
    """Validate all candidates, commit atomically, roll back dry-run and all errors."""
    _require(
        not (db.new or db.dirty or db.deleted),
        "Baseline requires a dedicated clean session",
    )
    evidence, now = evidence or {}, accounting.utc(now or datetime.now(UTC))
    report = _report(now, apply)
    try:
        _require(
            not accounting.quotas_enabled(),
            "Capacity import requires enforcement disabled",
        )
        source = inventory(db)
        _lock(db, source)
        db.expire_all()
        source = inventory(db)
        replay = _replay(db, evidence, source["inventory_digest"])
        if replay is not None:
            db.rollback()
            return replay
        since = _envelope(evidence, source, now)
        _inventory_blockers(db, source)
        protected = preservation(db)
        coverage = {}
        for item in source["projects"]:
            if not item["empty"]:
                project_id = item["project_id"]
                coverage[project_id] = _import_project(
                    db,
                    db.get(Project, project_id),
                    item,
                    evidence["projects"][project_id],
                    now,
                    since,
                )
        db.flush()
        _require(
            preservation(db) == protected,
            "Protected source data changed during capacity import",
        )
        report.update(
            capacity_ready=True,
            coverage=coverage,
            fresh_receipt=source["fresh_receipt"],
            inventory_digest=source["inventory_digest"],
            evidence_digest=legacy._hash(evidence),
            evidence=evidence,
            preservation_fingerprint=protected,
            allocation_count=db.query(QuotaAllocation).count(),
        )
        report["scopes"], report["overages"] = _scopes(db)
        _readiness(db, report, evidence, now)
        report["post_import_digest"] = source_digest(db)
        marker = db.get(QuotaReadiness, "baseline")
        if marker is None:
            marker = QuotaReadiness(id="baseline")
            db.add(marker)
        marker.ready, marker.baseline_version = report["ready"], VERSION
        marker.checked_at, marker.details = now, dict(report)
        db.flush()
        if apply:
            db.commit()
        else:
            db.rollback()
        return report
    except (
        AccountingUnavailable,
        ValueError,
        KeyError,
        TypeError,
        ArithmeticError,
        ValidationError,
    ) as error:
        db.rollback()
        report["blockers"].append(
            {"code": "capacity_unverified", "message": str(error)}
        )
        return report
    except BaseException:
        db.rollback()
        raise


def check_ready(db):
    """Recheck source/ledger, receipt and original freshness without any writes."""
    now = datetime.now(UTC)
    result = _report(now, False)
    try:
        marker = db.get(QuotaReadiness, "baseline")
        _require(
            marker is not None and marker.baseline_version == VERSION,
            "A reviewed fresh-capacity-v1 baseline is required",
        )
        details = marker.details
        _require(
            details.get("capacity_ready")
            and details.get("fresh_receipt") == _receipt(db),
            "Capacity proof or immutable receipt linkage is missing",
        )
        if accounting.quotas_enabled():
            projects = _runtime_current(db, details, now)
            result.update(
                capacity_ready=True,
                runtime_ready=True,
                baseline=details,
            )
            blockers = _runtime_accounting(db, projects, now)
            result.update(
                accounting_ready=not blockers,
                accounting_blockers=blockers,
                ready=not blockers,
                message="Current runtime and billing evidence verified"
                if not blockers
                else "Current billing coverage/rates require reconciliation",
            )
            return result
        _require(
            details.get("post_import_digest") == source_digest(db),
            "Current source/ledger changed; repeat reviewed capacity reconciliation",
        )
        for project_id, entry in details["evidence"]["projects"].items():
            project = db.get(Project, project_id)
            _timing(
                project,
                entry,
                now,
                legacy._timestamp(details["evidence"]["maintenance"]["since"]),
            )
            if entry["adapter"] == native.ADAPTER:
                supplied = entry["storage_observation"]
                measurement = native.storage_measurement(
                    db, project, entry["generation_id"], supplied["identity"], now
                )
                _require(
                    supplied["measurement"] == measurement.evidence(),
                    "Native latest storage attempt changed",
                )
            else:
                storage.validate(
                    entry["storage_observation"],
                    project_id,
                    entry["generation_id"],
                    project.host_id,
                    now,
                )
        result.update(capacity_ready=True, baseline=details)
        _readiness(db, result, details["evidence"], now)
        result[
            "message"
        ] = "Capacity verified; runtime and accounting gates remain separate"
    except (AccountingUnavailable, ValueError, KeyError, TypeError) as error:
        result["message"] = str(error)
    return result


def _runtime_current(db, details, now):
    """Enabled writers evolve the ledger; original replay remains immutable.

    As in the legacy gate, source equality belongs to pre-enable maintenance.
    Normal admission/reconciliation owns compute/billing observations. Validate
    its current supported identities and physical evidence, never old baseline
    observation times or an import-time whole-database fingerprint.
    """
    _require(
        details.get("capacity_ready"),
        "Reviewed fresh capacity proof is unavailable",
    )
    projects = list(db.query(Project).order_by(Project.id))
    source = {
        "projects": [
            {"project_id": p.id, "adapter": _adapter(db, p), "empty": False}
            for p in projects
        ],
        **_retained_inventory(db),
    }
    _inventory_blockers(db, source)
    for project in projects:
        _runtime_project(db, project, now)
    return projects


def _runtime_accounting(db, projects, now):
    blockers = []
    for project in projects:
        try:
            _runtime_billing_project(db, project, now)
        except (
            AccountingUnavailable,
            ValueError,
            KeyError,
            TypeError,
            ArithmeticError,
        ) as error:
            blockers.append({"project_id": project.id, "message": str(error)})
    return blockers


def _runtime_billing_project(db, project, now):
    """Compare current sources to the confirmed bounded nested on/off profile.

    Confirmed ledger observations own compute identity; do not renew their time
    or infer power from editable project state. Pending unobserved growth remains
    reserved and is not treated as an already billable deployed environment.
    """
    rows = _current(db, project)
    from app.services.native_managed_deploy import pending_profile

    if pending_profile(db, project, rows):
        if rows[0].dispatch_metadata["native_deploy"].get("initialized_report"):
            from app.services import metering_service as metering
            from app.services.native_managed_power import billing_snapshot

            _billing_coverage(
                db,
                project,
                metering._quota_snapshot(
                    project, billing_snapshot(db, project, rows), now
                ),
                metering.load_type_rates(db),
                now,
            )
        else:
            _require(
                not db.query(MeteringInterval)
                .filter_by(project_id=project.id, ended_at=None)
                .first(),
                "Pending native deployment has unexplained billing sources",
            )
        return
    if rows and storage.storage_adapter(rows[0]) == native.ADAPTER:
        from app.services import metering_service as metering
        from app.services.native_managed_power import billing_snapshot

        _billing_coverage(
            db,
            project,
            metering._quota_snapshot(project, billing_snapshot(db, project, rows), now),
            metering.load_type_rates(db),
            now,
        )
        return
    retained = [row for row in rows if any(row.observed.values())]
    if not retained:
        _require(
            not db.query(MeteringInterval)
            .filter_by(project_id=project.id, ended_at=None)
            .first(),
            "Unobserved pending/draft work has unexplained current billing sources",
        )
        return
    _require(
        project.state in {"active", "stopped"},
        "Retained billing state requires independent infrastructure reconciliation",
    )
    _require(
        all(row.state == "confirmed" for row in retained),
        "Current compute billing profile remains partially observed",
    )
    root = storage.measurement_row(rows)
    topology = operations.retained_topology(project)
    configured = topology_resources(
        topology, virtual_sizes=root.dispatch_metadata.get("virtual_sizes", {})
    )
    current = admission._vector_sum(row.observed for row in retained)
    _require(
        current.projects == 1
        and (current.cpu, current.ram_bytes)
        in {(0, 0), (configured.cpu, configured.ram_bytes)},
        "Current compute billing profile is incomplete or mixed",
    )
    allocations = [
        db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
        for row in retained
    ]
    _require(
        all(row.observed_at is not None for row in allocations),
        "Current compute observation time is unavailable",
    )
    observed = max(accounting.utc(row.observed_at) for row in allocations)
    states = {
        node["id"]: "running" if current.cpu or current.ram_bytes else "shutoff"
        for node in topology.get("nodes", [])
        if node.get("type") == "vmNode"
    }
    snap, rates = _billing_snapshot(
        db, project, {"states": states, "observed_at": observed.isoformat()}
    )
    _billing_coverage(db, project, snap, rates, now)


def _runtime_project(db, project, now):
    rows = _current(db, project)
    if _empty(db, project, rows):
        return
    from app.services.native_managed_deploy import pending_profile

    if pending_profile(db, project, rows):
        return
    operations.supported(db, project, deployed=True)
    _require(
        bool(rows), "Current retained project has no accounted operation generation"
    )
    identity = accounting.attribution(db, project)
    for row in rows:
        _require(
            (row.owner_id, row.organization_id)
            == (identity["owner_id"], identity["organization_id"]),
            "Current ledger attribution requires ordinary transfer/reconciliation",
        )
        if any(row.observed.values()):
            allocation = (
                db.query(QuotaAllocation)
                .filter_by(operation_id=row.operation_id)
                .one_or_none()
            )
            _require(
                allocation is not None
                and allocation.observed == row.observed
                and (allocation.owner_id, allocation.organization_id)
                == (row.owner_id, row.organization_id),
                "Current retained allocation pairing is inconsistent",
            )
    storage.scope_evidence(db, "project", project.id, now)
    if rows and storage.storage_adapter(rows[0]) == native.ADAPTER:
        from app.services.native_managed_power import current_profile

        current_profile(db, project, rows)
        return
    roots = [
        row
        for row in rows
        if row.dispatch_metadata.get("kind") == "deploy" and any(row.observed.values())
    ]
    for root in roots:
        evidence = root.dispatch_metadata["storage_evidence"]
        amount = storage.validate(
            evidence, project.id, operations._generation(root), project.host_id, now
        )
        _require(
            root.observed["storage_bytes"] == amount,
            "Current storage evidence differs from recorded physical allocation",
        )
