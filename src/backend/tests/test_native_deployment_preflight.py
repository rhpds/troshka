"""Proven pre-project-I/O failures can recover without provider mutation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services import native_deployment_origin as origin
from app.services import native_deployment_preflight as preflight
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_database,
)

isolated_quota_database = isolated_database


@pytest.fixture
def case(isolated_quota_database, monkeypatch):
    from app.services import quota_accounting

    monkeypatch.setattr(origin, "capture_enabled", lambda: True)
    monkeypatch.setattr(quota_accounting, "quotas_enabled", lambda: False)
    sessions = isolated_quota_database
    with sessions() as db:
        owner = User(email=f"preflight-{uuid4()}@example.test")
        provider = Provider(name=str(uuid4()), type="kubevirt")
        db.add_all([owner, provider])
        db.flush()
        host = Host(provider_id=provider.id, host_type="kubevirt-cluster")
        db.add(host)
        db.flush()
        project = Project(
            name="early-failure", owner_id=owner.id, host_id=host.id, state="deploying"
        )
        db.add(project)
        preflight.record_fresh_project(db, project)
        db.commit()
        attempt = preflight.begin(db, project)
        db.commit()
        return SimpleNamespace(
            sessions=sessions,
            project_id=project.id,
            host_id=host.id,
            attempt_id=attempt,
            owner_id=owner.id,
        )


@pytest.mark.parametrize("mode", ["undeploy", "delete"])
def test_pre_handoff_failure_local_recovery(case, mode, monkeypatch):
    from app.models.native_deployment_preflight import (
        NativeDeploymentPreflightDecision as Decision,
    )
    from app.services import quota_cleanup

    monkeypatch.setattr(origin, "active", lambda *_: None)
    with case.sessions() as db:
        project = db.get(Project, case.project_id)
        project.state, project.deploy_error = "error", "Named GET forbidden (403)"
        db.commit()
        result = quota_cleanup.request_native(db, project, mode)
        assert result["status"] == ("draft" if mode == "undeploy" else "deleted")
        assert (
            db.get(Decision, case.attempt_id).payload["kind"] == "closed_no_project_io"
        )
        if mode == "delete":
            assert db.get(Project, case.project_id) is None
        else:
            assert project.host_id is None


@pytest.mark.parametrize("mode", ["undeploy", "delete"])
def test_normal_api_early_failure_never_releases_eips_or_calls_destroy(
    case, mode, monkeypatch
):
    from app.api import projects

    def forbidden(*_args, **_kwargs):
        pytest.fail("Early-failure recovery invoked remote/unowned cleanup")

    monkeypatch.setattr(projects, "_release_project_eips_on_delete", forbidden)
    monkeypatch.setattr(projects, "destroy_project_sync", forbidden)
    with case.sessions() as db:
        project = db.get(Project, case.project_id)
        project.state, project.deploy_error = "error", "Named root GET failed: 403"
        db.commit()
        actor = db.get(User, case.owner_id)
        call = (
            projects.delete_project if mode == "delete" else projects.undeploy_project
        )
        assert call(case.project_id, actor, db)["status"] == (
            "deleted" if mode == "delete" else "draft"
        )


def test_standard_create_boundary_and_placement_attempt(case, monkeypatch):
    from app.api import projects
    from app.core import redis
    from app.models.native_deployment_preflight import (
        NativeDeploymentCleanBaseline as Baseline,
    )
    from app.schemas.project import ProjectCreate

    jobs = []
    monkeypatch.setattr(
        redis, "enqueue_job", lambda *args, **kwargs: jobs.append((args, kwargs))
    )
    monkeypatch.setattr(projects, "_check_single_host_disk", lambda *args: None)
    with case.sessions() as db:
        project = projects.create_project(
            ProjectCreate(name="standard-new"), db.get(User, case.owner_id), db
        )
        assert db.get(Baseline, project.id) is not None
        project.host_id, project.state = case.host_id, "deploying"
        db.commit()
        projects._persist_placement_and_enqueue(
            project,
            dict(host_id=case.host_id, host_ip="test", requirements={}),
            project.id,
            db,
        )
        assert jobs[0][1]["native_attempt_id"] == preflight.current(db, project.id)


def test_real_provider_preparation_root_get_403_recovers_through_api(case):
    from kubernetes.client.exceptions import ApiException

    from app.api import projects
    from app.services.deploy_service import _kubevirt_creation_failed
    from app.services.providers.kubevirt import _deploy_project_with_origin

    calls = []

    class Api:
        def get(self, kind, namespace, name):
            calls.append((kind, name))
            if kind == "Namespace" and name == "kube-system":
                return {"metadata": {"uid": "cluster"}}
            raise ApiException(status=403, reason="Named root GET forbidden")

        def post(self, *_args):
            pytest.fail("Failure preceded project writes")

    token = preflight.worker_attempt.set(case.attempt_id)
    try:
        with case.sessions() as db:
            project = db.get(Project, case.project_id)
            provider = db.get(Provider, db.get(Host, case.host_id).provider_id)
            cr = dict(metadata=dict(namespace="fresh-project", name="root"), spec={})
            with pytest.raises(ApiException) as error:
                _deploy_project_with_origin(
                    provider, project.id, cr, None, None, None, db, Api(), None
                )
            _kubevirt_creation_failed(project, db, project.id, error.value)
            assert origin.active(db, project.id) is None
            assert (
                projects.undeploy_project(project.id, db.get(User, case.owner_id), db)[
                    "status"
                ]
                == "draft"
            )
        assert calls == [("Namespace", "kube-system"), ("TroshkaProject", "root")]
    finally:
        preflight.worker_attempt.reset(token)


def test_pattern_auto_dispatch_freezes_current_attempt(case, monkeypatch):
    from app.api import patterns
    from app.core import redis
    from app.schemas.pattern import PatternDeployRequest
    from app.services.ocp import pull_through_registry

    jobs = []
    monkeypatch.setattr(
        redis, "enqueue_job", lambda *args, **kwargs: jobs.append((args, kwargs))
    )
    monkeypatch.setattr(
        pull_through_registry,
        "check_pull_through_registry_for_project",
        lambda *_: None,
    )
    with case.sessions() as db:
        project = Project(name="auto-new", owner_id=case.owner_id, state="draft")
        db.add(project)
        preflight.record_fresh_project(db, project)
        db.commit()
        patterns._start_auto_deploy(
            project, PatternDeployRequest(host_id=case.host_id, auto_deploy=True), db
        )
        attempt = jobs[0][1]["native_attempt_id"]
        assert attempt == preflight.current(db, project.id)
        assert preflight.enter_worker(project.id, attempt) == attempt


def test_retry_creates_new_attempt_and_old_worker_is_fenced(case):
    with case.sessions() as db:
        preflight.request(db, db.get(Project, case.project_id), "undeploy")
        project = db.get(Project, case.project_id)
        project.host_id, project.state = case.host_id, "deploying"
        db.commit()
        fresh = preflight.begin(db, project)
        db.commit()
        assert fresh != case.attempt_id
        with pytest.raises(AccountingUnavailable):
            preflight.check(db, case.project_id, case.attempt_id)
    with case.sessions() as stale:
        project = stale.get(Project, case.project_id)
        preflight.guard_worker_session(stale, case.project_id, case.attempt_id)
        project.state = "error"
        with pytest.raises(AccountingUnavailable):
            stale.commit()
    with case.sessions() as db:
        assert db.get(Project, case.project_id).state == "deploying"


def test_worker_guard_preserves_dirty_values(case):
    with case.sessions() as db:
        preflight.guard_worker_session(db, case.project_id, case.attempt_id)
        project = db.get(Project, case.project_id)
        project.deploy_error, project.state = "403", "error"
        db.commit()
    with case.sessions() as db:
        assert db.get(Project, case.project_id).deploy_error == "403"
        assert db.get(Project, case.project_id).state == "error"


def test_closed_worker_progress_and_rq_callbacks_cannot_overwrite_retry(
    case, monkeypatch
):
    from app.core import redis
    from app.services import deploy_service

    calls = []
    connection = SimpleNamespace(delete=lambda *args: calls.append(args))
    monkeypatch.setattr(redis, "delete_progress", lambda *args: calls.append(args))
    monkeypatch.setattr(redis, "set_progress", lambda *args: calls.append(args))
    with case.sessions() as db:
        preflight.request(db, db.get(Project, case.project_id), "undeploy")
        project = db.get(Project, case.project_id)
        project.host_id, project.state = case.host_id, "deploying"
        db.commit()
        fresh = preflight.begin(db, project)
        db.commit()
    token = preflight.worker_attempt.set(case.attempt_id)
    try:
        deploy_service._update_deploy_progress(case.project_id, "old", "delayed")
        deploy_service._delete_deploy_progress(case.project_id)
        preflight.fail_job(
            case.project_id, case.attempt_id, RuntimeError("old"), connection
        )
        preflight.finish_job(case.project_id, case.attempt_id, connection)
    finally:
        preflight.worker_attempt.reset(token)
    assert not calls
    with case.sessions() as db:
        assert preflight.current(db, case.project_id) == fresh
        assert db.get(Project, case.project_id).state == "deploying"


def test_handoff_is_irreversible_even_without_provider_call(case):
    token = preflight.worker_attempt.set(case.attempt_id)
    try:
        with case.sessions() as db:
            preflight.handoff(db, case.project_id)
            db.commit()
        with case.sessions() as db:
            assert (
                preflight.request(db, db.get(Project, case.project_id), "undeploy")
                is None
            )
    finally:
        preflight.worker_attempt.reset(token)


def test_no_historical_baseline_from_error_or_provider_absence(case):
    with case.sessions() as db:
        historical = Project(
            name="historical",
            owner_id=case.owner_id,
            host_id=case.host_id,
            state="error",
        )
        db.add(historical)
        db.commit()
        preflight.begin(db, historical)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="baseline"):
            preflight.request(db, historical, "delete")


def test_direct_worker_dispatch_invalidates_fresh_baseline(case):
    with case.sessions() as db:
        project = Project(
            name="autopattern",
            owner_id=case.owner_id,
            host_id=case.host_id,
            state="deploying",
        )
        db.add(project)
        preflight.record_fresh_project(db, project)
        db.commit()
        project_id = project.id
    assert preflight.enter_worker(project_id, None) is None
    with case.sessions() as db:
        assert preflight.request(db, db.get(Project, project_id), "delete") is None
        from app.models.native_deployment_preflight import (
            NativeDeploymentPreflight as Attempt,
        )

        assert (
            db.get(Attempt, preflight.current(db, project_id)).payload[
                "baseline_digest"
            ]
            is None
        )


def test_capture_toggle_cannot_rebind_or_bypass_attempt(case, monkeypatch):
    monkeypatch.setattr(origin, "capture_enabled", lambda: False)
    assert preflight.enter_worker(case.project_id, case.attempt_id) == case.attempt_id
    with pytest.raises(AccountingUnavailable, match="explicit"):
        preflight.enter_worker(case.project_id, None)


@pytest.mark.parametrize("winner", ["close", "handoff"])
def test_postgres_close_versus_handoff_exactly_one_wins(case, winner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    if case.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock race")
    blocked = Event()

    def compete():
        token = preflight.worker_attempt.set(case.attempt_id)
        try:
            with case.sessions() as db:
                blocked.set()
                try:
                    if winner == "close":
                        preflight.handoff(db, case.project_id)
                        db.commit()
                        return "handoff"
                    result = preflight.request(
                        db, db.get(Project, case.project_id), "undeploy"
                    )
                    return "closed" if result else "held"
                except AccountingUnavailable:
                    return "held"
        finally:
            preflight.worker_attempt.reset(token)

    token = preflight.worker_attempt.set(case.attempt_id)
    try:
        with case.sessions() as db, ThreadPoolExecutor(max_workers=1) as pool:
            preflight.check(db, case.project_id, case.attempt_id)
            future = pool.submit(compete)
            assert blocked.wait(timeout=5)
            assert not future.done()
            if winner == "close":
                preflight.request(db, db.get(Project, case.project_id), "undeploy")
            else:
                preflight.handoff(db, case.project_id)
                db.commit()
            assert future.result(timeout=10) == "held"
    finally:
        preflight.worker_attempt.reset(token)


def test_preflight_migration_graph_and_upgrade(isolated_quota_database):
    import importlib.util
    from pathlib import Path

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["28bc37e61a90"]
    assert scripts.get_revision("28bc37e61a90").down_revision == "f17ae65bd394"
    engine = isolated_quota_database.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL DDL contract")
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/28bc37e61a90_native_deployment_preflight.py"
    )
    spec = importlib.util.spec_from_file_location("preflight_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        # Earlier origin migration owns this function; reproduce that existing
        # prerequisite because metadata-only fixtures do not execute migrations.
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_native_origin_mutation() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'Native deployment origin facts are immutable'; END; $$ LANGUAGE plpgsql"
            )
        )
        migration.downgrade()
        migration.upgrade()
        assert inspect(connection).has_table("native_deployment_preflight_active")
        connection.execute(
            text(
                "INSERT INTO native_deployment_clean_baselines (project_id,digest,payload) "
                "VALUES (:id,'digest','{}')"
            ),
            dict(id=str(uuid4())),
        )
        for command in (
            "UPDATE native_deployment_clean_baselines SET digest='changed'",
            "DELETE FROM native_deployment_clean_baselines",
        ):
            with pytest.raises(
                DBAPIError, match="immutable"
            ), connection.begin_nested():
                connection.execute(text(command))
        migration.downgrade()
        migration.upgrade()


@pytest.mark.parametrize("fail", [False, True])
def test_worker_progress_and_failure_reuse_locked_postgres_transaction(
    case, monkeypatch, fail
):
    from sqlalchemy import event

    from app.core import redis
    from app.services import deploy_service as deploy
    from app.services import placement

    engine = case.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL same-worker lock regression")

    def timeout(connection, _record, _proxy):
        cursor = connection.cursor()
        cursor.execute("SET lock_timeout TO '1s'")
        connection.commit()
        cursor.close()

    event.listen(engine, "checkout", timeout)
    updates = []
    monkeypatch.setattr(redis, "set_progress", lambda *args: updates.append(args))
    monkeypatch.setattr(redis, "delete_progress", lambda *_: None)
    monkeypatch.setattr(deploy, "_clear_deploy_cancelled", lambda *_: None)
    monkeypatch.setattr(
        deploy, "_deploying_project", lambda db, pid: db.get(Project, pid)
    )
    monkeypatch.setattr(
        deploy,
        "_deploy_resolve_host",
        lambda db, *_: (db.get(Host, case.host_id), None),
    )
    monkeypatch.setattr(
        deploy, "_deploy_init_context", lambda *_: ({}, 0, {"existing": 1})
    )
    monkeypatch.setattr(deploy, "_ensure_host_uplink_mtu", lambda *_: 1500)
    monkeypatch.setattr(deploy, "_deploy_resolve_mtu_map", lambda *_: {})
    monkeypatch.setattr(deploy, "_deploy_after_special_host", lambda *_: None)
    monkeypatch.setattr(deploy, "_cleanup_stale_shared_cache", lambda *_: None)
    monkeypatch.setattr(placement, "record_deploy_start", lambda *_: None)
    monkeypatch.setattr(placement, "record_deploy_end", lambda *_: None)
    from app.services.ocp import pull_through_registry

    monkeypatch.setattr(
        pull_through_registry,
        "check_pull_through_registry_for_project",
        lambda *_: None,
    )

    def native(pid, project, host, topology, db, mtu_map):
        assert preflight.worker_session.get() is db
        if fail:
            project.deploy_error = "pending worker detail"
            db.flush()  # before_flush takes the canonical locks too.
            raise RuntimeError("scoped worker failure")
        deploy._update_deploy_progress(pid, "networks", "creating operator resources")

    monkeypatch.setattr(deploy, "_deploy_kubevirt_native", native)
    token = preflight.worker_attempt.set(case.attempt_id)
    try:
        deploy._deploy_project_inner(case.project_id, native_attempt_id=case.attempt_id)
    finally:
        preflight.worker_attempt.reset(token)
        event.remove(engine, "checkout", timeout)
    with case.sessions() as db:
        project = db.get(Project, case.project_id)
        if fail:
            assert project.state == "error"
            assert project.deploy_error == "scoped worker failure"
        else:
            assert project.deploy_progress["step"] == "networks"
            assert len(updates) == 1


def test_unsupported_dispatch_can_handoff_after_native_placement_without_clean_recovery(
    case,
):
    from app.models.native_deployment_preflight import (
        NativeDeploymentPreflight as Attempt,
    )

    with case.sessions() as db:
        project = Project(
            name="implicit-host", owner_id=case.owner_id, state="deploying"
        )
        db.add(project)
        preflight.record_fresh_project(db, project)
        db.commit()
        attempt_id = preflight.prepare_dispatch(db, project)
        project.host_id = case.host_id
        db.commit()
        assert preflight.enter_worker(project.id, attempt_id) is None
        preflight.handoff(db, project.id)
        provider = db.get(Provider, db.get(Host, case.host_id).provider_id)
        from tests.test_native_deployment_origin import descriptor

        intent = origin.prepare(db, project, provider, descriptor())
        db.commit()
        assert origin.active(db, project.id).id == intent.id
        assert db.get(Attempt, attempt_id).payload["baseline_digest"] is None
        assert preflight.request(db, project, "delete") is None


@pytest.mark.parametrize("failed", [False, True])
def test_unsupported_rq_callbacks_keep_cleanup_after_implicit_placement(
    case, monkeypatch, failed
):
    from app.core import redis
    from app.services import placement

    calls = []
    connection = SimpleNamespace(delete=lambda key: calls.append(("key", key)))
    monkeypatch.setattr(
        redis, "delete_progress", lambda key: calls.append(("progress", key))
    )
    monkeypatch.setattr(
        redis,
        "_cleanup_host_locks",
        lambda host, pid: calls.append(("locks", host, pid)),
    )
    monkeypatch.setattr(
        placement, "record_deploy_end", lambda host: calls.append(("host", host))
    )
    with case.sessions() as db:
        project = Project(name="implicit-rq", owner_id=case.owner_id, state="deploying")
        db.add(project)
        preflight.record_fresh_project(db, project)
        db.commit()
        attempt = preflight.prepare_dispatch(db, project)
        project.host_id = case.host_id
        db.commit()
        project_id = project.id
    job = SimpleNamespace(
        id="test-job",
        meta=dict(project_id=project_id, host_id=case.host_id),
        kwargs=dict(native_attempt_id=attempt),
    )
    if failed:
        redis._on_job_failure(
            job, connection, RuntimeError, RuntimeError("provider failed"), None
        )
        assert ("locks", case.host_id, project_id) in calls
        with case.sessions() as db:
            assert db.get(Project, project_id).state == "error"
    else:
        redis._on_job_success(job, connection, None)
        assert ("host", case.host_id) in calls
    assert ("key", f"job:project:{project_id}") in calls
