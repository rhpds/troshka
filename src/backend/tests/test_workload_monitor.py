from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.host import Host
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


def test_read_runner_logs_troshkad_returns_stdout(monkeypatch):
    """Mocked transport: troshkad containers/exec cat returns logfile contents."""
    fake_job_result = {
        "status": "completed",
        "result": {"stdout": "TASK [x] ***\nok: [localhost]"},
    }

    start_job_mock = MagicMock(return_value="job-123")
    wait_for_job_mock = MagicMock(return_value=fake_job_result)

    monkeypatch.setattr("app.services.troshkad_client.start_job", start_job_mock)
    monkeypatch.setattr("app.services.troshkad_client.wait_for_job", wait_for_job_mock)

    host = SimpleNamespace(host_type="shared")
    logs = run_service._read_runner_logs_troshkad(host)

    assert logs == "TASK [x] ***\nok: [localhost]"
    assert start_job_mock.call_args[0][1] == "/containers/exec"
    assert start_job_mock.call_args[0][2]["container_name"] == "runner"
    assert start_job_mock.call_args[0][2]["command"] == ["cat", "/workdir/run.log"]


def test_is_runner_pod_running_troshkad_checks_state(monkeypatch):
    """Mocked transport: troshkad get_all_container_states returns running."""
    fake_states = {"runner": {"state": "running"}}
    get_states_mock = MagicMock(return_value=fake_states)

    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )

    host = SimpleNamespace(host_type="shared")
    assert run_service._runner_pod_running_troshkad(host) is True


def test_check_exit_status_troshkad_reads_exit_code(monkeypatch):
    """Mocked transport: troshkad get_all_container_states returns exit_code."""
    fake_states = {"runner": {"state": "exited", "exit_code": 0}}
    get_states_mock = MagicMock(return_value=fake_states)

    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )

    host = SimpleNamespace(host_type="shared")
    status = run_service._check_exit_status_troshkad(host, "")

    assert status == "succeeded"
