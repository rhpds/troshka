"""Managed image source/API-worker contracts; synthetic I/O is not live acceptance."""

import io
import struct
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import NativeEnrollment
from app.models.library import Library, LibraryItem
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaReservation
from app.models.user import User
from app.schemas.kubevirt_storage_enrollment import DomainExtension
from app.schemas.kubevirt_storage_initialization import InitializationEvidence
from app.schemas.quota import AccountingUnavailable
from app.services import native_managed_deploy as deploy
from app.services import native_managed_images as images
from app.services.kubevirt_storage_enrollment import (
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    validated_enrollment,
    verify_native_enrollment,
)
from tests.test_native_managed_deploy import (
    advance_clock,
    ingest_current,
    start,
)
from tests.test_native_managed_deploy import (
    deploy_case as deploy_case_fixture,
)
from tests.test_native_managed_deploy import (
    isolated_quota_database as isolated_quota_database_fixture,
)
from tests.test_native_managed_deploy import (
    native_ledger as native_ledger_fixture,
)
from tests.test_native_managed_deploy import (
    resume_case as resume_case_fixture,
)

deploy_case = deploy_case_fixture
isolated_quota_database = isolated_quota_database_fixture
native_ledger = native_ledger_fixture
resume_case = resume_case_fixture


class CentralSource:
    def __init__(self, virtual_gib=20):
        self.virtual_gib = virtual_gib
        self.meta = SimpleNamespace(endpoint_url="https://s4.example.test")
        self.head = {
            "ETag": "frozen-etag",
            "ContentLength": 4096,
            "LastModified": datetime.now(UTC),
        }

    def head_object(self, **kwargs):
        assert kwargs == {"Bucket": "approved-images", "Key": "library/rhel.qcow2"}
        return deepcopy(self.head)

    def get_object(self, **kwargs):
        header = bytearray(72)
        header[:4] = b"QFI\xfb"
        header[24:32] = struct.pack(">Q", self.virtual_gib * 1024**3)
        return {"Body": io.BytesIO(header)}

    def close(self):
        pass


