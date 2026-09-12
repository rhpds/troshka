from app.models.workload_run import WorkloadRun
from tests.conftest import TestSession


def test_workload_run_defaults_and_persist():
    db = TestSession()
    try:
        run = WorkloadRun(
            kind="catalog_item",
            catalog_item="agd-v2.mcp-with-openshift.prod",
            ee_image="quay.io/agnosticd/ee-multicloud:x",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.id  # uuid string assigned
        assert run.status == "pending"
        assert run.created_at is not None
        assert run.project_id is None
    finally:
        db.close()


def test_workload_run_target_map_jsonb():
    db = TestSession()
    try:
        run = WorkloadRun(
            kind="ad_hoc",
            role_fqcn="agnosticd.core_workloads.ocp4_workload_gitea_operator",
            target_map={"clusters": {"default": "cl-1"}},
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        assert run.target_map is not None
        assert run.target_map["clusters"]["default"] == "cl-1"
    finally:
        db.close()
