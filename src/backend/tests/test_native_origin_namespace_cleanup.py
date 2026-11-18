"""Pre-root cleanup requires exact support, complete discovery, and retained absence."""

import base64
from copy import deepcopy

import pytest

from app.schemas.quota import AccountingUnavailable
from tests.test_native_deployment_origin import (
    isolated_quota_database as origin_database,
)
from tests.test_native_deployment_origin import origin_case as origin_fixture
from tests.test_native_origin_cleanup import rq_invocation

isolated_quota_database = origin_database
origin_case = origin_fixture


@pytest.fixture
def cleanup_case(origin_case, monkeypatch):
    from app.core import database, redis
    from app.models.provider import Provider
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_namespace_cleanup as cleanup
    from app.services.providers import kubevirt

    q = origin_case
    q.objects, q.posts, q.deletes, q.enqueued = {}, [], [], []
    q.cancel_at, q.lost_support, q.ambiguous_delete = None, False, False
    q.namespace = "troshka-" + q.project_id[:8]

    class API(DiscoveryAPI):
        def __init__(self):
            super().__init__()
            self.data["/apis"]["groups"] += [
                {
                    "name": "rbac.authorization.k8s.io",
                    "versions": [
                        {
                            "version": "v1",
                            "groupVersion": "rbac.authorization.k8s.io/v1",
                        }
                    ],
                },
                {
                    "name": "troshka.redhat.com",
                    "versions": [
                        {
                            "version": "v1alpha1",
                            "groupVersion": "troshka.redhat.com/v1alpha1",
                        }
                    ],
                },
            ]
            self.data["/api/v1"]["resources"] += [
                dict(
                    name="configmaps", kind="ConfigMap", namespaced=True, verbs=["list"]
                ),
                dict(
                    name="serviceaccounts",
                    kind="ServiceAccount",
                    namespaced=True,
                    verbs=["list"],
                ),
            ]
            for group, version, resource, kind in [
                ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding"),
                ("troshka.redhat.com", "v1alpha1", "troshkaprojects", "TroshkaProject"),
            ]:
                self.data[f"/apis/{group}/{version}"] = dict(
                    kind="APIResourceList",
                    groupVersion=f"{group}/{version}",
                    resources=[
                        dict(name=resource, kind=kind, namespaced=True, verbs=["list"])
                    ],
                )

        def get(self, kind, namespace, name):
            if name == "kube-system":
                return {"metadata": {"uid": "cluster-uid"}}
            if q.lost_support and kind != "Namespace":
                raise TimeoutError("response lost")
            return deepcopy(q.objects.get((kind, name)))

        def post(self, kind, body):
            value = deepcopy(body)
            value["metadata"].update(
                uid=kind + ":" + value["metadata"]["name"],
                resourceVersion="1",
                generation=1,
            )
            if kind == "Secret":
                value["data"] = {
                    k: base64.b64encode(v.encode()).decode()
                    for k, v in value.pop("stringData").items()
                }
            q.objects[(kind, value["metadata"]["name"])] = value
            q.posts.append(kind)
            if q.cancel_at == len(q.posts):
                with q.sessions() as db:
                    origin.cancel(db, q.project_id)
                    db.commit()
            if q.lost_support and kind != "Namespace":
                raise TimeoutError("support POST response lost")
            return deepcopy(value)

        def path(self, path, query=()):
            if "/namespaces/" not in path:
                return super().path(path, query)
            plural = path.rsplit("/", 1)[1]
            kind = {
                "secrets": "Secret",
                "rolebindings": "RoleBinding",
                "troshkaprojects": "TroshkaProject",
                "unknowns": "Unknown",
                "configmaps": "ConfigMap",
                "serviceaccounts": "ServiceAccount",
            }[plural]
            return dict(
                metadata=dict(resourceVersion="1"),
                items=[deepcopy(v) for (k, _), v in q.objects.items() if k == kind],
            )

        def delete_namespace(self, name, uid, resource_version):
            with q.sessions() as db:
                from app.models.native_origin_namespace import (
                    NativeOriginNamespaceCleanup,
                    NativeOriginNamespaceEvidence,
                )

                work = cleanup._load(db.query(NativeOriginNamespaceCleanup).one())
                assert work.phase == "namespace_delete_requested"
                assert (
                    db.query(NativeOriginNamespaceEvidence)
                    .filter_by(phase="namespace_delete_requested")
                    .one()
                )
            assert q.objects[("Namespace", name)]["metadata"]["uid"] == uid
            assert resource_version == "1"
            q.deletes.append((name, uid, resource_version))
            q.objects.pop(("Namespace", name))
            if q.ambiguous_delete:
                raise TimeoutError("DELETE response lost")

    q.api = API()
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    monkeypatch.setattr(redis, "enqueue_job", lambda *a, **k: q.enqueued.append(a))
    monkeypatch.setattr(kubevirt, "_get_k8s_clients", lambda *a: (None, None, None))
    monkeypatch.setattr(kubevirt, "_origin_api", lambda *a: q.api)
    monkeypatch.setattr(kubevirt, "_ensure_operator_crds", lambda *a: None)
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *a: q.namespace)
    monkeypatch.setattr(
        kubevirt,
        "_resolve_project_s3_config",
        lambda *a: (
            {"secret_access_key": "private-obc"},
            {"secret_access_key": "private-central"},
        ),
    )
    monkeypatch.setattr(kubevirt, "_ensure_cache_s3_secrets", lambda *a: None)
    monkeypatch.setattr(kubevirt, "base_namespace_grants_mode", lambda: "managed")
    monkeypatch.setattr(origin, "capture_enabled", lambda: True)
    original_begin = origin.begin_io

    def begin(db, intent_id, phase):
        if phase == "root":
            raise RuntimeError("failure before root submission")
        return original_begin(db, intent_id, phase)

    monkeypatch.setattr(origin, "begin_io", begin)

    def execute():
        with q.sessions() as db:
            kubevirt.KubeVirtDriver().deploy_project(
                db.get(Provider, q.provider_id),
                q.project_id,
                {},
                {"secret_access_key": "private-project"},
                db=db,
            )

    _, _, q.launch = rq_invocation(monkeypatch, execute)

    def request(mode="undeploy"):
        from app.models.project import Project
        from app.services.quota_cleanup import request_native

        with q.sessions() as db:
            result = request_native(db, db.get(Project, q.project_id), mode)
            q.cleanup_id = result["cleanup_id"]
            q.intent = origin.active(db, q.project_id)
            return result

    q.request = request
    return q


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
def test_request_delete_and_undeploy_retains_absence_without_storage_facts(
    cleanup_case, monkeypatch, mode
):
    from app.models.kubevirt_storage import NativeProjectRoot, NativeRetainedBinding
    from app.models.native_origin_namespace import (
        NativeOriginNamespaceEvidence,
        NativeOriginSupportReceipt,
    )
    from app.models.project import Project
    from app.models.quota import QuotaReservation
    from app.services import native_deployment_origin as origin
    from app.services import native_origin_namespace_cleanup as cleanup
    from app.services import ws_pubsub

    q = cleanup_case
    assert q.launch() is False
    assert q.request(mode)["status"] == "cleanup_pending"
    with q.sessions() as db:
        work = cleanup.advance(db, q.api, q.cleanup_id)
        assert work.phase == "namespace_delete_requested"
        assert (
            not db.query(NativeOriginNamespaceEvidence)
            .filter_by(phase="absent")
            .first()
        )
    with q.sessions() as db:
        assert cleanup.advance(db, q.api, q.cleanup_id).phase == "absent"
    monkeypatch.setattr(ws_pubsub, "notify_project", lambda *a: None)
    with q.sessions() as db:
        cleanup.finish(db, q.cleanup_id)
        project = db.get(Project, q.project_id)
        assert (
            project is None
            if mode == "delete"
            else project.state == "draft" and project.host_id is None
        )
        assert origin.active(db, q.project_id) is None
        assert origin.receipts(db, str(q.intent.id))[0] is not None
        assert db.query(NativeOriginSupportReceipt).count() == 7
        assert {r.phase for r in db.query(NativeOriginNamespaceEvidence)} == {
            "sealed",
            "namespace_delete_requested",
            "absent",
        }
        assert all(
            db.query(model).count() == 0
            for model in (QuotaReservation, NativeProjectRoot, NativeRetainedBinding)
        )


