"""Deployment contracts at the host-agent, database and progress boundaries."""

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import deploy_service as deploy


@pytest.fixture
def session(monkeypatch):
    db = Mock()
    monkeypatch.setattr("app.core.database.SessionLocal", lambda: db)
    return db


@pytest.mark.parametrize(
    "output,expected",
    [
        (["Downloading: 1.0 GB", "Downloading: 2.5 GB"], 2.5),
        (["Downloading: invalid GB"], 0),
        (["starting"], 0),
        ([], 0),
    ],
)
def test_download_progress_parses_latest_measurement(monkeypatch, output, expected):
    monkeypatch.setattr(
        "app.services.troshkad_client.poll_job", lambda *_: {"output": output}
    )
    assert deploy._get_download_progress_gb(Mock(), "job") == expected


def test_download_progress_tolerates_agent_outage(monkeypatch):
    monkeypatch.setattr(
        "app.services.troshkad_client.poll_job",
        Mock(side_effect=deploy.TroshkadError("offline")),
    )
    assert deploy._get_download_progress_gb(Mock(), "job") == 0


@pytest.mark.parametrize("status", ["completed", "failed", "running"])
def test_download_poll_marks_terminal_jobs_and_shared_cache(monkeypatch, status):
    poll = Mock(return_value={"status": status, "result": {"error": "failure"}})
    monkeypatch.setattr("app.services.troshkad_client.poll_job", poll)
    ready, error = Mock(), Mock()
    monkeypatch.setattr(deploy, "_mark_shared_cache_ready", ready)
    monkeypatch.setattr(deploy, "_mark_shared_cache_error", error)
    completed, failed = set(), set()
    job = {"job_id": "job", "name": "image", "item_id": "item"}
    db, pool = Mock(), SimpleNamespace(id="pool")
    deploy._poll_single_download_job(job, Mock(), completed, failed, True, db, pool)
    assert completed == ({"job"} if status == "completed" else set())
    assert failed == ({"job"} if status == "failed" else set())
    assert ready.called is (status == "completed")
    assert error.called is (status == "failed")
    if status != "running":
        deploy._poll_single_download_job(job, Mock(), completed, failed, True, db, pool)
        assert poll.call_count == 1


@pytest.mark.parametrize(
    "job,expected",
    [
        ({"status": "completed", "result": {"stdout": "ok"}}, "ok"),
        ({"status": "completed", "result": None}, ""),
        ({"status": "failed"}, ""),
    ],
)
def test_ops_exec_only_returns_completed_stdout(monkeypatch, job, expected):
    start, wait = Mock(return_value="job"), Mock(return_value=job)
    monkeypatch.setattr(deploy, "start_job", start)
    monkeypatch.setattr(deploy, "wait_for_job", wait)
    host = SimpleNamespace(host_type="libvirt")
    assert (
        deploy._ops_pod_exec(host, "project", "ops", ["echo", "ok"], timeout=42)
        == expected
    )
    assert start.call_args.args[2] == {
        "container_name": "ops",
        "command": ["echo", "ok"],
    }
    wait.assert_called_once_with(host, "job", timeout=42)


def test_ops_exec_agent_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        deploy, "start_job", Mock(side_effect=deploy.TroshkadError("offline"))
    )
    assert (
        deploy._ops_pod_exec(SimpleNamespace(host_type="libvirt"), "p", "ops", ["true"])
        == ""
    )


@pytest.mark.parametrize("has_context", [False, True])
def test_ops_kubevirt_routing_requires_context(monkeypatch, has_context):
    context = (Mock(), "ns", "pod") if has_context else None
    monkeypatch.setattr(deploy, "_kubevirt_ops_pod_ctx", lambda *_: context)
    execute, cat = Mock(return_value="stdout"), Mock(return_value="contents")
    monkeypatch.setattr(deploy, "_ops_pod_exec_kubevirt", execute)
    monkeypatch.setattr(deploy, "_exec_ops_pod_cat_kubevirt", cat)
    host = SimpleNamespace(host_type="kubevirt-cluster")
    assert deploy._ops_pod_exec(host, "p", "ops", ["true"]) == (
        "stdout" if has_context else ""
    )
    assert deploy._ops_pod_cat(host, "p", "ops", "/file") == (
        "contents" if has_context else ""
    )
    assert execute.called is has_context
    assert cat.called is has_context


