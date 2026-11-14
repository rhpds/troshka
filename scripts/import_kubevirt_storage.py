"""Privileged local native-report workflow; every command selects a DB target.

DB credential holders are trusted to submit evidence. Operator-owned files and
collector allowlists are not signatures. No HTTP ingestion or quota mutation.
"""

import argparse
import os
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / "src" / "backend" if (_root / "src" / "backend").is_dir() else _root))

from app.models.kubevirt_storage import NativeEnrollmentRequest, NativeReportingTarget
from app.models.provider import Provider
from app.schemas.kubevirt_storage import (
    CollectionLimits,
    NativeStorageReport,
)
from app.schemas.kubevirt_storage_enrollment import (
    EnrollmentRequest,
    VerificationPins,
    VerifiedEnrollment,
)
from app.services.kubevirt_storage_enrollment import (
    authorize_empty_domain,
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    requested_providers,
    verify_native_enrollment,
)
from app.services.kubevirt_storage_inventory import (
    KubernetesReader,
    read_static_kubeconfig,
)
from app.services.kubevirt_storage_reports import import_report
from app.services.native_collection_config import (
    DirectBackendConfig,
    ToolboxBackendConfig,
    _backend_reader,
    _check_backend_coverage,
    read_operator_file,
)
from app.services.native_collection_config import _managed_inputs as _managed_inputs
from app.services.native_provider_reader import ProviderReader
from collect_kubevirt_storage import atomic_report
from pydantic import RootModel, TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def _verify(db, args):
    document = read_operator_file(args.request)
    online = args.command == "verify-online"
    request = EnrollmentRequest.model_validate(document["request"] if online else document)
    prepared = db.get(NativeEnrollmentRequest, str(request.request_id))
    if prepared is None or prepared.digest != digest(request) or str(request.target_id) != args.target_id:
        raise ValueError("independent prepared request required")
    pins = VerificationPins.model_validate(read_operator_file(args.pins))
    backends = TypeAdapter(list[DirectBackendConfig | ToolboxBackendConfig]).validate_python(
        read_operator_file(args.backend_config)
    )
    _check_backend_coverage(backends, pins)
    limits = CollectionLimits(
        command_timeout_seconds=args.command_timeout_seconds,
        scan_timeout_seconds=args.verification_timeout_seconds,
    )
    deadline = time.monotonic() + limits.scan_timeout_seconds
    kubeconfigs = {
        str(config.ceph_fsid): read_static_kubeconfig(config.kubeconfig)
        for config in backends
        if isinstance(config, ToolboxBackendConfig)
    }
    with ExitStack() as cleanup:
        readers = {}
        for identity in requested_providers(request):
            provider = db.get(Provider, identity)
            if provider is None or provider.type != "kubevirt":
                raise ValueError("native provider missing")
            reader = ProviderReader(provider, limits, deadline)
            cleanup.callback(reader.close)
            readers[identity] = reader
        backend_readers = {
            str(config.ceph_fsid): _backend_reader(
                config,
                limits,
                deadline,
                cleanup,
                kubeconfigs.get(str(config.ceph_fsid)),
            )
            for config in backends
        }
        if online:
            readers = _online_readers(args, readers, backend_readers, pins, limits, deadline, cleanup)
        manifest = _verified_document(args, document, request, readers, backend_readers, pins)
        if time.monotonic() >= deadline:
            raise TimeoutError("verification limit")
        atomic_report(args.output, RootModel(manifest) if online else manifest)
        return digest(manifest) if online else manifest.digest


def _online_readers(args, providers, backends, pins, limits, deadline, cleanup):
    if len(providers) != 1 or len(backends) != 1:
        raise ValueError("online verification requires one provider/backend domain per invocation")
    provider_id, provider = next(iter(providers.items()))
    kube = KubernetesReader(
        args.kubeconfig, next(iter(backends.values())),
        collector_id=pins.collector_ids[0],
        cache_roots=[r.model_dump(mode="json") for r in pins.cache_roots],
        scan_deadline=deadline, max_objects=limits.max_objects,
    )
    cleanup.callback(kube.api.close)
    if kube.cluster_identity() != provider.cluster_identity():
        raise ValueError("online reader differs from configured provider cluster")
    return {provider_id: kube}