@pytest.fixture
def image_case(deploy_case, monkeypatch):
    from app.services import native_cache_birth, native_clone_lineage
    from tests.test_native_managed_resume import FixedDatetime

    monkeypatch.setattr(native_cache_birth, "datetime", FixedDatetime)
    monkeypatch.setattr(native_clone_lineage, "datetime", FixedDatetime)
    q = deploy_case
    q.source = CentralSource()
    monkeypatch.setattr(images, "source_client", lambda db, policy: q.source)
    with q.sessions() as db:
        owner = db.get(User, q.owner_id)
        owner.role = "user"
        library = Library(type="central")
        db.add(library)
        db.flush()
        item = LibraryItem(
            library_id=library.id,
            name="Approved RHEL",
            type="image",
            format="qcow2",
            state="ready",
            source="central",
            s3_key="library/rhel.qcow2",
        )
        db.add(item)
        db.flush()
        q.item_id = item.id
        profile = q.profile.model_dump(mode="json")
        profile.update(
            version="native-clone-initialization-v1",
            operation="managed-cache-clone",
            profile_name="odf-rbd-clone-startup-allowance-v1",
            maximum_capacity_bytes=128 * 1024**3,
            source_policy=dict(
                version="central-qcow2-direct-import-v1",
                provider_id=str(uuid4()),
                bucket="approved-images",
                key_prefix="library/",
                cache_secret_ref="s3-central-credentials",
            ),
        )
        q.profile = InitializationEvidence.model_validate(profile)
        db.add(
            Provider(
                id=str(q.profile.source_policy.provider_id),
                name="Approved central images",
                type="s3_readonly",
            )
        )
        db.flush()
        item.source_provider_id = str(q.profile.source_policy.provider_id)
        old = db.get(NativeEnrollment, q.enrollment_id)
        previous = validated_enrollment(old)
        pins = previous.pins.model_copy(
            update={
                "initialization_evidence": q.profile,
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=old.id,
                    previous_enrollment_digest=old.digest,
                    initialization_authorization_digest=digest(q.profile),
                ),
            }
        )
        request = prepare_native_enrollment(db, q.project_ids)
        verified = verify_native_enrollment(
            request, {q.provider_id: q.reader}, q.backend, pins=pins
        )
        q.enrollment_id = enroll_native_roots(
            db, verified, expected_target_id=q.target_id
        )
        q.pins, q.digest = pins, verified.digest
        project = db.get(Project, q.new_id)
        project.provider_id = q.provider_id
        project.topology = {
            "placement": {"requires_kubevirt": True},
            "nodes": [
                {"id": "tenant-vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 1}},
                {
                    "id": "image-disk",
                    "type": "storageNode",
                    "data": {
                        "size": 30,
                        "source": "library",
                        "libraryItemId": item.id,
                        "format": "qcow2",
                    },
                },
            ],
            "edges": [{"id": "disk-vm", "source": "image-disk", "target": "tenant-vm"}],
        }
        db.commit()
    advance_clock()
    ingest_current(q)
    return q


@pytest.mark.parametrize("virtual,expected", [(20, "40Gi"), (70, "81Gi")])
def test_nonadmin_api_worker_freezes_real_builder_capacity_and_owner(
    image_case, virtual, expected
):
    q = image_case
    q.source.virtual_gib = virtual
    operation_id = start(q)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        target = row.dispatch_metadata["native_deploy"]
        assert db.get(User, row.owner_id).role == "user"
        assert row.state == "reconciling" and target["phase"] == "bound"
        assert row.requested["cpu"] == 2 and row.requested["ram_bytes"] == 1024**3
        assert len(target["claims"]) == 1 and row.requested["storage_bytes"] == 32
        plan = deploy.helpers().render(
            {
                **target["payload"],
                "metadata": {**target["payload"]["metadata"], "uid": "pending"},
            },
            q.profile.target.storage_class_name,
        )
        dv = next(v for v in plan if v["kind"] == "DataVolume")
        assert dv["spec"]["pvc"]["resources"]["requests"]["storage"] == expected
        assert not any(v["kind"] == "PersistentVolumeClaim" for v in plan)
        assert dv["spec"]["source"]["pvc"]["namespace"] == "trusted-cache"
        assert (
            target["sources"]["image-disk"]["virtual_size_bytes"] == virtual * 1024**3
        )
        assert "s3-central-credentials" not in str(target["payload"])
        assert not q.apis[q.new_id].rows("DataVolume", None)


@pytest.mark.parametrize(
    "change",
    [
        "private",
        "wrong_source",
        "wrong_format",
        "wrong_state",
        "missing_key",
        "wrong_prefix",
        "wrong_provider",
        "type_disk",
        "type_vm",
        "type_template",
        "type_iso",
        "unknown_size",
        "changed_head",
    ],
)
def test_source_denial_precedes_reservation_or_provider_mutation(
    image_case, monkeypatch, change
):
    q = image_case
    with q.sessions() as db:
        item = db.get(LibraryItem, q.item_id)
        if change == "private":
            other = Library(type="personal")
            db.add(other)
            db.flush()
            item.library_id = other.id
        elif change == "wrong_source":
            item.source = "local"
        elif change == "wrong_format":
            item.format = "iso"
        elif change == "wrong_state":
            item.state = "pending"
        elif change == "missing_key":
            item.s3_key = None
        elif change == "wrong_prefix":
            item.s3_key = "unapproved/rhel.qcow2"
        elif change == "wrong_provider":
            item.source_provider_id = q.provider_id
        elif change.startswith("type_"):
            item.type = change.removeprefix("type_")
        db.commit()
    if change == "unknown_size":
        q.source.virtual_gib = 0
    if change == "changed_head":
        original = q.source.head_object
        calls = []

        def changed(**kwargs):
            result = original(**kwargs)
            calls.append(1)
            if len(calls) > 1:
                result["ETag"] = "changed"
            return result

        monkeypatch.setattr(q.source, "head_object", changed)
    before = deepcopy(q.apis[q.new_id].calls)
    with q.sessions() as db:
        expected_error = (
            ValueError if change == "changed_head" else AccountingUnavailable
        )
        with pytest.raises(expected_error):
            deploy.prepare(db, db.get(Project, q.new_id), host_id=q.host_id)
        db.rollback()
        assert db.query(QuotaReservation).filter_by(project_id=q.new_id).count() == 0
    assert q.apis[q.new_id].calls == before


@pytest.mark.parametrize("state", ["pending", "dispatched", "reconciling"])
@pytest.mark.parametrize("invalid", [False, True])
def test_pending_source_verification_keeps_active_validation(
    image_case, state, invalid
):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services.native_managed_verification import pending

    q = image_case
    operation_id = start(q)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        row.state = state
        metadata = deepcopy(row.dispatch_metadata)
        if invalid:
            metadata["native_deploy"]["payload_digest"] = "0" * 64
        row.dispatch_metadata = metadata
        db.commit()
        domain = db.get(NativeCollectionDomain, metadata["native_deploy"]["domain_id"])
        if invalid:
            with pytest.raises(AccountingUnavailable):
                pending(db, domain)
        else:
            assert pending(db, domain) == [(operation_id, metadata["native_deploy"])]


@pytest.mark.parametrize(
    "stage",
    [
        "draft",
        "placed",
        "draft-topology",
        "draft-owner",
        "placed-topology",
        "placed-host",
        "claimed-draft",
        "create-intent-draft",
    ],
)
def test_collector_validates_reserved_source_at_its_placement_stage(image_case, stage):
    from app.services import native_managed_collector as collector
    from app.services import placement, quota_operations

    q = image_case
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        operation_id = quota_operations.prepare(
            db, project, "deploy", host_id=q.host_id
        )
        db.commit()
        if stage.startswith("placed"):
            placement.place_project(
                db, project, host_id=q.host_id, quota_operation_id=operation_id
            )
            db.commit()
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        if stage.endswith("topology"):
            project.topology = {**project.topology, "changed": True}
        elif stage == "draft-owner":
            metadata = deepcopy(row.dispatch_metadata)
            metadata["native_deploy"]["owner_id"] = str(uuid4())
            row.dispatch_metadata = metadata
        elif stage == "placed-host":
            project.host_id = None
        elif stage == "claimed-draft":
            row.dispatch_metadata = {**row.dispatch_metadata, "claim": "claimed"}
        elif stage == "create-intent-draft":
            deploy.update(row, phase="create-intent")
        db.commit()
    if stage in {"draft", "placed"}:
        work = collector.snapshot(q.sessions, q.pins, q.target_id)
        assert [v[0] for v in work["images"]] == [operation_id]
    else:
        with pytest.raises(AccountingUnavailable):
            collector.snapshot(q.sessions, q.pins, q.target_id)


def test_creation_and_verification_adapter_interfaces_are_separate():
    from app.services.native_cache_provider import (
        CacheCreationAdapter,
        CacheVerificationAdapter,
    )

    assert not hasattr(CacheCreationAdapter, "observe")
    assert not hasattr(CacheVerificationAdapter, "create")
    assert not hasattr(CacheCreationAdapter, "backend")


@pytest.mark.parametrize("failure", ["lost", "stale", "capacity"])
def test_owner_cache_failure_never_recreates_or_publishes_receipt(image_case, failure):
    from datetime import timedelta

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import (
        prepare_managed_cache,
        submit_cache_birth,
        verify_cache_birth,
    )
    from tests.test_native_cache_birth import CreationAdapter, observed
    from tests.test_native_managed_resume import FixedDatetime

    q = image_case
    operation_id = start(q)
    identity = prepare_managed_cache(q.sessions, operation_id, "image-disk")
    adapter = CreationAdapter(q, ambiguous=failure == "lost")
    if failure == "stale":
        FixedDatetime.current += timedelta(seconds=65)
    if failure != "capacity":
        with pytest.raises(Exception):
            submit_cache_birth(q.sessions, identity, adapter)
        with pytest.raises(Exception):
            submit_cache_birth(q.sessions, identity, adapter)
        assert len(adapter.created) == (1 if failure == "lost" else 0)
    else:
        submit_cache_birth(q.sessions, identity, adapter)

        class Observer:
            def observe(self, intent, response):
                result = observed(intent, response)
                result["pvc"]["status"]["capacity"] = {"storage": "24Gi"}
                return result

        with pytest.raises(ValueError, match="capacity changed"):
            verify_cache_birth(q.sessions, identity, Observer())
    with q.sessions() as db:
        birth = db.get(NativeCacheBirth, identity)
        assert birth.receipt is None
        assert (
            birth.state
            == {"lost": "submitted", "stale": "prepared", "capacity": "created"}[
                failure
            ]
        )
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.requested["storage_bytes"] == 32 and row.observed["cpu"] == 0


def test_reconciling_owner_operation_prepares_frozen_cache_without_admin(image_case):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import prepare_managed_cache, validated_intent

    q = image_case
    operation_id = start(q)
    identity = prepare_managed_cache(q.sessions, operation_id, "image-disk")
    assert prepare_managed_cache(q.sessions, operation_id, "image-disk") == identity
    with q.sessions() as db:
        row = db.get(NativeCacheBirth, identity)
        intent = validated_intent(row)
        assert row.state == "prepared"
        assert intent.managed_authority["operation_id"] == operation_id
        assert intent.managed_authority["owner_id"] == q.owner_id
        assert intent.spec["pvc"]["resources"]["requests"]["storage"] == "23Gi"
        assert intent.spec["source"]["s3"]["secretRef"] == "s3-central-credentials"
        assert db.query(NativeCacheBirth).count() == 1


def test_prepared_cache_restart_uses_fresh_protected_dispatch_evidence(image_case):
    from datetime import timedelta

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import (
        prepare_managed_cache,
        submit_cache_birth,
    )
    from tests.test_native_cache_birth import CreationAdapter
    from tests.test_native_managed_resume import FixedDatetime

    q = image_case
    operation = start(q)
    identity = prepare_managed_cache(q.sessions, operation, "image-disk")
    with q.sessions() as db:
        original = deepcopy(db.get(NativeCacheBirth, identity).intent)
        original_digest = db.get(NativeCacheBirth, identity).intent_digest
    FixedDatetime.current += timedelta(seconds=65)
    ingest_current(q)

    class Adapter(CreationAdapter):
        def create(self, intent):
            with q.sessions() as db:
                row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
                saved = row.dispatch_metadata["native_cache_dispatch"][identity]
                assert saved["intent_digest"] == original_digest
                assert (
                    saved["authority"]["initialization_observation"]
                    != original["managed_authority"]["initialization_observation"]
                )
                assert db.get(NativeCacheBirth, identity).state == "submitted"
            return super().create(intent)

    adapter = Adapter(q)
    submit_cache_birth(q.sessions, identity, adapter)
    with q.sessions() as db:
        birth = db.get(NativeCacheBirth, identity)
        assert birth.state == "created" and birth.intent == original
        assert birth.intent_digest == original_digest
    assert adapter.created == [original["name"]]


@pytest.mark.parametrize(
    "change",
    [
        "cancelled",
        "generation",
        "settled",
        "owner",
        "organization",
        "host",
        "source",
        "plan",
    ],
)
def test_changed_managed_authority_stops_before_cache_mutation(image_case, change):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import prepare_managed_cache

    q = image_case
    operation_id = start(q)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        if change == "cancelled":
            metadata["cancelled"] = True
        elif change == "generation":
            metadata["generation_id"] = str(uuid4())
        elif change == "settled":
            row.state = "confirmed"
        elif change == "owner":
            db.get(User, q.owner_id).access_enabled = False
        elif change == "organization":
            db.get(User, q.owner_id).organization_id = None
        elif change == "host":
            from app.models.host import Host

            db.get(Host, q.host_id).accepting_work = False
        elif change == "source":
            db.get(LibraryItem, q.item_id).s3_key = "library/replaced.qcow2"
        elif change == "plan":
            metadata["native_deploy"]["payload"]["spec"]["managedSources"][
                "image-disk"
            ]["storage_request"] = "80Gi"
        row.dispatch_metadata = metadata
        db.commit()
    with pytest.raises(Exception):
        prepare_managed_cache(q.sessions, operation_id, "image-disk")
    with q.sessions() as db:
        assert db.query(NativeCacheBirth).count() == 0


def test_concurrent_cache_preparation_is_same_original_operation(image_case):
    from concurrent.futures import ThreadPoolExecutor

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import prepare_managed_cache

    q = image_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row lock contention requires disposable PostgreSQL")
    operation_id = start(q)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                prepare_managed_cache, q.sessions, operation_id, "image-disk"
            )
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    with q.sessions() as db:
        assert db.query(NativeCacheBirth).count() == 1