@pytest.mark.parametrize("boundary", range(1, 8))
def test_cancellation_after_each_namespace_and_support_post_is_read_only(
    cleanup_case, boundary
):
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.cancel_at = boundary
    assert q.launch() is False
    q.request()
    before = list(q.posts)
    with q.sessions() as db:
        assert (
            cleanup.advance(db, q.api, q.cleanup_id).phase
            == "namespace_delete_requested"
        )
    assert q.posts == before and len(q.posts) == boundary


@pytest.mark.parametrize("failure", ["conflict", "forbidden", "lost", "replacement"])
def test_first_support_receipt_never_adopts_foreign_uid(cleanup_case, failure):
    from app.models.native_origin_namespace import NativeOriginSupportReceipt
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    original_post = q.api.post

    def post(kind, body):
        if kind == "Namespace":
            return original_post(kind, body)
        value = original_post(kind, body)
        foreign = q.objects[(kind, body["metadata"]["name"])]
        foreign["metadata"]["uid"] = "foreign-uid-" + body["metadata"]["name"]
        if failure != "replacement":
            foreign["metadata"].pop("annotations", None)
            raise RuntimeError(failure)
        return value

    q.api.post = post
    assert q.launch() is False
    q.request()
    with q.sessions() as db, pytest.raises(AccountingUnavailable, match="support"):
        cleanup.advance(db, q.api, q.cleanup_id)
    with q.sessions() as db:
        assert all(
            not (row.payload["observed"] or {}).get("uid", "").startswith("foreign-uid")
            for row in db.query(NativeOriginSupportReceipt)
        )
    assert not q.deletes


def test_lost_support_response_is_recovered_without_repair(cleanup_case):
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    # Fail the POST/readback boundary after the first support object was created.
    original_post = q.api.post

    def post(kind, body):
        if kind != "Namespace":
            q.lost_support = True
        return original_post(kind, body)

    q.api.post = post
    assert q.launch() is False
    q.lost_support = False
    q.request()
    before = list(q.posts)
    with q.sessions() as db:
        assert (
            cleanup.advance(db, q.api, q.cleanup_id).phase
            == "namespace_delete_requested"
        )
    assert q.posts == before