def _verified_document(args, document, request, readers, backend_readers, pins):
    if args.command == "verify-online":
        from app.services.native_online_enrollment import verify

        return verify(
            document, readers, backend_readers, pins=pins,
            attestation=read_operator_file(args.attestation),
        )
    return verify_native_enrollment(request, readers, backend_readers, pins=pins)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("TROSHKA_NATIVE_REPORT_DATABASE_URL"),
        help="Explicit target URL; only TROSHKA_NATIVE_REPORT_DATABASE_URL is a fallback",
    )
    parser.add_argument("--target-id", required=True, type=lambda v: str(UUID(v)))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "configure-target",
        help="Create immutable reporting target UUID once; no schema creation",
    )
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--project-id", action="append", required=True)
    prepare.add_argument("--output", required=True)
    online_prepare = commands.add_parser(
        "prepare-online", help="Prepare existing-project custody enrollment without stopping workloads"
    )
    online_prepare.add_argument("--project-id", action="append", required=True)
    online_prepare.add_argument("--output", required=True)
    empty = commands.add_parser("prepare-empty-domain", help="Explicit trusted-admin fresh zero-root bootstrap")
    for name in ("pins", "pins-output", "output"):
        empty.add_argument("--" + name, required=True)
    verify = commands.add_parser("verify", help="Read-only cluster/backend verification, independent of reports")
    verify.add_argument("--verification-timeout-seconds", type=int, default=600)
    verify.add_argument("--command-timeout-seconds", type=int, default=15)
    for name in ("request", "pins", "backend-config", "output"):
        verify.add_argument("--" + name, required=True)
    online_verify = commands.add_parser(
        "verify-online", help="Read-only verification of existing project custody and identity"
    )
    online_verify.add_argument("--verification-timeout-seconds", type=int, default=600)
    online_verify.add_argument("--command-timeout-seconds", type=int, default=15)
    for name in ("request", "pins", "backend-config", "kubeconfig", "attestation", "output"):
        online_verify.add_argument("--" + name, required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--enrollment", required=True)
    online_enroll = commands.add_parser(
        "enroll-online", help="Apply verified custody receipt; no provider writes or quota enablement"
    )
    online_enroll.add_argument("--enrollment", required=True)
    ingest = commands.add_parser("import")
    for name in ("report", "enrollment-id", "enrollment-digest"):
        ingest.add_argument("--" + name, required=True)
    managed = commands.add_parser(
        "reconcile-managed",
        help="One scheduled enrollment and full-domain collection/import cycle",
    )
    for name in ("pins", "backend-config", "inventory", "kubeconfig", "output"):
        managed.add_argument("--" + name, required=True)
    managed.add_argument("--verification-timeout-seconds", type=int, default=600)
    managed.add_argument("--command-timeout-seconds", type=int, default=15)
    return parser


def _dispatch(db, args):
    target = db.get(NativeReportingTarget, 1)
    if args.command == "configure-target" and target is None:
        db.add(NativeReportingTarget(id=1, target_id=args.target_id))
        return args.target_id
    if target is None or target.target_id != args.target_id:
        raise ValueError("target UUID does not match configured database")
    if args.command == "configure-target":
        return target.target_id
    if args.command == "prepare":
        request = prepare_native_enrollment(db, args.project_id)
        atomic_report(args.output, request)
        return str(request.request_id)
    if args.command == "prepare-empty-domain":
        pins = authorize_empty_domain(read_operator_file(args.pins))
        request = prepare_native_enrollment(db, [], empty_domain_pins=pins)
        atomic_report(args.pins_output, pins)
        atomic_report(args.output, request)
        return str(request.request_id)
    if args.command in {"prepare-online", "enroll-online"}:
        return _online_dispatch(db, args)
    if args.command in {"verify", "verify-online"}:
        return _verify(db, args)
    if args.command == "reconcile-managed":
        return _managed_cycle(db, args)
    if args.command == "enroll":
        manifest = VerifiedEnrollment.model_validate(read_operator_file(args.enrollment))
        return enroll_native_roots(db, manifest, expected_target_id=args.target_id)
    report = NativeStorageReport.model_validate(read_operator_file(args.report))
    return import_report(
        db,
        report,
        enrollment_id=args.enrollment_id,
        expected_enrollment_digest=args.enrollment_digest,
        now=datetime.now(UTC),
    )


def _online_dispatch(db, args):
    from app.services import native_online_enrollment

    if args.command == "prepare-online":
        prepared = native_online_enrollment.prepare(db, args.project_id)
        atomic_report(args.output, RootModel(prepared))
        return prepared["request"]["request_id"]
    return native_online_enrollment.apply(
        db, read_operator_file(args.enrollment), expected_target_id=args.target_id
    )


def _managed_cycle(db, args):
    from app.services.native_collection_runtime import collect_configured

    report = collect_configured(db, args)
    atomic_report(args.output, report)
    if report.collection_errors or report.inventory_blockers:
        raise ValueError("managed collection persisted an incomplete attempt")
    return str(report.report_id)


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("explicit --database-url or TROSHKA_NATIVE_REPORT_DATABASE_URL required")
    engine = None
    try:
        # Do not use the application SessionLocal or resolve application DB defaults.
        engine = create_engine(args.database_url, hide_parameters=True)
        with Session(engine) as db:
            result = _dispatch(db, args)
            db.commit()
        print(result)
        return 0
    except Exception:  # noqa: BLE001 -- DSNs and client errors can contain secrets
        print(
            "Native reporting operation failed; check target, enrollment and evidence.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
