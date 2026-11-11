"""Read-only authorized scope projections from one coherent snapshot.

Revocations committed before the snapshot are honored. Concurrent transfers
cannot mix authorization with another version of owner/org membership. PostgreSQL
uses a separate read-only repeatable-read transaction, never caller quota writes.
"""

from contextlib import contextmanager
from datetime import timedelta
from typing import TypedDict

from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from app.models.host import Host
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeProjectRoot,
    NativeRetainedBinding,
    NativeStorageSnapshot,
)
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_storage import NativeStorageReport, NativeStorageUsage
from app.services.kubevirt_storage_enrollment import utc
from app.services.kubevirt_storage_graph import attribute_graph
from app.services.kubevirt_storage_reports import normalized_inventory
from app.services.quota_policy import authorize_policy

MAX_REPORT_AGE = timedelta(minutes=15)
MAX_BYTES = 2**63 - 1
PUBLIC_REASON_CODES = frozenset(
    """
ancestry_cycle parent_depth_limit missing_image unverified_cache_ancestor
duplicate_project_root unresolved_ownership incomplete_measurement incomplete_cache_evidence
csi_snapshot_backend_unresolved snapshot_identity_unresolved scan_limit pvc_binding_unresolved
project_discovery_unavailable cache_discovery_unavailable retained_identity_conflict
kubernetes_discovery_unavailable namespace_identity_mismatch project_identity_mismatch
project_generation_mismatch cluster_identity_mismatch backend_identity_changed
backend_identity_recheck_unavailable backend_identity_or_capability_unavailable
trash_inventory_unavailable inventory_changed inventory_recheck_unavailable
backend_graph_changed backend_graph_recheck_unavailable current_project_provider_mismatch
unenrolled_project_root collection_domain_mismatch unenrolled_backend_pool unenrolled_snapshot_root
unenrolled_binding_root new_binding_requires_complete_discovery unenrolled_cache_namespace
retained_binding_identity_changed expected_root_missing retained_binding_missing
collection_domain_conflict report_unavailable stale_evidence no_native_projects
retained_attribution_unavailable unavailable timeout identity_changed invalid_output unsupported output_limit
aggregation_overflow
""".split()
)


class _AdminFields(TypedDict, total=False):
    platform_bytes: int | None
    unattributed_retained_projects: int
    unattributed_retained_known_bytes: int | None


@contextmanager
def _read_snapshot(db):
    bind = db.get_bind()
    if not isinstance(bind, Engine) or isinstance(
        bind.pool, (StaticPool, SingletonThreadPool)
    ):
        raise HTTPException(
            503, "Native reporting requires an independent snapshot connection"
        )
    isolation = (
        "REPEATABLE READ" if bind.dialect.name == "postgresql" else "SERIALIZABLE"
    )
    with bind.connect().execution_options(isolation_level=isolation) as connection:
        transaction = connection.begin()
        try:
            if bind.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            elif bind.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection, autoflush=False) as snapshot:
                yield snapshot
        finally:
            transaction.rollback()


def _project_membership(db, scope, identity):
    query = (
        db.query(
            Project.id,
            Project.owner_id,
            User.organization_id,
            Provider.type.label("provider_type"),
        )
        .join(User, User.id == Project.owner_id)
        .outerjoin(Host, Host.id == Project.host_id)
        .outerjoin(Provider, Provider.id == Host.provider_id)
    )
    if scope == "project":
        query = query.filter(Project.id == identity)
    elif scope == "user":
        query = query.filter(Project.owner_id == identity)
    else:
        query = query.filter(User.organization_id == identity)
    return query.all()


def _domain_evidence(db, domain, roots):
    if domain.conflicted:
        return None, None, {"collection_domain_conflict"}
    report = (
        db.get(NativeStorageSnapshot, domain.latest_report_id)
        if domain.latest_report_id
        else None
    )
    if report is None:
        return None, None, {"report_unavailable"}
    if not report.accepted:
        return None, None, set(report.reason_codes)
    source = NativeStorageReport.model_validate(report.payload)
    retained = db.query(NativeRetainedBinding).filter_by(domain_id=domain.id).all()
    inventory = normalized_inventory(source, roots, retained)
    return attribute_graph(inventory, source.images), source, set()


def _collect_evidence(db, roots, domains):
    allocations, platform, sources, reasons = {}, [], {}, {}
    for domain in domains:
        domain_roots = [r for r in roots if r.domain_id == domain.id]
        graph, source, failures = _domain_evidence(db, domain, domain_roots)
        reasons[domain.id] = failures
        if graph is None:
            continue
        platform.extend(graph.platform_pools)
        sources[domain.id] = source
        for project in graph.projects:
            identity = str(project.project_id)
            if identity in allocations:
                # Multiple namespaces/domains for a project need explicit resolution.
                allocations[identity] = None
            else:
                allocations[identity] = (project, domain.id)
    return allocations, platform, sources, reasons


