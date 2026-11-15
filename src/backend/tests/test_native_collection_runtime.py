"""Worker collection scopes and protected configuration boundaries."""

from collections import UserDict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.kubevirt_storage_helpers import report_fixture
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


@pytest.mark.parametrize("field", ["namespace", "container"])
@pytest.mark.parametrize("invalid", [None, "other", "", "trailing-newline"])
@pytest.mark.parametrize("mapping_type", [dict, UserDict])
def test_toolbox_fixed_pins_are_explicit_and_exact(field, invalid, mapping_type):
    from pydantic import ValidationError

    from app.services.native_collection_config import PinnedToolboxConfig

    pins = {
        "cluster_key": "cluster",
        "namespace": "openshift-storage",
        "pod_name": "rook-ceph-tools-test",
        "pod_uid": "toolbox-uid",
        "container": "rook-ceph-tools",
        "ceph_fsid": str(uuid4()),
        "allowed_pools": ["pool"],
    }
    pins = mapping_type(pins)
    assert PinnedToolboxConfig.model_validate(pins).model_dump() == pins
    if invalid is None:
        del pins[field]
    elif invalid == "trailing-newline":
        pins[field] += "\n"
    else:
        pins[field] = invalid
    with pytest.raises(ValidationError):
        PinnedToolboxConfig.model_validate(pins)


def test_project_failure_contains_only_requested_root():
    from app.services.native_managed_collector import failed_report

    source = report_fixture("zero-private-parent.json")
    manifest = SimpleNamespace(
        pins=SimpleNamespace(
            project_roots=source.project_roots, pool_keys=source.pool_keys
        )
    )
    target = str(source.project_roots[0].project_id)
    report = failed_report(manifest, source.collector_id, source.started_at, target)
    assert str(report.project_scope) == target
    assert [str(r.project_id) for r in report.project_roots] == [target]
    assert {str(b.project_id) for b in report.collection_errors} == {target}


def test_unknown_project_scope_is_not_a_domain_failure():
    from app.services.native_managed_collector import scoped_roots

    source = report_fixture("zero-private-parent.json")
    manifest = SimpleNamespace(pins=SimpleNamespace(project_roots=source.project_roots))
    with pytest.raises(ValueError, match="exactly one enrolled"):
        scoped_roots(manifest, str(uuid4()))


def test_worker_profile_rejects_duplicate_or_unconfigured_domain(monkeypatch):
    from app.services import native_collection_runtime as runtime

    domain = str(uuid4())
    profile = dict(
        domain_id=domain,
        target_id=str(uuid4()),
        pins="/pins",
        backend_config="/backends",
        inventory="/inventory",
        kubeconfig="/kube",
    )
    monkeypatch.setattr(
        runtime, "config", SimpleNamespace(get=lambda *a: [profile, profile])
    )
    with pytest.raises(ValueError, match="exactly one"):
        runtime.profile_for(domain)
    monkeypatch.setattr(runtime, "config", SimpleNamespace(get=lambda *a: []))
    with pytest.raises(ValueError, match="exactly one"):
        runtime.profile_for(domain)


def test_worker_profile_never_accepts_relative_paths(monkeypatch):
    from app.services import native_collection_runtime as runtime

    domain = str(uuid4())
    profile = dict(
        domain_id=domain,
        target_id=str(uuid4()),
        pins="../pins",
        backend_config="/backends",
        inventory="/inventory",
        kubeconfig="/kube",
    )
    monkeypatch.setattr(runtime, "config", SimpleNamespace(get=lambda *a: [profile]))
    with pytest.raises(ValueError, match="absolute"):
        runtime.profile_for(domain)


def test_missing_worker_config_records_failed_attempt(monkeypatch):
    from app.services import native_collection_runtime as runtime

    domain, project = str(uuid4()), str(uuid4())
    failures = []

    def missing(_):
        raise ValueError("configuration unavailable")

    monkeypatch.setattr(runtime, "profile_for", missing)
    monkeypatch.setattr(
        runtime,
        "record_runtime_failure",
        lambda *args: failures.append(args),
        raising=False,
    )
    with pytest.raises(ValueError, match="configuration unavailable"):
        runtime.run_collection(domain, project)
    assert len(failures) == 1
    assert failures[0][:2] == (domain, project)


