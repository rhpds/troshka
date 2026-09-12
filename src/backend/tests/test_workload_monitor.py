from unittest.mock import MagicMock

from app.models.project import Project
from app.models.provider import Provider
from app.models.workload_run import WorkloadRun
from app.services.workloads import run_service
from tests.conftest import TestSession


def test_parse_workload_progress_extracts_task():
    text = "TASK [ocp4_workload_gitea_operator : Create namespace] ***\nok: [localhost]"
    prog = run_service.parse_workload_progress(text)
    assert "step" in prog and prog["step"]


def test_resume_workload_monitors_reattaches_running(monkeypatch):
    from app.models.host import Host

    db = TestSession()
    prov = Provider(name="pm", type="troshka", credentials="{}")
    db.add(prov)
    db.flush()
    host = Host(ip_address="10.0.0.1", provider_id=prov.id, agent_status="connected")
    db.add(host)
    db.flush()
    proj = Project(
        name="mon", owner_id="u1", provider_id=prov.id, state="active", host_id=host.id
    )
    db.add(proj)
    db.flush()
    run = WorkloadRun(
        project_id=proj.id, kind="ad_hoc", role_fqcn="x", status="running"
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    monkeypatch.setattr(run_service, "SessionLocal", TestSession)
    reattached = MagicMock()
    monkeypatch.setattr(run_service, "_enqueue_monitor_by_ids", reattached)
    run_service.resume_workload_monitors()
    assert reattached.called
    # Normalize UUIDs (raw SQL returns without dashes in SQLite)
    called_run_id = reattached.call_args[0][0].replace("-", "")
    expected_run_id = rid.replace("-", "")
    assert called_run_id == expected_run_id
