"""Privileged local native-report workflow; every command selects a DB target.

DB credential holders are trusted to submit evidence. Operator-owned files and
collector allowlists are not signatures. No HTTP ingestion or quota mutation.
"""

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from pydantic import Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.kubevirt_storage import NativeEnrollmentRequest, NativeReportingTarget
from app.models.provider import Provider
from app.schemas.kubevirt_storage import CollectionLimits, Evidence, NativeStorageReport
from app.schemas.kubevirt_storage_enrollment import EnrollmentRequest, VerificationPins, VerifiedEnrollment
from app.services.kubevirt_storage_enrollment import (
    digest, enroll_native_roots, prepare_native_enrollment, verify_native_enrollment,
)
from app.services.kubevirt_storage_reports import import_report
from app.services.kubevirt_storage_transport import CapabilityProfile, DirectTransport, PoolConfig, RbdReader
from collect_kubevirt_storage import atomic_report


def read_operator_file(path, limit=64 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("operator-owned regular file without group/other writes required")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("operator input exceeds size bound")
    return json.loads(raw)


class BackendConfig(Evidence):
    ceph_fsid: UUID
    pools: list[PoolConfig] = Field(min_length=1, max_length=100)
    ceph_conf: str
    ceph_keyring: str


class ProviderReader:
    """Only bounded GET/list methods, using the configured provider API identity."""

    def __init__(self, provider):
        from kubernetes import client
        credentials = provider.get_credentials()
        config = client.Configuration()
        config.host = credentials["api_url"]
        config.api_key = {"authorization": "Bearer " + credentials["token"]}
        config.verify_ssl = credentials.get("verify_ssl", False)
        self.api = client.ApiClient(config)
        self.api.rest_client.pool_manager.connection_pool_kw["retries"] = 0
        self.core = client.CoreV1Api(self.api)
        self.custom = client.CustomObjectsApi(self.api)

    def namespace(self, name):
        result = self.core.read_namespace(name, _request_timeout=15)
        return self.api.sanitize_for_serialization(result)

    def cluster_identity(self):
        return self.namespace("kube-system")["metadata"]["uid"]

    def projects(self, namespace):
        result = self.custom.list_namespaced_custom_object(
            "troshka.redhat.com", "v1alpha1", namespace, "troshkaprojects",
            limit=2, _request_timeout=15,
        )
        if result.get("metadata", {}).get("continue"):
            raise ValueError("ambiguous project namespace")
        return result["items"]

    def close(self):
        self.api.close()


def _verify(db, args):
    request = EnrollmentRequest.model_validate(read_operator_file(args.request))
    prepared = db.get(NativeEnrollmentRequest, str(request.request_id))
    if prepared is None or prepared.digest != digest(request) or str(request.target_id) != args.target_id:
        raise ValueError("independent prepared request required")
    pins = VerificationPins.model_validate(read_operator_file(args.pins))
    backends = [BackendConfig.model_validate(value) for value in read_operator_file(args.backend_config)]
    readers = {}
    try:
        for identity in {str(p.provider_id) for p in request.projects}:
            provider = db.get(Provider, identity)
            if provider is None or provider.type != "kubevirt":
                raise ValueError("native provider missing")
            readers[identity] = ProviderReader(provider)
        backend_readers = {
            str(config.ceph_fsid): RbdReader(
                DirectTransport(CollectionLimits(), ceph_conf=config.ceph_conf, ceph_keyring=config.ceph_keyring),
                str(config.ceph_fsid), config.pools,
                CapabilityProfile(command_version="enrollment-identity-only"),
            ) for config in backends
        }
        manifest = verify_native_enrollment(request, readers, backend_readers, pins=pins)
        atomic_report(args.output, manifest)
        return manifest.digest
    finally:
        for reader in readers.values():
            reader.close()


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("TROSHKA_NATIVE_REPORT_DATABASE_URL"),
                        help="Explicit target URL; only TROSHKA_NATIVE_REPORT_DATABASE_URL is a fallback")
    parser.add_argument("--target-id", required=True, type=lambda v: str(UUID(v)))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure-target", help="Create immutable reporting target UUID once; no schema creation")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--project-id", action="append", required=True)
    prepare.add_argument("--output", required=True)
    verify = commands.add_parser("verify", help="Read-only cluster/backend verification, independent of reports")
    for name in ("request", "pins", "backend-config", "output"):
        verify.add_argument("--" + name, required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--enrollment", required=True)
    ingest = commands.add_parser("import")
    for name in ("report", "enrollment-id", "enrollment-digest"):
        ingest.add_argument("--" + name, required=True)
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
    if args.command == "verify":
        return _verify(db, args)
    if args.command == "enroll":
        manifest = VerifiedEnrollment.model_validate(read_operator_file(args.enrollment))
        return enroll_native_roots(db, manifest, expected_target_id=args.target_id)
    report = NativeStorageReport.model_validate(read_operator_file(args.report))
    return import_report(db, report, enrollment_id=args.enrollment_id,
                         expected_enrollment_digest=args.enrollment_digest, now=datetime.now(UTC))


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
        print("Native reporting operation failed; check target, enrollment and evidence.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
