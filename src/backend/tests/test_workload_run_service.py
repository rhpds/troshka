# src/backend/tests/test_workload_run_service.py
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.project import Project
from app.models.provider import Provider
from app.models.workload_run import WorkloadRun
from app.services.workloads import run_service
from tests.conftest import TestSession


def _active_project(db, name="rs"):
    prov = Provider(name=f"prov-{uuid.uuid4()}", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    proj = Project(
        name=name,
        owner_id=str(uuid.uuid4()),
        provider_id=prov.id,
        state="active",
        topology={"nodes": []},
    )
    db.add(proj)
    db.flush()
    proj_id = proj.id
    db.commit()
    return db.get(Project, proj_id)


def test_start_workload_run_creates_row_and_enqueues(monkeypatch):
    db = TestSession()
    try:
        proj = _active_project(db)
        assert proj is not None
        enq = MagicMock()
        monkeypatch.setattr(run_service, "enqueue_job", enq)
        run = run_service.start_workload_run(
            db,
            project_id=proj.id,
            kind="catalog_item",
            catalog_item="agd-v2.x.prod",
            owner_id="u1",
        )
        assert run.status == "pending"
        assert run.catalog_item == "agd-v2.x.prod"
        assert enq.called
    finally:
        db.close()


def test_run_workload_job_happy_path(monkeypatch):
    db = TestSession()
    proj = _active_project(db, name="rs2")
    assert proj is not None
    run = WorkloadRun(
        project_id=proj.id,
        kind="catalog_item",
        catalog_item="agd-v2.x.prod",
        status="pending",
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    monkeypatch.setattr(
        run_service,
        "_host_for_project",
        lambda db, p: SimpleNamespace(id="h1", host_type="shared"),
    )
    monkeypatch.setattr(
        run_service,
        "resolve_catalog_item",
        lambda db, cid: SimpleNamespace(
            extra_vars={"config": "openshift-workloads"},
            ee_image="ee:1",
            scm_ref="main",
            requirements_content=None,
        ),
    )
    monkeypatch.setattr(run_service, "_prepare_agnosticd", lambda db, item: None)
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(run_service, "resolve_cluster_access", lambda p: {})
    monkeypatch.setattr(
        run_service, "validate_ansible_groups", lambda t, require_bastion: None
    )
    launched = MagicMock(return_value="job-1")
    monkeypatch.setattr(run_service, "launch_runner_pod", launched)
    monkeypatch.setattr(run_service, "_start_workload_monitor", lambda *a, **k: None)

    run_service.run_workload_job(rid)

    db = TestSession()
    row = db.get(WorkloadRun, rid)
    assert row is not None
    assert row.status == "running"
    assert row.started_at is not None
    assert launched.called
    db.close()


def test_synthesize_ad_hoc_minimal():
    """Test ad-hoc role synthesis with minimal FQCN."""
    db = TestSession()
    run = WorkloadRun(
        project_id=str(uuid.uuid4()),
        kind="ad_hoc",
        role_fqcn="redhat.openshift.install_operator",
        status="pending",
    )
    db.add(run)
    db.commit()

    item = run_service._synthesize_ad_hoc(db, run)
    assert item.extra_vars["config"] == "openshift-workloads"
    assert item.extra_vars["workloads"] == ["redhat.openshift.install_operator"]
    assert item.requirements_content == {"collections": [{"name": "redhat.openshift"}]}
    assert item.ee_image == "quay.io/redhat-gpte/troshka-runner:latest"
    assert item.scm_ref is None
    db.close()


def test_synthesize_ad_hoc_invalid_fqcn():
    """Test ad-hoc synthesis rejects malformed FQCNs."""
    db = TestSession()
    run = WorkloadRun(
        project_id=str(uuid.uuid4()),
        kind="ad_hoc",
        role_fqcn="invalid.role",
        status="pending",
    )
    db.add(run)
    db.commit()

    try:
        run_service._synthesize_ad_hoc(db, run)
        assert False, "Expected RuntimeError for invalid FQCN"
    except RuntimeError as e:
        assert "Invalid role FQCN" in str(e)
    finally:
        db.close()
