"""Protected native collection runtime for the operator CLI and RQ jobs."""

import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.core.config import config
from app.core.database import SessionLocal
from app.models.provider import Provider
from app.schemas.kubevirt_storage import BackendScope, CollectionLimits
from app.services.kubevirt_storage_inventory import (
    KubernetesReader,
    read_static_kubeconfig,
)
from app.services.native_collection_config import (
    ToolboxBackendConfig,
    _backend_reader,
    _managed_inputs,
)
from app.services.native_provider_reader import ProviderReader


class PlatformStorageUnavailableError(RuntimeError):
    """Installation was verified; only the independent platform scan failed."""


def profile_for(domain_id, *, readiness=False):
    identity = str(UUID(str(domain_id)))
    profiles = config.get("native_collection.profiles", [])
    matches = [p for p in profiles if str(p.get("domain_id")) == identity]
    if len(matches) != 1:
        raise ValueError("exactly one protected collection profile required")
    profile = matches[0]
    paths = {}
    for field in ("pins", "backend_config", "inventory", "kubeconfig"):
        path = profile.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("collector configuration paths must be absolute")
        paths[field] = path
    settings = config.get("native_collection", {})
    timeout = (
        int(settings.get("job_timeout_seconds", 240))
        if isinstance(settings, dict)
        else 240
    )
    margin = 30
    if readiness:
        from app.services.native_collection_jobs import readiness_timeout

        timeout, margin = readiness_timeout(), 10
    return SimpleNamespace(
        **paths,
        target_id=str(UUID(str(profile["target_id"]))),
        command_timeout_seconds=15,
        verification_timeout_seconds=max(1, min(900, timeout) - margin),
    )


def run_collection(domain_id, project_id=None):
    """IDs only; credentials and authority come from administrator configuration."""
    domain_id = str(UUID(str(domain_id)))
    if project_id is not None:
        project_id = str(UUID(str(project_id)))
    started = datetime.now(UTC)
    try:
        args = profile_for(domain_id)
        pins, _, _ = _managed_inputs(args)
        if str(pins.domain_id) != domain_id:
            raise ValueError("protected collection domain mismatch")
        with SessionLocal() as db:
            report = collect_configured(
                db, args, project_id=project_id, coordinate_only=project_id is None
            )
            if report is None:
                return "shared observation published"
            if report.collection_errors or report.inventory_blockers:
                raise ValueError("managed collection persisted an incomplete attempt")
            return str(report.report_id)
    except PlatformStorageUnavailableError:
        raise
    except Exception:
        record_runtime_failure(domain_id, project_id, started)
        raise


def run_readiness(domain_id):
    """Refresh installed identity only, without enrollment work or storage scans."""
    domain_id = str(UUID(str(domain_id)))
    started = datetime.now(UTC)
    try:
        args = profile_for(domain_id, readiness=True)
        pins, _, _ = _managed_inputs(args)
        if str(pins.domain_id) != domain_id:
            raise ValueError("protected readiness domain mismatch")
        with SessionLocal() as db:
            collect_configured(db, args, readiness_only=True)
        return "readiness observation published"
    except Exception:
        record_runtime_failure(domain_id, None, started, readiness=True)
        raise


def record_runtime_failure(domain_id, project_id, started, *, readiness=False):
    """Configuration/transport failures must not retain an older successful attempt."""
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.services.kubevirt_storage_enrollment import utc, validated_enrollment
    from app.services.kubevirt_storage_reports import import_report, report_cursor
    from app.services.native_collection_jobs import assert_collection_lease
    from app.services.native_collection_shared import publish_shared_observation
    from app.services.native_collection_shared_storage import (
        publish_shared_storage_failure,
    )
    from app.services.native_managed_collector import failed_report

    assert_collection_lease()
    with SessionLocal() as db:
        domain = db.get(NativeCollectionDomain, domain_id)
        if domain is None:
            return
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        enrollment_id = domain.enrollment_id
        if project_id is not None:
            cursor = report_cursor(db, domain, project_id)
            if cursor.latest_started_at and utc(cursor.latest_started_at) >= started:
                return  # cycle already published this attempt or a newer one won.
            report = failed_report(
                manifest, manifest.pins.collector_ids[0], started, project_id
            )
            import_report(
                db,
                report,
                enrollment_id=enrollment_id,
                expected_enrollment_digest=manifest.digest,
                now=datetime.now(UTC),
            )
            assert_collection_lease()
            db.commit()
            return
    if readiness:
        publish_shared_observation(
            SessionLocal,
            domain_id,
            enrollment_id,
            None,
            started,
            assert_current=assert_collection_lease,
        )
    else:
        publish_shared_storage_failure(SessionLocal, domain_id, enrollment_id, started)


