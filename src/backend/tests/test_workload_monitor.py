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


def test_read_runner_logs_troshkad_returns_logs(monkeypatch):
    """troshkad `containers/logs` (podman logs) — works on running AND exited."""
    fake_job_result = {
        "status": "completed",
        "result": {"logs": "TASK [x] ***\nok: [localhost]"},
    }

    start_job_mock = MagicMock(return_value="job-123")
    wait_for_job_mock = MagicMock(return_value=fake_job_result)

    monkeypatch.setattr("app.services.troshkad_client.start_job", start_job_mock)
    monkeypatch.setattr("app.services.troshkad_client.wait_for_job", wait_for_job_mock)

    host = SimpleNamespace(host_type="shared")
    # The monitor must target the FULL podman container name, not bare "runner".
    cname = "troshka-p1234567-workload-runner-runner"
    logs = run_service._read_runner_logs_troshkad(host, cname)

    assert logs == "TASK [x] ***\nok: [localhost]"
    # podman logs (containers/logs), NOT exec cat — exec can't reach an exited pod.
    assert start_job_mock.call_args[0][1] == "/containers/logs"
    assert start_job_mock.call_args[0][2]["container_name"] == cname


def test_read_runner_logs_kubevirt_uses_pod_log_api(monkeypatch):
    """k8s Pod logs API works after Succeeded/Failed; exec cat does not."""
    core = MagicMock()
    core.read_namespaced_pod_log.return_value = "PLAY RECAP\nok=3"
    monkeypatch.setattr(
        run_service,
        "_runner_pod_kubevirt_ctx",
        lambda _h, _r: (core, "troshka-abc", "workload-runner"),
    )
    host = SimpleNamespace(host_type="kubevirt-cluster")
    logs = run_service._read_runner_logs_kubevirt(host, "run-1")
    assert logs == "PLAY RECAP\nok=3"
    core.read_namespaced_pod_log.assert_called_once_with(
        name="workload-runner",
        namespace="troshka-abc",
        container="ops",
        tail_lines=20000,
    )


def test_coalesce_runner_logs_keeps_last_nonempty():
    assert (
        run_service._coalesce_runner_logs("", "prior PLAY RECAP") == "prior PLAY RECAP"
    )
    assert run_service._coalesce_runner_logs("new", "prior") == "new"
    assert run_service._coalesce_runner_logs("", "") == ""


def test_is_runner_pod_running_troshkad_checks_state(monkeypatch):
    """Mocked transport: troshkad get_all_container_states keyed by full name."""
    cname = "troshka-p1234567-workload-runner-runner"
    get_states_mock = MagicMock(return_value={cname: {"state": "running"}})

    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )

    host = SimpleNamespace(host_type="shared")
    assert run_service._runner_pod_running_troshkad(host, cname) is True


def test_is_runner_pod_running_troshkad_created_is_alive(monkeypatch):
    """A just-started container reports 'created' briefly — treat as alive."""
    cname = "troshka-p1234567-workload-runner-runner"
    get_states_mock = MagicMock(return_value={cname: {"state": "created"}})
    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )
    host = SimpleNamespace(host_type="shared")
    assert run_service._runner_pod_running_troshkad(host, cname) is True


def test_is_runner_pod_running_troshkad_exited_is_dead(monkeypatch):
    """An exited container is dead."""
    cname = "troshka-p1234567-workload-runner-runner"
    get_states_mock = MagicMock(return_value={cname: {"state": "exited"}})
    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )
    host = SimpleNamespace(host_type="shared")
    assert run_service._runner_pod_running_troshkad(host, cname) is False


def test_check_exit_status_troshkad_reads_exit_code(monkeypatch):
    """Mocked transport: troshkad get_all_container_states returns exit_code."""
    cname = "troshka-p1234567-workload-runner-runner"
    get_states_mock = MagicMock(
        return_value={cname: {"state": "exited", "exit_code": 0}}
    )

    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_container_states", get_states_mock
    )

    host = SimpleNamespace(host_type="shared")
    status = run_service._check_exit_status_troshkad(host, "", cname)

    assert status == "succeeded"
