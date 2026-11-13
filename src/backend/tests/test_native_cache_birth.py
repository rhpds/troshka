"""New-source journal tests; synthetic observations are not live COW acceptance."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tests.test_kubevirt_storage_reports import native_case as native_case_fixture
from tests.test_native_initialization_evidence import synthetic_profile
from tests.test_native_initialization_observer import installed as installed_fixture
from tests.test_quota_concurrency import postgres as postgres_fixture

native_case = native_case_fixture
postgres = postgres_fixture
installed = installed_fixture


def test_source_rejects_credentials_and_unidentified_content():
    from app.schemas.kubevirt_cache_birth import CacheSource

    source = dict(
        endpoint="https://s4.example.test",
        bucket="images",
        key="rhel.qcow2",
        etag="content-etag",
        size_bytes=4096,
        last_modified=datetime.now(UTC),
        immutability_policy="trusted-admin-frozen-source-v1",
    )
    assert CacheSource(**source).key == "rhel.qcow2"
    with pytest.raises(ValueError):
        CacheSource(**{**source, "endpoint": "https://user:secret@s4.example.test"})
    with pytest.raises(ValueError):
        CacheSource(**{**source, "etag": ""})


def test_intent_rejects_adoption_inputs():
    from app.schemas.kubevirt_cache_birth import validate_import_spec

    spec = {
        "source": {
            "s3": {
                "url": "https://s4.example.test/images/rhel.qcow2",
                "secretRef": "source-auth",
            }
        },
        "pvc": {
            "storageClassName": "rbd",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "40Gi"}},
        },
    }
    validate_import_spec(spec)
    for forbidden in ("dataSource", "dataSourceRef", "volumeName", "selector"):
        with pytest.raises(ValueError):
            validate_import_spec({**spec, "pvc": {**spec["pvc"], forbidden: {}}})
    with pytest.raises(ValueError):
        validate_import_spec({**spec, "source": {"pvc": {"name": "old"}}})


def prepare(case, actor=None, target=None, namespace_uid="cache-ns"):
    from app.schemas.kubevirt_cache_birth import CacheSource
    from app.schemas.kubevirt_storage import InitializationTarget
    from app.services.native_cache_birth import prepare_cache_birth

    target = target or InitializationTarget.model_validate(
        synthetic_profile()["target"]
    )
    target = target.model_copy(
        update={"provider_id": UUID(str(case.pins.provider_clusters[0].provider_id))}
    )
    return prepare_cache_birth(
        case.sessions,
        actor_id=actor or case.admin_id,
        domain_id=case.pins.domain_id,
        target_id=case.target_id,
        target=target,
        namespace="trusted-cache",
        namespace_uid=namespace_uid,
        source=CacheSource(
            endpoint="https://s4.example.test",
            bucket="images",
            key="rhel.qcow2",
            etag="etag",
            size_bytes=4096,
            last_modified=datetime.now(UTC),
            immutability_policy="trusted-admin-frozen-source-v1",
        ),
        storage_request="40Gi",
        secret_ref="source-auth",
    )


class CreationAdapter:
    """Provider boundary: create must see its write-ahead latch already committed."""

    def __init__(self, case, *, existing=False, ambiguous=False):
        self.case, self.existing, self.ambiguous = case, existing, ambiguous
        self.created = []

    def preflight(self, intent):
        if self.existing:
            raise ValueError("preexisting target")

    def create(self, intent):
        from app.models.kubevirt_cache_birth import NativeCacheBirth

        with self.case.sessions() as db:
            assert (
                db.get(NativeCacheBirth, str(intent.operation_id)).state == "submitted"
            )
        self.created.append(intent.name)
        if self.ambiguous:
            raise TimeoutError("lost create response")
        return {
            "metadata": {
                "uid": "original-dv",
                "name": intent.name,
                "namespace": intent.namespace,
                "annotations": {"cdi.kubevirt.io/storage.usePopulator": "false"},
                "creationTimestamp": datetime.now(UTC).isoformat(),
            },
            "spec": intent.spec,
        }


def test_creation_journal_is_committed_before_io_and_never_recreates(native_case):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import submit_cache_birth

    operation = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, operation, adapter)
    submit_cache_birth(native_case.sessions, operation, adapter)
    assert len(adapter.created) == 1
    with native_case.sessions() as db:
        row = db.get(NativeCacheBirth, operation)
        assert row.state == "created"
        assert row.create_response["metadata"]["uid"] == "original-dv"


@pytest.mark.parametrize("failure", ["existing", "ambiguous"])
def test_ambiguous_or_preexisting_create_never_adopts_or_retries(native_case, failure):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import submit_cache_birth

    operation = prepare(native_case)
    adapter = CreationAdapter(native_case, **{failure: True})
    with pytest.raises((ValueError, TimeoutError)):
        submit_cache_birth(native_case.sessions, operation, adapter)
    with pytest.raises(ValueError, match="ambiguous"):
        submit_cache_birth(native_case.sessions, operation, adapter)
    assert len(adapter.created) == (1 if failure == "ambiguous" else 0)
    with native_case.sessions() as db:
        row = db.get(NativeCacheBirth, operation)
        assert row.state == "submitted"
        assert row.receipt is None


def test_unprivileged_user_cannot_prepare_source_authority(native_case):
    with pytest.raises(ValueError, match="administrator"):
        prepare(native_case, native_case.owner_id)


def observed(intent, response):
    """Independent Kubernetes/backend shape, with no live credentials."""
    from copy import deepcopy

    dv = deepcopy(response)
    dv["status"] = {"phase": "Succeeded"}
    now = response["metadata"]["creationTimestamp"]
    pvc = {
        "metadata": {
            "name": intent.name,
            "namespace": intent.namespace,
            "uid": "pvc-uid",
            "creationTimestamp": now,
            "ownerReferences": [
                {"kind": "DataVolume", "uid": "original-dv", "controller": True}
            ],
        },
        "spec": {**intent.spec["pvc"], "volumeName": "pvc-pvc-uid"},
        "status": {"phase": "Bound"},
    }
    pv = {
        "metadata": {
            "name": "pvc-pvc-uid",
            "uid": "pv-uid",
            "creationTimestamp": now,
            "annotations": {
                "pv.kubernetes.io/provisioned-by": intent.target.provisioner
            },
        },
        "spec": {
            "storageClassName": intent.target.storage_class_name,
            "claimRef": {
                "uid": "pvc-uid",
                "name": intent.name,
                "namespace": intent.namespace,
            },
            "csi": {"driver": intent.target.provisioner, "volumeHandle": "handle"},
        },
    }
    return {
        "dv": dv,
        "pvc": pvc,
        "pv": pv,
        "pods": [],
        "snapshots": [],
        "image_info": {"id": "fresh-image"},
        "image_key": {
            "ceph_fsid": intent.target.ceph_fsid,
            "pool_id": intent.target.pool_id,
            "rbd_namespace": intent.target.rbd_namespace,
            "image_id": "fresh-image",
        },
    }


def test_terminal_receipt_requires_stable_identity_and_survives_new_session(
    native_case,
):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import (
        submit_cache_birth,
        validated_receipt,
        verify_cache_birth,
    )

    operation = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, operation, adapter)
    adapter.observe = lambda intent, response: observed(intent, response)
    reference = verify_cache_birth(native_case.sessions, operation, adapter)
    with native_case.sessions() as db:
        intent, receipt = validated_receipt(db, operation, reference.digest)
        assert receipt.image_key.image_id == "fresh-image"
        assert receipt.original_snapshots == []
        row = db.get(NativeCacheBirth, operation)
        row.receipt = {**row.receipt, "pv_uid": "replacement"}
        db.commit()
    with native_case.sessions() as db, pytest.raises(ValueError, match="identity"):
        validated_receipt(db, operation, reference.digest)


@pytest.mark.parametrize(
    "change",
    [
        "dv",
        "source",
        "pvc",
        "claim",
        "backend",
        "snapshot",
        "writer",
        "incomplete",
        "adoption",
        "populator",
        "annotation",
    ],
)
def test_independent_verification_rejects_changed_or_unsafe_import(native_case, change):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth

    operation = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, operation, adapter)

    def observe(intent, response):
        value = observed(intent, response)
        if change == "dv":
            value["dv"]["metadata"]["uid"] = "replacement"
        if change == "source":
            value["dv"]["spec"] = {**intent.spec, "source": {"blank": {}}}
        if change == "pvc":
            value["pvc"]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        if change == "claim":
            value["pv"]["spec"]["claimRef"]["uid"] = "foreign"
        if change == "backend":
            value["image_info"]["parent"] = {"image_id": "historical"}
        if change == "snapshot":
            value["snapshots"] = [{"id": 1}]
        if change == "writer":
            value["pods"] = [
                {
                    "spec": {
                        "volumes": [
                            {"persistentVolumeClaim": {"claimName": intent.name}}
                        ]
                    }
                }
            ]
        if change == "incomplete":
            value["dv"]["status"]["phase"] = "ImportInProgress"
        if change == "adoption":
            value["pvc"]["spec"]["dataSource"] = {}
        if change == "populator":
            value["pvc"]["spec"]["dataSourceRef"] = {
                "apiGroup": "cdi.kubevirt.io",
                "kind": "VolumeImportSource",
                "name": "volume-import-source-original-dv",
            }
        if change == "annotation":
            value["dv"]["metadata"]["annotations"] = {}
        return value

    adapter.observe = observe
    with pytest.raises(ValueError):
        verify_cache_birth(native_case.sessions, operation, adapter)
    with native_case.sessions() as db:
        assert db.get(NativeCacheBirth, operation).receipt is None


@pytest.mark.parametrize("replacement", ["pvc", "pv", "image"])
def test_replacement_between_verification_reads_cannot_publish(
    native_case, replacement
):
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth

    operation = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, operation, adapter)
    calls = 0

    def observe(intent, response):
        nonlocal calls
        calls += 1
        value = observed(intent, response)
        if calls == 2:
            if replacement == "pvc":
                value["pvc"]["metadata"]["uid"] = "replacement"
                value["pvc"]["spec"]["volumeName"] = "pvc-replacement"
                value["pv"]["metadata"]["name"] = "pvc-replacement"
                value["pv"]["spec"]["claimRef"]["uid"] = "replacement"
            elif replacement == "pv":
                value["pv"]["metadata"]["uid"] = "replacement"
            else:
                value["image_info"]["id"] = "replacement"
                value["image_key"]["image_id"] = "replacement"
        return value

    adapter.observe = observe
    with pytest.raises(ValueError, match="changed during"):
        verify_cache_birth(native_case.sessions, operation, adapter)


@pytest.mark.parametrize("database", ["sqlite", "postgres"])
def test_cache_journal_migration_roundtrip(request, database):
    import importlib.util
    import os
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = (
        Path(__file__).parents[1]
        / "alembic/versions/b741df9860ac_native_cache_births.py"
    )
    spec = importlib.util.spec_from_file_location("cache_birth_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    if database == "postgres":
        if not os.getenv("QUOTA_TEST_POSTGRES_URL"):
            pytest.skip("disposable PostgreSQL URL not configured")
        _, _, engine = request.getfixturevalue("postgres")
    else:
        engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        if database == "postgres":
            # The existing fixture creates all model tables in a UUID-isolated
            # schema. Remove only this empty table before testing its migration.
            migration.downgrade()
        migration.upgrade()
        assert "native_cache_births" in inspect(connection).get_table_names()
        assert inspect(connection).get_foreign_keys("native_cache_births") == []
        migration.downgrade()
        assert "native_cache_births" not in inspect(connection).get_table_names()
    if database == "sqlite":
        engine.dispose()


def published(case, target=None):
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth

    operation = prepare(case, target=target)
    adapter = CreationAdapter(case)
    submit_cache_birth(case.sessions, operation, adapter)
    adapter.observe = lambda intent, response: observed(intent, response)
    return verify_cache_birth(case.sessions, operation, adapter)


def enrollment(case):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    with case.sessions() as db:
        return validated_enrollment(db.get(NativeEnrollment, case.enrollment_id))


def test_cache_authority_transition_is_explicit_additive_and_preserves_legacy_digest(
    native_case,
):
    from app.schemas.kubevirt_storage_enrollment import DomainExtension
    from app.services.kubevirt_storage_enrollment import (
        cache_authorization_digest,
        validate_domain_extension,
    )

    old = enrollment(native_case)
    assert "cache_birth_receipts" not in old.pins.model_dump(mode="json")
    reference = published(native_case)
    extension = DomainExtension(
        version="native-domain-extension-v1",
        previous_enrollment_id=native_case.enrollment_id,
        previous_enrollment_digest=old.digest,
    )
    pins = old.pins.model_copy(
        update={"cache_birth_receipts": [reference], "domain_extension": extension}
    )
    current = old.model_copy(update={"pins": pins})
    with pytest.raises(ValueError, match="cache authority"):
        validate_domain_extension(old, current)
    explicit = extension.model_copy(
        update={"cache_authorization_digest": cache_authorization_digest(pins)}
    )
    current = current.model_copy(
        update={"pins": pins.model_copy(update={"domain_extension": explicit})}
    )
    validate_domain_extension(old, current)
    with pytest.raises(ValueError, match="additive"):
        validate_domain_extension(
            current,
            old.model_copy(
                update={
                    "pins": old.pins.model_copy(update={"domain_extension": explicit})
                }
            ),
        )


def test_enrollment_rejects_unpublished_or_wrong_scope_receipt(native_case):
    from app.schemas.kubevirt_cache_birth import CacheBirthReference
    from app.services.kubevirt_storage_enrollment import _validate_cache_receipts

    unknown = CacheBirthReference(operation_id=uuid4(), digest="a" * 64)
    manifest = enrollment(native_case)
    with native_case.sessions() as db:
        with pytest.raises(ValueError, match="independently verified"):
            _validate_cache_receipts(
                db,
                manifest.model_copy(
                    update={
                        "pins": manifest.pins.model_copy(
                            update={"cache_birth_receipts": [unknown]}
                        )
                    }
                ),
            )
    reference = published(native_case)
    # Fixture target's cluster differs from original enrolled cluster.
    with native_case.sessions() as db, pytest.raises(ValueError, match="scope"):
        _validate_cache_receipts(
            db,
            manifest.model_copy(
                update={
                    "pins": manifest.pins.model_copy(
                        update={"cache_birth_receipts": [reference]}
                    )
                }
            ),
        )


def test_protected_enrollment_publishes_cache_without_replacing_project_roots(
    native_case,
):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_storage import InitializationTarget
    from app.schemas.kubevirt_storage_enrollment import DomainExtension
    from app.services.kubevirt_storage_enrollment import (
        cache_authorization_digest,
        digest,
        enroll_native_roots,
        validated_enrollment,
    )

    previous = enrollment(native_case)
    pool = previous.pins.pool_keys[0]
    target = InitializationTarget.model_validate(
        {
            **synthetic_profile()["target"],
            "cluster_key": previous.pins.provider_clusters[0].cluster_key,
            "ceph_fsid": pool.ceph_fsid,
            "pool_id": pool.pool_id,
        }
    )
    reference = published(native_case, target=target)
    pins = previous.pins.model_copy(update={"cache_birth_receipts": [reference]})
    extension = DomainExtension(
        version="native-domain-extension-v1",
        previous_enrollment_id=native_case.enrollment_id,
        previous_enrollment_digest=previous.digest,
        cache_authorization_digest=cache_authorization_digest(pins),
    )
    current = previous.model_copy(
        update={"pins": pins.model_copy(update={"domain_extension": extension})}
    )
    current = current.model_copy(
        update={"digest": digest(current.model_dump(mode="json", exclude={"digest"}))}
    )
    with native_case.sessions.begin() as db:
        identifier = enroll_native_roots(
            db, current, expected_target_id=native_case.target_id
        )
    with native_case.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(pins.domain_id))
        manifest = validated_enrollment(db.get(NativeEnrollment, domain.enrollment_id))
        assert domain.enrollment_id == identifier
        assert manifest.pins.project_roots == previous.pins.project_roots
        assert manifest.pins.cache_birth_receipts == [reference]
        assert (
            db.get(NativeEnrollment, native_case.enrollment_id).digest
            == previous.digest
        )


@pytest.fixture(params=["sqlite", "postgres"])
def concurrent_case(request, native_case):
    import os

    from app.models.kubevirt_storage import NativeReportingTarget
    from app.models.provider import Provider

    if request.param == "sqlite":
        return native_case
    if not os.getenv("QUOTA_TEST_POSTGRES_URL"):
        pytest.skip("disposable PostgreSQL URL not configured")
    sessions, (_, users, _, _), _ = request.getfixturevalue("postgres")
    with sessions.begin() as db:
        provider = Provider(name="native-cache", type="kubevirt")
        target_id = str(uuid4())
        db.add_all([provider, NativeReportingTarget(id=1, target_id=target_id)])
        db.flush()
        provider_id = provider.id
    return SimpleNamespace(
        sessions=sessions,
        admin_id=users[0],
        target_id=target_id,
        pins=SimpleNamespace(
            domain_id=uuid4(),
            provider_clusters=[SimpleNamespace(provider_id=provider_id)],
        ),
    )


def test_concurrent_publication_returns_one_durable_receipt(concurrent_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth

    case = concurrent_case
    operation = prepare(case)
    adapter = CreationAdapter(case)
    submit_cache_birth(case.sessions, operation, adapter)
    barrier = Barrier(2)

    def observe(intent, response):
        barrier.wait(timeout=5)
        return observed(intent, response)

    adapter.observe = observe
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(verify_cache_birth, case.sessions, operation, adapter)
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with case.sessions() as db:
        row = db.get(NativeCacheBirth, operation)
        assert row.state == "verified"
        assert row.receipt_digest == results[0].digest


def test_concurrent_submission_creates_at_most_once(concurrent_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services.native_cache_birth import submit_cache_birth

    case = concurrent_case
    operation = prepare(case)
    adapter = CreationAdapter(case)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        try:
            submit_cache_birth(case.sessions, operation, adapter)
        except ValueError as error:
            assert "ambiguous" in str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(adapter.created) == 1


@pytest.mark.parametrize("split", [False, True])
def test_concrete_provider_creates_and_independently_verifies_new_source(
    native_case, installed, monkeypatch, split
):
    from copy import deepcopy

    from kubernetes.client.exceptions import ApiException

    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.schemas.kubevirt_storage import ImageKey, ReadResult
    from app.services.native_cache_birth import (
        submit_cache_birth,
        validated_receipt,
        verify_cache_birth,
    )
    from app.services.native_cache_provider import (
        CacheCreationAdapter,
        CacheProviderAdapter,
        CacheVerificationAdapter,
    )

    q = installed
    operation = prepare(
        native_case, target=q.profile.target, namespace_uid="storage-ns-uid"
    )
    with native_case.sessions() as db:
        from app.services.native_cache_birth import validated_intent

        intent = validated_intent(db.get(NativeCacheBirth, operation))
    q.reader.provider_id = str(intent.target.provider_id)
    profile = q.profile.model_copy(update={"target": intent.target})
    source = intent.source
    head = {
        "ETag": source.etag,
        "ContentLength": source.size_bytes,
        "LastModified": source.last_modified,
    }
    source_client = SimpleNamespace(
        meta=SimpleNamespace(endpoint_url=source.endpoint),
        head_object=lambda **kw: head,
    )
    pool_options = {"retries": 4}
    q.reader.api.rest_client = SimpleNamespace(
        pool_manager=SimpleNamespace(connection_pool_kw=pool_options)
    )
    adapter = (
        CacheCreationAdapter(q.reader, source_client, profile)
        if split
        else CacheProviderAdapter(q.reader, q.backend, source_client, profile)
    )
    verifier = (
        CacheVerificationAdapter(q.reader, q.backend, source_client, profile)
        if split
        else adapter
    )
    resources = {}

    def dv_read(*args, **kwargs):
        if not resources:
            raise ApiException(status=404)
        return deepcopy(resources["dv"])

    def pvc_read(*args, **kwargs):
        if not resources:
            raise ApiException(status=404)
        return deepcopy(resources["pvc"])

    def create(*args, **kwargs):
        body = args[4]
        assert body["spec"]["pvc"]["volumeMode"] == "Filesystem"
        response = {
            **body,
            "metadata": {
                **body["metadata"],
                "uid": "original-dv",
                "creationTimestamp": datetime.now(UTC).isoformat(),
            },
        }
        resources.update(observed(intent, response))
        return response

    q.reader.custom = SimpleNamespace(
        get_namespaced_custom_object=dv_read, create_namespaced_custom_object=create
    )
    q.reader.core.read_namespaced_persistent_volume_claim = pvc_read
    q.reader.core.read_persistent_volume = lambda *a, **kw: deepcopy(resources["pv"])
    original_pods = q.reader.core.list_namespaced_pod
    q.reader.core.list_namespaced_pod = lambda *a, **kw: (
        original_pods(*a, **kw)
        if "label_selector" in kw
        else {"metadata": {}, "items": []}
    )

    def resolved(driver, handle, *, expected_request, volume_attributes):
        assert expected_request == "pvc-pvc-uid"
        assert driver == intent.target.provisioner and handle == "handle"
        return ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=ImageKey.model_validate(resources["image_key"]),
        )

    monkeypatch.setattr(q.backend, "resolve_csi", resolved)
    monkeypatch.setattr(
        q.backend,
        "image_info",
        lambda image: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=resources["image_info"],
        ),
    )
    monkeypatch.setattr(
        q.backend,
        "image_snapshots",
        lambda image: ReadResult(
            status="complete",
            collected_at=datetime.now(UTC),
            value=resources["snapshots"],
        ),
    )
    submit_cache_birth(native_case.sessions, operation, adapter)
    if split:
        assert not q.commands
    reference = verify_cache_birth(native_case.sessions, operation, verifier)
    assert pool_options["retries"] == 0
    with native_case.sessions() as db:
        _, receipt = validated_receipt(db, operation, reference.digest)
        assert receipt.image_key.image_id == "fresh-image"
    with pytest.raises(ValueError, match="preexisting"):
        adapter.preflight(intent)
    head["ETag"] = "replaced-source"
    with pytest.raises(ValueError, match="source identity"):
        verifier.observe(intent, resources["dv"])