@pytest.mark.parametrize("collector_overlap", [False, True])
def test_normal_owner_route_selects_pinned_host_and_hands_real_worker_frozen_plan(
    image_case, monkeypatch, collector_overlap
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import projects
    from app.core import redis
    from app.core.auth import get_current_user
    from app.core.database import get_db
    from app.models.host import Host
    from app.services import deploy_service
    from app.services import native_managed_collector as collector
    from tests.test_native_managed_deploy import report_for

    q = image_case
    app = FastAPI()
    app.include_router(projects.router)
    queued = []
    captured = {}
    original_place = projects.place_project

    def place_with_collector_snapshot(*args, **kwargs):
        # API reservation has committed; placement has not started yet.
        try:
            captured["work"] = collector.snapshot(q.sessions, q.pins, q.target_id)
        except AccountingUnavailable as error:
            captured["error"] = error
        return original_place(*args, **kwargs)

    def original_snapshot(*args):
        if "error" in captured:
            raise captured["error"]
        return captured["work"]

    def enqueue(function, *args, **kwargs):
        if collector_overlap:
            # Complete that exact snapshot's cycle after placement commits,
            # before the queued worker can claim its operation.
            with monkeypatch.context() as patch:
                patch.setattr(collector, "snapshot", original_snapshot)
                collector.cycle(
                    q.sessions,
                    q.pins,
                    q.target_id,
                    q.report.collector_id,
                    {q.provider_id: q.reader},
                    q.backend,
                    lambda manifest, previous, started: report_for(q, manifest),
                )
        queued.append((function, args, kwargs))

    if collector_overlap:
        monkeypatch.setattr(projects, "place_project", place_with_collector_snapshot)
    monkeypatch.setattr(
        redis,
        "enqueue_job",
        enqueue,
    )
    # This unrelated nested-host disk probe is a provider transport seam only.
    monkeypatch.setattr(
        "app.services.troshkad_client.check_disk_usage", lambda host: None
    )
    with q.sessions.begin() as db:
        host = db.get(Host, q.host_id)
        host.agent_status = "connected"
        host.total_vcpus, host.total_ram_mb = 100, 100000
        actor = db.get(User, q.owner_id)
        db.expunge(actor)

    def session():
        with q.sessions() as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as client:
        response = client.post(f"/projects/{q.new_id}/deploy")
    assert response.status_code == 200, response.text
    assert response.json()["host_id"] == q.host_id
    assert len(queued) == 1 and queued[0][0] is projects.deploy_project_async
    _, args, kwargs = queued[0]
    operation_id = kwargs["quota_operation_id"]
    with q.sessions() as db:
        assert db.get(User, q.owner_id).role == "user"
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["native_deploy"]["host_id"] == q.host_id
        assert row.dispatch_metadata["native_deploy"]["payload"]["spec"][
            "managedSources"
        ]
    deploy_service.deploy_project_async(*args, quota_operation_id=operation_id)
    assert q.apis[q.new_id].rows("TroshkaProject", None)
    assert not q.apis[q.new_id].rows("VirtualMachine", None)


@pytest.mark.parametrize("kind", ["cache", "clone"])
@pytest.mark.parametrize(
    "outcome", ["success", "partial", "other", "cancelled", "changed", "lease_lost"]
)
def test_successful_verification_clears_only_its_current_diagnostic(
    image_case, monkeypatch, kind, outcome
):
    from app.services import native_cache_birth, native_clone_lineage
    from app.services import native_managed_verification as verification
    from tests.test_native_cache_birth import CreationAdapter

    q = image_case
    operation = start(q)
    identity = native_cache_birth.prepare_managed_cache(
        q.sessions, operation, "image-disk"
    )
    native_cache_birth.submit_cache_birth(q.sessions, identity, CreationAdapter(q))
    code = kind + "_verification_pending"
    with q.sessions.begin() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        target = deepcopy(row.dispatch_metadata["native_deploy"])
        # Two subjects ensure success for one cannot clear another's failure.
        target["sources"]["second"] = deepcopy(next(iter(target["sources"].values())))
        target.update(
            phase="initialization-intent",
            clone_lineages={"first": "one", "second": "two"},
        )
        stored = deepcopy(target)
        if outcome == "changed":
            stored["phase"] = "runtime-intent"
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "native_deploy": stored,
            "native_image_diagnostic": "unrelated_error"
            if outcome == "other"
            else code,
            "cancelled": outcome == "cancelled",
        }
    calls = []

    def verified(*args):
        calls.append(args)
        if outcome == "partial" and len(calls) == 1:
            raise ValueError("still pending")
        return object()

    monkeypatch.setattr(native_cache_birth, "verify_cache_birth", verified)
    monkeypatch.setattr(native_clone_lineage, "verify_clone", verified)
    monkeypatch.setattr(verification, "transfer_creation", lambda *args: True)
    if outcome == "lease_lost":

        def lost():
            raise RuntimeError("lease lost")

        monkeypatch.setattr(verification, "assert_collection_lease", lost)
    run = verification.verify_sources if kind == "cache" else verification.verify_clones
    args = (
        q.sessions,
        [(operation, target)],
        {q.provider_id: q.reader},
        q.backend,
        q.pins.initialization_evidence,
    )
    if outcome == "lease_lost":
        with pytest.raises(RuntimeError, match="lease lost"):
            run(*args)
    else:
        run(*args)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        expected = (
            None
            if outcome == "success"
            else "unrelated_error"
            if outcome == "other"
            else code
        )
        assert row.dispatch_metadata.get("native_image_diagnostic") == expected


