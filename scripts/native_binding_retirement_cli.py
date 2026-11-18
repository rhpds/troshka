"""Explicitly reviewed absence records; these commands never delete resources."""

import time
from contextlib import ExitStack

from app.schemas.kubevirt_storage import CollectionLimits, ProjectRoot
from app.schemas.native_binding_retirement import (
    PreparedBindingRetirement,
    VerifiedBindingRetirement,
)
from app.services import native_binding_retirement as retirement
from app.services.kubevirt_storage_inventory import (
    KubernetesReader,
    read_static_kubeconfig,
)
from app.services.native_collection_config import (
    ToolboxBackendConfig,
    _backend_reader,
    _managed_inputs,
    read_operator_file,
)
from collect_kubevirt_storage import atomic_report
from pydantic import RootModel

COMMANDS = {
    "prepare-binding-retirement",
    "verify-binding-retirement",
    "review-binding-retirement",
    "apply-binding-retirement",
}


def add_parsers(commands):
    prepare = commands.add_parser(
        "prepare-binding-retirement",
        help="Pin one original binding for independent absence verification",
    )
    for name in ("project-id", "pvc-uid", "administrator", "reason", "output"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument(
        "--root",
        help="Protected JSON exact current root for a reviewed generation transition",
    )
    verify = commands.add_parser(
        "verify-binding-retirement",
        help="Read-only Kubernetes and backend absence verification",
    )
    for name in (
        "request",
        "pins",
        "inventory",
        "backend-config",
        "kubeconfig",
        "output",
    ):
        verify.add_argument("--" + name, required=True)
    verify.add_argument("--verification-timeout-seconds", type=int, default=600)
    verify.add_argument("--command-timeout-seconds", type=int, default=15)
    review = commands.add_parser(
        "review-binding-retirement", help="Write exact absence evidence for review"
    )
    for name in ("evidence", "output"):
        review.add_argument("--" + name, required=True)
    apply = commands.add_parser(
        "apply-binding-retirement",
        help="Store reviewed evidence; grants no deletion authority",
    )
    for name in ("evidence", "review-digest", "output"):
        apply.add_argument("--" + name, required=True)


def dispatch(db, args):
    if args.command == "prepare-binding-retirement":
        root = (
            ProjectRoot.model_validate(read_operator_file(args.root))
            if args.root
            else None
        )
        value = retirement.prepare(
            db,
            args.project_id,
            args.pvc_uid,
            administrator=args.administrator,
            reason=args.reason,
            root=root,
        )
        atomic_report(args.output, RootModel(value))
        return str(value["request_id"])
    if args.command == "verify-binding-retirement":
        return _verify(db, args)
    verified = VerifiedBindingRetirement.model_validate(
        read_operator_file(args.evidence)
    )
    if str(verified.prepared.target_id) != args.target_id:
        raise ValueError("retirement evidence target differs")
    if args.command == "review-binding-retirement":
        value = retirement.review(verified)
        atomic_report(args.output, RootModel(value))
        return value["digest"]
    reference = retirement.apply(db, verified, review_digest=args.review_digest)
    atomic_report(args.output, RootModel([reference.model_dump(mode="json")]))
    return str(reference.receipt_id)


def _verify(db, args):
    prepared = PreparedBindingRetirement.model_validate(
        read_operator_file(args.request)
    )
    retirement.validate_prepared(db, prepared)
    pins, config, backend_config = _managed_inputs(args)
    if (
        str(prepared.target_id) != args.target_id
        or prepared.domain_id != pins.domain_id
    ):
        raise ValueError("retirement target/domain differs from selected profile")
    limits = CollectionLimits(
        command_timeout_seconds=args.command_timeout_seconds,
        scan_timeout_seconds=args.verification_timeout_seconds,
    )
    deadline = time.monotonic() + limits.scan_timeout_seconds
    with ExitStack() as cleanup:
        data = (
            read_static_kubeconfig(backend_config.kubeconfig)
            if isinstance(backend_config, ToolboxBackendConfig)
            else None
        )
        backend = _backend_reader(backend_config, limits, deadline, cleanup, data)
        backend.capability = config.capability
        kube = KubernetesReader(
            args.kubeconfig,
            backend,
            collector_id=config.collector_id,
            cache_roots=[r.model_dump(mode="json") for r in pins.cache_roots],
            scan_deadline=deadline,
            max_objects=limits.max_objects,
        )
        cleanup.callback(kube.api.close)
        verified = retirement.verify(prepared, kube, backend)
        if time.monotonic() >= deadline:
            raise TimeoutError("retirement verification limit")
        atomic_report(args.output, verified)
        return verified.digest