@pytest.mark.parametrize("replacement", [False, True])
def test_ambiguous_delete_restart_observes_original_uid_and_retains_absence(
    cleanup_case, replacement
):
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.launch()
    q.request()
    original = deepcopy(q.objects[("Namespace", q.namespace)])
    q.ambiguous_delete = True
    with q.sessions() as db, pytest.raises(TimeoutError):
        cleanup.advance(db, q.api, q.cleanup_id)
    if replacement:
        original["metadata"]["uid"] = "replacement-namespace"
        q.objects[("Namespace", q.namespace)] = original
    with q.sessions() as db:
        if replacement:
            with pytest.raises(AccountingUnavailable, match="UID"):
                cleanup.advance(db, q.api, q.cleanup_id)
        else:
            assert cleanup.advance(db, q.api, q.cleanup_id).phase == "absent"
    assert len(q.deletes) == 1


@pytest.mark.parametrize(
    "fault",
    ["unknown", "root", "secret_bytes", "secret_uid", "incomplete", "submitted"],
)
def test_namespace_cleanup_holds_on_unknown_objects_or_changed_support(
    cleanup_case, fault
):
    from app.models.native_origin_cleanup import NativeOriginRootSubmission
    from app.services import native_origin_namespace_cleanup as cleanup
    from app.services.kubevirt_storage_enrollment import digest

    q = cleanup_case
    q.launch()
    q.request()
    if fault in {"unknown", "root"}:
        kind = "Unknown" if fault == "unknown" else "TroshkaProject"
        version = (
            "example.test/v1" if fault == "unknown" else "troshka.redhat.com/v1alpha1"
        )
        q.objects[(kind, "unexpected")] = dict(
            apiVersion=version,
            kind=kind,
            metadata=dict(
                name="unexpected", namespace=q.namespace, uid="unexpected-uid"
            ),
        )
    elif fault.startswith("secret"):
        secret = q.objects[("Secret", "s3-credentials")]
        if fault == "secret_bytes":
            secret["data"]["secretKey"] = base64.b64encode(b"changed").decode()
        else:
            secret["metadata"]["uid"] = "replacement"
    elif fault == "incomplete":
        q.api.data["/api/v1"].pop("resources")
    else:
        with q.sessions() as db:
            payload = {"intent_id": str(q.intent.id), "original_post_started": True}
            db.add(
                NativeOriginRootSubmission(
                    intent_id=str(q.intent.id), payload=payload, digest=digest(payload)
                )
            )
            db.commit()
    with q.sessions() as db, pytest.raises(AccountingUnavailable):
        cleanup.advance(db, q.api, q.cleanup_id)
    assert not q.deletes


@pytest.mark.parametrize("has_records", [False, True])
def test_configured_dns_is_not_liability_but_retained_records_hold(
    cleanup_case, has_records
):
    from fastapi import HTTPException

    from app.models.dns_provider import DnsProvider
    from app.models.project import Project

    q = cleanup_case
    q.launch()
    with q.sessions() as db:
        provider = DnsProvider(name="unused-dns", type="route53", config={})
        db.add(provider)
        db.flush()
        project = db.get(Project, q.project_id)
        project.dns_provider_id, project.domain = provider.id, "configured.example.test"
        project.deployed_topology = (
            {"_dns_records": [{"name": "retained", "type": "A", "value": "192.0.2.1"}]}
            if has_records
            else {}
        )
        db.commit()
    if has_records:
        with pytest.raises(HTTPException, match="DNS"):
            q.request()
    else:
        assert q.request()["status"] == "cleanup_pending"


def test_unresolved_original_root_slot_can_never_be_treated_as_never_submitted(
    cleanup_case,
):
    from fastapi import HTTPException

    from app.services import native_deployment_origin as origin

    q = cleanup_case
    q.launch()
    with q.sessions() as db:
        intent = origin.active(db, q.project_id)
        _, pointer = origin._locked(db, str(intent.id))
        pointer.io_phase = "root"
        db.commit()
    with pytest.raises(HTTPException, match="root"):
        q.request()
    assert not q.deletes


def test_active_original_writer_with_empty_io_slot_holds(cleanup_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services import native_deployment_origin as origin
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    entered, release = Event(), Event()
    before = origin.begin_io

    def begin(db, identity, phase):
        if phase == "root":
            entered.set()
            assert release.wait(8)
        return before(db, identity, phase)

    monkeypatch.setattr(origin, "begin_io", begin)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(q.launch)
        assert entered.wait(5)
        try:
            q.request()
            with q.sessions() as db, pytest.raises(
                AccountingUnavailable, match="completion"
            ):
                cleanup.advance(db, q.api, q.cleanup_id)
            assert not q.deletes and not future.done()
        finally:
            release.set()
        assert future.result(8) is False
    with q.sessions() as db:
        assert (
            cleanup.advance(db, q.api, q.cleanup_id).phase
            == "namespace_delete_requested"
        )


def test_unresolved_namespace_post_absence_is_not_cleanup_success(cleanup_case):
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.api.post = lambda *a: (_ for _ in ()).throw(TimeoutError("unknown original POST"))
    assert q.launch() is False
    q.request()
    with q.sessions() as db, pytest.raises(
        AccountingUnavailable, match="POST unresolved"
    ):
        cleanup.advance(db, q.api, q.cleanup_id)
    assert not q.deletes


def test_protected_completion_survives_expired_remote_tracking(
    cleanup_case, monkeypatch
):
    from rq.job import Job

    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.launch()
    q.request()
    monkeypatch.setattr(
        Job,
        "fetch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not inspect RQ status")
        ),
    )
    with q.sessions() as db:
        assert (
            cleanup.advance(db, q.api, q.cleanup_id).phase
            == "namespace_delete_requested"
        )


