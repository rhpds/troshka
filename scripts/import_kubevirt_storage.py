"""Privileged local native-report workflow; every command selects a DB target.

DB credential holders are trusted to submit evidence. Operator-owned files and
collector allowlists are not signatures. No HTTP ingestion or quota mutation.
"""

import argparse
import json
import os
import stat
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from app.models.kubevirt_storage import NativeEnrollmentRequest, NativeReportingTarget
from app.models.provider import Provider
from app.schemas.kubevirt_storage import (
    CollectionLimits,
    Evidence,
    NativeStorageReport,
    ReadResult,
)
from app.schemas.kubevirt_storage_enrollment import (
    EnrollmentRequest,
    VerificationPins,
    VerifiedEnrollment,
)
from app.services.kubevirt_storage_enrollment import (
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    verify_native_enrollment,
)
from app.services.kubevirt_storage_inventory import (
    KubernetesReader,
    read_static_kubeconfig,
)
from app.services.kubevirt_storage_reports import import_report
from app.services.kubevirt_storage_transport import (
    CapabilityProfile,
    DirectTransport,
    PoolConfig,
    RbdReader,
    ToolboxConfig,
    ToolboxTransport,
)
from collect_kubevirt_storage import atomic_report
from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def read_operator_file(path, limit=64 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ValueError(
                "operator-owned regular file without group/other writes required"
            )
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("operator input exceeds size bound")
    return json.loads(raw)


class BackendConfig(Evidence):
    ceph_fsid: UUID
    pools: list[PoolConfig] = Field(min_length=1, max_length=100)


class DirectBackendConfig(BackendConfig):
    # Legacy direct documents may omit mode; both credential paths stay mandatory.
    mode: Literal["direct"] = "direct"
    ceph_conf: str = Field(min_length=1)
    ceph_keyring: str = Field(min_length=1)


class PinnedToolboxConfig(ToolboxConfig):
    cluster_key: str = Field(min_length=1)
    namespace: Literal["openshift-storage"]
    pod_name: str = Field(pattern=r"^rook-ceph-tools-[A-Za-z0-9_.-]+$")
    pod_uid: str = Field(min_length=1)
    container: Literal["rook-ceph-tools"]
    allowed_pools: list[str] = Field(min_length=1, max_length=100)


class ToolboxBackendConfig(BackendConfig):
    mode: Literal["toolbox"]
    kubeconfig: str = Field(min_length=2, pattern=r"^/")
    toolbox: PinnedToolboxConfig

    @model_validator(mode="after")
    def matching_pins(self):
        if self.toolbox.ceph_fsid != str(self.ceph_fsid):
            raise ValueError("toolbox Ceph identity mismatch")
        allowed = self.toolbox.allowed_pools
        if len(set(allowed)) != len(allowed) or set(allowed) != {
            p.name for p in self.pools
        }:
            raise ValueError("toolbox pool allowlist must match configured pools")
        return self


class ToolboxIdentityReader(KubernetesReader):
    """Reuse collector identity GETs with verification's budget and cleanup."""

    def __init__(self, kubeconfig, limits, deadline, kubeconfig_data):
        self.command_timeout_seconds = limits.command_timeout_seconds
        try:
            super().__init__(
                kubeconfig,
                None,
                collector_id="enrollment-identity-only",
                cache_roots=[],
                scan_deadline=deadline,
                kubeconfig_data=kubeconfig_data,
            )
        except Exception:
            if getattr(self, "api", None) is not None:
                self.api.close()
            raise

    def _timeout(self):
        return min(super()._timeout(), self.command_timeout_seconds)


class ProviderReader:
    """Only bounded GET/list methods, using the configured provider API identity."""

    def __init__(self, provider, limits, deadline):
        from kubernetes import client

        credentials = provider.get_credentials()
        config = client.Configuration()
        config.host = credentials["api_url"]
        config.api_key = {"authorization": "Bearer " + credentials["token"]}
        config.verify_ssl = credentials.get("verify_ssl", False)
        self.api = client.ApiClient(config)
        self.provider_id = provider.id
        try:
            self.api.rest_client.pool_manager.connection_pool_kw["retries"] = 0
            self.core = client.CoreV1Api(self.api)
            self.custom = client.CustomObjectsApi(self.api)
            self.limits, self.deadline = limits, deadline
        except Exception:
            self.api.close()
            raise

    def _timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("verification limit")
        return min(15, self.limits.command_timeout_seconds, remaining)

    def namespace(self, name):
        result = self.core.read_namespace(name, _request_timeout=self._timeout())
        return cast(dict[str, Any], self.api.sanitize_for_serialization(result))

    def cluster_identity(self):
        return self.namespace("kube-system")["metadata"]["uid"]

    def projects(self, namespace):
        result = cast(
            dict[str, Any],
            self.custom.list_namespaced_custom_object(
                "troshka.redhat.com",
                "v1alpha1",
                namespace,
                "troshkaprojects",
                limit=2,
                _request_timeout=self._timeout(),
            ),
        )
        if result.get("metadata", {}).get("continue"):
            raise ValueError("ambiguous project namespace")
        return result["items"]

    def close(self):
        self.api.close()

    def pending_root(self, target, operation_id, profile):
        from app.services.native_managed_deploy import bind
        from app.services.native_managed_power import helpers

        return bind(helpers().PowerClient(self.api), target, operation_id, profile)

    def storage_class(self, name) -> dict[str, Any]:
        from kubernetes import client

        result = client.StorageV1Api(self.api).read_storage_class(
            name, _request_timeout=self._timeout()
        )
        return cast(dict[str, Any], self.api.sanitize_for_serialization(result))

    def _identity_list(self, method, namespace, selector):
        if selector.get("matchExpressions") or not selector.get("matchLabels"):
            raise ValueError("unsupported CSI selector")
        result = cast(
            dict[str, Any],
            self.api.sanitize_for_serialization(
                method(
                    namespace,
                    label_selector=",".join(
                        f"{k}={v}" for k, v in sorted(selector["matchLabels"].items())
                    ),
                    limit=min(self.limits.max_objects, 1000),
                    _request_timeout=self._timeout(),
                )
            ),
        )
        if result.get("metadata", {}).get("continue"):
            raise ValueError("CSI inventory exceeds bound")
        return result["items"]

    def initialization_identity(self, storage_class_name):
        from kubernetes import client

        started = datetime.now(UTC)
        try:
            sc = self.storage_class(storage_class_name)
            driver = sc["provisioner"]
            namespace = sc["parameters"]["clusterID"]
            if (
                namespace != "openshift-storage"
                or driver != namespace + ".rbd.csi.ceph.com"
            ):
                return ReadResult(status="unsupported", collected_at=started)
            apps, storage = client.AppsV1Api(self.api), client.StorageV1Api(self.api)

            def read(method, *args) -> dict[str, Any]:
                return cast(
                    dict[str, Any],
                    self.api.sanitize_for_serialization(
                        method(*args, _request_timeout=self._timeout())
                    ),
                )

            controller = read(
                apps.read_namespaced_deployment, driver + "-ctrlplugin", namespace
            )
            node = read(
                apps.read_namespaced_daemon_set, driver + "-nodeplugin", namespace
            )
            value = {
                "provider_id": self.provider_id,
                "cluster_key": self.cluster_identity(),
                "storage_class": sc,
                "namespace": self.namespace(namespace),
                "driver": read(storage.read_csi_driver, driver),
                "controller": controller,
                "node": node,
                "replica_sets": self._identity_list(
                    apps.list_namespaced_replica_set,
                    namespace,
                    controller["spec"]["selector"],
                ),
                "controller_pods": self._identity_list(
                    self.core.list_namespaced_pod,
                    namespace,
                    controller["spec"]["selector"],
                ),
                "node_pods": self._identity_list(
                    self.core.list_namespaced_pod, namespace, node["spec"]["selector"]
                ),
            }
            return ReadResult(status="complete", collected_at=started, value=value)
        except (
            Exception
        ):  # noqa: BLE001 - sanitize API/transport failures at reader boundary
            return ReadResult(status="unavailable", collected_at=started)


def _verify(db, args):
    request = EnrollmentRequest.model_validate(read_operator_file(args.request))
    prepared = db.get(NativeEnrollmentRequest, str(request.request_id))
    if (
        prepared is None
        or prepared.digest != digest(request)
        or str(request.target_id) != args.target_id
    ):
        raise ValueError("independent prepared request required")
    pins = VerificationPins.model_validate(read_operator_file(args.pins))
    backends = TypeAdapter(
        list[DirectBackendConfig | ToolboxBackendConfig]
    ).validate_python(read_operator_file(args.backend_config))
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
        for identity in {str(p.provider_id) for p in request.projects}:
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
        manifest = verify_native_enrollment(
            request, readers, backend_readers, pins=pins
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("verification limit")
        atomic_report(args.output, manifest)
        return manifest.digest


def _check_backend_coverage(backends, pins):
    fsids = [str(config.ceph_fsid) for config in backends]
    configured = {(str(c.ceph_fsid), p.pool_id) for c in backends for p in c.pools}
    pinned = {(p.ceph_fsid, p.pool_id) for p in pins.pool_keys}
    if len(fsids) != len(set(fsids)) or configured != pinned:
        raise ValueError("backend configuration must exactly cover pinned pools")


def _backend_reader(config, limits, deadline, cleanup, kubeconfig_data):
    if isinstance(config, ToolboxBackendConfig):
        kube = ToolboxIdentityReader(
            config.kubeconfig, limits, deadline, kubeconfig_data
        )
        cleanup.callback(kube.api.close)
        transport = ToolboxTransport(
            limits, config.toolbox, kube, config.kubeconfig, scan_deadline=deadline
        )
    else:
        transport = DirectTransport(
            limits, ceph_conf=config.ceph_conf, ceph_keyring=config.ceph_keyring
        )
        transport.process.scan_deadline = deadline
    return RbdReader(
        transport,
        str(config.ceph_fsid),
        config.pools,
        CapabilityProfile(command_version="enrollment-identity-only"),
    )


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
    verify = commands.add_parser(
        "verify", help="Read-only cluster/backend verification, independent of reports"
    )
    verify.add_argument("--verification-timeout-seconds", type=int, default=600)
    verify.add_argument("--command-timeout-seconds", type=int, default=15)
    for name in ("request", "pins", "backend-config", "output"):
        verify.add_argument("--" + name, required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--enrollment", required=True)
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
    if args.command == "verify":
        return _verify(db, args)
    if args.command == "reconcile-managed":
        return _managed_cycle(db, args)
    if args.command == "enroll":
        manifest = VerifiedEnrollment.model_validate(
            read_operator_file(args.enrollment)
        )
        return enroll_native_roots(db, manifest, expected_target_id=args.target_id)
    report = NativeStorageReport.model_validate(read_operator_file(args.report))
    return import_report(
        db,
        report,
        enrollment_id=args.enrollment_id,
        expected_enrollment_digest=args.enrollment_digest,
        now=datetime.now(UTC),
    )


def _managed_inputs(args):
    from collect_kubevirt_storage import Manifest

    pins = VerificationPins.model_validate(read_operator_file(args.pins))
    config = Manifest.model_validate(read_operator_file(args.inventory))
    backends = TypeAdapter(
        list[DirectBackendConfig | ToolboxBackendConfig]
    ).validate_python(read_operator_file(args.backend_config))
    _check_backend_coverage(backends, pins)
    if (
        len(backends) != 1
        or config.collector_id not in pins.collector_ids
        or config.capability.initialization_evidence != pins.initialization_evidence
    ):
        raise ValueError(
            "managed collection needs one matching protected initialization/collector profile"
        )
    if config.pools != backends[0].pools or str(config.ceph_fsid) != str(
        backends[0].ceph_fsid
    ):
        raise ValueError("collector/backend pool configuration mismatch")
    return pins, config, backends[0]


def _managed_cycle(db, args):
    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_inventory import discover_inventory
    from app.services.native_managed_collector import cycle, retained_inputs, retiring_inputs
    from sqlalchemy.orm import sessionmaker

    pins, config, backend_config = _managed_inputs(args)
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
        readers = {provider_id: ProviderReader(provider, limits, deadline)}
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

        def collect(manifest, previous, started):
            retained, caches = retained_inputs(sessions, manifest.pins.domain_id)
            retiring, retired = retiring_inputs(sessions, manifest.pins.domain_id)
            from app.services.kubevirt_storage_enrollment import root_key

            roots = [r for r in manifest.pins.project_roots if root_key(r) not in retired]
            kube.scan_started_at = backend.scan_started_at = started
            kube.retained_cache_bindings = caches
            kube.retiring_intents = retiring
            inventory = discover_inventory(kube, retained, roots)
            backend.recheck_inventory = lambda: discover_inventory(
                kube, retained, roots
            )
            return collect_report(
                inventory, backend, limits=limits, clock=lambda: datetime.now(UTC)
            )

        report = cycle(
            sessions,
            pins,
            args.target_id,
            config.collector_id,
            readers,
            backend,
            collect,
        )
        atomic_report(args.output, report)
        if report.collection_errors or report.inventory_blockers:
            raise ValueError("managed collection persisted an incomplete attempt")
        return str(report.report_id)


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error(
            "explicit --database-url or TROSHKA_NATIVE_REPORT_DATABASE_URL required"
        )
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
