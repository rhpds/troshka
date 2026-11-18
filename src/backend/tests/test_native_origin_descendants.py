"""Parent exit never substitutes for protected descendant settlement."""

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
from tests.test_native_origin_cleanup import rq_invocation

isolated_quota_database = origin_database
origin_case = origin_fixture


def test_parent_exit_retained_without_erasing_unregistered_hold(
    origin_case, monkeypatch
):
    from app.core import database
    from app.models.native_origin_cleanup import (
        NativeOriginWriterHold,
        NativeOriginWriterTerminal,
    )
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)

    def execute():
        with q.sessions() as db:
            writers.mark_async_support(db, q.project_id)

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    with q.sessions() as db:
        writer = writers.registrations(db, str(intent.id))[0]
        assert db.get(NativeOriginWriterTerminal, writer.id) is not None
        assert db.get(NativeOriginWriterHold, writer.id) is not None
        service().cancel(db, q.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="support"):
            writers.observe_terminal(db, str(intent.id))


def test_child_handoff_must_settle_separately_after_parent_exit(
    origin_case, monkeypatch
):
    from app.core import database
    from app.models.native_origin_cleanup import (
        NativeOriginWriterHold,
        NativeOriginWriterTerminal,
    )
    from app.services import native_origin_descendants as children
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    dispatched = []

    def execute():
        with q.sessions() as db:
            children.mark_support(db, q.project_id)
            dispatched.append(
                children.prepare(db, q.project_id, "monitor", {"clusters": []})
            )

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    with q.sessions() as db:
        parent = writers.registrations(db, str(intent.id))[0]
        assert db.get(NativeOriginWriterTerminal, parent.id)
        hold_digest = db.get(NativeOriginWriterHold, parent.id).digest
        service().cancel(db, q.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="descendant"):
            writers.observe_terminal(db, str(intent.id))
    called = []
    _, _, child = rq_invocation(
        monkeypatch,
        lambda: children.invoke(
            dispatched[0],
            q.project_id,
            "monitor",
            {"clusters": []},
            lambda: called.append(True),
        ),
        "child-one",
    )
    assert child()
    assert not called
    with q.sessions() as db:
        assert len(writers.observe_terminal(db, str(intent.id))) == 1
        assert db.get(NativeOriginWriterHold, parent.id).digest == hold_digest


def test_claimed_child_exit_and_replay_are_independent(origin_case, monkeypatch):
    from app.core import database
    from app.services import native_origin_descendants as children
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with q.sessions() as db:
        handoff = children.prepare(db, q.project_id, "monitor", {"clusters": []})
    called = []

    def execute():
        children.invoke(
            handoff,
            q.project_id,
            "monitor",
            {"clusters": []},
            lambda: called.append(True),
        )

    _, _, run = rq_invocation(monkeypatch, execute, "child")
    assert run()
    _, _, replay = rq_invocation(monkeypatch, execute, "replay")
    assert not replay()
    assert called == [True]
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        assert len(writers.observe_terminal(db, str(intent.id))) == 1
        with pytest.raises(AccountingUnavailable, match="cancel"):
            children.prepare(db, q.project_id, "monitor", {"clusters": []})


def test_join_timeout_keeps_child_unsettled_but_preserves_parent_exit(
    origin_case, monkeypatch
):
    from threading import Event

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.services import native_origin_descendants as children
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    entered, release = Event(), Event()
    child_threads = []

    def child():
        entered.set()
        assert release.wait(timeout=5)

    def parent():
        thread = children.start_joined(q.project_id, {}, child)
        child_threads.append(thread)
        assert entered.wait(timeout=5)
        children.join(thread, timeout=0.01)

    _, _, run = rq_invocation(monkeypatch, parent)
    try:
        assert run()
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()
            assert db.query(NativeOriginWriterTerminal).count() == 1
            with pytest.raises(AccountingUnavailable, match="completion"):
                writers.observe_terminal(db, str(intent.id))
    finally:
        release.set()
        for thread in child_threads:
            thread.join(timeout=5)
    with q.sessions() as db:
        assert len(writers.observe_terminal(db, str(intent.id))) == 2


def test_actual_monitor_without_remote_dispatch_finishes(origin_case, monkeypatch):
    from app.core import database
    from app.models.native_origin_descendants import NativeOriginRemoteHold
    from app.services import deploy_service
    from app.services import native_origin_descendants as children
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with q.sessions() as db:
        handoff = children.prepare(
            db,
            q.project_id,
            "monitor",
            {"host_id": str(intent.host_id), "clusters": []},
        )
    calls = []

    def detached(host_id):
        with q.sessions() as db:
            assert db.query(NativeOriginRemoteHold).count() == 0
        calls.append(host_id)
        return None

    monkeypatch.setattr(deploy_service, "_detached_host_copy", detached)
    monkeypatch.setattr(
        deploy_service, "_release_ops_monitor_lock", lambda project_id: None
    )
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: deploy_service._ops_pod_install_monitor_job(
            q.project_id, str(intent.host_id), [], origin_handoff_id=handoff
        ),
    )
    assert run()
    assert calls == [str(intent.host_id)]
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        assert writers.observe_terminal(db, str(intent.id))