def prepare_verified_image_operation(q, monkeypatch, *, publish_initialization=True):
    """Real journals, publication, enrollment, worker and renderer; synthetic I/O."""
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.schemas.kubevirt_storage import CacheBinding, ImageObservation
    from app.services import native_managed_collector as collector
    from app.services.native_cache_birth import (
        prepare_managed_cache,
        submit_cache_birth,
        validated_receipt,
        verify_cache_birth,
    )
    from app.services.native_managed_verification import publish_sources
    from tests.test_native_cache_birth import CreationAdapter, observed
    from tests.test_native_managed_deploy import report_for
    from tests.test_native_managed_resume import FixedDatetime
    from tests.test_quota_native_storage import revised

    operation = start(q)
    identity = prepare_managed_cache(q.sessions, operation, "image-disk")
    submit_cache_birth(q.sessions, identity, CreationAdapter(q))

    class Observer:
        def observe(self, intent, response):
            result = observed(intent, response)
            result["pvc"]["status"]["capacity"] = {
                "storage": intent.spec["pvc"]["resources"]["requests"]["storage"]
            }
            return result

    reference = verify_cache_birth(q.sessions, identity, Observer())
    with q.sessions() as db:
        intent, receipt = validated_receipt(
            db, reference.operation_id, reference.digest
        )
        binding = CacheBinding(
            cluster_key=intent.target.cluster_key,
            namespace=intent.namespace,
            namespace_uid=intent.namespace_uid,
            pvc_uid=receipt.pvc_uid,
            pv_uid=receipt.pv_uid,
            csi_driver=receipt.csi_driver,
            volume_handle=receipt.volume_handle,
            image_key=receipt.image_key,
        )
    publish_sources(
        q.sessions,
        q.pins,
        q.target_id,
        [reference],
        {q.provider_id: q.reader},
        q.backend,
    )
    image = ImageObservation(
        key=receipt.image_key,
        collected_at=FixedDatetime.current,
        allocated_bytes=100,
        provisioned_bytes=23 * 1024**3,
        measurement_status="complete",
        command_version="synthetic-rbd-v1",
        measurement_semantics_version="kubevirt-rbd-allocated-v1",
    )
    q.report = revised(
        q.report,
        cache_bindings=[*q.report.cache_bindings, binding],
        images=[*q.report.images, image],
    )
    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        lambda manifest, previous, started: report_for(q, manifest),
    )
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        assert (
            row.dispatch_metadata["native_deploy"]["phase"] == "initialization-intent"
        )
        assert row.dispatch_metadata["native_deploy"]["clone_lineages"]
        assert db.get(NativeCacheBirth, identity).receipt["original_snapshots"] == []
        target = deepcopy(row.dispatch_metadata["native_deploy"])
    api = q.apis[q.new_id]
    original = api.request

    def created(method, kind, namespace, name, desired):
        value = original(method, kind, namespace, name, desired)
        if kind == "DataVolume":
            value["status"] = {"phase": "Succeeded"}
            api.values[kind, value["metadata"]["name"]] = deepcopy(value)
            pvc = q.support.resource(
                "PersistentVolumeClaim",
                value["metadata"]["name"],
                value,
                deepcopy(value["spec"]["pvc"]),
            )
            pvc["metadata"]["namespace"] = namespace
            pvc["spec"]["volumeName"] = "tenant-clone-pv"
            source_ref = {
                "apiGroup": "cdi.kubevirt.io",
                "kind": "VolumeCloneSource",
                "name": "volume-clone-source-" + value["metadata"]["uid"],
            }
            pvc["spec"].update(
                dataSource=deepcopy(source_ref), dataSourceRef=source_ref
            )
            pvc["metadata"]["annotations"] = {
                "cdi.kubevirt.io/dataSourceNamespace": value["spec"]["source"]["pvc"][
                    "namespace"
                ]
            }
            pvc["status"] = {"phase": "Bound"}
            api.values["PersistentVolumeClaim", pvc["metadata"]["name"]] = pvc
            pv = q.support.resource(
                "PersistentVolume",
                "tenant-clone-pv",
                spec={
                    "claimRef": {
                        k: pvc["metadata"][k] for k in ("uid", "name", "namespace")
                    }
                },
            )
            pv["metadata"].pop("namespace")
            api.values["PersistentVolume", "tenant-clone-pv"] = pv
        return value

    monkeypatch.setattr(api, "request", created)
    if not publish_initialization:
        return operation, target, binding
    deploy.reconcile(
        q.new_id
    )  # Publish initializing marker; missing children stay off.
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    return operation, target, binding


