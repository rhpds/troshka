"""API cancellation of original native reservations before any provider handoff."""

from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.api import projects
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.models.user import User
from app.services import quota_operations as operations
from app.services.quota_admission import OperationConflict
from tests.test_native_managed_deploy import deploy_case as deploy_case_fixture
from tests.test_native_managed_resume import FixedDatetime
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_database_fixture,
)
from tests.test_native_managed_resume import resume_case as resume_case_fixture
from tests.test_quota_native_storage import native_ledger as native_ledger_fixture

isolated_quota_database = isolated_database_fixture
native_ledger = native_ledger_fixture
resume_case = resume_case_fixture
deploy_case = deploy_case_fixture


@pytest.fixture
def unclaimed(deploy_case):
    from app.services import placement

    q = deploy_case
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        q.operation_id = operations.prepare(db, project, "deploy", host_id=q.host_id)
        db.commit()
        placement.place_project(
            db, project, host_id=q.host_id, quota_operation_id=q.operation_id
        )
        project.state = "error"  # Simulate failure before claim; no worker completion.
        project.deploy_error = "Synthetic collector readiness failure"
        q.topology = deepcopy(project.topology)
        db.commit()
    return q


@pytest.mark.parametrize("state", ["error", "deploying"])
def test_api_unclaimed_undeploy_fences_stale_delivery(unclaimed, monkeypatch, state):
    from app.models.kubevirt_storage import NativeCollectionDomain
    from app.services import native_initialization

    q = unclaimed
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        project.state = state
        row = db.query(QuotaReservation).filter_by(operation_id=q.operation_id).one()
        assert row.dispatch_metadata["worker_finished"] is False
        assert not row.dispatch_metadata.get("claim")
        # Cancellation must not require the collector that prevented the claim.
        domain = db.query(NativeCollectionDomain).first()
        domain.latest_report_id = None
        db.commit()
        monkeypatch.setattr(
            native_initialization,
            "current",
            lambda *a, **kw: pytest.fail("physical readiness consulted"),
        )
        result = projects.undeploy_project(project.id, db.get(User, q.owner_id), db)
        assert result == {"status": "draft"}
        assert project.owner_id == q.owner_id and project.topology == q.topology
        assert project.host_id is None and project.deployed_topology is None
        assert not project.deploy_error
        db.refresh(row)
        assert row.dispatch_metadata["cancelled"]
        assert not any(row.observed.values())
        assert not operations.current_generation(operations._rows(db, project.id))
        assert (
            not db.query(QuotaAllocation)
            .filter_by(project_id=project.id)
            .filter(QuotaAllocation.state != "released")
            .first()
        )
    called = []

    @operations.guarded_job("deploy")
    def stale(project_id):
        called.append(project_id)

    with pytest.raises(OperationConflict, match="Completed operation"):
        stale(q.new_id, quota_operation_id=q.operation_id)
    assert not called
    with q.sessions() as db:
        assert db.get(Project, q.new_id).state == "draft"


@pytest.mark.parametrize(
    "condition",
    [
        "claim",
        "calls",
        "create_intent",
        "root",
        "storage_identity",
        "observed",
        "known_empty",
        "allocation",
        "retained_binding",
        "enrolled_root",
        "owner",
        "topology",
        "dispatched",
        "malformed_historical_binding",
    ],
)
def test_api_unclaimed_cleanup_rejects_ambiguous_work(
    unclaimed, condition, monkeypatch
):
    from app.models.kubevirt_storage import NativeProjectRoot, NativeRetainedBinding

    q = unclaimed
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=q.operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        target = metadata["native_deploy"]
        project = db.get(Project, q.new_id)
        if condition in {"claim", "calls", "storage_identity"}:
            key = (
                "native_storage_identity"
                if condition == "storage_identity"
                else condition
            )
            metadata[key] = {"ambiguous": True}
        elif condition in {"create_intent", "root"}:
            target[
                "create_started_at" if condition == "create_intent" else "root"
            ] = "uncertain"
        elif condition == "observed":
            row.observed = {**row.observed, "storage_bytes": 1}
        elif condition == "known_empty":
            metadata["known_empty"] = False
        elif condition == "allocation":
            db.add(
                QuotaAllocation(
                    operation_id=row.operation_id,
                    project_id=project.id,
                    owner_id=row.owner_id,
                    organization_id=row.organization_id,
                    state="released",
                    requested=row.requested,
                    observed={**row.observed, "storage_bytes": 1},
                )
            )
        elif condition == "retained_binding":
            db.add(
                NativeRetainedBinding(
                    key="unknown-retained",
                    root_key="unknown",
                    domain_id=target["domain_id"],
                    payload={"root": {"project_id": project.id}},
                )
            )
        elif condition == "enrolled_root":
            db.query(NativeProjectRoot).first().project_id = project.id
        elif condition == "malformed_historical_binding":
            from app.services import quota_cleanup

            root = db.query(NativeProjectRoot).first()
            root.project_id = project.id
            binding = (
                db.query(NativeRetainedBinding).filter_by(root_key=root.key).first()
            )
            binding.payload = {"unknown": "retained-storage"}
            # A retired root is historical, but a changed binding is not covered
            # by its original exact settlement receipt.
            monkeypatch.setattr(quota_cleanup, "active_roots", lambda db, roots: [])
        elif condition == "owner":
            target["owner_id"] = "different-owner"
        elif condition == "topology":
            project.topology = {}
        elif condition == "dispatched":
            row.dispatched_at = FixedDatetime.current
        row.dispatch_metadata = metadata
        db.commit()
        before = deepcopy(row.dispatch_metadata)
        with pytest.raises(HTTPException):
            projects.undeploy_project(project.id, db.get(User, q.owner_id), db)
        db.rollback()
        db.refresh(row)
        assert row.dispatch_metadata == before
        assert db.get(Project, q.new_id).host_id == q.host_id


def test_postgres_unclaimed_cleanup_serializes_with_claim(unclaimed):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.services import quota_admission, quota_cleanup

    q = unclaimed
    with q.sessions() as holder:
        if holder.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL operation lock boundary only")
        quota_admission.lock_operations(holder, [q.operation_id])

        def contend():
            with q.sessions() as db:
                db.execute(text("SET LOCAL lock_timeout = '150ms'"))
                with pytest.raises(OperationalError) as caught:
                    quota_cleanup.request_native(
                        db, db.get(Project, q.new_id), "undeploy"
                    )
                assert caught.value.orig.pgcode == "55P03"
                assert "pg_advisory_xact_lock" in caught.value.statement
                db.rollback()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contend).result(timeout=10)