def readiness_snapshot(db, pins, target_id):
    from app.models.kubevirt_storage import NativeReportingTarget
    from app.services.native_managed_collector import anchored

    domain, manifest = anchored(db, pins)
    target = db.get(NativeReportingTarget, 1)
    if target is None or target.target_id != target_id:
        raise ValueError("readiness reporting target changed")
    return domain.enrollment_id, manifest


def observe_readiness(sessions, pins, target_id, readers, backend):
    from app.services import native_initialization as initialization
    from app.services.native_collection_jobs import assert_collection_lease
    from app.services.native_collection_shared import publish_shared_observation

    started = datetime.now(UTC)
    with sessions() as db:
        enrollment_id, manifest = readiness_snapshot(db, pins, target_id)
    profile = manifest.pins.initialization_evidence
    if profile is None:
        raise ValueError("protected initialization profile required")
    observed = initialization.observe(
        profile,
        readers[str(profile.target.provider_id)],
        backend,
        clock=lambda: datetime.now(UTC),
    )
    accepted = publish_shared_observation(
        sessions,
        str(pins.domain_id),
        enrollment_id,
        observed,
        started,
        assert_current=assert_collection_lease,
    )
    if not accepted:
        raise ValueError("Readiness observation incomplete or superseded")


def coordinate(
    sessions, pins, target_id, readers, backend, *, limits=None, shared_inventory=None
):
    from app.services.native_collection_shared_storage import (
        collect_shared_storage,
        publish_shared_storage_failure,
    )
    from app.services.native_managed_collector import anchored, prepare_work

    started = datetime.now(UTC)
    with sessions() as db:
        domain, _ = anchored(db, pins)
        enrollment_id = domain.enrollment_id
    try:
        prepare_work(sessions, pins, target_id, readers, backend)
        with sessions() as db:
            domain, manifest = anchored(db, pins)
            enrollment_id = domain.enrollment_id
    except Exception:
        publish_shared_storage_failure(
            sessions, str(pins.domain_id), enrollment_id, started
        )
        raise
    # Platform cache uncertainty cannot undo independently verified installation
    # identity. Project graphs still validate their own exact shared ancestors.
    try:
        collect_shared_storage(
            sessions,
            manifest,
            backend,
            started,
            limits=limits,
            cache_inventory=(
                (lambda: shared_inventory(manifest, started))
                if shared_inventory
                else None
            ),
        )
    except Exception as error:
        raise PlatformStorageUnavailableError(
            "Shared platform storage attempt failed"
        ) from error