@pytest.mark.parametrize("change", [None, "marker", "uid", "spec"])
def test_collector_waits_for_exact_initialization_publication(
    image_case, monkeypatch, change
):
    import json

    from app.models.native_clone_lineage import NativeCloneLineage
    from app.services import native_managed_collector as collector
    from app.services.quota_admission import usage
    from tests.test_native_managed_deploy import report_for

    q = image_case
    operation, target, _ = prepare_verified_image_operation(
        q, monkeypatch, publish_initialization=False
    )
    q.reader.api = object()
    api = q.apis[q.new_id]
    root = api.rows("TroshkaProject", None)[0]
    assert deploy.power.helpers().intent(root)["phase"] == "pending"
    assert target["phase"] == "initialization-intent"
    live = api.values["TroshkaProject", root["metadata"]["name"]]
    if change == "marker":
        marker = deploy.power.helpers().intent(root)
        marker["payloadDigest"] = "0" * 64
        live["metadata"]["annotations"][deploy.power.helpers().ANNOTATION] = json.dumps(
            marker
        )
    elif change == "uid":
        live["metadata"]["uid"] = "replacement"
    elif change == "spec":
        live["spec"]["action"] = "changed"
    advance_clock()
    report = collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        lambda manifest, previous, started: report_for(q, manifest),
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        assert usage(db, "project", q.new_id)[1].storage_bytes > 0
        for identity in target["clone_lineages"].values():
            lineage = db.get(NativeCloneLineage, identity)
            assert not lineage.creation and not lineage.proof
        assert (
            row.dispatch_metadata.get("native_image_diagnostic")
            != "clone_verification_pending"
        )
    if change:
        assert report.collection_errors
        assert (
            deploy.power.helpers().intent(api.rows("TroshkaProject", None)[0])["phase"]
            == "pending"
        )
    else:
        assert not report.collection_errors
        deploy.reconcile(q.new_id)
        assert (
            deploy.power.helpers().intent(api.rows("TroshkaProject", None)[0])
            == target["initialization_request"]
        )
        deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
        from app.services.native_managed_verification import transfer_creation

        assert transfer_creation(q.sessions, operation, target, q.reader) is True
        with q.sessions() as db:
            for identity in target["clone_lineages"].values():
                assert db.get(NativeCloneLineage, identity).creation["dv_uid"]