@pytest.mark.parametrize("changed", [False, True])
def test_postgres_inventory_releases_locks_and_rechecks_handoff(cleanup_case, changed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    from sqlalchemy import text

    from app.services import native_deployment_origin as origin
    from app.services import native_origin_namespace_cleanup as cleanup
    from app.services.quota_locks import lock_retained_project

    q = cleanup_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")
    q.launch()
    q.request()
    entered, release = Event(), Event()
    original_path = q.api.path

    def path(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(8)
        return original_path(*args, **kwargs)

    q.api.path = path

    def advance():
        with q.sessions() as db:
            return cleanup.advance(db, q.api, q.cleanup_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(advance)
        assert entered.wait(5)
        try:
            with q.sessions() as db:
                db.execute(text("SET LOCAL statement_timeout = '1000ms'"))
                lock_retained_project(db, q.project_id)
                if changed:
                    _, pointer = origin._locked(
                        db, str(q.intent.id), allow_cancelled=True
                    )
                    pointer.io_token = str(uuid4())
                db.commit()
        finally:
            release.set()
        if changed:
            with pytest.raises(AccountingUnavailable, match="handoff changed"):
                future.result(8)
            assert not q.deletes
        else:
            assert future.result(8).phase == "namespace_delete_requested"


def test_postgres_namespace_evidence_ddl_is_immutable(origin_case):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine = origin_case.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL immutable namespace evidence")
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/c84db32ea061_native_origin_namespace_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location("namespace_migration", path)
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
        for table, key, columns, values in (
            ("native_origin_support_handoffs", "id", ",intent_id", ",:intent"),
            ("native_origin_support_receipts", "handoff_id", "", ""),
            (
                "native_origin_namespace_evidence",
                "id",
                ",cleanup_id,phase",
                ",:intent,'sealed'",
            ),
        ):
            conn.execute(
                text(
                    f"INSERT INTO {table} ({key},digest,payload{columns}) VALUES (:id,'digest','{{}}'{values})"
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


class DiscoveryAPI:
    def __init__(self):
        self.calls = []
        self.data = {
            "/api": {"kind": "APIVersions", "versions": ["v1"]},
            "/apis": {
                "kind": "APIGroupList",
                "groups": [
                    {
                        "name": "example.test",
                        "versions": [
                            {"groupVersion": "example.test/v1", "version": "v1"}
                        ],
                        "preferredVersion": {
                            "groupVersion": "example.test/v1",
                            "version": "v1",
                        },
                    }
                ],
            },
            "/api/v1": {
                "kind": "APIResourceList",
                "groupVersion": "v1",
                "resources": [
                    {
                        "name": "secrets",
                        "kind": "Secret",
                        "namespaced": True,
                        "verbs": ["get", "list"],
                    }
                ],
            },
            "/apis/example.test/v1": {
                "kind": "APIResourceList",
                "groupVersion": "example.test/v1",
                "resources": [
                    {
                        "name": "unknowns",
                        "kind": "Unknown",
                        "namespaced": True,
                        "verbs": ["list"],
                    }
                ],
            },
            "/api/v1/namespaces/ns/secrets": {
                "metadata": {"resourceVersion": "2"},
                "items": [],
            },
            "/apis/example.test/v1/namespaces/ns/unknowns": {
                "metadata": {"resourceVersion": "3"},
                "items": [],
            },
        }

    def path(self, path, query=()):
        self.calls.append((path, query))
        value = self.data[path]
        return value(query) if callable(value) else deepcopy(value)


def test_discovery_lists_unknown_custom_types_and_all_pages():
    from app.services.native_origin_namespace_cleanup import discover, inventory_rows

    api = DiscoveryAPI()
    path = "/apis/example.test/v1/namespaces/ns/unknowns"
    api.data[path] = (
        lambda query: {
            "metadata": {"resourceVersion": "3", "continue": "next"},
            "items": [],
        }
        if not dict(query).get("continue")
        else {
            "metadata": {"resourceVersion": "3"},
            "items": [
                {"metadata": {"namespace": "ns", "name": "alien", "uid": "alien-uid"}}
            ],
        }
    )
    rows = inventory_rows(api, "ns", discover(api))
    assert rows[0]["kind"] == "Unknown" and rows[0]["apiVersion"] == "example.test/v1"
    assert len([v for v in api.calls if v[0] == path]) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "missing_resources",
        "pagination_loop",
        "changed_rv",
        "wrong_namespace",
        "permission",
    ],
)
def test_incomplete_inventory_is_never_empty(fault):
    from app.services.native_origin_namespace_cleanup import discover, inventory_rows

    api = DiscoveryAPI()
    path = "/api/v1/namespaces/ns/secrets"
    if fault == "missing_resources":
        api.data["/api/v1"].pop("resources")
    elif fault == "pagination_loop":
        api.data[path]["metadata"]["continue"] = "loop"
    elif fault == "changed_rv":
        api.data[path] = lambda q: {
            "metadata": {
                "resourceVersion": "4" if dict(q).get("continue") else "3",
                "continue": "next",
            },
            "items": [],
        }
    elif fault == "wrong_namespace":
        api.data[path]["items"] = [
            {"metadata": {"namespace": "other", "name": "n", "uid": "u"}}
        ]
    else:
        api.data[path] = lambda q: (_ for _ in ()).throw(
            AccountingUnavailable("permission denied")
        )
    with pytest.raises(AccountingUnavailable):
        inventory_rows(api, "ns", discover(api))


def _legacy_binding(canonical):
    value = deepcopy(canonical)
    value["apiVersion"] = "authorization.openshift.io/v1"
    ref = value["roleRef"]
    value["roleRef"] = {"name": ref["name"]}
    if ref["kind"] == "Role":
        value["roleRef"]["namespace"] = value["metadata"]["namespace"]
    users, groups = [], []
    for subject in value["subjects"]:
        subject.pop("apiGroup", None)
        if subject["kind"] == "ServiceAccount":
            users.append(
                f"system:serviceaccount:{subject['namespace']}:{subject['name']}"
            )
        elif subject["kind"] == "Group":
            groups.append(subject["name"])
            subject["kind"] = "SystemGroup" if ":" in subject["name"] else "Group"
        else:
            users.append(subject["name"])
    value["userNames"], value["groupNames"] = users or None, groups or None
    annotations = value["metadata"].get("annotations", {})
    auto = annotations.get("rbac.authorization.kubernetes.io/autoupdate")
    if auto in {"true", "false"}:
        annotations.pop("rbac.authorization.kubernetes.io/autoupdate")
        annotations["openshift.io/reconcile-protect"] = (
            "false" if auto == "true" else "true"
        )
    return value


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "uid",
        "role",
        "subject",
        "mirrors",
        "metadata",
        "missing",
        "duplicate",
        "unknown_group",
    ],
)
def test_openshift_rolebinding_alias_requires_complete_semantic_identity(
    cleanup_case, fault
):
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.launch()
    group = (
        "authorization.openshift.io"
        if fault != "unknown_group"
        else "unknown.openshift.io"
    )
    version = group + "/v1"
    q.api.data["/apis"]["groups"].append(
        dict(name=group, versions=[dict(version="v1", groupVersion=version)])
    )
    q.api.data["/apis/" + version] = dict(
        kind="APIResourceList",
        groupVersion=version,
        resources=[
            dict(
                name="rolebindings", kind="RoleBinding", namespaced=True, verbs=["list"]
            )
        ],
    )
    original = q.api.path

    def path(route, query=()):
        result = original(route, query)
        if route.startswith("/apis/" + version + "/namespaces/"):
            result["items"] = [_legacy_binding(v) for v in result["items"]]
            item = result["items"][0]
            item["apiVersion"] = version
            if fault == "uid":
                item["metadata"]["uid"] = "foreign-alias"
            elif fault == "role":
                item["roleRef"]["name"] = "foreign-role"
            elif fault == "subject":
                item["subjects"][0]["name"] = "foreign-sa"
            elif fault == "mirrors":
                item["userNames"] = ["foreign-user"]
            elif fault == "metadata":
                item["metadata"]["labels"] = {"foreign": "true"}
            elif fault == "missing":
                result["items"].pop()
            elif fault == "duplicate":
                result["items"].append(deepcopy(item))
        return result

    q.api.path = path
    q.request()
    with q.sessions() as db:
        if fault:
            with pytest.raises(AccountingUnavailable):
                cleanup.advance(db, q.api, q.cleanup_id)
            assert not q.deletes
        else:
            work = cleanup.advance(db, q.api, q.cleanup_id)
            assert work.phase == "namespace_delete_requested"
            assert (
                len([v for v in work.inventory.objects if v.kind == "RoleBinding"]) == 3
            )
            assert all(
                v.api_version == "rbac.authorization.k8s.io/v1"
                for v in work.inventory.objects
                if v.kind == "RoleBinding"
            )


@pytest.mark.parametrize("role_kind", ["Role", "ClusterRole"])
@pytest.mark.parametrize("auto", ["true", "false", "other"])
def test_rolebinding_alias_conversion_annotations_scope_and_subjects(role_kind, auto):
    from app.services.native_origin_namespace_cleanup import _validate_rolebinding_alias

    value = dict(
        apiVersion="rbac.authorization.k8s.io/v1",
        kind="RoleBinding",
        metadata=dict(
            name="binding",
            namespace="ns",
            uid="uid",
            resourceVersion="1",
            annotations={
                "rbac.authorization.kubernetes.io/autoupdate": auto,
                "original": "preserved",
            },
        ),
        roleRef=dict(apiGroup="rbac.authorization.k8s.io", kind=role_kind, name="role"),
        subjects=[
            dict(kind="ServiceAccount", namespace="ns", name="default"),
            dict(kind="User", apiGroup="rbac.authorization.k8s.io", name="alice"),
            dict(
                kind="Group",
                apiGroup="rbac.authorization.k8s.io",
                name="system:serviceaccounts:ns",
            ),
        ],
    )
    legacy = _legacy_binding(value)
    _validate_rolebinding_alias(value, legacy)
    legacy["roleRef"]["namespace"] = "other-namespace"
    with pytest.raises(AccountingUnavailable):
        _validate_rolebinding_alias(value, legacy)


def test_support_manifest_pins_rendered_secret_bytes_without_disclosure():
    from app.services import native_origin_support as support

    bodies = support.render(
        "original-ns",
        "managed",
        {"secret_access_key": "private-test-value"},
        None,
        None,
    )
    manifest = support.manifest(bodies)
    assert len(manifest) == 4
    assert "private-test-value" not in repr(manifest)
    secret = next(v for v in bodies if v["kind"] == "Secret")
    observed = deepcopy(secret)
    observed["metadata"]["uid"] = "original-secret-uid"
    observed["data"] = {
        k: base64.b64encode(v.encode()).decode()
        for k, v in observed.pop("stringData").items()
    }
    receipt = support.readback(secret, observed)
    assert receipt["uid"] == "original-secret-uid"
    assert "private-test-value" not in repr(receipt)
    observed["data"]["secretKey"] = base64.b64encode(b"changed-private-value").decode()
    with pytest.raises(AccountingUnavailable, match="support") as error:
        support.readback(secret, observed)
    assert "private" not in str(error.value)


def test_inventory_binding_is_never_rendered_for_external_namespace():
    from app.services import native_origin_support as support

    bodies = support.render("external", "external", {}, None, None)
    assert all(v["metadata"]["name"] != "troshka-origin-inventory" for v in bodies)


def test_inventory_role_has_list_only_and_exact_bind_bootstrap():
    from pathlib import Path

    import yaml

    docs = list(
        yaml.safe_load_all(
            (Path(__file__).parents[3] / "infra/ocpvirt-rbac.yaml").read_text()
        )
    )
    role = next(
        d
        for d in docs
        if d.get("metadata", {}).get("name") == "troshka-origin-inventory"
    )
    assert role["kind"] == "ClusterRole"
    assert role["rules"] == [
        {"apiGroups": ["*"], "resources": ["*"], "verbs": ["list"]}
    ]
    assert not any(
        d.get("kind") == "ClusterRoleBinding"
        and d.get("roleRef", {}).get("name") == role["metadata"]["name"]
        for d in docs
    )
    provider = next(
        d
        for d in docs
        if d.get("kind") == "ClusterRole"
        and d["metadata"]["name"] == "troshka-provider"
    )
    rules = [
        r
        for r in provider["rules"]
        if "troshka-origin-inventory" in r.get("resourceNames", [])
    ]
    assert len(rules) == 1 and rules[0]["verbs"] == ["bind"]


def test_clean_install_and_release_apply_inventory_role_without_bulk_binding():
    import contextlib
    import io
    import sys
    from pathlib import Path
    from unittest.mock import patch

    import yaml

    from app.services.providers.kubevirt import _load_provider_rbac_docs

    root = Path(__file__).parents[3]
    assert any(
        d["kind"] == "ClusterRole"
        and d["metadata"]["name"] == "troshka-origin-inventory"
        for d in _load_provider_rbac_docs()
    )
    script = (root / "scripts/deploy-full.sh").read_text()
    # Execute the actual source filter with a local fixture argv, never the shell/release workflow.
    source = script.split(
        "python3 - infra/ocpvirt-rbac.yaml >\"$STATE/provider-rbac.yaml\" <<'PY'\n", 1
    )[1].split("\nPY", 1)[0]
    out = io.StringIO()
    with patch.object(
        sys, "argv", ["-", str(root / "infra/ocpvirt-rbac.yaml")]
    ), contextlib.redirect_stdout(out):
        exec(compile(source, "release-provider-filter", "exec"), {})
    docs = list(yaml.safe_load_all(out.getvalue()))
    assert any(d["metadata"]["name"] == "troshka-origin-inventory" for d in docs)
    assert not any(d["kind"] == "SecurityContextConstraints" for d in docs)
    loop = script.split("for role in ", 1)[1].split("done", 1)[0]
    assert "troshka-origin-inventory" not in loop
    assert (
        'oc apply -f "$MANIFEST"'
        in (root / "scripts/apply-ocpvirt-rbac.sh").read_text()
    )


@pytest.mark.parametrize(
    "fault", [None, "forbidden", "not_found", "oversized", "deadline", "aggregate"]
)
def test_inventory_transport_is_bounded_and_only_explicit_404_means_absence(
    fault, monkeypatch
):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import native_origin_inventory_transport
    from app.services.native_origin_namespace_cleanup import InventoryClient

    response = Mock(
        status=403 if fault == "forbidden" else 404 if fault == "not_found" else 200
    )
    response.read.return_value = (
        b"x" * (4 * 1024 * 1024 + 1)
        if fault == "oversized"
        else b'{"metadata":{"uid":"original"}}'
    )
    transport = Mock(return_value=response)
    client = SimpleNamespace(
        default_headers={},
        configuration=SimpleNamespace(host="https://fixture.invalid"),
        update_params_for_auth=Mock(),
        rest_client=SimpleNamespace(pool_manager=SimpleNamespace(request=transport)),
    )
    api = InventoryClient(client)

    def read(pool, url, *, deadline, **options):
        observed = pool.request("GET", url, **options)
        try:
            return observed.status, observed.read()
        finally:
            observed.close()

    monkeypatch.setattr(native_origin_inventory_transport, "read", read)
    if fault == "deadline":
        api.deadline = 0
    if fault == "aggregate":
        api.response_bytes = 32 * 1024 * 1024
    if fault in {"forbidden", "oversized", "deadline", "aggregate"}:
        with pytest.raises(AccountingUnavailable):
            api.get("Namespace", None, "original")
    else:
        result = api.get("Namespace", None, "original")
        assert (
            result is None
            if fault == "not_found"
            else result["metadata"]["uid"] == "original"
        )
    if fault != "deadline":
        assert (
            transport.call_args.kwargs["retries"] is False
            and transport.call_args.kwargs["redirect"] is False
        )
        assert transport.call_args.kwargs["timeout"].total <= 5
        response.close.assert_called_once()
    if fault is None:
        api.delete_namespace("original", "uid-pinned", "rv-pinned")
        body = json.loads(transport.call_args.kwargs["body"])
        assert body["preconditions"] == {
            "uid": "uid-pinned",
            "resourceVersion": "rv-pinned",
        }
        assert "finalizers" not in body


@pytest.mark.parametrize("stream", ["body", "headers", "tls"])
def test_actual_urllib3_slow_stream_deadline_closes_connection(stream):
    import socket
    import threading
    import time
    from types import SimpleNamespace
    from unittest.mock import Mock

    import urllib3

    from app.services.native_origin_namespace_cleanup import InventoryClient

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    closed, stop = threading.Event(), threading.Event()

    def serve():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(2)
            conn.recv(4096)
            prefix = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
                if stream == "body"
                else b"HTTP/1.1 200 OK\r\nX-Test: "
            )
            if stream != "tls":
                conn.sendall(prefix)
            for _ in range(30):
                if stop.wait(0.03):
                    break
                try:
                    if stream != "tls":
                        conn.sendall(b" ")
                    else:
                        if conn.recv(1) == b"":
                            closed.set()
                            return
                except (BrokenPipeError, ConnectionResetError):
                    closed.set()
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    pool = urllib3.PoolManager(cert_reqs="CERT_NONE")
    client = SimpleNamespace(
        default_headers={},
        configuration=SimpleNamespace(
            host=f"{'https' if stream == 'tls' else 'http'}://127.0.0.1:{server.getsockname()[1]}"
        ),
        update_params_for_auth=Mock(),
        rest_client=SimpleNamespace(pool_manager=pool),
    )
    api = InventoryClient(client)
    start = time.monotonic()
    api.deadline = start + 0.2
    try:
        with pytest.raises(AccountingUnavailable, match="deadline"):
            api.path("/api")
        assert time.monotonic() - start < 0.6
        assert closed.wait(1), "deadline must shut down the actual connection"
    finally:
        stop.set()
        pool.clear()
        server.close()
        thread.join(2)


