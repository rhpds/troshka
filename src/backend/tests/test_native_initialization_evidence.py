"""Syntactic/integrity checks using an explicitly synthetic blank-PVC profile.

These tests cannot certify any physical initialization bound or real CSI path.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.kubevirt_storage import InitializationObservation
from app.schemas.kubevirt_storage_enrollment import VerificationPins
from app.schemas.kubevirt_storage_initialization import InitializationEvidence
from app.schemas.quota import AccountingUnavailable
from app.services import native_initialization as initialization
from app.services.kubevirt_storage_enrollment import digest
from app.services.kubevirt_storage_transport import CapabilityProfile
from tests.test_kubevirt_storage_reports import native_case as native_case_fixture

native_case = native_case_fixture


def synthetic_profile():
    return {
        "version": "native-blank-initialization-v1",
        "profile_name": "synthetic-test-only",
        "operation": "blank-pvc",
        "metric": "rbd_allocated_extent_bytes",
        "measurement_version": "native-rbd-measurement-v1",
        "validation_reference": "synthetic:test-fixture-not-physical-acceptance",
        "validation_sha256": "a" * 64,
        "bound_justification": "Synthetic bound for state/integrity tests only",
        "target": {
            "provider_id": "00000000-0000-4000-8000-000000000003",
            "cluster_key": "synthetic-cluster",
            "ceph_fsid": "synthetic-fsid",
            "pool_id": 1,
            "rbd_namespace": "",
            "storage_class_name": "synthetic-sc",
            "storage_class_uid": "synthetic-sc-uid",
            "provisioner": "synthetic.rbd.csi.ceph.com",
            "storage_class_parameters": {"pool": "synthetic-pool"},
            "csi_version": "synthetic-csi-v1",
            "ceph_version": "synthetic-ceph-v1",
            "rbd_command_version": "synthetic-rbd-v1",
        },
        "minimum_capacity_bytes": 1024,
        "maximum_capacity_bytes": 4096,
        "preallocation": "disabled",
        "initial_storage_bound_bytes": 32,
        "reservation_semantics": "measured-startup-allowance-v1",
        "vnc_image": "quay.io/redhat-gpte/troshka-vnc-proxy:v0-synthetic-acceptance",
    }


def capability(profile):
    return CapabilityProfile.model_validate(
        {
            "command_version": "synthetic-rbd-v1",
            "initialization_evidence": profile,
        }
    )


def test_optional_initialization_is_absent_from_legacy_capability_serialization():
    original = {
        "command_version": "legacy-v1",
        "merge_snapshots_validated": False,
        "validation_reference": None,
        "semantics_version": "kubevirt-rbd-allocated-v1",
    }
    assert (
        CapabilityProfile.model_validate(original).model_dump(mode="json") == original
    )


def test_synthetic_initialization_envelope_roundtrips_without_certifying_physics():
    supplied = synthetic_profile()
    assert (
        capability(supplied).model_dump(mode="json")["initialization_evidence"]
        == supplied
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "warm-clone"),
        ("metric", "virtual_pvc_bytes"),
        ("measurement_version", "future-v2"),
        ("validation_reference", ""),
        ("validation_sha256", "not-a-digest"),
        ("initial_storage_bound_bytes", True),
        ("initial_storage_bound_bytes", -1),
        ("initial_storage_bound_bytes", 0),
        ("reservation_semantics", "physical-upper-bound-v1"),
        ("vnc_image", "quay.io/redhat-gpte/troshka-vnc-proxy:latest"),
        ("vnc_image", "quay.io/redhat-gpte/troshka-vnc-proxy@sha256:" + "a" * 64),
        ("vnc_image", "quay.io/foreign/troshka-vnc-proxy:v1"),
        ("minimum_capacity_bytes", 0),
        ("minimum_capacity_bytes", 8192),
        ("preallocation", "enabled"),
    ],
)
def test_invalid_initialization_evidence_is_not_a_candidate(field, value):
    supplied = synthetic_profile()
    supplied[field] = value
    with pytest.raises(ValidationError):
        capability(supplied)


def test_report_or_source_fields_cannot_be_smuggled_into_blank_profile():
    supplied = synthetic_profile()
    supplied["source_image"] = {"image_id": "warm-source"}
    with pytest.raises(ValidationError):
        capability(supplied)


@pytest.mark.parametrize(
    "field", ["reservation_semantics", "vnc_image", "initial_storage_bound_bytes"]
)
def test_legacy_profile_cannot_implicitly_authorize_startup_overage(field):
    supplied = synthetic_profile()
    supplied.pop(field)
    with pytest.raises(ValidationError):
        capability(supplied)


def test_enrollment_absence_preserves_legacy_digest_and_report_cannot_authorize(
    native_case,
):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.services.kubevirt_storage_enrollment import digest

    q = native_case
    with q.sessions() as db:
        original = db.get(NativeEnrollment, q.enrollment_id).payload
        restored = VerificationPins.model_validate(original["pins"])
        assert restored.model_dump(mode="json") == original["pins"]
        assert (
            digest({k: v for k, v in original.items() if k != "digest"})
            == original["digest"]
        )
        payload = q.report.model_dump(mode="json")
        payload["initialization_evidence"] = synthetic_profile()
        with pytest.raises(ValidationError):
            type(q.report).model_validate(payload)


def test_collector_capability_cannot_author_domain_authorization():
    body = capability(synthetic_profile()).model_dump(mode="json")
    body["domain_extension"] = {
        "version": "native-domain-extension-v1",
        "previous_enrollment_id": "00000000-0000-4000-8000-000000000003",
        "previous_enrollment_digest": "a" * 64,
        "initialization_authorization_digest": "b" * 64,
    }
    with pytest.raises(ValidationError):
        CapabilityProfile.model_validate(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", "00000000-0000-4000-8000-000000000099"),
        ("cluster_key", "foreign"),
        ("pool_id", 99),
    ],
)
def test_enrollment_profile_target_must_belong_to_pinned_scope(
    native_case, field, value
):
    q = native_case
    body = q.pins.model_dump(mode="json")
    profile = synthetic_profile()
    profile["target"].update(
        provider_id=str(q.request.projects[0].provider_id),
        cluster_key="cluster",
        ceph_fsid=q.pins.pool_keys[0].ceph_fsid,
    )
    profile["target"][field] = value
    body["initialization_evidence"] = profile
    with pytest.raises(ValidationError):
        VerificationPins.model_validate(body)


@pytest.mark.parametrize("offset", [0, -61, 1])
def test_initialization_observation_uses_post_read_clock_without_restamping(
    monkeypatch, offset
):
    profile = InitializationEvidence.model_validate(synthetic_profile())
    state = SimpleNamespace(now=datetime(2026, 11, 13, tzinfo=UTC), sample=None)
    events = []

    class SyntheticReader:
        headroom_semantics = "synthetic-allocated-extents-headroom-v1"

        def observe(self, profile, kube, backend):
            events.append("read")
            state.now += timedelta(seconds=2)
            state.sample = InitializationObservation(
                version="native-initialization-observation-v1",
                metric=profile.metric,
                measurement_version=profile.measurement_version,
                profile_digest=digest(profile),
                target=profile.target,
                observed_at=state.now + timedelta(seconds=offset),
                headroom_bytes=100,
                headroom_semantics=self.headroom_semantics,
            )
            return state.sample

    monkeypatch.setattr(initialization, "implementation", lambda _: SyntheticReader())

    def clock():
        events.append("clock")
        return state.now

    if offset:
        with pytest.raises(AccountingUnavailable):
            initialization.observe(profile, None, None, clock=clock)
    else:
        result = initialization.observe(profile, None, None, clock=clock)
        assert result.observed_at == state.sample.observed_at
    assert events == ["read", "clock"]
