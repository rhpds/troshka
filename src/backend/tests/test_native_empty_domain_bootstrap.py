"""Fresh rootless enrollment with real persistence and synthetic API transports."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.host import Host
from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeEnrollmentRequest,
    NativeProjectRoot,
    NativeReportingTarget,
    NativeStorageSnapshot,
)
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_storage import (
    BackendScope,
    CollectionLimits,
    NativeInventory,
    NativeStorageReport,
    ProjectRoot,
)
from app.schemas.kubevirt_storage_enrollment import (
    DomainExtension,
    EnrollmentRequest,
    VerificationPins,
)
from app.schemas.quota import AccountingUnavailable
from app.services import native_initialization as initialization
from app.services import native_managed_collector as managed
from app.services.kubevirt_storage_collector import collect_report
from app.services.kubevirt_storage_enrollment import (
    authorize_empty_domain,
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    verify_native_enrollment,
)
from app.services.kubevirt_storage_inventory import inventory_digest
from app.services.kubevirt_storage_reports import import_report
from tests.test_kubevirt_storage_import_cli import cli as cli_fixture
from tests.test_native_initialization_observer import allowance_profile
from tests.test_native_initialization_observer import installed as installed_fixture
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_quota_database_fixture,
)

installed = installed_fixture
isolated_quota_database = isolated_quota_database_fixture
cli = cli_fixture


@pytest.fixture
def empty_case(installed, isolated_quota_database):
    q = installed
    q.sessions = isolated_quota_database
    q.target_id = str(uuid4())
    q.profile = allowance_profile(q)
    with q.sessions.begin() as db:
        db.add(Provider(id=q.reader.provider_id, name="native", type="kubevirt"))
        db.add(NativeReportingTarget(id=1, target_id=q.target_id))
    q.raw_pins = dict(
        domain_id=str(uuid4()),
        collector_ids=["fresh-collector"],
        provider_clusters=[
            dict(provider_id=q.reader.provider_id, cluster_key="cluster-uid")
        ],
        project_roots=[],
        cache_roots=[
            dict(
                cluster_key="cluster-uid",
                namespace="managed-cache",
                namespace_uid="storage-ns-uid",
            )
        ],
        pool_keys=[dict(ceph_fsid="ceph-fsid", pool_id=1)],
        initialization_evidence=q.profile.model_dump(mode="json"),
    )
    q.pins = authorize_empty_domain(q.raw_pins)
    with q.sessions.begin() as db:
        q.request = prepare_native_enrollment(db, [], empty_domain_pins=q.pins)
    q.verify = lambda request=q.request, pins=q.pins: verify_native_enrollment(
        request, {q.reader.provider_id: q.reader}, q.backend, pins=pins
    )
    return q


def enroll(q, manifest=None):
    manifest = manifest or q.verify()
    with q.sessions.begin() as db:
        identity = enroll_native_roots(db, manifest, expected_target_id=q.target_id)
    return identity, manifest


def inventory(q):
    return NativeInventory(
        collector_id=q.pins.collector_ids[0],
        collected_at=datetime.now(UTC),
        backend_scope=BackendScope(
            cluster_keys=["cluster-uid"], pool_keys=q.pins.pool_keys
        ),
    )


def collect(q):
    value = inventory(q)
    q.backend.recheck_inventory = lambda: value
    return collect_report(
        value, q.backend, limits=CollectionLimits(), clock=lambda: datetime.now(UTC)
    )


def ingest(q, enrollment_id, manifest, report):
    with q.sessions.begin() as db:
        return import_report(
            db,
            report,
            enrollment_id=enrollment_id,
            expected_enrollment_digest=manifest.digest,
            now=datetime.now(UTC),
        )


def test_explicit_mode_persists_without_any_dummy_project_and_replays(empty_case):
    q = empty_case
    with q.sessions() as db:
        assert db.query(Project).count() == 0
        saved = db.get(NativeEnrollmentRequest, str(q.request.request_id))
        assert saved.digest == digest(q.request)
        with pytest.raises(ValidationError):
            prepare_native_enrollment(db, [])
    identity, manifest = enroll(q)
    assert enroll(q, manifest)[0] == identity
    with q.sessions() as db:
        assert db.query(Project).count() == db.query(NativeProjectRoot).count() == 0
        assert db.query(NativeEnrollment).count() == 1
        assert (
            db.get(NativeCollectionDomain, str(q.pins.domain_id)).enrollment_id
            == identity
        )


def test_cli_explicit_preparation_publishes_bound_request_and_pins(
    empty_case, cli, tmp_path
):
    import json

    q = empty_case
    source, output, pins_output = [
        tmp_path / name for name in ("input.json", "request.json", "pins.json")
    ]
    source.write_text(json.dumps(q.raw_pins))
    args = cli._parser().parse_args(
        [
            "--target-id",
            q.target_id,
            "prepare-empty-domain",
            "--pins",
            str(source),
            "--output",
            str(output),
            "--pins-output",
            str(pins_output),
        ]
    )
    with q.sessions.begin() as db:
        request_id = cli._dispatch(db, args)
    request = EnrollmentRequest.model_validate_json(output.read_text())
    pins = VerificationPins.model_validate_json(pins_output.read_text())
    assert str(request.request_id) == request_id
    assert request.empty_domain_bootstrap == pins.empty_domain_bootstrap
    assert output.stat().st_mode & 0o777 == pins_output.stat().st_mode & 0o777 == 0o600
    assert cli.requested_providers(request) == {q.reader.provider_id}


def test_bootstrap_cannot_adopt_orphaned_retained_accounting(empty_case):
    from app.models.kubevirt_storage import NativeRetainedBinding

    q = empty_case
    with q.sessions.begin() as db:
        db.add(
            NativeRetainedBinding(
                key="a" * 64,
                root_key="cache",
                domain_id=str(q.pins.domain_id),
                payload={"preserve": True},
            )
        )
    with pytest.raises(ValueError, match="existing domain accounting"):
        enroll(q)
    with q.sessions() as db:
        assert db.get(NativeRetainedBinding, "a" * 64).payload == {"preserve": True}
        assert db.query(NativeEnrollment).count() == 0


@pytest.mark.parametrize(
    "change",
    [
        "missing_profile",
        "wrong_profile",
        "wrong_provider",
        "missing_provider",
        "wrong_cache",
        "wrong_cluster",
        "wrong_pool",
        "wrong_backend",
    ],
)
def test_empty_authority_never_skips_independent_identity_checks(empty_case, change):
    q = empty_case
    if change in {"missing_profile", "wrong_profile"}:
        body = deepcopy(q.raw_pins)
        if change == "missing_profile":
            body.pop("initialization_evidence")
        else:
            body["initialization_evidence"]["profile_name"] = "unsupported"
        with pytest.raises((ValueError, AccountingUnavailable)):
            pins = authorize_empty_domain(body)
            with q.sessions() as db:
                prepare_native_enrollment(db, [], empty_domain_pins=pins)
        return
    if change in {"wrong_provider", "missing_provider"}:
        with q.sessions.begin() as db:
            provider = db.get(Provider, q.reader.provider_id)
            if change == "missing_provider":
                db.delete(provider)
            else:
                provider.type = "aws"
        with pytest.raises(ValueError, match="native provider"):
            with q.sessions() as db:
                prepare_native_enrollment(db, [], empty_domain_pins=q.pins)
        return
    if change == "wrong_cache":
        q.reader.namespace = lambda name: {"metadata": {"uid": "changed"}}
    elif change == "wrong_cluster":
        q.reader.cluster_identity = lambda: "changed"
    elif change == "wrong_pool":
        q.outputs["pools"] = [{"poolnum": 2, "poolname": "tenant"}]
    else:
        q.outputs["fsid"] = "changed"
    with pytest.raises((ValueError, AccountingUnavailable)):
        q.verify()


@pytest.mark.parametrize("change", ["storage_class", "csi", "pool_name"])
def test_bootstrap_requires_complete_installed_profile_observation(empty_case, change):
    q = empty_case
    if change == "storage_class":
        q.data["sc"]["metadata"]["uid"] = "replaced"
    elif change == "csi":
        q.data["driver"]["metadata"]["uid"] = "replaced"
    else:
        q.outputs["pools"][0]["poolname"] = "replaced"
    with pytest.raises((ValueError, AccountingUnavailable)):
        q.verify()


def test_prepared_request_binds_all_pins_and_preserves_ordinary_empty_rejection(
    empty_case,
):
    q = empty_case
    for field, value in [
        ("collector_ids", ["other"]),
        ("cache_roots", []),
        ("pool_keys", [dict(ceph_fsid="other", pool_id=1)]),
    ]:
        body = q.pins.model_dump(mode="json")
        body[field] = value
        with pytest.raises(ValueError):
            q.verify(pins=VerificationPins.model_validate(body))
    body = q.request.model_dump(mode="json")
    body.pop("empty_domain_bootstrap")
    with pytest.raises(ValidationError):
        EnrollmentRequest.model_validate(body)
    with pytest.raises(ValidationError):
        VerificationPins.model_validate(q.raw_pins)


def test_bootstrap_rejects_existing_domain_reset_and_pool_collision(empty_case):
    q = empty_case
    original_id, original = enroll(q)
    with q.sessions.begin() as db:
        request = prepare_native_enrollment(db, [], empty_domain_pins=q.pins)
    with pytest.raises(ValueError, match="explicit domain extension"):
        enroll(q, q.verify(request=request))
    body = deepcopy(q.raw_pins)
    body["domain_id"] = str(uuid4())
    pins = authorize_empty_domain(body)
    with q.sessions.begin() as db:
        request = prepare_native_enrollment(db, [], empty_domain_pins=pins)
    with pytest.raises(ValueError, match="physical pool already belongs"):
        enroll(q, q.verify(request=request, pins=pins))
    assert enroll(q, original)[0] == original_id


def test_empty_collection_binds_verified_scope_without_inventing_images(empty_case):
    q = empty_case
    identity, manifest = enroll(q)
    q.commands.clear()
    report = collect(q)
    assert not report.collection_errors
    assert report.images == report.projects == report.platform_pools == []
    assert report.pool_keys == q.pins.pool_keys
    assert report.cluster_keys == ["cluster-uid"]
    assert len([c for c in q.commands if c == ["ceph", "fsid"]]) == 2
    assert ingest(q, identity, manifest, report) == str(report.report_id)
    body = report.model_dump(mode="json")
    body.pop("backend_scope")
    stripped = NativeStorageReport.model_validate(body)
    with pytest.raises(ValueError, match="inventory digest"):
        ingest(q, identity, manifest, stripped)


@pytest.mark.parametrize(
    "read,stage",
    [("fsid", "first"), ("fsid", "final"), ("pools", "first"), ("pools", "final")],
)
def test_empty_collection_rechecks_actual_backend_before_and_after(
    empty_case, read, stage
):
    q = empty_case
    calls = 0
    method = "cluster_identity" if read == "fsid" else "pool_identity"
    original = getattr(q.backend, method)

    def changed():
        nonlocal calls
        calls += 1
        result = original()
        if calls == (1 if stage == "first" else 2):
            return result.model_copy(
                update={"value": "other" if read == "fsid" else []}
            )
        return result

    setattr(q.backend, method, changed)
    report = collect(q)
    assert calls == 2
    assert report.collection_errors and report.images == []


@pytest.mark.parametrize("missing", [False, True])
def test_import_checks_scope_against_protected_enrollment(empty_case, missing):
    q = empty_case
    identity, manifest = enroll(q)
    report = collect(q)
    changed = (
        None
        if missing
        else report.backend_scope.model_copy(update={"cluster_keys": ["foreign"]})
    )
    report = report.model_copy(update={"backend_scope": changed, "report_id": uuid4()})
    from app.services.kubevirt_storage_reports import report_inventory

    report = report.model_copy(
        update={"inventory_digest": inventory_digest(report_inventory(report))}
    )
    ingest(q, identity, manifest, report)
    with q.sessions() as db:
        saved = db.get(NativeStorageSnapshot, str(report.report_id))
        assert "collection_backend_scope_mismatch" in saved.reason_codes


@pytest.mark.parametrize("failure", ["exception", "discovery"])
def test_fresh_empty_cycle_can_initialize_but_partial_and_stale_cannot(
    empty_case, failure
):
    q = empty_case
    enroll(q)
    # Observation sources are inside the collection; retain their original times.
    q.timestamp = datetime.now(UTC)
    original_identity = q.reader.initialization_identity
    q.reader.initialization_identity = lambda name: original_identity(name).model_copy(
        update={"collected_at": datetime.now(UTC)}
    )
    original_run = q.backend.transport.process.run
    q.backend.transport.process.run = lambda *a, **kw: original_run(
        *a, **kw
    ).model_copy(update={"collected_at": datetime.now(UTC)})

    def fresh_collect(manifest, previous, started):
        return collect(q).model_copy(update={"started_at": started})

    report = managed.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.pins.collector_ids[0],
        {q.reader.provider_id: q.reader},
        q.backend,
        fresh_collect,
    )
    assert not report.collection_errors
    with q.sessions() as db:
        initialization.current(db, q.reader.provider_id, datetime.now(UTC))
        with pytest.raises(AccountingUnavailable):
            initialization.current(
                db, q.reader.provider_id, datetime.now(UTC) + timedelta(minutes=16)
            )

    def failed(*args):
        if failure == "exception":
            raise ValueError("independent read failed")
        from app.services.kubevirt_storage_inventory import discover_inventory

        value = discover_inventory(
            SimpleNamespace(collector_id=q.pins.collector_ids[0]), [], []
        )
        value = value.model_copy(update={"backend_scope": inventory(q).backend_scope})
        q.backend.recheck_inventory = lambda: value
        return collect_report(
            value, q.backend, limits=CollectionLimits(), clock=lambda: datetime.now(UTC)
        )

    report = managed.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.pins.collector_ids[0],
        {q.reader.provider_id: q.reader},
        q.backend,
        failed,
    )
    assert report.collection_errors
    with q.sessions() as db:
        with pytest.raises(AccountingUnavailable):
            initialization.current(db, q.reader.provider_id, datetime.now(UTC))


def extension(q, original_id, original, suffix):
    with q.sessions.begin() as db:
        owner = User(email=f"{suffix}@test.invalid", role="user")
        host = Host(provider_id=q.reader.provider_id)
        db.add_all([owner, host])
        db.flush()
        project = Project(name=suffix, owner_id=owner.id, host_id=host.id)
        db.add(project)
        db.flush()
        root = ProjectRoot(
            project_id=project.id,
            cluster_key="cluster-uid",
            namespace=suffix,
            namespace_uid=suffix + "-ns",
            project_cr_uid=suffix + "-cr",
            deployment_generation=1,
        )
        request = prepare_native_enrollment(db, [project.id])
    pins = q.pins.model_copy(
        update={
            "project_roots": [root],
            "empty_domain_bootstrap": None,
            "domain_extension": DomainExtension(
                version="native-domain-extension-v1",
                previous_enrollment_id=original_id,
                previous_enrollment_digest=original.digest,
            ),
        }
    )
    reader = SimpleNamespace(
        cluster_identity=lambda: "cluster-uid",
        namespace=lambda name: {
            "metadata": {
                "uid": root.namespace_uid if name == suffix else "storage-ns-uid"
            }
        },
        projects=lambda name: [
            {
                "metadata": {"uid": root.project_cr_uid},
                "spec": {"projectId": str(root.project_id)},
            }
        ],
    )
    return verify_native_enrollment(
        request, {q.reader.provider_id: reader}, q.backend, pins=pins
    )


def test_first_real_root_extension_keeps_rootless_predecessor_and_authority(empty_case):
    q = empty_case
    identity, original = enroll(q)
    manifest = extension(q, identity, original, "first")
    first_id, _ = enroll(q, manifest)
    assert first_id != identity
    with q.sessions() as db:
        domain, current = managed.anchored(db, q.pins)
        assert domain.enrollment_id == first_id
        assert len(current.pins.project_roots) == 1
        assert current.pins.initialization_evidence == q.pins.initialization_evidence
        assert db.get(NativeEnrollment, identity).payload == original.model_dump(
            mode="json"
        )
        assert db.query(NativeProjectRoot).count() == 1
    assert (
        enroll(q, original)[0] == identity
    )  # Replay cannot reset the current pointer.


def test_managed_collector_first_extension_removes_only_bootstrap_marker(empty_case):
    q = empty_case
    identity, original = enroll(q)
    prepared = extension(q, identity, original, "managed-first")
    root = prepared.pins.project_roots[0]
    reader = SimpleNamespace(
        initialization_identity=q.reader.initialization_identity,
        cluster_identity=lambda: "cluster-uid",
        namespace=lambda name: {
            "metadata": {
                "uid": root.namespace_uid
                if name == root.namespace
                else "storage-ns-uid"
            }
        },
        projects=lambda name: [
            {
                "metadata": {"uid": root.project_cr_uid},
                "spec": {"projectId": str(root.project_id)},
            }
        ],
        pending_root=lambda *args: root.model_dump(mode="json"),
    )
    work = dict(
        manifest=original,
        enrollment_id=identity,
        request=prepared.request,
        roots=[root],
        selected=[
            (
                "first-operation",
                dict(
                    provider_id=q.reader.provider_id, root=root.model_dump(mode="json")
                ),
            )
        ],
    )
    verified = managed.verify_new_roots(work, {q.reader.provider_id: reader}, q.backend)
    assert verified.pins.empty_domain_bootstrap is None
    assert verified.pins.cache_roots == original.pins.cache_roots
    assert (
        verified.pins.initialization_evidence == original.pins.initialization_evidence
    )
    enroll(q, verified)


def test_bootstrap_does_not_make_unrelated_stopped_inventory_empty(
    empty_case, monkeypatch
):
    from app.services import quota_capacity_baseline as capacity
    from tests.test_quota_capacity_baseline import fresh, reviewed

    q = empty_case
    enroll(q)
    monkeypatch.setattr(capacity.accounting, "quotas_enabled", lambda: False)
    now = datetime.now(UTC)
    with q.sessions() as db:
        owner = User(email="retained@test.invalid", role="user")
        host = Host(provider_id=q.reader.provider_id)
        db.add_all([owner, host])
        db.flush()
        project = Project(
            name="retained-diagnostic",
            owner_id=owner.id,
            host_id=host.id,
            state="stopped",
        )
        db.add(project)
        db.commit()
        project_id = project.id
        fresh(db, now)
        source = capacity.inventory(db)
        assert source["projects"][0]["project_id"] == project_id
        assert not source["projects"][0]["empty"]
        manifest = reviewed(db, [project], now)
        assert manifest["projects"] == {}
        db.rollback()
        result = capacity.run(db, evidence=manifest, now=now)
        assert not result["capacity_ready"]
        assert any(
            b["message"] == "Manifest must cover exactly every retained project"
            for b in result["blockers"]
        )
        assert db.get(Project, project_id).state == "stopped"


def test_concurrent_first_extensions_use_existing_predecessor_cas(empty_case):
    q = empty_case
    with q.sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("row-lock contention requires disposable PostgreSQL")
    identity, original = enroll(q)
    choices = [extension(q, identity, original, name) for name in ("one", "two")]
    barrier = Barrier(2)

    def attempt(manifest):
        barrier.wait(timeout=10)
        try:
            return enroll(q, manifest)[0]
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, choices))
    assert sum("predecessor is no longer current" in result for result in results) == 1
    with q.sessions() as db:
        assert db.query(NativeEnrollment).count() == 2
        assert db.query(NativeProjectRoot).count() == 1


def test_legacy_optional_fields_keep_serialized_payload_and_inventory_digest():
    body = dict(target_id=str(uuid4()), request_id=str(uuid4()), projects=[])
    from tests.test_kubevirt_storage_graph import inventory as legacy_inventory

    value = legacy_inventory()
    restored = NativeInventory.model_validate(value.model_dump(mode="json"))
    assert "backend_scope" not in restored.model_dump(mode="json")
    assert inventory_digest(restored) == inventory_digest(value)
    with pytest.raises(ValidationError):
        EnrollmentRequest.model_validate(body)
