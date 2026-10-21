"""Failure contracts for workload monitors and provider cleanup boundaries."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from app.core import redis as redis_module
from app.services import troshkad_client
from app.services.workloads import run_service as service


@pytest.fixture
def session(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    monkeypatch.setattr(service, "SessionLocal", lambda: db)
    return db


@pytest.fixture
def kube_context(monkeypatch):
    api = MagicMock()
    monkeypatch.setattr(
        service,
        "_runner_pod_kubevirt_ctx",
        lambda *_: (api, "project-ns", "workload-runner"),
    )
    return api


@pytest.mark.parametrize(
    "available,result,error,expected",
    [
        (False, None, False, True),
        (True, True, False, True),
        (True, False, False, False),
        (True, None, True, True),
    ],
)
def test_monitor_lock_fallback_and_exclusion(
    monkeypatch, available, result, error, expected
):
    client = MagicMock()
    client.set.return_value = result
    if error:
        client.set.side_effect = ConnectionError("redis down")
        client.delete.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(redis_module, "is_redis_available", lambda: available)
    monkeypatch.setattr(redis_module, "get_redis", lambda: client)
    assert service._acquire_workload_monitor_lock("run") is expected
    service._refresh_workload_monitor_lock("run")
    service._release_workload_monitor_lock("run")
    if available:
        assert client.set.call_args_list[0].kwargs == {"nx": True, "ex": 120}
        client.delete.assert_called_once_with("workload-monitor:run")
    else:
        client.set.assert_not_called()


@pytest.mark.parametrize("failure", [None, 404, 503, "transport"])
def test_kubevirt_cleanup_always_attempts_secret(kube_context, failure):
    if failure:
        kube_context.delete_namespaced_pod.side_effect = (
            ApiException(status=failure)
            if isinstance(failure, int)
            else RuntimeError(failure)
        )
    kube_context.delete_namespaced_secret.side_effect = RuntimeError("already absent")
    service._destroy_runner_pod_kubevirt(NS(), "run")
    kube_context.delete_namespaced_pod.assert_called_once_with(
        name="workload-runner", namespace="project-ns", grace_period_seconds=0
    )
    kube_context.delete_namespaced_secret.assert_called_once_with(
        name="workload-runner-config", namespace="project-ns"
    )


@pytest.mark.parametrize("failure", [False, True])
def test_troshkad_cleanup_is_best_effort(monkeypatch, failure):
    start = MagicMock(return_value="job")
    wait = MagicMock()
    if failure:
        wait.side_effect = troshkad_client.TroshkadError("offline")
    monkeypatch.setattr(troshkad_client, "start_job", start)
    monkeypatch.setattr(troshkad_client, "wait_for_job", wait)
    host = NS()
    service._destroy_runner_pod_troshkad(host, "project-123")
    assert start.call_args.args[1] == "/pods/destroy"
    assert start.call_args.args[2]["project_id"] == "project-123"
    wait.assert_called_once_with(host, "job", timeout=60)


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("Running", True),
        ("Pending", True),
        ("Succeeded", False),
        ("Failed", False),
        (None, True),
    ],
)
def test_kubevirt_liveness_only_terminal_phases_stop_monitor(
    kube_context, phase, expected
):
    kube_context.read_namespaced_pod.return_value = NS(status=NS(phase=phase))
    assert service._runner_pod_running_kubevirt(NS(), "run") is expected


@pytest.mark.parametrize(
    "failure,expected", [(404, False), (503, True), ("transport", True)]
)
def test_kubevirt_liveness_tolerates_uncertainty(kube_context, failure, expected):
    kube_context.read_namespaced_pod.side_effect = (
        ApiException(status=failure)
        if isinstance(failure, int)
        else RuntimeError(failure)
    )
    assert service._runner_pod_running_kubevirt(NS(), "run") is expected


@pytest.mark.parametrize(
    "code,expected", [(0, "succeeded"), (1, "error"), (137, "error")]
)
def test_kubevirt_exit_code_overrides_misleading_log(kube_context, code, expected):
    kube_context.read_namespaced_pod.return_value = NS(
        status=NS(
            container_statuses=[
                NS(state=NS(terminated=None)),
                NS(state=NS(terminated=NS(exit_code=code))),
            ]
        )
    )
    assert (
        service._check_exit_status_kubevirt(NS(), "run", "PLAY RECAP failed=0")
        == expected
    )


@pytest.mark.parametrize("failure", [False, True])
def test_kubevirt_exit_falls_back_to_failure_log(kube_context, failure):
    kube_context.read_namespaced_pod.return_value = NS(
        status=NS(container_statuses=[NS(state=NS(terminated=NS(exit_code=None)))])
    )
    if failure:
        kube_context.read_namespaced_pod.side_effect = RuntimeError("disconnected")
    assert (
        service._check_exit_status_kubevirt(NS(), "run", "fatal: unreachable")
        == "error"
    )


@pytest.mark.parametrize(
    "api_result,exec_result,expected",
    [
        ("finished logs", "unused", "finished logs"),
        ("", "TASK [recover]", "TASK [recover]"),
        (None, "cat: /workdir/run.log: No such file", ""),
        (None, None, ""),
    ],
)
def test_kubevirt_logs_api_then_exec_fallback(
    monkeypatch, kube_context, api_result, exec_result, expected
):
    import kubernetes.stream

    kube_context.read_namespaced_pod_log.return_value = api_result
    stream = MagicMock(return_value=exec_result)
    monkeypatch.setattr(kubernetes.stream, "stream", stream)
    assert service._read_runner_logs_kubevirt(NS(), "run") == expected
    assert stream.call_count == (0 if api_result else 1)


def test_kubevirt_log_transport_failures_return_empty(monkeypatch, kube_context):
    import kubernetes.stream

    kube_context.read_namespaced_pod_log.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(
        kubernetes.stream, "stream", MagicMock(side_effect=RuntimeError("pod gone"))
    )
    assert service._read_runner_logs_kubevirt(NS(), "run") == ""


@pytest.mark.parametrize("row", [None, NS(project_id=None), NS(project_id="project")])
def test_progress_publish_routes_only_existing_project(monkeypatch, session, row):
    from app.services import ws_pubsub

    session.get.return_value = row
    persist = MagicMock()
    notify = MagicMock()
    monkeypatch.setattr(redis_module, "set_progress", persist)
    monkeypatch.setattr(ws_pubsub, "notify_project", notify)
    service._publish_workload_progress("run", {"step": "install"})
    persist.assert_called_once_with(
        "workload:run", {"run_id": "run", "step": "install"}
    )
    assert notify.call_count == int(row is not None and bool(row.project_id))
    session.close.assert_called_once()


@pytest.mark.parametrize("allowed", [False, True])
def test_monitor_enqueues_once_under_lock(monkeypatch, allowed):
    monkeypatch.setattr(service, "_acquire_workload_monitor_lock", lambda _: allowed)
    enqueue = MagicMock()
    monkeypatch.setattr(service, "enqueue_job", enqueue)
    service._start_workload_monitor(NS(id="host"), "run")
    service._enqueue_monitor_by_ids("run2", "host")
    assert enqueue.call_count == (2 if allowed else 0)
    if allowed:
        assert enqueue.call_args.args[1:] == ("run2", "host")
        assert enqueue.call_args.kwargs == {"job_timeout": 14400}


def test_resume_continues_after_one_failed_enqueue(monkeypatch, session):
    session.execute.return_value = [("run1", "host1"), ("run2", "host2")]
    enqueue = MagicMock(side_effect=[RuntimeError("queue unavailable"), None])
    monkeypatch.setattr(service, "_enqueue_monitor_by_ids", enqueue)
    service.resume_workload_monitors()
    assert enqueue.call_count == 2
    enqueue.assert_called_with("run2", "host2")
    session.close.assert_called_once()


def test_resume_closes_session_on_query_failure(session):
    session.execute.side_effect = RuntimeError("database down")
    service.resume_workload_monitors()
    session.close.assert_called_once()


@pytest.mark.parametrize("terminal", [False, True])
def test_log_checkpoint_preserves_terminal_record(session, terminal):
    row = NS(status="cancelled" if terminal else "running", log_ref="existing")
    session.get.return_value = row
    service._persist_workload_log("run", "x" * (service._LOG_TAIL_BYTES + 10))
    assert row.log_ref == ("existing" if terminal else "x" * service._LOG_TAIL_BYTES)
    assert session.commit.call_count == (0 if terminal else 1)


@pytest.mark.parametrize("provider", ["kubevirt-cluster", "libvirt"])
def test_cancel_preserves_logs_and_dispatches_cleanup(monkeypatch, session, provider):
    host = NS(host_type=provider)
    project = NS(id="project", host_id="host")
    session.get.return_value = project
    session.query.return_value.filter_by.return_value.first.return_value = host
    row = NS(id="run", project_id="project", status="running", log_ref=None)
    monkeypatch.setattr(
        service, "_read_runner_pod_logs", lambda *_: "last useful output"
    )
    release = MagicMock()
    kube = MagicMock()
    troop = MagicMock()
    monkeypatch.setattr(service, "_release_workload_monitor_lock", release)
    monkeypatch.setattr(service, "_destroy_runner_pod_kubevirt", kube)
    monkeypatch.setattr(service, "_destroy_runner_pod_troshkad", troop)
    assert service.cancel_workload_run(session, row) is row
    assert row.status == "cancelled"
    assert row.log_ref == "last useful output"
    assert row.error == "Cancelled by user"
    release.assert_called_once_with("run")
    assert kube.call_count == int(provider == "kubevirt-cluster")
    assert troop.call_count == int(provider != "kubevirt-cluster")


@pytest.mark.parametrize("project", [None, NS(id="project", host_id=None)])
def test_cleanup_ignores_deleted_or_unassigned_project(session, project):
    session.get.return_value = project
    service._destroy_runner_pod_best_effort(session, NS(id="run", project_id="project"))
    session.query.assert_not_called()


def test_cleanup_handles_host_lookup_failure(session):
    session.get.side_effect = RuntimeError("database down")
    service._destroy_runner_pod_best_effort(session, NS(id="run", project_id="project"))
    session.get.assert_called_once()


@pytest.mark.parametrize("missing", ["project", "host"])
def test_job_missing_records_fail_and_close_session(session, missing):
    row = NS(project_id="project", status="pending")
    project = NS(id="project", host_id=None)
    session.get.side_effect = [row, None if missing == "project" else project, row]
    with pytest.raises(RuntimeError):
        service.run_workload_job("run")
    assert row.status == "error"
    assert "Project" in row.error
    session.close.assert_called_once()


@pytest.mark.parametrize("found", [False, True])
def test_detached_host_is_usable_after_session_close(monkeypatch, session, found):
    from sqlalchemy import inspect

    from app.models.host import Host

    host = Host(id="host", host_type="libvirt") if found else None
    session.query.return_value.filter_by.return_value.first.return_value = host
    assert service._detached_host_copy("host") is host
    if found:
        session.expunge.assert_called_once_with(host)
        assert host.host_type == "libvirt"
        assert "host_type" in inspect(host).mapper.column_attrs
    session.close.assert_called_once()


@pytest.mark.parametrize("found", [False, True])
def test_kubevirt_context_resolves_project_namespace(monkeypatch, session, found):
    from app.core import database
    from app.services.providers import kubevirt

    monkeypatch.setattr(database, "SessionLocal", lambda: session)
    session.get.return_value = NS(project_id="project")
    provider = NS(id="provider") if found else None
    session.query.return_value.filter_by.return_value.first.return_value = provider
    core = MagicMock()
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", lambda _: (None, core, None))
    monkeypatch.setattr(kubevirt, "_project_ns", lambda _, project: "ns-" + project)
    result = service._runner_pod_kubevirt_ctx(NS(provider_id="provider"), "run")
    assert result == ((core, "ns-project", "workload-runner") if found else None)
    assert session.close.call_count == 2


def test_missing_run_has_no_provider_context(session):
    session.get.return_value = None
    assert service._runner_pod_kubevirt_ctx(NS(provider_id="provider"), "run") is None
    session.query.assert_not_called()


@pytest.mark.parametrize("mode", ["missing_host", "cancelled", "timeout"])
def test_monitor_exits_and_releases_lock(monkeypatch, session, mode):
    import time

    host = None if mode == "missing_host" else NS(id="host")
    monkeypatch.setattr(service, "_detached_host_copy", lambda _: host)
    session.get.return_value = NS(status="cancelled", log_ref="old")
    release = MagicMock()
    finalize = MagicMock()
    monkeypatch.setattr(service, "_release_workload_monitor_lock", release)
    monkeypatch.setattr(service, "_refresh_workload_monitor_lock", MagicMock())
    monkeypatch.setattr(service, "_finalize_workload_run", finalize)
    values = iter([0, 8000] if mode == "timeout" else [0, 1])
    monkeypatch.setattr(time, "time", lambda: next(values, 9000))
    service.monitor_workload_run("run", "host")
    release.assert_called_once_with("run")
    if mode == "timeout":
        finalize.assert_called_once_with(
            "run", "timeout", "\nMonitor timeout after 2 hours"
        )
    else:
        finalize.assert_not_called()


@pytest.mark.parametrize(
    "role,chain_failure",
    [(None, False), ("ns.collection.role", False), ("ns.collection.role", True)],
)
def test_success_persists_before_template_continuation(
    monkeypatch, session, role, chain_failure
):
    from app.services.workloads import template_workloads

    row = NS(
        status="running", log_ref="saved log", project_id="project", role_fqcn=role
    )
    session.get.return_value = row
    stamp = MagicMock()
    chain = MagicMock()
    if chain_failure:
        chain.side_effect = RuntimeError("queue down")
    monkeypatch.setattr(template_workloads, "stamp_run_once_role_done", stamp)
    monkeypatch.setattr(template_workloads, "maybe_enqueue_template_workloads", chain)
    service._finalize_workload_run("run", "succeeded", "")
    assert row.status == "succeeded"
    assert row.log_ref == "saved log"
    session.commit.assert_called_once()
    assert stamp.call_count == int(role is not None)
    chain.assert_called_once_with("project")


@pytest.mark.parametrize("provider_type", ["libvirt", "kubevirt-cluster"])
def test_runner_dispatch_uses_persisted_project(monkeypatch, session, provider_type):
    session.get.return_value = NS(project_id="project-123")
    host = NS(host_type=provider_type)
    logs = MagicMock(return_value="finished")
    running = MagicMock(return_value=False)
    status = MagicMock(return_value="error")
    suffix = "kubevirt" if provider_type == "kubevirt-cluster" else "troshkad"
    monkeypatch.setattr(service, "_read_runner_logs_" + suffix, logs)
    monkeypatch.setattr(service, "_runner_pod_running_" + suffix, running)
    monkeypatch.setattr(service, "_check_exit_status_" + suffix, status)
    assert service._read_runner_pod_logs(host, "run") == "finished"
    assert service._is_runner_pod_running(host, "run") is False
    assert (
        service._check_runner_pod_exit_status(host, "run", "fatal: failure") == "error"
    )
    if suffix == "troshkad":
        assert "project-" in logs.call_args.args[1]
    else:
        logs.assert_called_once_with(host, "run")


@pytest.mark.parametrize("result", [None, {}, {"runner": {"state": "created"}}])
def test_transient_troshkad_state_keeps_monitor_alive(monkeypatch, result):
    monkeypatch.setattr(troshkad_client, "get_all_container_states", lambda _: result)
    assert service._runner_pod_running_troshkad(NS(), "runner") is True
    assert service._check_exit_status_troshkad(NS(), "fatal: gone", "runner") == "error"


@pytest.mark.parametrize("failed", [False, True])
def test_troshkad_log_read_failure_is_empty(monkeypatch, failed):
    monkeypatch.setattr(troshkad_client, "start_job", MagicMock(return_value="job"))
    wait = MagicMock(return_value={"status": "error"})
    if failed:
        wait.side_effect = troshkad_client.TroshkadError("offline")
    monkeypatch.setattr(troshkad_client, "wait_for_job", wait)
    assert service._read_runner_logs_troshkad(NS(), "runner") == ""


@pytest.mark.parametrize("exists", [False, True])
def test_live_ceph_credentials_merge_without_mutating_topology(
    monkeypatch, session, exists
):
    from kubernetes import client

    from app.services import project_ceph
    from app.services.providers import kubevirt

    session.get.return_value = NS(id="provider")
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", lambda _: (None, None, "api"))
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *_: "namespace")
    monkeypatch.setattr(client, "CustomObjectsApi", lambda _: "custom")
    cr = (
        {"status": {"phase": "Ready"}, "spec": {"labIp": "10.1.2.4"}}
        if exists
        else None
    )
    monkeypatch.setattr(project_ceph, "fetch_troshka_ceph_cr", lambda *_: cr)
    topo = {"nodes": []}
    result, variables = service._merge_kubevirt_ceph_extra_vars(
        session,
        NS(host_type="kubevirt-cluster", provider_id="provider"),
        NS(id="project"),
        topo,
        {"user": "kept"},
    )
    assert topo == {"nodes": []}
    assert variables["user"] == "kept"
    if exists:
        assert variables["troshka_ceph_mon_host"] == "10.1.2.4"
        assert variables["secretNamespace"] == "namespace"
        assert result["projectCeph"]["phase"] == "Ready"
    else:
        assert variables == {"user": "kept"}


@pytest.mark.parametrize("operation", ["destroy", "logs", "running", "exit"])
def test_missing_provider_context_is_conservative(monkeypatch, operation):
    monkeypatch.setattr(service, "_runner_pod_kubevirt_ctx", lambda *_: None)
    host = NS()
    if operation == "destroy":
        assert service._destroy_runner_pod_kubevirt(host, "run") is None
    elif operation == "logs":
        assert service._read_runner_logs_kubevirt(host, "run") == ""
    elif operation == "running":
        assert service._runner_pod_running_kubevirt(host, "run") is True
    else:
        assert (
            service._check_exit_status_kubevirt(host, "run", "PLAY RECAP failed=0")
            == "succeeded"
        )


def test_host_lookup_missing_row_is_explicit(session):
    project = NS(id="project", host_id="deleted")
    session.query.return_value.filter_by.return_value.first.return_value = None
    with pytest.raises(RuntimeError, match="Host deleted not found"):
        service._host_for_project(session, project)


def test_failed_run_lookup_still_closes_session(session):
    session.get.return_value = None
    with pytest.raises(RuntimeError, match="WorkloadRun absent not found"):
        service.run_workload_job("absent")
    session.close.assert_called_once()


def test_cancel_ignores_log_failure_but_still_cleans_up(monkeypatch, session):
    row = NS(id="run", project_id="project", status="running", log_ref=None)
    session.get.side_effect = RuntimeError("project removed")
    monkeypatch.setattr(service, "_release_workload_monitor_lock", MagicMock())
    assert service.cancel_workload_run(session, row).status == "cancelled"
    assert row.log_ref is None
    session.commit.assert_called_once()


def test_ad_hoc_missing_role_is_rejected(session):
    run = NS(kind="role", role_fqcn=None)
    with pytest.raises(RuntimeError, match="requires role_fqcn"):
        service._resolve_item(session, run)


def test_absent_and_terminal_rows_are_not_overwritten(session):
    session.get.return_value = None
    service.mark_workload_run_interrupted("absent")
    service._persist_workload_log("absent", "")
    row = NS(status="cancelled", log_ref="preserved")
    session.get.return_value = row
    service._finalize_workload_run("run", "error", "late error")
    assert row.log_ref == "preserved"
    assert row.status == "cancelled"
    session.commit.assert_not_called()


@pytest.mark.parametrize("created", [None, "invalid"])
def test_reconcile_ignores_missing_creation_timestamp(session, created):
    row = NS(status="pending", created_at=created)
    assert service.reconcile_stale_workload_run(session, row) is row
    session.commit.assert_not_called()


def test_retry_rejects_already_started_pending_run(session):
    run = NS(status="pending", started_at=object())
    with pytest.raises(ValueError, match="already started"):
        service.retry_workload_run(session, run)


def test_cancel_rejects_unknown_nonterminal_state(session):
    run = NS(status="invalid")
    with pytest.raises(ValueError, match="Cannot cancel"):
        service.cancel_workload_run(session, run)


def test_orphan_run_keeps_persisted_log(session):
    run = NS(id="run", status="running", project_id="missing", log_ref="saved")
    session.get.return_value = None
    assert service.get_workload_log(session, run) == "saved"
    run.project_id = None
    service._destroy_runner_pod_best_effort(session, run)
    session.query.assert_not_called()


def test_error_finalization_bounds_message_and_tail(session):
    row = NS(status="running", log_ref=None, project_id="project", role_fqcn=None)
    session.get.return_value = row
    logs = "failure\n" * 40000
    service._finalize_workload_run("run", "error", logs)
    assert row.error == logs[:2000]
    assert row.log_ref == logs[-service._LOG_TAIL_BYTES :]
    assert row.ended_at is not None