@pytest.mark.parametrize(
    "check,expected", [("OK\n", True), ("MISSING\n", False), ("", False)]
)
def test_ops_file_write_requires_nonempty_verification(monkeypatch, check, expected):
    execute = Mock(side_effect=["", check])
    monkeypatch.setattr(deploy, "_ops_pod_exec", execute)
    payload = b"kubeconfig:\n  token: fake\n"
    assert (
        deploy._ops_pod_write_file(Mock(), "p", "ops", "/work/config", payload)
        is expected
    )
    assert base64.b64encode(payload).decode() in execute.call_args_list[0].args[3][2]
    assert "[ -s /work/config ]" in execute.call_args_list[1].args[3][2]


@pytest.mark.parametrize(
    "live,expected",
    [({"c": "live"}, "live"), ({}, "cached"), (RuntimeError("offline"), "cached")],
)
def test_cluster_log_prefers_live_and_retains_cached_on_failure(
    monkeypatch, live, expected
):
    monkeypatch.setattr("app.core.redis.get_progress", lambda _: {"c": "cached"})
    reader = Mock()
    if isinstance(live, Exception):
        reader.side_effect = live
    else:
        reader.return_value = live
    monkeypatch.setattr(deploy, "read_ops_pod_install_log", reader)
    assert deploy._cluster_install_log_text(Mock(), "p", {}, "c") == expected
    assert deploy._cluster_install_log_text(None, "p", {}, "c") == "cached"


@pytest.mark.parametrize("running", [False, True])
def test_restart_truncates_only_requested_cluster_logs_when_pod_running(
    monkeypatch, running
):
    monkeypatch.setattr(deploy, "_ops_pod_running", lambda *_: running)
    execute = Mock()
    monkeypatch.setattr(deploy, "_ops_pod_exec", execute)
    deploy._truncate_ops_pod_cluster_install_logs(
        Mock(), "project", ["cluster-a", "cluster-b"]
    )
    assert execute.called is running
    if running:
        script = execute.call_args.args[3][2]
        assert "cluster-a/install.log" in script
        assert "cluster-b/install.log" in script
        assert len(script.splitlines()) == 2


def test_restart_log_truncation_is_best_effort(monkeypatch):
    execute = Mock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(deploy, "_ops_pod_running", lambda *_: True)
    monkeypatch.setattr(deploy, "_ops_pod_exec", execute)
    deploy._truncate_ops_pod_cluster_install_logs(Mock(), "project", [])
    deploy._truncate_ops_pod_cluster_install_logs(None, "project", ["a"])
    execute.assert_not_called()
    deploy._truncate_ops_pod_cluster_install_logs(Mock(), "project", ["a"])
    execute.assert_called_once()


@pytest.mark.parametrize("present", [False, True])
def test_deploy_start_epoch_reads_persisted_timestamp_and_closes_session(
    session, present
):
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    session.query.return_value.filter_by.return_value.first.return_value = (
        SimpleNamespace(deploy_started_at=ts) if present else None
    )
    assert deploy._project_deploy_start_epoch("p") == (
        ts.timestamp() if present else None
    )
    session.close.assert_called_once()


def test_detached_host_copy_loads_mapped_columns_before_session_close(session):
    from app.models.host import Host

    host = Host(id="host", host_type="libvirt")
    session.query.return_value.filter_by.return_value.first.return_value = host
    assert deploy._detached_host_copy("host") is host
    session.expunge.assert_called_once_with(host)
    session.close.assert_called_once()