def test_actual_monitor_cannot_bypass_handoff_for_origin(origin_case, monkeypatch):
    from app.core import database
    from app.services import deploy_service

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with pytest.raises(AccountingUnavailable, match="handoff required"):
        deploy_service._ops_pod_install_monitor_job(
            q.project_id, str(intent.host_id), []
        )


def test_actual_monitor_joins_actual_recert_launch_before_exit(
    origin_case, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from app.core import database
    from app.models.native_origin_cleanup import NativeOriginWriterTerminal
    from app.models.project import Project
    from app.services import deploy_service
    from app.services import native_origin_descendants as children
    from app.services import native_origin_writers as writers

    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with q.sessions() as db:
        db.get(Project, q.project_id).topology = {"nodes": [{"id": "test-node"}]}
        db.commit()
        handoff = children.prepare(
            db,
            q.project_id,
            "monitor",
            {"host_id": str(intent.host_id), "clusters": []},
        )
    entered, monitor_returned, release = Event(), Event(), Event()

    def deliver(*args):
        entered.set()
        assert release.wait(timeout=5)

    def monitor(*args, **kwargs):
        assert entered.wait(timeout=5)
        monitor_returned.set()

    monkeypatch.setattr(
        deploy_service,
        "_detached_host_copy",
        lambda host_id: SimpleNamespace(id=host_id),
    )
    monkeypatch.setattr(
        deploy_service,
        "_partition_ops_pod_clusters",
        lambda *args: ([{"id": "cluster"}], []),
    )
    monkeypatch.setattr(deploy_service, "_deliver_recert_kubeconfigs", deliver)
    monkeypatch.setattr(deploy_service, "_monitor_ops_pod_install", monitor)
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: deploy_service._ops_pod_install_monitor_job(
            q.project_id, str(intent.host_id), [], origin_handoff_id=handoff
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert monitor_returned.wait(timeout=5)
            assert not future.done()
            with q.sessions() as db:
                assert len(writers.registrations(db, str(intent.id))) == 2
                assert db.query(NativeOriginWriterTerminal).count() == 0
        finally:
            release.set()
        assert future.result(timeout=5)
    with q.sessions() as db:
        assert db.query(NativeOriginWriterTerminal).count() == 2
        service().cancel(db, q.project_id)
        db.commit()
        assert len(writers.observe_terminal(db, str(intent.id))) == 2


def test_dispatch_bound_and_changed_arguments_hold(origin_case, monkeypatch):
    from app.core import database
    from app.services import native_origin_descendants as children

    q = origin_case
    prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    monkeypatch.setattr(children, "_LIMIT", 1)
    with q.sessions() as db:
        handoff = children.prepare(db, q.project_id, "monitor", {"clusters": []})
        with pytest.raises(AccountingUnavailable, match="history limit"):
            children.prepare(db, q.project_id, "monitor", {"clusters": []})
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: children.invoke(
            handoff,
            q.project_id,
            "monitor",
            {"clusters": ["substitution"]},
            lambda: pytest.fail("entered"),
        ),
    )
    assert not run()


def test_postgres_claim_wait_rechecks_cancellation_and_closes_dispatch(
    origin_case, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.core import database
    from app.models.native_origin_descendants import (
        NativeOriginDescendantClaim,
        NativeOriginDispatchClosure,
    )
    from app.services import native_origin_descendants as children

    q = origin_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL identity lock race")
    intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with q.sessions() as db:
        handoff = children.prepare(db, q.project_id, "monitor", {})
    entering = Event()
    called = []
    locked = service()._locked

    def lock(db, intent_id, **kwargs):
        entering.set()
        return locked(db, intent_id, **kwargs)

    monkeypatch.setattr(service(), "_locked", lock)
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: children.invoke(
            handoff, q.project_id, "monitor", {}, lambda: called.append(True)
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            future = pool.submit(run)
            assert entering.wait(timeout=5)
            assert not future.done()
            db.commit()
        assert future.result(timeout=5)
    assert called == []
    with q.sessions() as db:
        assert (
            db.get(NativeOriginDescendantClaim, handoff).payload["outcome"]
            == "never_entered"
        )
        closure = db.get(NativeOriginDispatchClosure, str(intent.id))
        assert [v["id"] for v in closure.payload["handoffs"]] == [handoff]


def test_postgres_descendant_evidence_ddl_is_immutable(origin_case):
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
        / "alembic/versions/d95ec43fb172_native_origin_descendant_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("descendant_migration", path)
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
        for table, key in [
            ("native_origin_descendant_handoffs", "id"),
            *migration._KEYS,
        ]:
            extra_columns = ",intent_id" if key == "id" else ""
            extra_values = ",:intent" if key == "id" else ""
            conn.execute(
                text(
                    f"INSERT INTO {table} ({key},digest,payload{extra_columns}) "
                    f"VALUES (:id,'digest','{{}}'{extra_values})"
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