def publish_synthetic_clone(q, operation, target, source, monkeypatch):
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.schemas.kubevirt_storage import ProjectRoot, SnapshotKey
    from app.schemas.native_clone_lineage import CloneLineage, CloneObservation
    from app.services import native_clone_lineage
    from app.services.native_managed_verification import transfer_creation
    from tests.test_native_managed_deploy import report_for
    from tests.test_native_managed_resume import FixedDatetime

    monkeypatch.setattr(native_clone_lineage, "datetime", FixedDatetime)
    q.reader.api = object()
    transfer_creation(q.sessions, operation, target, q.reader)
    report = report_for(
        q,
        SimpleNamespace(
            pins=SimpleNamespace(
                project_roots=[
                    *q.report.project_roots,
                    ProjectRoot.model_validate(target["root"]),
                ]
            )
        ),
    )
    binding = next(
        b for b in report.volume_bindings if str(b.root.project_id) == q.new_id
    )
    binding = binding.model_copy(update={"provisioned_bytes": 40 * 1024**3})
    identity = next(iter(target["clone_lineages"].values()))
    middle = binding.image_key.model_copy(update={"image_id": "private-intermediate"})
    temp = SnapshotKey(image_key=middle, snapshot_id="1")
    golden = SnapshotKey(image_key=source.image_key, snapshot_id="2")

    class Observer:
        def observe_clone(self, intent, creation):
            return CloneObservation(
                target=intent.target,
                lineage=CloneLineage(
                    lineage_id=intent.lineage_id,
                    operation_id=operation,
                    claim_name=intent.claim_name,
                    domain_id=intent.domain_id,
                    target_id=intent.target_id,
                    cache_reference=intent.cache_reference,
                    binding=binding.model_copy(
                        update={"provisioned_bytes": None, "owner_reference_uids": []}
                    ),
                    source=source,
                    intermediate=middle,
                    head_parent=temp,
                    intermediate_parent=golden,
                    original_dv_uid=creation.dv_uid,
                    verified_at=FixedDatetime.current,
                ),
            )

    reference = native_clone_lineage.verify_clone(q.sessions, identity, Observer())
    with q.sessions() as db:
        assert db.get(NativeCloneLineage, identity).creation["dv_uid"]
    return reference, binding, middle, temp, golden