def test_detached_host_copy_missing_and_database_failure_close_session(session):
    session.query.return_value.filter_by.return_value.first.return_value = None
    assert deploy._detached_host_copy("missing") is None
    session.expunge.assert_not_called()
    session.query.side_effect = RuntimeError("db unavailable")
    with pytest.raises(RuntimeError):
        deploy._detached_host_copy("missing")
    assert session.close.call_count == 2


@pytest.mark.parametrize("stored", [True, False])
def test_pattern_backing_uses_stored_format_and_original_disk_id(session, stored):
    session.query.return_value.filter_by.return_value.first.return_value = (
        SimpleNamespace(source_disk_id="original", s3_key="snapshots/original.qcow2")
        if stored
        else None
    )
    disk = {
        "source": "pattern",
        "patternId": "pattern",
        "patternDiskId": "record",
        "format": "raw",
    }
    path = deploy._resolve_disk_backing(disk)
    assert ("original.qcow2" if stored else "record.raw") in path
    session.close.assert_called_once()


@pytest.mark.parametrize("stored", [True, False])
def test_snapshot_backing_uses_first_boot_disk_and_handles_empty_snapshot(
    session, stored
):
    session.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = (
        [SimpleNamespace(s3_key="snapshots/boot.qcow2")] if stored else []
    )
    path = deploy._resolve_disk_backing(
        {"source": "snapshot", "snapshotItemId": "snap", "format": "qcow2"}
    )
    if stored:
        assert "boot.qcow2" in path
    else:
        assert path is None
    session.close.assert_called_once()


def test_recert_result_updates_only_target_and_preserves_missing_outputs():
    topology = {
        "nodes": [
            {"id": "other", "data": {}},
            {"id": "target", "data": {"ocpKubeconfig": "original"}},
        ]
    }
    deploy._apply_recert_results(topology, "target", "requested", "generated", None)
    assert topology["nodes"][1]["data"] == {
        "ocpKubeconfig": "original",
        "ocpKubeadminPassword": "generated",
    }
    deploy._apply_recert_results(topology, "target", "", "ignored", "new")
    assert topology["nodes"][1]["data"]["ocpKubeconfig"] == "new"
    assert topology["nodes"][0]["data"] == {}


@pytest.mark.parametrize(
    "password", ["", "short", "a-long-password-over-23-characters"]
)
def test_recert_password_generation_produces_valid_bcrypt_hash(password):
    import bcrypt

    params, resolved = deploy._build_recert_params(
        {"node_id": "node", "name": "control-plane"}, "/disk", "/bastion", "p", password
    )
    assert params["bastion_disk"] == "/bastion"
    assert params["extend_expiration"] is True
    if password:
        assert len(resolved) >= 23
        assert bcrypt.checkpw(
            resolved.encode(), params["kubeadmin_password_hash"].encode()
        )
        if len(password) >= 23:
            assert resolved == password
    else:
        assert resolved == ""
        assert "kubeadmin_password_hash" not in params


