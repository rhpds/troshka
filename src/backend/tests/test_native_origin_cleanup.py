"""Original writer evidence must survive handoff completion and cancellation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.schemas.quota import AccountingUnavailable
from tests.test_native_deployment_origin import (
    isolated_quota_database as origin_database,
)
from tests.test_native_deployment_origin import (
    origin_case as origin_fixture,
)
from tests.test_native_deployment_origin import (
    prepare,
    service,
)

isolated_quota_database = origin_database
origin_case = origin_fixture


def rq_invocation(monkeypatch, execute, execution_id="execution-one"):
    """Real RQ job stack/perform lifecycle; only Redis transport is synthetic."""
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    from redis import Redis
    from rq import SimpleWorker
    from rq.executions import Execution
    from rq.job import Job, JobStatus

    from app.workers.deploy_worker import NativeOriginWorkerMixin

    connection = MagicMock(spec=Redis)
    connection.connection_pool = SimpleNamespace(
        connection_kwargs={"host": "unit", "port": 6379, "db": 0}
    )
    status = {"value": b"started"}
    connection.hget.side_effect = lambda *args: status["value"]
    connection.pipeline.return_value.hset.side_effect = (
        lambda key, field, value: status.update(value=value.encode())
    )
    job = Job(id="original-job", connection=connection)
    job.origin = "default"
    job._status = JobStatus.STARTED
    monkeypatch.setattr(job, "_execute", execute)

    class TestWorker(NativeOriginWorkerMixin, SimpleWorker):
        pass

    worker = TestWorker(
        ["default"], connection=connection, name="worker-one", prepare_for_work=False
    )
    worker.execution = Execution(
        execution_id, job.id, connection, worker_name=worker.name
    )
    worker.death_penalty_class = lambda *a, **k: nullcontext()
    monkeypatch.setattr(worker, "prepare_job_execution", lambda *a: None)
    monkeypatch.setattr(worker, "handle_job_success", Mock())
    monkeypatch.setattr(worker, "handle_job_failure", Mock())
    monkeypatch.setattr(worker, "handle_exception", Mock())
    return job, worker, lambda: worker.perform_job(job, worker.queues[0])


def test_real_observer_executor_does_not_strand_producer_exit(origin_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_checkpoints as checkpoints
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    monkeypatch.setattr(checkpoints, "observe_project", lambda value: None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(checkpoints, "_executor", executor)

        def execute():
            with q.sessions() as db:
                writers.register_current(db, str(intent.id))
                db.commit()
            checkpoints.schedule(q.project_id).result(timeout=5)

        _, _, run = rq_invocation(monkeypatch, execute)
        assert run()
        assert any(t.is_alive() for t in executor._threads)
        with q.sessions() as db:
            assert db.query(NativeOriginWriterTerminal).count() == 1
            service().cancel(db, q.project_id)
            db.commit()
            assert writers.observe_terminal(db, str(intent.id))


@pytest.mark.parametrize("failed", [False, True])
def test_actual_rq_cancel_does_not_finish_running_execution(
    origin_case, monkeypatch, failed
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rq.job import JobStatus

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    entered, release = Event(), Event()

    def execute():
        with q.sessions() as db:
            service().begin_io(db, str(intent.id), "root")
            db.commit()
        entered.set()
        assert release.wait(timeout=8)
        if failed:
            raise RuntimeError("fully unwound producer failure")
        return "returned"

    job, worker, run = rq_invocation(monkeypatch, execute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert entered.wait(timeout=5)
        try:
            job.cancel()  # Installed RQ implementation; no work horse termination.
            assert job._status == JobStatus.CANCELED and not future.done()
            with q.sessions() as db:
                service().cancel(db, q.project_id)
                db.commit()
                rows = writers.registrations(db, str(intent.id))
                assert rows[0].payload["rq_execution_id"] == worker.execution.id
                assert db.query(NativeOriginWriterTerminal).count() == 0
                with pytest.raises(
                    AccountingUnavailable, match="completion|termination"
                ):
                    writers.observe_terminal(db, str(intent.id))
        finally:
            release.set()
        assert future.result(timeout=8) is (not failed)
    with q.sessions() as db:
        proofs = writers.observe_terminal(db, str(intent.id))
        assert len(proofs) == 1
        assert next(iter(proofs.values()))["outcome"] == (
            "failed" if failed else "returned"
        )


@pytest.mark.parametrize("ops_pod", [False, True])
def test_native_deploy_async_support_branch_holds_before_dispatch(
    origin_case, monkeypatch, ops_pod
):
    from app.core import database, redis
    from app.services import deploy_service
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    monkeypatch.setattr(deploy_service, "_should_use_ops_pod", lambda topology: ops_pod)
    dispatched = []

    def enqueue(*args, **kwargs):
        # Separate reader sees the durable hold before enqueue/Pod mutation.
        from app.models.native_origin_cleanup import NativeOriginWriterHold

        with q.sessions() as db:
            holds = db.query(NativeOriginWriterHold).all()
            assert holds and all(
                h.payload["reason"] == "native_ops_pod_registered_descendants_v1"
                for h in holds
            )
        dispatched.append(True)

    monkeypatch.setattr(redis, "enqueue_job", enqueue)
    monkeypatch.setattr(deploy_service, "_acquire_ops_monitor_lock", lambda *args: True)

    def dispatch(*args):
        deploy_service._start_ops_pod_install_monitor(
            SimpleNamespace(id=str(intent.host_id)), q.project_id, []
        )
        raise RuntimeError("support dispatch failed after hold")

    monkeypatch.setattr(deploy_service, "_deploy_ops_pod", dispatch)

    def execute():
        with q.sessions() as db:
            token = service().begin_io(db, str(intent.id), "support")
            service().finish_io(db, str(intent.id), token)
            db.commit()
            deploy_service._deploy_after_special_host(
                db,
                SimpleNamespace(host_type="kubevirt-cluster"),
                q.project_id,
                SimpleNamespace(state="running"),
                {"nodes": []},
                {},
            )

    # Retrying the same RQ job is a fresh exact execution; neither callback may
    # overwrite the first asynchronous-support or remote-operation liability.
    for execution_id in ("one", "two"):
        _, _, run = rq_invocation(monkeypatch, execute, execution_id)
        assert run() is (not ops_pod)
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        assert len(writers.registrations(db, str(intent.id))) == 2
        if ops_pod:
            with pytest.raises(AccountingUnavailable, match="support|descendant"):
                writers.observe_terminal(db, str(intent.id))
        else:
            assert len(writers.observe_terminal(db, str(intent.id))) == 2
    assert len(dispatched) == (2 if ops_pod else 0)


@pytest.mark.parametrize(
    "uncertainty",
    ["detached_thread", "missing_callback", "status_only", "completion_write_failed"],
)
def test_incomplete_execution_evidence_remains_hold(
    origin_case, monkeypatch, uncertainty
):
    from threading import Event, Thread

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_writers as writers
    from app.services.kubevirt_storage_enrollment import digest

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    release = Event()
    children = []

    def execute():
        with q.sessions() as db:
            service().begin_io(db, str(intent.id), "root")
            service().cancel(db, q.project_id)
            db.commit()
        if uncertainty == "detached_thread":
            child = Thread(target=lambda: release.wait(timeout=5), daemon=True)
            children.append(child)
            child.start()
        if uncertainty == "completion_write_failed":
            monkeypatch.setattr(
                database,
                "SessionLocal",
                Mock(side_effect=RuntimeError("DB unavailable")),
            )
            raise RuntimeError("original producer failure")

    if uncertainty in {"missing_callback", "status_only"}:
        monkeypatch.setattr(writers, "_complete_execution", lambda *args: None)
    _, _, run = rq_invocation(monkeypatch, execute)
    try:
        assert run() is (uncertainty != "completion_write_failed")
    finally:
        release.set()
        for child in children:
            child.join(timeout=5)
    with q.sessions() as db:
        if uncertainty == "status_only":
            writer = writers.registrations(db, str(intent.id))[0]
            payload = dict(
                writer_id=writer.id, writer_digest=writer.digest, status="finished"
            )
            db.add(
                NativeOriginWriterTerminal(
                    writer_id=writer.id, digest=digest(payload), payload=payload
                )
            )
            db.commit()
        with pytest.raises(AccountingUnavailable, match="completion|termination"):
            writers.observe_terminal(db, str(intent.id))


def test_finished_io_retains_writer_and_root_submission_history(
    origin_case, monkeypatch
):
    import rq

    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(
        rq, "get_current_job", lambda: SimpleNamespace(id="original-job")
    )
    with q.sessions() as db:
        token = service().begin_io(db, str(intent.id), "root")
        db.commit()
        service().finish_io(db, str(intent.id), token)
        db.commit()
        registrations = writers.registrations(db, str(intent.id))
        assert len(registrations) == 1 and registrations[0].payload[
            "execution_id"
        ].endswith(":original-job")
        assert writers.root_submitted(db, str(intent.id))
        service().cancel(db, q.project_id)
        db.commit()
        assert writers.root_submitted(db, str(intent.id))


@pytest.mark.parametrize("status", ["started", "missing", "finished", "canceled"])
def test_empty_slot_or_missing_job_cannot_prove_writer_terminal(
    origin_case, monkeypatch, status
):
    import rq
    from rq.job import Job

    from app.core import redis
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(
        rq, "get_current_job", lambda: SimpleNamespace(id="original-job")
    )
    monkeypatch.setattr(redis, "is_redis_available", lambda: True)
    monkeypatch.setattr(redis, "get_redis_raw", lambda: object())
    fetch = (
        Mock(side_effect=RuntimeError("expired"))
        if status == "missing"
        else Mock(return_value=SimpleNamespace(get_status=lambda **_: status))
    )
    monkeypatch.setattr(Job, "fetch", fetch)
    with q.sessions() as db:
        token = service().begin_io(db, str(intent.id), "support")
        service().finish_io(db, str(intent.id), token)
        db.commit()
        service().cancel(db, q.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="writer|tracking"):
            writers.observe_terminal(db, str(intent.id))


def test_persisted_terminal_proof_survives_expired_rq_metadata(
    origin_case, monkeypatch
):
    from rq.job import Job

    from app.core import database
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)

    def execute():
        with q.sessions() as db:
            token = service().begin_io(db, str(intent.id), "support")
            service().finish_io(db, str(intent.id), token)
            service().cancel(db, q.project_id)
            db.commit()

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    monkeypatch.setattr(
        Job, "fetch", Mock(side_effect=AssertionError("mutable RQ state queried"))
    )
    with q.sessions() as db:
        proof = writers.observe_terminal(db, str(intent.id))
        assert len(proof) == 1
    monkeypatch.setattr(
        Job, "fetch", Mock(side_effect=AssertionError("expired metadata queried"))
    )
    with q.sessions() as db:
        assert writers.observe_terminal(db, str(intent.id)) == proof


def test_thread_fallback_is_explicit_hold_after_cancellation(origin_case, monkeypatch):
    import rq

    from app.services import native_origin_writers as writers

    intent = prepare(origin_case)
    monkeypatch.setattr(rq, "get_current_job", lambda: None)
    with origin_case.sessions() as db:
        service().begin_io(db, str(intent.id), "namespace")
        service().cancel(db, origin_case.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="thread"):
            writers.observe_terminal(db, str(intent.id))


def test_provider_legacy_destroy_cannot_touch_origin_before_authority(
    origin_case, monkeypatch
):
    from app.core import database
    from app.services.providers import kubevirt

    prepare(origin_case)
    monkeypatch.setattr(database, "SessionLocal", origin_case.sessions)
    remote = Mock(side_effect=AssertionError("legacy provider action reached"))
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", remote)
    with pytest.raises(AccountingUnavailable, match="origin"):
        kubevirt.KubeVirtDriver().destroy_project(None, origin_case.project_id)
    remote.assert_not_called()


def test_writer_facts_survive_project_deletion(origin_case, monkeypatch):
    import rq

    from app.models.project import Project
    from app.services import native_origin_writers as writers

    intent = prepare(origin_case)
    monkeypatch.setattr(
        rq, "get_current_job", lambda: SimpleNamespace(id="original-job")
    )
    with origin_case.sessions() as db:
        service().begin_io(db, str(intent.id), "root")
        db.commit()
        db.delete(db.get(Project, origin_case.project_id))
        db.commit()
        assert len(writers.registrations(db, str(intent.id))) == 1
        assert writers.root_submitted(db, str(intent.id))


def test_postgres_completion_serializes_with_cancellation_without_remote_io(
    origin_case, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_writers as writers
    from app.services import quota_locks

    q = origin_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL observation lock contract")
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    entered, release = Event(), Event()
    completing = Event()
    original_complete = writers._complete_execution

    def execute():
        with q.sessions() as db:
            service().begin_io(db, str(intent.id), "support")
            db.commit()
        entered.set()
        assert release.wait(timeout=8)

    def complete(scope, outcome):
        completing.set()
        original_complete(scope, outcome)

    monkeypatch.setattr(writers, "_complete_execution", complete)
    _, _, run = rq_invocation(monkeypatch, execute)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert entered.wait(timeout=5)
        try:
            with q.sessions() as db:
                db.execute(text("SET LOCAL lock_timeout='500ms'"))
                quota_locks.lock_rows(
                    db, user_ids=[str(intent.owner_id)], project_ids=[q.project_id]
                )
                service().cancel(db, q.project_id)
                release.set()
                assert completing.wait(timeout=5)
                assert not future.done()
                assert db.query(NativeOriginWriterTerminal).count() == 0
                db.commit()
        finally:
            release.set()
        assert future.result(timeout=8)
    with q.sessions() as db:
        assert len(writers.observe_terminal(db, str(intent.id))) == 1


def test_postgres_writer_evidence_ddl_is_immutable(origin_case):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine = origin_case.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL evidence DDL")
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/b73ca21d9e50_native_origin_cleanup_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("writer_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.downgrade()
        conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_native_origin_mutation() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'Native deployment origin facts are immutable'; END $$"
            )
        )
        migration.upgrade()
        for table, key in (
            ("native_origin_writers", "id"),
            ("native_origin_writer_terminals", "writer_id"),
            ("native_origin_writer_holds", "writer_id"),
            ("native_origin_root_submissions", "intent_id"),
        ):
            extra_columns = ",intent_id,execution_id" if key == "id" else ""
            extra_values = ",:intent,'job'" if key == "id" else ""
            conn.execute(
                text(
                    f"INSERT INTO {table} ({key},digest,payload{extra_columns}) VALUES (:id,'digest','{{}}'{extra_values})"
                ),
                {"id": str(uuid4()), "intent": str(uuid4())},
            )
            for statement in (
                f"UPDATE {table} SET digest='changed'",
                f"DELETE FROM {table}",
            ):
                with pytest.raises(DBAPIError, match="immutable"), conn.begin_nested():
                    conn.execute(text(statement))
        migration.downgrade()
        migration.upgrade()