def test_stalled_resolvers_retain_capacity_and_cannot_dispatch_late(monkeypatch):
    import socket
    import threading
    import time
    from unittest.mock import Mock

    import urllib3

    from app.services import native_origin_inventory_transport as transport

    # Use an isolated capacity semaphore so the assertion includes actual exit.
    capacity = threading.BoundedSemaphore(2)
    monkeypatch.setattr(transport, "_CAPACITY", capacity)
    release = threading.Event()
    entered = []

    def resolver(*args):
        entered.append(args)
        assert release.wait(3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    create = Mock(
        side_effect=AssertionError("cancelled DNS must never create/connect a socket")
    )
    monkeypatch.setattr(socket, "socket", create)
    pool = urllib3.PoolManager()
    options = dict(
        preload_content=False,
        retries=False,
        redirect=False,
        timeout=urllib3.Timeout(total=1),
    )
    try:
        for _ in range(2):
            start = time.monotonic()
            with pytest.raises(AccountingUnavailable, match="deadline"):
                transport.read(
                    pool, "http://resolver.invalid/api", deadline=start + 0.1, **options
                )
            assert time.monotonic() - start < 0.5
        assert len(entered) == 2
        with pytest.raises(AccountingUnavailable, match="capacity"):
            transport.read(
                pool,
                "http://resolver.invalid/api",
                deadline=time.monotonic() + 1,
                **options,
            )
        assert len(entered) == 2 and not capacity.acquire(blocking=False)
    finally:
        release.set()
        assert capacity.acquire(timeout=2)
        assert capacity.acquire(timeout=2)
        capacity.release()
        capacity.release()
        pool.clear()
    create.assert_not_called()


def test_inventory_request_pool_preserves_proxy_tls_settings():
    import ssl

    import urllib3

    from app.services.native_origin_inventory_transport import _pool, _Request

    context = ssl.create_default_context()
    original = urllib3.ProxyManager(
        "https://proxy.invalid:8443",
        proxy_headers={"Proxy-Authorization": "test"},
        proxy_ssl_context=context,
        ca_certs="original-ca",
        cert_file="original-cert",
        key_file="original-key",
        assert_hostname="original-name",
    )
    local = _pool(original, _Request(0))
    assert local.connection_pool_kw == original.connection_pool_kw
    assert (
        local.proxy == original.proxy and local.proxy_headers == original.proxy_headers
    )
    assert (
        local.proxy_config == original.proxy_config
        and local.pools is not original.pools
    )


def test_origin_transport_does_not_route_unknown_support_kinds_to_root():
    from unittest.mock import Mock

    from app.services.providers.kubevirt import _origin_api

    custom, core, serializer = Mock(), Mock(), Mock()
    api = _origin_api(custom, core, serializer)
    for action in (
        lambda: api.get("Unknown", "ns", "x"),
        lambda: api.post("Unknown", {"metadata": {"namespace": "ns"}}),
    ):
        with pytest.raises(ValueError, match="Unsupported"):
            action()
    assert not custom.mock_calls


@pytest.mark.parametrize(
    "default", ["ca", "sa", "pull_secret", "binding", "unknown_secret", "wrong_ca"]
)
def test_only_supported_bootstrap_shapes_are_admitted(cleanup_case, default):
    from app.services import native_origin_namespace_cleanup as cleanup
    from app.services.native_managed_power import helpers

    q = cleanup_case
    q.launch()
    q.request()
    sa = dict(
        apiVersion="v1",
        kind="ServiceAccount",
        metadata=dict(name="default", namespace=q.namespace, uid="sa-uid"),
    )
    if default in {"ca", "wrong_ca"}:
        value = dict(
            apiVersion="v1",
            kind="ConfigMap",
            metadata=dict(
                name="openshift-service-ca.crt",
                namespace=q.namespace,
                uid="default-ca-uid",
                annotations={"service.beta.openshift.io/inject-cabundle": "true"},
            ),
            data={"service-ca.crt": "ca"},
        )
        if default == "wrong_ca":
            value["data"]["unexpected"] = "custom"
    elif default == "sa":
        value = sa
    elif default == "binding":
        value = dict(
            apiVersion="rbac.authorization.k8s.io/v1",
            kind="RoleBinding",
            metadata=dict(
                name="system:image-pullers",
                namespace=q.namespace,
                uid="default-grant-uid",
            ),
            **helpers().cleanup_grant_body("system:image-pullers", q.namespace),
        )
    else:
        value = dict(
            apiVersion="v1",
            kind="Secret",
            metadata=dict(
                name="default-dockercfg", namespace=q.namespace, uid="pull-secret-uid"
            ),
            type="kubernetes.io/dockercfg",
            data={".dockercfg": base64.b64encode(b"{}").decode()},
        )
        if default == "pull_secret":
            sa["metadata"]["annotations"] = {
                "openshift.io/internal-registry-pull-secret-ref": "default-dockercfg"
            }
            q.objects[("ServiceAccount", "default")] = sa
            value["metadata"].update(
                ownerReferences=[
                    dict(
                        apiVersion="v1",
                        kind="ServiceAccount",
                        name="default",
                        uid="sa-uid",
                    )
                ],
                annotations={
                    "openshift.io/internal-registry-auth-token.service-account": "default",
                    "openshift.io/internal-registry-auth-token.binding": "bound",
                },
            )
    q.objects[(value["kind"], value["metadata"]["name"])] = value
    with q.sessions() as db:
        if default in {"unknown_secret", "wrong_ca"}:
            with pytest.raises(AccountingUnavailable, match="unknown"):
                cleanup.advance(db, q.api, q.cleanup_id)
            assert not q.deletes
        else:
            assert (
                cleanup.advance(db, q.api, q.cleanup_id).phase
                == "namespace_delete_requested"
            )


def test_late_platform_default_requires_new_persisted_seal(cleanup_case, monkeypatch):
    from app.models.native_origin_namespace import NativeOriginNamespaceEvidence
    from app.services import native_origin_namespace_cleanup as cleanup

    q = cleanup_case
    q.launch()
    q.request()
    original_seal = cleanup.seal
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("worker exits after seal publication")
        return original_seal(*args)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "seal", interrupted)
        with q.sessions() as db, pytest.raises(TimeoutError):
            cleanup.advance(db, q.api, q.cleanup_id)
    assert not q.deletes
    q.objects[("ServiceAccount", "default")] = dict(
        apiVersion="v1",
        kind="ServiceAccount",
        metadata=dict(name="default", namespace=q.namespace, uid="late-sa-uid"),
    )
    with q.sessions() as db:
        assert (
            cleanup.advance(db, q.api, q.cleanup_id).phase
            == "namespace_delete_requested"
        )
        assert (
            len(
                [
                    r
                    for r in db.query(NativeOriginNamespaceEvidence)
                    if r.phase.startswith("sealed")
                ]
            )
            == 2
        )