def test_domain_coordinator_keeps_initialization_independent_of_platform_failure(
    native_case, monkeypatch
):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services import native_collection_runtime as runtime
    from app.services import native_collection_shared as initialization_shared
    from app.services import native_collection_shared_storage as storage
    from app.services import native_initialization as initialization
    from app.services import native_managed_collector as collector

    case = native_case
    provider_id = str(case.pins.provider_clusters[0].provider_id)
    profile = SimpleNamespace(target=SimpleNamespace(provider_id=provider_id))
    manifest = SimpleNamespace(pins=SimpleNamespace(initialization_evidence=profile))
    monkeypatch.setattr(
        collector,
        "anchored",
        lambda db, pins: (
            db.get(NativeCollectionDomain, str(pins.domain_id)),
            manifest,
        ),
    )
    monkeypatch.setattr(collector, "prepare_work", lambda *args: None)
    monkeypatch.setattr(initialization, "observe", lambda *args, **kw: "observed")
    called = []
    monkeypatch.setattr(
        storage,
        "collect_shared_storage",
        lambda *args, **kw: called.append(("storage", args[1], kw["limits"])),
    )
    monkeypatch.setattr(
        initialization_shared,
        "publish_shared_observation",
        lambda *args, **kw: called.append(("initialization", args[3])),
    )
    runtime.coordinate(
        case.sessions,
        case.pins,
        case.target_id,
        {provider_id: object()},
        object(),
        limits="bounded",
    )
    assert called == [("storage", manifest, "bounded")]
    called.clear()

    def failed(*args, **kwargs):
        raise ValueError("unrelated platform source unavailable")

    monkeypatch.setattr(storage, "collect_shared_storage", failed)
    with pytest.raises(runtime.PlatformStorageUnavailableError):
        runtime.coordinate(
            case.sessions, case.pins, case.target_id, {provider_id: object()}, object()
        )
    assert called == []


def test_worker_platform_failure_does_not_republish_initialization_failure(
    native_case, monkeypatch
):
    from app.services import native_collection_runtime as runtime

    case = native_case
    monkeypatch.setattr(runtime, "SessionLocal", case.sessions)
    monkeypatch.setattr(runtime, "profile_for", lambda _: object())
    monkeypatch.setattr(runtime, "_managed_inputs", lambda _: (case.pins, None, None))
    failures = []
    monkeypatch.setattr(
        runtime, "record_runtime_failure", lambda *args: failures.append(args)
    )

    def platform_failed(*args, **kwargs):
        raise runtime.PlatformStorageUnavailableError("platform measurement failed")

    monkeypatch.setattr(runtime, "collect_configured", platform_failed)
    with pytest.raises(runtime.PlatformStorageUnavailableError):
        runtime.run_collection(str(case.pins.domain_id))
    assert failures == []


def test_preconfig_domain_failure_invalidates_only_storage(native_case, monkeypatch):
    from datetime import timedelta

    from app.models.kubevirt_storage import (
        NativeCollectionDomain,
        NativeSharedObservation,
        NativeSharedStorageObservation,
    )
    from app.services import native_collection_runtime as runtime
    from app.services.native_collection_shared_storage import _advance

    case = native_case
    monkeypatch.setattr(runtime, "SessionLocal", case.sessions)
    domain_id = str(case.pins.domain_id)
    runtime.record_runtime_failure(domain_id, None, case.report.started_at)
    with case.sessions() as db:
        assert db.get(NativeSharedObservation, domain_id) is None
        assert db.get(NativeSharedStorageObservation, domain_id).status == "failed"
    runtime.record_runtime_failure(
        domain_id, None, case.report.started_at, readiness=True
    )
    # A late setup failure cannot replace a newer completed shared storage scan.
    newer = case.report.started_at + timedelta(seconds=2)
    with case.sessions.begin() as db:
        _advance(
            db, db.get(NativeCollectionDomain, domain_id), {"images": []}, newer, newer
        )
        initial = db.get(NativeSharedObservation, domain_id)
        initial.started_at = initial.completed_at = newer
        initial.status = "complete"
    runtime.record_runtime_failure(
        domain_id, None, case.report.started_at + timedelta(seconds=1)
    )
    with case.sessions() as db:
        assert db.get(NativeSharedStorageObservation, domain_id).status == "complete"
        assert db.get(NativeSharedObservation, domain_id).status == "complete"


@pytest.mark.parametrize("readiness", [True, False])
def test_shared_failure_cannot_invalidate_independent_success(
    native_case, monkeypatch, readiness
):
    from datetime import timedelta

    from app.models.kubevirt_storage import (
        NativeSharedObservation,
        NativeSharedStorageObservation,
    )
    from app.services import native_collection_runtime as runtime

    case = native_case
    monkeypatch.setattr(runtime, "SessionLocal", case.sessions)
    domain_id = str(case.pins.domain_id)
    started = case.report.started_at
    for mode in (True, False):
        runtime.record_runtime_failure(domain_id, None, started, readiness=mode)
    with case.sessions.begin() as db:
        db.get(NativeSharedObservation, domain_id).status = "complete"
        db.get(NativeSharedStorageObservation, domain_id).status = "complete"
    runtime.record_runtime_failure(
        domain_id, None, started + timedelta(seconds=1), readiness=readiness
    )
    with case.sessions() as db:
        assert db.get(NativeSharedObservation, domain_id).status == (
            "failed" if readiness else "complete"
        )
        assert db.get(NativeSharedStorageObservation, domain_id).status == (
            "complete" if readiness else "failed"
        )