def collect_configured(
    db, args, *, project_id=None, coordinate_only=False, readiness_only=False
):
    from sqlalchemy.orm import sessionmaker

    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_inventory import discover_inventory
    from app.services.native_managed_collector import (
        cycle,
        retained_inputs,
        retiring_inputs,
    )

    pins, config, backend_config = _managed_inputs(args)
    if readiness_only:
        readiness_snapshot(db, pins, args.target_id)  # Before any network I/O.
    limits = CollectionLimits(
        command_timeout_seconds=args.command_timeout_seconds,
        scan_timeout_seconds=args.verification_timeout_seconds,
    )
    deadline = time.monotonic() + limits.scan_timeout_seconds
    provider_id = (
        str(pins.initialization_evidence.target.provider_id)
        if pins.initialization_evidence
        else None
    )
    provider = db.get(Provider, provider_id) if provider_id else None
    if provider is None:
        raise ValueError("protected initialization provider required")
    db.expunge(provider)
    sessions = sessionmaker(bind=db.get_bind())
    db.rollback()  # Never hold the CLI's initial read transaction over network I/O.
    with ExitStack() as cleanup:
        readers = {
            provider_id: ProviderReader.from_kubeconfig(
                provider_id, read_static_kubeconfig(args.kubeconfig), limits, deadline
            )
        }
        cleanup.callback(readers[provider_id].close)
        kubeconfig_data = (
            read_static_kubeconfig(backend_config.kubeconfig)
            if isinstance(backend_config, ToolboxBackendConfig)
            else None
        )
        backend: Any = _backend_reader(
            backend_config, limits, deadline, cleanup, kubeconfig_data
        )
        backend.capability = config.capability
        if readiness_only:
            observe_readiness(sessions, pins, args.target_id, readers, backend)
            return None
        kube: Any = KubernetesReader(
            args.kubeconfig,
            backend,
            collector_id=config.collector_id,
            cache_roots=[r.model_dump(mode="json") for r in pins.cache_roots],
            scan_deadline=deadline,
            csi_requests=config.csi_requests,
            max_objects=limits.max_objects,
        )
        cleanup.callback(kube.api.close)

        if coordinate_only:

            def shared_inventory(manifest, started):
                _, caches = retained_inputs(sessions, manifest.pins.domain_id)
                kube.scan_started_at = started
                kube.retained_cache_bindings = caches
                kube.cache_roots = [
                    r.model_dump(mode="json") for r in manifest.pins.cache_roots
                ]
                return discover_inventory(kube, [], [])

            coordinate(
                sessions,
                pins,
                args.target_id,
                readers,
                backend,
                limits=limits,
                shared_inventory=shared_inventory,
            )
            return None

        def collect(manifest, previous, started):
            retained, caches = retained_inputs(sessions, manifest.pins.domain_id)
            retiring, retired = retiring_inputs(sessions, manifest.pins.domain_id)
            from app.services.kubevirt_storage_enrollment import root_key
            from app.services.native_managed_collector import scoped_roots

            if project_id is not None:
                retained = [b for b in retained if str(b.root.project_id) == project_id]
                retiring = [r for r in retiring if str(r.root.project_id) == project_id]

            roots = [
                r
                for r in scoped_roots(manifest, project_id)
                if root_key(r) not in retired
            ]
            kube.scan_started_at = backend.scan_started_at = started
            kube.retained_cache_bindings = caches
            kube.retiring_intents = retiring
            kube.cache_roots = [
                r.model_dump(mode="json") for r in manifest.pins.cache_roots
            ]
            from app.services.native_managed_verification import lineage_inputs

            references, lineages = lineage_inputs(sessions, manifest)

            def discover():
                inventory = discover_inventory(kube, retained, roots)
                if project_id is not None:
                    sources = {
                        p.source.image_key
                        for p in lineages
                        if str(p.binding.root.project_id) == project_id
                    }
                    inventory = inventory.model_copy(
                        update={
                            "cache_bindings": [
                                b
                                for b in inventory.cache_bindings
                                if b.image_key in sources
                            ],
                            "absent_cache_bindings": [
                                b
                                for b in inventory.absent_cache_bindings
                                if b.image_key in sources
                            ],
                        }
                    )
                return inventory.model_copy(
                    update={
                        "project_scope": (
                            UUID(project_id) if project_id is not None else None
                        ),
                        "backend_scope": BackendScope(
                            cluster_keys=sorted(
                                {p.cluster_key for p in manifest.pins.provider_clusters}
                            ),
                            pool_keys=manifest.pins.pool_keys,
                        ),
                        "clone_references": references,
                    }
                )

            inventory = discover()
            backend.recheck_inventory = discover
            return collect_report(
                inventory,
                backend,
                limits=limits,
                clock=lambda: datetime.now(UTC),
                clone_lineages=lineages,
            )

        report = cycle(
            sessions,
            pins,
            args.target_id,
            config.collector_id,
            readers,
            backend,
            collect,
            project_id=project_id,
        )
        return report