def clone_measurement(
    q, target, reference, binding, middle, temp, golden, *, amount, base_report=None
):
    from app.schemas.kubevirt_storage import (
        ImageObservation,
        ProjectRoot,
        SourceSnapshotChildren,
    )
    from tests.test_native_managed_deploy import report_for
    from tests.test_native_managed_resume import FixedDatetime
    from tests.test_quota_native_storage import revised

    manifest = SimpleNamespace(
        pins=SimpleNamespace(
            project_roots=[
                *q.report.project_roots,
                ProjectRoot.model_validate(target["root"]),
            ]
        )
    )
    report = (base_report or report_for)(q, manifest, amount=amount)
    images = []
    for image in report.images:
        if image.key == binding.image_key:
            image = image.model_copy(
                update={
                    "parent": temp,
                    "allocated_bytes": amount - 7,
                    "provisioned_bytes": 40 * 1024**3,
                }
            )
        if image.key == golden.image_key:
            image = image.model_copy(
                update={
                    "snapshots": [golden],
                    "children": [middle],
                    "source_snapshot_children": [
                        SourceSnapshotChildren(
                            snapshot=golden, present=True, children=[middle]
                        )
                    ],
                }
            )
        images.append(image)
    images.append(
        ImageObservation(
            key=middle,
            collected_at=FixedDatetime.current,
            allocated_bytes=7,
            provisioned_bytes=40 * 1024**3,
            measurement_status="complete",
            parent=golden,
            snapshots=[temp],
            children=[binding.image_key],
            command_version="synthetic-rbd-v1",
            measurement_semantics_version="kubevirt-rbd-allocated-v1",
        )
    )
    return revised(
        report,
        images=images,
        clone_references=[reference],
        volume_bindings=[
            b for b in report.volume_bindings if str(b.root.project_id) != q.new_id
        ]
        + [binding],
    )


