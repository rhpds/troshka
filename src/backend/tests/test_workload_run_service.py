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
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(
        run_service, "validate_ansible_groups", lambda t, require_bastion: None
    )

    # Capture build_run_command calls to verify agnosticd_v2_url and scm_ref are passed
    build_run_calls = []

    def mock_build_run_command(
        item, paths, *, agnosticd_v2_url, scm_ref, kubeconfig=None
    ):
        build_run_calls.append(
            {
                "agnosticd_v2_url": agnosticd_v2_url,
                "scm_ref": scm_ref,
                "kubeconfig": kubeconfig,
            }
        )
        return ["bash", "-lc", "echo test"]

    monkeypatch.setattr(run_service, "build_run_command", mock_build_run_command)
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
    # Verify build_run_command was called with agnosticd_v2_url and scm_ref
    assert len(build_run_calls) == 1
    assert (
        build_run_calls[0]["agnosticd_v2_url"]
        == "https://github.com/rhpds/agnosticd-v2.git"
    )
    assert build_run_calls[0]["scm_ref"] == "main"
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
    # Verify ad-hoc uses the configured default EE image (not the dead troshka-runner)
    assert item.ee_image == "quay.io/redhat-gpte/troshka-ops-pod:latest"
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


def test_infer_status_from_logs_multihost_recap_with_failure():
    """Log inference should NOT infer succeeded when multi-host recap has failures."""
    logs = """
PLAY RECAP *********************************************************************
host1.example.com          : ok=10   changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
host2.example.com          : ok=8    changed=2    unreachable=0    failed=2    skipped=0    rescued=0    ignored=0
"""
    status = run_service._infer_status_from_logs(logs)
    assert status == "error"  # NOT "succeeded" despite host1 having failed=0


def test_infer_status_from_logs_all_hosts_succeeded():
    """Log inference should infer succeeded when all hosts have failed=0."""
    logs = """
PLAY RECAP *********************************************************************
host1.example.com          : ok=10   changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
host2.example.com          : ok=8    changed=2    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
"""
    status = run_service._infer_status_from_logs(logs)
    assert status == "succeeded"


def test_infer_status_from_logs_no_recap():
    """Log inference without PLAY RECAP should infer error."""
    logs = "Some ansible output without recap"
    status = run_service._infer_status_from_logs(logs)
    assert status == "error"


def test_run_workload_job_ocp_no_kubeconfig_fails(monkeypatch):
    """OCP project with no resolvable kubeconfig should fail the run."""
    db = TestSession()
    proj = _active_project(db, name="rs-ocp-bad")
    assert proj is not None
    # Topology has a node with ocpKubeconfig (so _has_ocp returns True),
    # but NOT a vmNode with clusterId (so _stored_cluster_creds yields nothing)
    proj.topology = {
        "nodes": [{"type": "networkNode", "data": {"ocpKubeconfig": True}}]
    }
    # deployed_topology is None so _has_ocp falls back to topology
    proj.deployed_topology = None
    db.commit()
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
    monkeypatch.setattr(run_service, "mint_run_key", lambda db, p: "trk_k")
    monkeypatch.setattr(
        run_service, "validate_ansible_groups", lambda t, require_bastion: None
    )
    monkeypatch.setattr(run_service, "build_inventory_yaml", lambda *a, **k: "inv")

    # The job should raise before reaching launch_runner_pod
    try:
        run_service.run_workload_job(rid)
        assert False, "Expected RuntimeError for OCP project with no kubeconfig"
    except RuntimeError as e:
        assert "targets OCP but no admin kubeconfig" in str(e)

    # Verify the run was failed
    db = TestSession()
    row = db.get(WorkloadRun, rid)
    assert row is not None
    assert row.status == "error"
    assert row.error is not None
    assert "kubeconfig" in row.error.lower()
    db.close()
