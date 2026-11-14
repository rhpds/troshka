"""Lease loss after remote reads must roll back protected authority publication."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import native_collection_jobs as jobs
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


@pytest.fixture
def lease():
    state = SimpleNamespace(active=True)
    redis = SimpleNamespace(get=lambda key: "owner" if state.active else None)
    token = jobs._execution.set((redis, {"lease": "owner"}))
    try:
        yield state
    finally:
        jobs._execution.reset(token)


def test_cache_receipt_lease_loss_after_observation_rolls_back(native_case, lease):
    from app.models.kubevirt_cache_birth import NativeCacheBirth
    from app.services.native_cache_birth import submit_cache_birth, verify_cache_birth
    from tests.test_native_cache_birth import CreationAdapter, observed, prepare

    identity = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, identity, adapter)

    def observe(intent, response):
        lease.active = False
        return observed(intent, response)

    adapter.observe = observe
    with pytest.raises(RuntimeError, match="lease expired"):
        verify_cache_birth(native_case.sessions, identity, adapter)
    with native_case.sessions() as db:
        row = db.get(NativeCacheBirth, identity)
        assert row.state == "created"
        assert row.receipt is None and row.receipt_digest is None


def test_clone_proof_lease_loss_after_observation_rolls_back(native_case, lease):
    from app.models.native_clone_lineage import NativeCloneLineage
    from app.services.native_clone_lineage import verify_clone
    from tests.test_native_clone_lineage import CloneAdapter, managed_case

    identity = managed_case(native_case)[0]
    adapter = CloneAdapter(native_case, 0)
    original = adapter.observe_clone

    def observe(*args):
        lease.active = False
        return original(*args)

    adapter.observe_clone = observe
    with pytest.raises(RuntimeError, match="lease expired"):
        verify_clone(native_case.sessions, identity, adapter)
    with native_case.sessions() as db:
        row = db.get(NativeCloneLineage, identity)
        assert row.proof is None and row.proof_digest is None


def test_expired_verification_cannot_commit_diagnostic(native_case, lease):
    from app.models.quota import QuotaReservation
    from app.services.native_managed_verification import diagnostic
    from tests.test_native_clone_lineage import managed_case

    managed_case(native_case)
    with native_case.sessions() as db:
        row = db.query(QuotaReservation).first()
        operation_id = row.operation_id
        target = row.dispatch_metadata["native_deploy"]
    lease.active = False
    with pytest.raises(RuntimeError, match="lease expired"):
        diagnostic(native_case.sessions, operation_id, target, "pending")
    with native_case.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert "native_image_diagnostic" not in row.dispatch_metadata


def test_source_verification_propagates_lease_loss(native_case, lease, monkeypatch):
    from app.services import native_cache_provider, native_managed_images
    from app.services.native_cache_birth import submit_cache_birth
    from app.services.native_managed_verification import verify_sources
    from tests.test_native_cache_birth import CreationAdapter, observed, prepare

    identity = prepare(native_case)
    adapter = CreationAdapter(native_case)
    submit_cache_birth(native_case.sessions, identity, adapter)
    closed = []
    monkeypatch.setattr(
        native_managed_images,
        "source_client",
        lambda *a: SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(
        native_cache_provider, "CacheVerificationAdapter", lambda *a: adapter
    )

    def observe(intent, response):
        lease.active = False
        return observed(intent, response)

    adapter.observe = observe
    profile = SimpleNamespace(
        source_policy=None, target=SimpleNamespace(provider_id="provider")
    )
    selected = [("operation", {"sources": {"disk": {"operation_id": identity}}})]
    with pytest.raises(RuntimeError, match="lease expired"):
        verify_sources(
            native_case.sessions, selected, {"provider": None}, None, profile
        )
    assert closed == [True]


def test_source_enrollment_lease_loss_after_network_verification_rolls_back(
    native_case, lease, monkeypatch
):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_cache_birth import CacheBirthReference
    from app.services import kubevirt_storage_enrollment as enrollment
    from app.services import native_managed_collector, native_managed_verification

    domain_id = str(native_case.pins.domain_id)
    reference = CacheBirthReference(operation_id=uuid4(), digest="a" * 64)
    selected = [
        (
            "operation",
            {"sources": {"disk": {"operation_id": str(reference.operation_id)}}},
        )
    ]

    def anchored(db, anchor):
        domain = db.get(NativeCollectionDomain, domain_id)
        return domain, enrollment.validated_enrollment(
            db.get(NativeEnrollment, domain.enrollment_id)
        )

    def verify(*args, **kwargs):
        lease.active = False
        return None

    def enroll(db, *args, **kwargs):
        db.get(NativeCollectionDomain, domain_id).enrollment_id = str(uuid4())

    monkeypatch.setattr(native_managed_collector, "anchored", anchored)
    monkeypatch.setattr(native_managed_verification, "pending", lambda *a: selected)
    monkeypatch.setattr(enrollment, "prepare_native_enrollment", lambda *a: None)
    monkeypatch.setattr(enrollment, "verify_native_enrollment", verify)
    monkeypatch.setattr(enrollment, "enroll_native_roots", enroll)
    with pytest.raises(RuntimeError, match="lease expired"):
        native_managed_verification.publish_sources(
            native_case.sessions, None, native_case.target_id, [reference], {}, None
        )
    with native_case.sessions() as db:
        assert (
            db.get(NativeCollectionDomain, domain_id).enrollment_id
            == native_case.enrollment_id
        )