@pytest.mark.parametrize("initial,delayed", [(16, 16), (40, 40), (16, 40)])
def test_independent_receipt_lineage_measured_initial_and_delayed_admission(
    image_case, monkeypatch, initial, delayed
):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services.kubevirt_storage_enrollment import validated_enrollment
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_native_managed_resume import FixedDatetime

    q = image_case
    operation, target, source = prepare_verified_image_operation(q, monkeypatch)
    proof = publish_synthetic_clone(q, operation, target, source, monkeypatch)

    def ingest(amount):
        advance_clock()
        report = clone_measurement(q, target, *proof, amount=amount)
        with q.sessions.begin() as db:
            domain = db.get(NativeCollectionDomain, target["domain_id"])
            manifest = validated_enrollment(
                db.get(NativeEnrollment, domain.enrollment_id)
            )
            import_report(
                db,
                report,
                enrollment_id=domain.enrollment_id,
                expected_enrollment_digest=manifest.digest,
                now=FixedDatetime.current,
            )
        from app.services.native_managed_collector import reconcile_allocations

        reconcile_allocations(q.sessions, manifest)

    ingest(initial)
    off = deploy.observe(q.apis[q.new_id], target, operation)
    deploy.admit_runtime(q.new_id, operation, target, off)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        assert row.observed["storage_bytes"] == initial
        target_now = deepcopy(row.dispatch_metadata["native_deploy"])
        assert target_now["phase"] == (
            "runtime-intent" if initial <= 32 else "initialization-intent"
        )
        assert row.observed["cpu"] == 0
    if initial > 32:
        assert target_now["blocker"] == "initialization_bound_exceeded"
        return
    ingest(delayed)
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
        assert row.observed["storage_bytes"] == delayed
        latest = row.dispatch_metadata["native_deploy"]
        if delayed > 32:
            assert latest["blocker"] == "initialization_bound_exceeded"
            assert not latest.get("release_handoff")
        else:
            assert latest["release_handoff"]
    assert (
        q.apis[q.new_id].rows("VirtualMachine", None)[0]["spec"]["runStrategy"]
        == "Halted"
    )
    if delayed <= 32:
        from app.models.quota import QuotaAction
        from app.services import quota_accounting, quota_enforcement
        from tests import test_native_managed_deploy as original

        api = q.apis[q.new_id]
        deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
        original.settle_runtime(q)
        advance_clock()
        deploy.reconcile(q.new_id)
        with q.sessions() as db:
            row = db.query(QuotaReservation).filter_by(operation_id=operation).one()
            assert row.state == "confirmed" and row.observed["cpu"] == 2
            assert row.observed["storage_bytes"] == 16
            quota_accounting.require_ready(db)
        base_report = original.report_for
        monkeypatch.setattr(
            original,
            "report_for",
            lambda q, manifest=None, amount=16: clone_measurement(
                q, target, *proof, amount=16, base_report=base_report
            ),
        )
        original.stop_and_resume(q, operation, 2, 16, monkeypatch)
        with q.sessions() as db:
            project = db.get(Project, q.new_id)
            project.budget_usd = 0
            db.commit()
            quota_enforcement.evaluate(db, FixedDatetime.current)
            db.commit()
            action = (
                db.query(QuotaAction)
                .filter_by(project_id=q.new_id, kind="stop_run")
                .order_by(QuotaAction.created_at.desc())
                .first()
            )
            quota_enforcement.reconcile_action(db, action.id, FixedDatetime.current)
            db.refresh(action)
            assert action.state == "completed"
            rows = db.query(QuotaReservation).filter_by(project_id=q.new_id).all()
            assert sum(r.observed["cpu"] for r in rows) == 0
            assert sum(r.observed["storage_bytes"] for r in rows) == 16