def test_restart_preparation_resets_only_target_cluster_and_copies_snapshots(
    monkeypatch,
):
    from app.models.project import Project

    original = {
        "clusters": [
            {"id": "a", "ocpInstallStatus": "error", "ocpInstallElapsed": 12},
            {"id": "b", "ocpInstallStatus": "ready"},
        ],
        "nodes": [
            {
                "id": "vm",
                "type": "vmNode",
                "data": {
                    "clusterId": "a",
                    "ocpKubeconfig": "old",
                    "ocpKubeadminPassword": "old",
                },
            }
        ],
    }
    project = Project(
        id="p",
        topology=original,
        deployed_topology=original,
        ocp_status="error",
        ocp_status_detail="failed",
        ocp_install_elapsed=12,
    )
    monkeypatch.setattr(deploy._time, "time", lambda: 1234)
    marker, clear = Mock(), Mock()
    monkeypatch.setattr(deploy, "set_progress", marker)
    monkeypatch.setattr(deploy, "clear_ops_pod_log_cache_keys", clear)
    db = Mock()
    assert deploy._prepare_ocp_install_restart(db, project, "a") == 1234
    marker.assert_called_once_with("ocp-restart:p:a", {"at": 1234}, ttl=3600)
    clear.assert_called_once_with("p", ["a"])
    assert project.topology["clusters"][0] == {
        "id": "a",
        "ocpInstallStatus": "monitoring",
        "ocpInstallStartedAt": 1234,
    }
    assert project.deployed_topology == project.topology
    assert project.deployed_topology is not project.topology
    assert project.topology["clusters"][1]["ocpInstallStatus"] == "ready"
    assert project.topology["nodes"][0]["data"] == {"clusterId": "a"}
    assert original["clusters"][0]["ocpInstallStatus"] == "error"
    assert project.ocp_status == "monitoring"
    assert project.ocp_install_elapsed is None
    assert project.ocp_status_detail is None
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("missing", 1000),
        ("stamped", 100),
        ("fresh", 50),
        ("recert", 200),
        ("recert-no-monitor", 1000),
    ],
)
def test_ops_elapsed_base_tracks_restart_or_original_deploy(session, mode, expected):
    cluster = {"id": "c", "type": "ocp"}
    topology = {"clusters": [cluster], "nodes": []}
    if mode == "stamped":
        cluster["ocpInstallStartedAt"] = 100
    if mode.startswith("recert"):
        topology["nodes"] = [
            {
                "id": "vm",
                "type": "vmNode",
                "data": {
                    "clusterId": "c",
                    "clusterRole": "control-plane",
                    "ocpKubeconfig": "config",
                },
            }
        ]
    project = SimpleNamespace(
        topology=topology,
        deployed_topology=None,
        deploy_started_at=datetime.fromtimestamp(50, UTC),
        ocp_monitor_started_at=None
        if mode.endswith("no-monitor")
        else datetime.fromtimestamp(200, UTC),
    )
    session.query.return_value.filter_by.return_value.first.return_value = (
        None if mode == "missing" else project
    )
    assert deploy._ops_pod_elapsed_base("p", [cluster], 1000) == expected
    assert session.close.called


@pytest.fixture
def recert_topology():
    return {
        "nodes": [
            {"id": "vm", "type": "vmNode", "data": {"name": "control-plane"}},
            {"id": "disk", "type": "storageNode", "data": {"format": "qcow2"}},
        ],
        "edges": [{"source": "vm", "sourceHandle": "dp-ctrl", "target": "disk"}],
    }


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "unavailable"])
def test_recert_execution_requires_success_for_expired_pattern(
    monkeypatch, recert_topology, required, outcome
):
    start = Mock(return_value="job")
    if outcome == "unavailable":
        start.side_effect = ValueError("unavailable")
    monkeypatch.setattr(deploy, "start_job", start)
    wait = Mock(
        return_value={
            "status": outcome,
            "result": {"kubeconfig": "new-config", "error": "certificate failure"},
        }
    )
    monkeypatch.setattr(deploy, "wait_for_job", wait)
    args = (
        Mock(),
        "project",
        {"node_id": "vm", "name": "control-plane"},
        recert_topology,
        None,
        None,
        required,
        "",
    )
    if required and outcome != "completed":
        with pytest.raises(RuntimeError, match="Recert required"):
            deploy._run_recert_for_vm(*args)
    else:
        assert deploy._run_recert_for_vm(*args) is (outcome == "completed")
    if outcome == "completed":
        assert recert_topology["nodes"][0]["data"]["ocpKubeconfig"] == "new-config"
        assert start.call_args.args[1] == "/vms/recert"
        assert start.call_args.args[2]["disk"].endswith("disk.qcow2")
    else:
        assert "ocpKubeconfig" not in recert_topology["nodes"][0]["data"]


def test_recert_skips_vm_without_boot_disk(monkeypatch):
    start = Mock()
    monkeypatch.setattr(deploy, "start_job", start)
    assert (
        deploy._run_recert_for_vm(
            Mock(), "p", {"node_id": "vm"}, {}, None, None, True, ""
        )
        is False
    )
    start.assert_not_called()
