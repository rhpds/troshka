"""Explicit generation changes preserve ownership and reject active work."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.kubevirt_storage import (
    NativeCollectionDomain,
    NativeEnrollment,
    NativeProjectAttempt,
)
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.models.workload_run import WorkloadRun
from app.schemas.kubevirt_storage_enrollment import DomainExtension, VerifiedEnrollment
from app.services import kubevirt_storage_enrollment as enrollment
from tests.test_kubevirt_storage_reports import make_native_case


@pytest.fixture
def native_case(isolated_quota_database, monkeypatch):
    def forbidden_recovery(_):
        raise AssertionError("read-only enrollment must not invoke zombie recovery")

    monkeypatch.setattr("app.core.redis.get_job_info", forbidden_recovery)
    monkeypatch.setattr("app.core.redis.is_redis_available", lambda: True)
    monkeypatch.setattr(
        "app.core.redis.get_redis", lambda: SimpleNamespace(get=lambda _: None)
    )
    return make_native_case(isolated_quota_database)


def resign(value):
    return value.model_copy(
        update={
            "digest": enrollment.digest(
                value.model_dump(mode="json", exclude={"digest"})
            )
        }
    )


def transition(db, case):
    row = db.get(NativeEnrollment, case.enrollment_id)
    old = VerifiedEnrollment.model_validate(row.payload)
    roots = [
        r.model_copy(update={"deployment_generation": 1})
        for r in old.pins.project_roots
    ]
    old = resign(
        old.model_copy(
            update={"pins": old.pins.model_copy(update={"project_roots": roots})}
        )
    )
    row.payload, row.digest = old.model_dump(mode="json"), old.digest
    new_root = roots[0].model_copy(update={"deployment_generation": 2})
    reviewed = enrollment.digest(
        [
            {
                "before": roots[0].model_dump(mode="json"),
                "after": new_root.model_dump(mode="json"),
            }
        ]
    )
    extension = DomainExtension(
        version="native-domain-extension-v1",
        previous_enrollment_id=row.id,
        previous_enrollment_digest=row.digest,
        root_transition_digest=reviewed,
    )
    new = resign(
        old.model_copy(
            update={
                "pins": old.pins.model_copy(
                    update={
                        "project_roots": [new_root, *roots[1:]],
                        "domain_extension": extension,
                    }
                )
            }
        )
    )
    for root in roots:
        db.get(Project, str(root.project_id)).state = "active"
    db.flush()
    return old, new


def test_generation_transition_requires_exact_review_and_preserves_history(
    native_case, monkeypatch
):
    with native_case.sessions() as db:
        old, new = transition(db, native_case)
        assert (
            enrollment.root_transition_digest(old.pins, new.pins)
            == new.pins.domain_extension.root_transition_digest
        )
        domain = db.get(NativeCollectionDomain, str(old.pins.domain_id))
        enrollment._extension_predecessor(db, new, domain)
        row = NativeEnrollment(
            id=str(uuid4()),
            domain_id=domain.id,
            digest=new.digest,
            payload=new.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
        # Historical validation must not consult today's project state.
        db.get(Project, str(old.pins.project_roots[0].project_id)).state = "deploying"
        assert len(list(enrollment._enrollment_chain(db, domain, row.id))) == 2
        assert db.get(
            NativeEnrollment, native_case.enrollment_id
        ).payload == old.model_dump(mode="json")


@pytest.mark.parametrize(
    "mutation",
    ["missing", "wrong", "decrease", "unknown", "uid", "drop", "collection", "noop"],
)
def test_transition_rejects_unreviewed_or_non_generation_changes(native_case, mutation):
    with native_case.sessions() as db:
        old, new = transition(db, native_case)
        pins = new.pins
        if mutation in {"missing", "wrong"}:
            pins = pins.model_copy(
                update={
                    "domain_extension": pins.domain_extension.model_copy(
                        update={
                            "root_transition_digest": None
                            if mutation == "missing"
                            else "0" * 64
                        }
                    )
                }
            )
        elif mutation == "drop":
            pins = pins.model_copy(update={"project_roots": pins.project_roots[:1]})
        elif mutation == "collection":
            pins = pins.model_copy(update={"collector_ids": ["foreign"]})
        else:
            change = {
                "deployment_generation": {
                    "decrease": 0,
                    "unknown": None,
                    "noop": 1,
                }.get(mutation, 2)
            }
            if mutation == "uid":
                change["project_cr_uid"] = "replacement"
            pins = pins.model_copy(
                update={
                    "project_roots": [
                        pins.project_roots[0].model_copy(update=change),
                        *pins.project_roots[1:],
                    ]
                }
            )
            # Even an accurately reviewed digest cannot authorize replacement,
            # backward/unknown generation, or a no-op transition.
            reviewed = (
                []
                if mutation == "noop"
                else [
                    {
                        "before": old.pins.project_roots[0].model_dump(mode="json"),
                        "after": pins.project_roots[0].model_dump(mode="json"),
                    }
                ]
            )
            pins = pins.model_copy(
                update={
                    "domain_extension": pins.domain_extension.model_copy(
                        update={"root_transition_digest": enrollment.digest(reviewed)}
                    )
                }
            )
        with pytest.raises(ValueError):
            enrollment.validate_domain_extension(
                old, new.model_copy(update={"pins": pins})
            )


@pytest.mark.parametrize(
    "blocker",
    [
        "reservation",
        "allocation",
        "cleanup",
        "deploying",
        "job",
        "unavailable",
        "read_failure",
        "pending",
        "running",
    ],
)
def test_transition_rejects_live_work(native_case, monkeypatch, blocker):
    if blocker == "job":
        monkeypatch.setattr(
            "app.core.redis.get_redis",
            lambda: SimpleNamespace(get=lambda _: "retained-job-reference"),
        )
    if blocker == "unavailable":
        monkeypatch.setattr("app.core.redis.is_redis_available", lambda: False)
    if blocker == "read_failure":

        def failed_read(_):
            raise ConnectionError("tracker unreachable")

        monkeypatch.setattr(
            "app.core.redis.get_redis", lambda: SimpleNamespace(get=failed_read)
        )
    with native_case.sessions() as db:
        old, new = transition(db, native_case)
        project = db.get(Project, str(old.pins.project_roots[0].project_id))
        if blocker in {"reservation", "allocation", "cleanup"}:
            model = QuotaAllocation if blocker == "allocation" else QuotaReservation
            kwargs = (
                {"dispatch_metadata": {"native_cleanup": {"phase": "pending"}}}
                if blocker == "cleanup"
                else {}
            )
            db.add(
                model(
                    operation_id=str(uuid4()),
                    project_id=project.id,
                    owner_id=project.owner_id,
                    organization_id=str(uuid4()),
                    **kwargs,
                )
            )
        elif blocker == "deploying":
            project.state = "deploying"
        elif blocker in {"pending", "running"}:
            db.add(WorkloadRun(project_id=project.id, kind="ad_hoc", status=blocker))
        db.flush()
        domain = db.get(NativeCollectionDomain, str(old.pins.domain_id))
        with pytest.raises(ValueError, match="root transition"):
            enrollment._extension_predecessor(db, new, domain)


def test_legacy_extension_digest_unchanged():
    original = {
        "version": "native-domain-extension-v1",
        "previous_enrollment_id": str(uuid4()),
        "previous_enrollment_digest": "a" * 64,
    }
    assert DomainExtension.model_validate(original).model_dump(mode="json") == original


def test_apply_resets_only_transitioned_cursor(native_case, monkeypatch):
    with native_case.sessions() as db:
        old, new = transition(db, native_case)
        attempts = []
        for root in old.pins.project_roots:
            attempt = NativeProjectAttempt(
                domain_id=str(old.pins.domain_id),
                project_id=str(root.project_id),
                root_key=enrollment.root_key(root),
                enrollment_id=native_case.enrollment_id,
                latest_report_id=str(uuid4()),
                latest_started_at=old.verified_at,
                conflicted=True,
            )
            db.add(attempt)
            attempts.append(attempt)
        db.flush()
        other_report = attempts[1].latest_report_id
        request = enrollment.prepare_native_enrollment(
            db, [str(r.project_id) for r in new.pins.project_roots]
        )
        new = resign(new.model_copy(update={"request": request}))
        result = enrollment.enroll_native_roots(
            db, new, expected_target_id=native_case.target_id
        )
        assert attempts[0].latest_report_id is None
        assert attempts[0].latest_started_at is None
        assert attempts[0].conflicted is False
        assert attempts[0].enrollment_id == result
        assert attempts[1].latest_report_id == other_report
        assert attempts[1].enrollment_id == native_case.enrollment_id
        assert attempts[1].conflicted is True


@pytest.mark.parametrize("live_generation", [None, 1, True, 2, 3])
def test_live_verification_rejects_pinned_generation_mismatch(
    native_case, live_generation
):
    root = native_case.pins.project_roots[0].model_copy(
        update={"deployment_generation": 2}
    )

    class Kube:
        def namespace(self, _):
            return {"metadata": {"uid": root.namespace_uid}}

        def projects(self, _):
            return [
                {
                    "metadata": {
                        "uid": root.project_cr_uid,
                        "generation": live_generation,
                    },
                    "spec": {"projectId": str(root.project_id)},
                }
            ]

    if type(live_generation) is int and live_generation == 2:
        enrollment._verify_project(Kube(), root)
    else:
        with pytest.raises(ValueError, match="generation"):
            enrollment._verify_project(Kube(), root)