def _scope_values(expected, roots, evidence, now):
    allocations, _, sources, failures = evidence
    known, covered, observed, reasons = 0, 0, [], set()
    provisioned: list[int | None] = []
    domains = {r.domain_id for r in roots if r.project_id in expected}
    for domain in domains:
        reasons |= failures.get(domain, {"report_unavailable"})
    for identity in sorted(expected):
        value = allocations.get(identity)
        if value is None:
            reasons.add("expected_root_missing")
            provisioned.append(None)
            continue
        project, domain = value
        if known + project.known_allocated_bytes > MAX_BYTES:
            reasons.add("aggregation_overflow")
        else:
            known += project.known_allocated_bytes
        provisioned.append(project.provisioned_bytes)
        timestamp = _oldest_evidence(sources[domain])
        observed.append(timestamp)
        reasons.update(project.blockers)
        if now - timestamp > MAX_REPORT_AGE:
            reasons.add("stale_evidence")
        elif project.coverage == "complete":
            covered += 1
    if not expected:
        reasons.add("no_native_projects")
    total_provisioned = _provisioned_total(provisioned)
    if total_provisioned is not None and total_provisioned > MAX_BYTES:
        reasons.add("aggregation_overflow")
        provisioned = [None]
    return known, provisioned, covered, observed, reasons


def _oldest_evidence(source):
    # Pool/cache validation participates in exclusive attribution for every root.
    return min([utc(source.started_at), *[utc(i.collected_at) for i in source.images]])


def _admin_fields(actor, evidence, orphan_ids, now) -> _AdminFields:
    if actor.role != "admin":
        return {}
    allocations, platform, sources, failures = evidence
    known = sum(
        allocations[p][0].known_allocated_bytes
        for p in orphan_ids
        if allocations.get(p)
    )
    # Physical pool claims prevent overlapping domains; Task 1 deduplicates images.
    incomplete = (
        not platform
        or any(failures.values())
        or any(p.allocated_bytes is None for p in platform)
    )
    stale = any(
        now - _oldest_evidence(source) > MAX_REPORT_AGE for source in sources.values()
    )
    platform_total = (
        None if incomplete or stale else sum(p.allocated_bytes for p in platform)
    )
    return {
        "platform_bytes": _bounded_total(platform_total),
        "unattributed_retained_projects": len(orphan_ids),
        "unattributed_retained_known_bytes": _bounded_total(known),
    }


def _bounded_total(value):
    return value if value is None or value <= MAX_BYTES else None


def _provisioned_total(values: list[int | None]) -> int | None:
    if not values or None in values:
        return None
    return sum(value for value in values if value is not None)


def _usage_status(expected, covered, known, reasons):
    if "stale_evidence" in reasons:
        return "stale"
    if expected and covered == len(expected) and not reasons:
        return "complete"
    return "partial" if covered or known else "unavailable"


def _read_authorized(db, actor_id, scope, scope_id, now):
    actor = db.get(User, actor_id)
    if actor is None or not actor.access_enabled:
        raise HTTPException(403, "Quota access denied")
    target = authorize_policy(db, actor, scope, scope_id, lock=False)
    projects = _project_membership(db, scope, target.id)
    roots = db.query(NativeProjectRoot).all()
    live_ids = {p for (p,) in db.query(Project.id)}
    orphan_ids = {r.project_id for r in roots if r.project_id not in live_ids}
    enrolled_ids = {r.project_id for r in roots}
    expected = {
        p.id for p in projects if p.provider_type == "kubevirt" or p.id in enrolled_ids
    }
    domain_ids = {
        r.domain_id
        for r in roots
        if r.project_id in expected | (orphan_ids if actor.role == "admin" else set())
    }
    domains = (
        db.query(NativeCollectionDomain)
        .filter(NativeCollectionDomain.id.in_(domain_ids))
        .all()
    )
    evidence = _collect_evidence(db, roots, domains)
    known, provisioned, covered, observed, reasons = _scope_values(
        expected, roots, evidence, now
    )
    if orphan_ids and scope != "project":
        reasons.add("retained_attribution_unavailable")
    status = _usage_status(expected, covered, known, reasons)
    public_reasons = {
        r if r in PUBLIC_REASON_CODES else "collection_incomplete" for r in reasons
    }
    return NativeStorageUsage(
        scope=scope,
        scope_id=target.id,
        status=status,
        allocated_bytes=known if status == "complete" else None,
        known_allocated_bytes=known,
        provisioned_bytes=_provisioned_total(provisioned),
        observed_at=min(observed) if observed else None,
        covered_projects=covered,
        expected_projects=len(expected),
        reason_codes=sorted(public_reasons),
        **_admin_fields(actor, evidence, orphan_ids, now),
    )


def read_native_usage(db, actor, scope, scope_id, now):
    with _read_snapshot(db) as snapshot:
        return _read_authorized(snapshot, actor.id, scope, scope_id, utc(now))