def test_readiness_only_observes_identity_without_enrollment_or_storage(
    native_case, monkeypatch
):
    from unittest.mock import MagicMock

    from app.services import native_collection_runtime as runtime
    from app.services import (
        native_collection_shared,
        native_collection_shared_storage,
        native_initialization,
        native_managed_collector,
    )

    case = native_case
    provider_id = str(case.pins.provider_clusters[0].provider_id)
    manifest = SimpleNamespace(
        pins=SimpleNamespace(
            initialization_evidence=SimpleNamespace(
                target=SimpleNamespace(provider_id=provider_id)
            )
        )
    )
    monkeypatch.setattr(
        runtime, "readiness_snapshot", lambda *args: ("enrollment", manifest)
    )
    observe = MagicMock(return_value="new observation")
    publish = MagicMock()
    prepare = MagicMock(side_effect=AssertionError("readiness cannot enroll"))
    storage = MagicMock(side_effect=AssertionError("readiness cannot scan images"))
    monkeypatch.setattr(native_initialization, "observe", observe)
    monkeypatch.setattr(native_collection_shared, "publish_shared_observation", publish)
    monkeypatch.setattr(native_managed_collector, "prepare_work", prepare)
    monkeypatch.setattr(
        native_collection_shared_storage, "collect_shared_storage", storage
    )
    runtime.observe_readiness(
        case.sessions, case.pins, case.target_id, {provider_id: "reader"}, "backend"
    )
    assert observe.call_count == publish.call_count == 1
    assert publish.call_args.args[3] == "new observation"
    prepare.assert_not_called()
    storage.assert_not_called()


def test_runtime_scoped_inventory_authorizes_shared_clone_graph(monkeypatch):
    from unittest.mock import MagicMock

    from app.schemas.kubevirt_storage import PoolKey
    from app.services import kubevirt_storage_collector as collector
    from app.services import kubevirt_storage_inventory as inventory_service
    from app.services import native_collection_runtime as runtime
    from app.services import native_managed_collector as managed
    from app.services import native_managed_verification as verification
    from app.services.kubevirt_storage_graph import attribute_graph
    from tests.test_native_project_clone_graph import scoped_chain

    inventory, images, proofs = scoped_chain()
    provider_id = str(uuid4())
    pins = SimpleNamespace(
        domain_id=uuid4(),
        initialization_evidence=SimpleNamespace(
            target=SimpleNamespace(provider_id=provider_id)
        ),
        project_roots=inventory.project_roots,
        cache_roots=[],
        provider_clusters=[SimpleNamespace(cluster_key="cluster")],
        pool_keys=[
            PoolKey(ceph_fsid=images[0].key.ceph_fsid, pool_id=images[0].key.pool_id)
        ],
    )
    config = SimpleNamespace(capability=None, collector_id="test", csi_requests={})
    manifest = SimpleNamespace(pins=pins)
    backend = SimpleNamespace()
    monkeypatch.setattr(
        runtime, "_managed_inputs", lambda args: (pins, config, SimpleNamespace())
    )
    monkeypatch.setattr(runtime, "_backend_reader", lambda *args: backend)
    protected_transport = {"test": "protected-collector-config"}
    factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(
        runtime, "ProviderReader", SimpleNamespace(from_kubeconfig=factory)
    )
    monkeypatch.setattr(
        runtime, "read_static_kubeconfig", lambda path: protected_transport
    )
    monkeypatch.setattr(runtime, "KubernetesReader", lambda *args, **kw: MagicMock())
    monkeypatch.setattr(
        inventory_service,
        "discover_inventory",
        lambda *args: inventory.model_copy(update={"project_scope": None}),
    )
    monkeypatch.setattr(managed, "retained_inputs", lambda *args: ([], []))
    monkeypatch.setattr(managed, "retiring_inputs", lambda *args: ([], set()))
    monkeypatch.setattr(verification, "lineage_inputs", lambda *args: ([], proofs))
    monkeypatch.setattr(
        collector,
        "collect_report",
        lambda inv, backend, **kw: attribute_graph(
            inv, images, clone_lineages=kw["clone_lineages"]
        ),
    )

    def cycle(*args, project_id):
        return args[-1](manifest, None, inventory.collected_at)

    monkeypatch.setattr(managed, "cycle", cycle)
    args = SimpleNamespace(
        command_timeout_seconds=1,
        verification_timeout_seconds=10,
        kubeconfig="/unused",
        target_id=str(uuid4()),
    )
    result = runtime.collect_configured(
        MagicMock(), args, project_id=str(inventory.project_scope)
    )
    assert len(result.projects) == 1
    assert result.projects[0].allocated_bytes == 30
    assert not result.projects[0].blockers
    assert factory.call_args.args[0] == provider_id
    assert factory.call_args.args[1] is protected_transport
