"""Creation receipts are independent of admission and survive the project."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_origin_database,
)

isolated_quota_database = isolated_origin_database


def root_list_adapter(response):
    """Model existing provider LIST access without a namespace's named GET grant."""
    from kubernetes.client.exceptions import ApiException

    from app.services.providers.kubevirt import _origin_api

    def forbidden_get(**kwargs):
        raise ApiException(status=403, reason="Named GET needs namespace binding")

    def list_root(**kwargs):
        assert kwargs == dict(
            group="troshka.redhat.com",
            version="v1alpha1",
            namespace="project-namespace",
            plural="troshkaprojects",
            field_selector="metadata.name=project-root",
            limit=2,
            _request_timeout=10,
        )
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    return _origin_api(
        SimpleNamespace(
            get_namespaced_custom_object=forbidden_get,
            list_namespaced_custom_object=list_root,
        ),
        None,
        None,
    )


@pytest.mark.parametrize("present", [False, True])
def test_origin_root_list_bootstraps_without_named_get_permission(present):
    root = {
        "metadata": {
            "name": "project-root",
            "namespace": "project-namespace",
            "uid": "root-uid",
        }
    }
    response = {
        "metadata": {"resourceVersion": "17"},
        "items": [root] if present else [],
    }
    assert root_list_adapter(response).get(
        "TroshkaProject", "project-namespace", "project-root"
    ) == (root if present else None)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"metadata": {"resourceVersion": "17"}},
        {"metadata": {}, "items": []},
        {"metadata": {"resourceVersion": "17", "continue": "next-page"}, "items": []},
        {"metadata": {"resourceVersion": "17", "remainingItemCount": 1}, "items": []},
        {"metadata": {"resourceVersion": "17"}, "items": {}},
        {"metadata": {"resourceVersion": "17"}, "items": [None]},
        {
            "metadata": {"resourceVersion": "17"},
            "items": [
                {
                    "metadata": {
                        "name": "other",
                        "namespace": "project-namespace",
                        "uid": "u",
                    }
                },
            ],
        },
        {
            "metadata": {"resourceVersion": "17"},
            "items": [
                {
                    "metadata": {
                        "name": "project-root",
                        "namespace": "other",
                        "uid": "u",
                    }
                },
            ],
        },
        {
            "metadata": {"resourceVersion": "17"},
            "items": [
                {
                    "metadata": {
                        "name": "project-root",
                        "namespace": "project-namespace",
                    }
                },
            ],
        },
        {
            "metadata": {"resourceVersion": "17"},
            "items": [
                {
                    "metadata": {
                        "name": "project-root",
                        "namespace": "project-namespace",
                        "uid": "a",
                    }
                },
                {
                    "metadata": {
                        "name": "project-root",
                        "namespace": "project-namespace",
                        "uid": "b",
                    }
                },
            ],
        },
    ],
)
def test_origin_root_list_never_interprets_incomplete_or_foreign_results_as_absence(
    response,
):
    with pytest.raises(AccountingUnavailable):
        root_list_adapter(response).get(
            "TroshkaProject", "project-namespace", "project-root"
        )


@pytest.mark.parametrize("failure", ["forbidden", "not-found", "timeout"])
def test_origin_root_list_propagates_api_uncertainty(failure):
    from kubernetes.client.exceptions import ApiException

    error = (
        TimeoutError("unavailable")
        if failure == "timeout"
        else ApiException(
            status=403 if failure == "forbidden" else 404, reason="unavailable"
        )
    )
    with pytest.raises(type(error)) as raised:
        root_list_adapter(error).get(
            "TroshkaProject", "project-namespace", "project-root"
        )
    assert raised.value is error


@pytest.fixture
def origin_case(isolated_quota_database):
    sessions = isolated_quota_database
    with sessions() as db:
        owner = User(email=f"origin-{uuid4()}@example.test")
        provider = Provider(name=str(uuid4()), type="kubevirt")
        db.add_all([owner, provider])
        db.flush()
        host = Host(provider_id=provider.id, host_type="kubevirt-cluster")
        db.add(host)
        db.flush()
        project = Project(
            name="origin", owner_id=owner.id, host_id=host.id, state="deploying"
        )
        db.add(project)
        db.commit()
        yield SimpleNamespace(
            sessions=sessions, project_id=project.id, provider_id=provider.id
        )


def service():
    from app.services import native_deployment_origin

    return native_deployment_origin


def descriptor(**changes):
    return dict(
        cluster_uid="cluster-uid",
        namespace="project-namespace",
        root_name="project-root",
        namespace_mode="managed",
        cr_spec_digest="a" * 64,
        support_intents=[],
        **changes,
    )


def prepare(q):
    with q.sessions() as db:
        intent = service().prepare(
            db,
            db.get(Project, q.project_id),
            db.get(Provider, q.provider_id),
            descriptor(),
        )
        db.commit()
        return intent


def observation(intent, *, root=False):
    value = dict(
        cluster_uid=intent.cluster_uid,
        namespace={
            "metadata": {
                "name": intent.namespace,
                "uid": "namespace-uid",
                "annotations": service().annotations(intent),
            }
        },
    )
    if root:
        value["root"] = {
            "metadata": {
                "name": intent.root_name,
                "namespace": intent.namespace,
                "uid": "root-uid",
                "generation": 1,
                "annotations": service().annotations(intent),
            },
            "spec": {"projectId": str(intent.project_id)},
        }
    return value


def test_prepare_retry_keeps_original_token_and_digest(origin_case):
    q = origin_case
    first = prepare(q)
    assert prepare(q) == first
    with q.sessions() as db:
        other = descriptor()
        other["operation_token"] = str(uuid4())
        with pytest.raises(AccountingUnavailable, match="intent"):
            service().prepare(
                db,
                db.get(Project, q.project_id),
                db.get(Provider, q.provider_id),
                other,
            )


@pytest.mark.parametrize(
    "field", ["cluster", "token", "namespace_uid", "root_uid", "spec"]
)
def test_binding_and_recovery_reject_changed_original_identity(origin_case, field):
    q = origin_case
    from app.services.kubevirt_storage_enrollment import digest

    original = descriptor()
    original["cr_spec_digest"] = digest({"projectId": q.project_id})
    with q.sessions() as db:
        intent = service().prepare(
            db, db.get(Project, q.project_id), db.get(Provider, q.provider_id), original
        )
        db.commit()
    ns, root = observation(intent), observation(intent, root=True)
    with q.sessions() as db:
        service().bind_namespace(db, str(intent.id), ns)
        service().bind_root(db, str(intent.id), root)
        db.commit()
    bad = deepcopy(root)
    if field == "cluster":
        bad["cluster_uid"] = "other"
    if field == "token":
        bad["root"]["metadata"]["annotations"][service().TOKEN] = str(uuid4())
    if field == "namespace_uid":
        bad["namespace"]["metadata"]["uid"] = "replacement"
    if field == "root_uid":
        bad["root"]["metadata"]["uid"] = "replacement"
    if field == "spec":
        bad["root"]["spec"]["action"] = "destroy"
    with q.sessions() as db, pytest.raises(AccountingUnavailable):
        service().bind_root(db, str(intent.id), bad)


def test_cancellation_fences_late_namespace_response(origin_case):
    q = origin_case
    intent = prepare(q)
    with q.sessions() as db:
        service().begin_io(db, str(intent.id), "namespace")
        db.commit()
    with q.sessions() as db:
        pointer = service().cancel(db, q.project_id)
        assert pointer.io_phase == "namespace"
        db.commit()
    with q.sessions() as db, pytest.raises(AccountingUnavailable, match="cancel"):
        service().bind_namespace(db, str(intent.id), observation(intent))


def test_receipts_survive_project_deletion_and_are_immutable(origin_case):
    q = origin_case
    intent = prepare(q)
    with q.sessions() as db:
        service().bind_namespace(db, str(intent.id), observation(intent))
        db.commit()
        db.delete(db.get(Project, q.project_id))
        db.commit()
        retained = service().receipts(db, str(intent.id))
        assert retained[0].namespace_uid == "namespace-uid"
        from app.models.native_deployment_origin import NativeDeploymentOriginIntent

        row = db.get(NativeDeploymentOriginIntent, str(intent.id))
        row.digest = "b" * 64
        with pytest.raises(ValueError, match="immutable"):
            db.flush()


def test_unavailable_job_tracking_never_proves_idle(monkeypatch):
    from app.core import redis

    monkeypatch.setattr(redis, "is_redis_available", lambda: False)
    with pytest.raises(AccountingUnavailable, match="tracking"):
        service().require_idle(str(uuid4()))


@pytest.fixture
def provider_io(origin_case, monkeypatch):
    from kubernetes.client.exceptions import ApiException

    from app.services.providers import kubevirt as driver

    q = origin_case
    objects, events = {}, []

    class Api:
        def get(self, kind, namespace, name):
            if name == "kube-system":
                return {"metadata": {"name": name, "uid": "cluster-uid"}}
            return deepcopy(objects.get((kind, name)))

        def post(self, kind, body):
            with q.sessions() as db:
                assert (
                    service().active(db, q.project_id) is not None
                ), "provider POST before committed intent"
                if kind != "Namespace":
                    assert (
                        service().receipts(
                            db, str(service().active(db, q.project_id).id)
                        )[0]
                        is not None
                    )
            key = kind, body["metadata"]["name"]
            if key in objects:
                raise ApiException(status=409, reason="AlreadyExists")
            body = deepcopy(body)
            body["metadata"].update(uid=f"{kind}-uid", generation=1)
            objects[key] = body
            events.append(kind)
            if q.fault == kind:
                raise TimeoutError("lost response")
            return deepcopy(body)

    q.api, q.events, q.objects, q.fault = Api(), events, objects, None
    monkeypatch.setattr(service(), "capture_enabled", lambda: True)
    monkeypatch.setattr(driver, "_ensure_operator_crds", lambda p: None)
    monkeypatch.setattr(driver, "_origin_api", lambda *a: q.api)
    monkeypatch.setattr(driver, "_get_k8s_clients", lambda p: (None, None, None))
    monkeypatch.setattr(driver, "_resolve_project_s3_config", lambda *a: (None, None))
    monkeypatch.setattr(
        driver, "_write_project_s3_secrets", lambda *a: events.append("Secrets")
    )
    monkeypatch.setattr(
        driver, "_origin_namespace_grants", lambda *a: events.append("RoleBinding")
    )
    monkeypatch.setattr(
        driver, "_ensure_cache_s3_secrets", lambda *a: events.append("SharedCache")
    )
    monkeypatch.setattr(driver, "base_namespace_grants_mode", lambda: "managed")
    return q


@pytest.fixture
def provider_list_io(provider_io, monkeypatch):
    """Use the real adapter, with API authorization enforced at its boundary."""
    from kubernetes.client.exceptions import ApiException

    from app.services.providers import kubevirt

    q = provider_io

    def read(kind, name):
        value = q.api.get(kind, None, name)
        if value is None:
            raise ApiException(status=404)
        return value

    class CustomApi:
        def get_namespaced_custom_object(self, **kwargs):
            binding = ("RoleBinding", "troshka-provider-namespaced")
            if binding not in q.objects:
                raise ApiException(status=403, reason="Namespace binding absent")
            return read("TroshkaProject", kwargs["name"])

        def list_namespaced_custom_object(self, **kwargs):
            assert kwargs["plural"] == "troshkaprojects"
            assert kwargs["limit"] == 2 and kwargs["_request_timeout"] == 10
            name = kwargs["field_selector"].removeprefix("metadata.name=")
            value = q.api.get("TroshkaProject", kwargs["namespace"], name)
            return {
                "metadata": {"resourceVersion": "17"},
                "items": [value] if value else [],
            }

        def create_namespaced_custom_object(self, **kwargs):
            assert ("RoleBinding", "troshka-provider-namespaced") in q.objects
            return q.api.post("TroshkaProject", kwargs["body"])

    core = SimpleNamespace(
        read_namespace=lambda name: read("Namespace", name),
        create_namespace=lambda body: q.api.post("Namespace", body),
    )
    adapter = kubevirt._OriginApi(
        CustomApi(),
        core,
        SimpleNamespace(sanitize_for_serialization=lambda value: value),
    )

    def support_method(kind, action):
        if action == "read":
            return lambda namespace, name: read(kind, name)
        assert action == "create"
        return lambda namespace, body: q.api.post(kind, body)

    monkeypatch.setattr(adapter, "_support_method", support_method)
    monkeypatch.setattr(kubevirt, "_origin_api", lambda *args: adapter)
    q.adapter = adapter
    return q


@pytest.mark.parametrize("restart", [False, True])
def test_origin_root_list_supports_first_deploy_and_recovery_before_namespace_grants(
    provider_list_io, monkeypatch, restart
):
    from app.services.providers.kubevirt import KubeVirtDriver

    q = provider_list_io
    if restart:
        original_begin = service().begin_io

        def interrupt_before_support(db, intent_id, phase):
            if phase == "support":
                raise TimeoutError("interrupted before first binding")
            return original_begin(db, intent_id, phase)

        with monkeypatch.context() as interrupted:
            interrupted.setattr(service(), "begin_io", interrupt_before_support)
            with q.sessions() as db, pytest.raises(
                TimeoutError, match="before first binding"
            ):
                KubeVirtDriver().deploy_project(
                    db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
                )
        assert q.events == ["Namespace"]
    with q.sessions() as db:
        name = KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
        intent = service().active(db, q.project_id)
        namespace_receipt, root_receipt = service().receipts(db, str(intent.id))
        assert namespace_receipt.namespace_uid == "Namespace-uid"
        assert (
            root_receipt.root_name == name
            and root_receipt.root_uid == "TroshkaProject-uid"
        )
    assert q.events == [
        "Namespace",
        "RoleBinding",
        "RoleBinding",
        "RoleBinding",
        "Secret",
        "SharedCache",
        "TroshkaProject",
    ]


def test_origin_root_list_rejects_historical_root_before_any_write(
    provider_list_io, monkeypatch
):
    from app.services.providers import kubevirt

    q = provider_list_io
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *args: "project-namespace")
    root_name = "project-" + q.project_id[:8]
    q.objects["TroshkaProject", root_name] = {
        "metadata": {
            "name": root_name,
            "namespace": "project-namespace",
            "uid": "historical-root",
        }
    }
    with q.sessions() as db, pytest.raises(
        AccountingUnavailable, match="Historical native root"
    ):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    assert q.events == []
    with q.sessions() as db:
        assert service().active(db, q.project_id) is None


def test_origin_root_list_preserves_post_readback_uid_validation():
    from app.services.providers.kubevirt import _origin_post

    root = {
        "metadata": {
            "name": "project-root",
            "namespace": "project-namespace",
            "uid": "replacement",
        }
    }
    adapter = root_list_adapter(
        {"metadata": {"resourceVersion": "17"}, "items": [root]}
    )
    adapter.custom_api.create_namespaced_custom_object = lambda **kwargs: {
        "metadata": {"uid": "original"}
    }
    with pytest.raises(AccountingUnavailable, match="POST/readback UID changed"):
        _origin_post(
            adapter,
            "TroshkaProject",
            {
                "metadata": {"name": "project-root", "namespace": "project-namespace"},
                "spec": {},
            },
        )


@pytest.mark.parametrize("fault", [None, "Namespace", "TroshkaProject"])
def test_provider_commits_intent_and_namespace_before_any_child_write(
    provider_io, fault
):
    q = provider_io
    from app.models.native_deployment_origin import NativeDeploymentOriginIntent
    from app.services.providers.kubevirt import KubeVirtDriver

    q.fault = fault
    with q.sessions() as db:
        provider = db.get(Provider, q.provider_id)
        name = KubeVirtDriver().deploy_project(
            provider,
            q.project_id,
            {"nodes": []},
            {"secret_access_key": "secret-value"},
            db=db,
        )
        intent = service().active(db, q.project_id)
        ns, root = service().receipts(db, str(intent.id))
        assert ns is not None and root.root_name == name
        assert q.events == [
            "Namespace",
            "RoleBinding",
            "RoleBinding",
            "RoleBinding",
            "Secret",
            "SharedCache",
            "TroshkaProject",
        ]
        assert "secret-value" not in str(
            db.get(NativeDeploymentOriginIntent, str(intent.id)).payload
        )
    # A quota/capture toggle cannot redispatch the original generic attempt.
    from unittest.mock import Mock

    from app.services import native_managed_deploy, quota_accounting

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as changes:
        changes.setattr(service(), "capture_enabled", lambda: False)
        changes.setattr(quota_accounting, "quotas_enabled", lambda: True)
        managed = Mock(side_effect=AssertionError("generic attempt changed mode"))
        changes.setattr(native_managed_deploy, "create_pending", managed)
        with q.sessions() as db:
            assert (
                KubeVirtDriver().deploy_project(
                    db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
                )
                == name
            )
        managed.assert_not_called()
    assert q.events == [
        "Namespace",
        "RoleBinding",
        "RoleBinding",
        "RoleBinding",
        "Secret",
        "SharedCache",
        "TroshkaProject",
    ]


@pytest.mark.parametrize(
    "phase", ["namespace", "grants", "secrets", "root", "root_bind"]
)
def test_creation_crash_restarts_only_same_intent(provider_io, monkeypatch, phase):
    q = provider_io
    from app.services import native_origin_support as support
    from app.services.providers import kubevirt

    original_post = q.api.post
    original_finish = support.finish
    original_bind = service().bind_root
    fired = False

    def fail_once(current):
        nonlocal fired
        if current == phase and not fired:
            fired = True
            raise TimeoutError("worker interrupted")

    def post(kind, body):
        if kind in {"Namespace", "TroshkaProject"}:
            fail_once("namespace" if kind == "Namespace" else "root")
        return original_post(kind, body)

    def finish(db, intent, token, observed):
        if observed:
            fail_once("grants" if observed["kind"] == "RoleBinding" else "secrets")
        return original_finish(db, intent, token, observed)

    def bind(*args, **kwargs):
        fail_once("root_bind")
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(q.api, "post", post)
    monkeypatch.setattr(support, "finish", finish)
    monkeypatch.setattr(service(), "bind_root", bind)
    monkeypatch.setattr(service(), "require_idle", lambda *_: None)
    with q.sessions() as db, pytest.raises(TimeoutError):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {"nodes": []}, {}, db=db
        )
    with q.sessions() as db:
        first = service().active(db, q.project_id)
        if phase in {"grants", "secrets"}:
            with pytest.raises(AccountingUnavailable, match="support handoff"):
                kubevirt.KubeVirtDriver().deploy_project(
                    db.get(Provider, q.provider_id),
                    q.project_id,
                    {"nodes": []},
                    {},
                    db=db,
                )
            return
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {"nodes": []}, {}, db=db
        )
        assert service().active(db, q.project_id) == first
        assert service().receipts(db, str(first.id))[1] is not None
    assert q.events.count("Namespace") == 1
    assert q.events.count("TroshkaProject") == 1
    assert q.events.count("RoleBinding") == 3
    assert q.events.count("Secret") == 1


@pytest.mark.parametrize("fault", ["uid", "content", "provenance", "absent"])
def test_root_post_retry_never_replaces_original_support(
    provider_io, monkeypatch, fault
):
    from app.services.providers import kubevirt

    q = provider_io
    original_post = q.api.post

    def post(kind, body):
        if kind == "TroshkaProject":
            raise TimeoutError("root POST not sent")
        return original_post(kind, body)

    monkeypatch.setattr(q.api, "post", post)
    monkeypatch.setattr(service(), "require_idle", lambda *_: None)
    with q.sessions() as db, pytest.raises(TimeoutError):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    key = ("RoleBinding", "troshka-provider-namespaced")
    value = q.objects[key]
    if fault == "uid":
        value["metadata"]["uid"] = "foreign-uid"
    elif fault == "content":
        value["roleRef"]["name"] = "foreign-role"
    elif fault == "provenance":
        value["metadata"].pop("annotations")
    else:
        q.objects.pop(key)
    before = list(q.events)
    with q.sessions() as db, pytest.raises(AccountingUnavailable, match="support"):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    assert q.events == before


def test_late_cancelled_result_records_evidence_without_child_writes(
    provider_io, monkeypatch
):
    q = provider_io
    from app.services.providers import kubevirt

    original_post = q.api.post

    def cancel_on_post(kind, body):
        result = original_post(kind, body)
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()
        return result

    monkeypatch.setattr(q.api, "post", cancel_on_post)
    monkeypatch.setattr(service(), "require_idle", lambda *_: None)
    with q.sessions() as db, pytest.raises(AccountingUnavailable, match="cancel"):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    with q.sessions() as db:
        intent = service().active(db, q.project_id)
        service().reconcile_cancelled(db, q.api, intent)
        assert service().receipts(db, str(intent.id))[0] is not None
        from app.models.native_deployment_origin import NativeDeploymentOriginActive

        pointer = db.get(NativeDeploymentOriginActive, q.project_id)
        assert pointer.cancelled and pointer.io_phase is None
        with pytest.raises(AccountingUnavailable, match="cancel"):
            service().begin_io(db, str(intent.id), "support")
    assert q.events == ["Namespace"]


def test_cancelled_absence_does_not_settle_uncertain_post(origin_case, monkeypatch):
    q = origin_case
    intent = prepare(q)
    monkeypatch.setattr(service(), "require_idle", lambda *_: None)
    from app.models.native_deployment_origin import NativeDeploymentOriginActive

    with q.sessions() as db:
        service().begin_io(db, str(intent.id), "namespace")
        db.commit()
        service().cancel(db, q.project_id)
        db.commit()
        api = SimpleNamespace(
            get=lambda kind, namespace, name: (
                {"metadata": {"uid": intent.cluster_uid}}
                if name == "kube-system"
                else None
            )
        )
        service().reconcile_cancelled(db, api, intent)
        assert (
            db.get(NativeDeploymentOriginActive, q.project_id).io_phase == "namespace"
        )


@pytest.mark.parametrize("replacement", ["namespace", "root", "token", "spec"])
def test_existing_deploying_root_requires_exact_recovery(
    provider_io, monkeypatch, replacement
):
    q = provider_io
    from app.services import deploy_service
    from app.services.providers.kubevirt import KubeVirtDriver

    with q.sessions() as db:
        provider = db.get(Provider, q.provider_id)
        KubeVirtDriver().deploy_project(provider, q.project_id, {}, {}, db=db)
        intent = service().active(db, q.project_id)
    root = q.objects[("TroshkaProject", intent.root_name)]
    ns = q.objects[("Namespace", intent.namespace)]
    if replacement == "namespace":
        ns["metadata"]["uid"] = "new"
    if replacement == "root":
        root["metadata"]["uid"] = "new"
    if replacement == "token":
        root["metadata"]["annotations"][service().TOKEN] = "new"
    if replacement == "spec":
        root["spec"]["action"] = "destroy"
    from unittest.mock import Mock

    stale_replace = Mock(side_effect=AssertionError("name-only replacement"))
    monkeypatch.setattr(deploy_service, "_replace_stale_kubevirt_cr", stale_replace)
    before = list(q.events)
    with q.sessions() as db:
        result = deploy_service._kubevirt_create_or_resume_cr(
            db.get(Provider, q.provider_id),
            KubeVirtDriver(),
            q.project_id,
            db.get(Project, q.project_id),
            {},
            db,
            {},
            {},
            {},
            "",
        )
        assert result is None
        assert db.get(Project, q.project_id).state == "error"
    stale_replace.assert_not_called()
    assert q.events == before


def test_external_namespace_receipt_grants_no_delete_authority(
    provider_io, monkeypatch
):
    q = provider_io
    from app.services.providers import kubevirt

    namespace = "troshka-" + q.project_id[:8]
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *args: namespace)
    monkeypatch.setattr(kubevirt, "base_namespace_grants_mode", lambda: "external")
    q.objects[("Namespace", namespace)] = {
        "metadata": {"name": namespace, "uid": "external-uid"}
    }
    from app.services import native_origin_support as support

    for body in support.render(namespace, "external", {}, None, None):
        if body["kind"] == "RoleBinding":
            body["metadata"]["uid"] = "external-" + body["metadata"]["name"]
            q.objects[(body["kind"], body["metadata"]["name"])] = body
    with q.sessions() as db:
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
        intent = service().active(db, q.project_id)
        ns, root = service().receipts(db, str(intent.id))
        assert not ns.namespace_delete_authority
        assert not root.namespace_delete_authority
    assert "Namespace" not in q.events


def test_origin_migration_has_postgresql_types_and_immutable_facts(origin_case):
    q = origin_case
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    engine = q.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL DDL contract")
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/e12fc482a731_native_deployment_origin.py"
    )
    spec = importlib.util.spec_from_file_location("origin_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(
        MigrationContext.configure(connection)
    ):
        # Metadata fixture creates model tables; exercise deployment DDL itself.
        from sqlalchemy import UUID

        from app.models.native_deployment_origin import NativeDeploymentOriginIntent

        for name in (
            "native_deployment_origin_launches",
            "native_deployment_origin_active",
            "native_deployment_root_receipts",
            "native_deployment_namespace_receipts",
            "native_deployment_origin_intents",
        ):
            connection.execute(text(f"DROP TABLE {name}"))
        migration.upgrade()
        columns = inspect(connection).get_columns("native_deployment_origin_intents")
        assert all(
            isinstance(c["type"], UUID)
            for c in columns
            if c["name"] in {"id", "project_id", "attempt_id"}
        )
        row_id = str(uuid4())
        connection.execute(
            NativeDeploymentOriginIntent.__table__.insert().values(
                id=row_id,
                project_id=q.project_id,
                attempt_id=str(uuid4()),
                digest="a" * 64,
                payload={},
            )
        )
        for command in (
            "UPDATE native_deployment_origin_intents SET digest = 'changed'",
            "DELETE FROM native_deployment_origin_intents",
        ):
            with pytest.raises(
                DBAPIError, match="immutable"
            ), connection.begin_nested():
                connection.execute(text(command))
        migration.downgrade()
        migration.upgrade()


@pytest.mark.parametrize("racer", ["cancel", "owner", "provider", "retry"])
def test_postgres_identity_lock_serializes_origin_publication(
    origin_case, monkeypatch, racer
):
    q = origin_case
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services import quota_locks

    engine = q.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency contract")
    intent = prepare(q)
    waiting = Event()
    original_lock = quota_locks.lock_rows

    def announced(db, **kwargs):
        waiting.set()
        return original_lock(db, **kwargs)

    with q.sessions() as holder:
        quota_locks.lock_rows(
            holder, user_ids=(str(intent.owner_id),), project_ids=(q.project_id,)
        )
        monkeypatch.setattr(quota_locks, "lock_rows", announced)

        def worker():
            with q.sessions() as db:
                try:
                    service().bind_namespace(db, str(intent.id), observation(intent))
                    db.commit()
                    return "bound"
                except AccountingUnavailable:
                    return "held"

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            assert waiting.wait(timeout=5)
            if racer == "cancel":
                service().cancel(holder, q.project_id)
            elif racer == "owner":
                new_owner = User(email=f"replacement-{uuid4()}@example.test")
                holder.add(new_owner)
                holder.flush()
                holder.get(Project, q.project_id).owner_id = new_owner.id
            elif racer == "provider":
                replacement = Provider(name=str(uuid4()), type="kubevirt")
                holder.add(replacement)
                holder.flush()
                holder.get(Host, str(intent.host_id)).provider_id = replacement.id
            else:
                service().bind_namespace(holder, str(intent.id), observation(intent))
            holder.commit()
            assert future.result(timeout=8) == ("bound" if racer == "retry" else "held")


def test_outstanding_call_cannot_be_claimed_or_cleared_by_another_writer(origin_case):
    q = origin_case
    intent = prepare(q)
    with q.sessions() as db:
        token = service().begin_io(db, str(intent.id), "support")
        db.commit()
    with q.sessions() as db:
        with pytest.raises(AccountingUnavailable, match="unresolved"):
            service().begin_io(db, str(intent.id), "support")
        db.rollback()
        with pytest.raises(AccountingUnavailable, match="owner changed"):
            service().finish_io(db, str(intent.id), str(uuid4()))
        db.rollback()
        service().finish_io(db, str(intent.id), token)
        db.commit()


def test_live_original_writer_blocks_exact_matching_readback_recovery(
    provider_io, monkeypatch
):
    q = provider_io
    intent = prepare(q)
    q.objects[("Namespace", intent.namespace)] = observation(intent)["namespace"]
    with q.sessions() as db:
        service().begin_io(db, str(intent.id), "namespace")
        db.commit()
    from app.core import redis

    monkeypatch.setattr(redis, "is_redis_available", lambda: False)
    with q.sessions() as db, pytest.raises(AccountingUnavailable, match="tracking"):
        service().reconcile_cancelled(db, q.api, intent)
    assert q.events == []


def test_restart_uses_encrypted_original_generated_launch_material(
    provider_io, monkeypatch
):
    q = provider_io
    from app.models.native_deployment_origin import NativeDeploymentOriginLaunch
    from app.services.providers import kubevirt

    original_begin = service().begin_io
    first = True

    def before_grants(db, intent_id, phase):
        nonlocal first
        if phase == "support" and first:
            first = False
            raise TimeoutError("crash after namespace bind")
        return original_begin(db, intent_id, phase)

    monkeypatch.setattr(service(), "begin_io", before_grants)
    with q.sessions() as db, pytest.raises(TimeoutError):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id),
            q.project_id,
            {"generatedUrl": "https://import.invalid/?signature=original"},
            {"secret_access_key": "original-secret"},
            exec_ssh_key="original-private-key",
            db=db,
        )
    with q.sessions() as db:
        intent = service().active(db, q.project_id)
        retained = db.get(
            NativeDeploymentOriginLaunch, str(intent.id)
        ).encrypted_payload
        assert all(
            value not in retained
            for value in (
                "original-private-key",
                "original-secret",
                "signature=original",
            )
        )
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id),
            q.project_id,
            {"generatedUrl": "https://import.invalid/?signature=new"},
            {"secret_access_key": "changed-secret"},
            exec_ssh_key="changed-private-key",
            db=db,
        )
        root = q.objects[("TroshkaProject", intent.root_name)]
        assert root["spec"]["execSshKey"] == "original-private-key"
        assert root["spec"]["topology"]["generatedUrl"].endswith("signature=original")
        assert (
            service().receipts(db, str(intent.id))[1].cr_spec_digest
            == intent.cr_spec_digest
        )


def test_namespace_only_recovery_rechecks_namespace_after_root_absence(origin_case):
    intent = prepare(origin_case)
    original = observation(intent)["namespace"]
    reads = 0

    def get(kind, namespace, name):
        nonlocal reads
        if name == "kube-system":
            return {"metadata": {"uid": intent.cluster_uid}}
        if kind == "TroshkaProject":
            return None
        reads += 1
        value = deepcopy(original)
        if reads > 1:
            value["metadata"]["uid"] = "replacement"
        return value

    with pytest.raises(AccountingUnavailable, match="bracket"):
        service().recover(SimpleNamespace(get=get), intent, (None, None))


@pytest.mark.parametrize(
    "current_project,original_status,accepted",
    [
        ("selected", "failed", True),
        ("other", "failed", False),
        ("selected", "started", False),
    ],
)
def test_recovery_worker_tracks_own_job_and_terminal_original(
    monkeypatch, current_project, original_status, accepted
):
    from unittest.mock import Mock

    import rq
    from rq.job import Job

    from app.core import redis

    monkeypatch.setattr(redis, "is_redis_available", lambda: True)
    monkeypatch.setattr(
        redis, "get_redis", lambda: SimpleNamespace(get=lambda key: "recovery-job")
    )
    monkeypatch.setattr(
        rq,
        "get_current_job",
        lambda: SimpleNamespace(
            id="recovery-job", meta={"project_id": current_project}
        ),
    )
    monkeypatch.setattr(
        Job,
        "fetch",
        Mock(return_value=SimpleNamespace(get_status=lambda **kwargs: original_status)),
    )
    if accepted:
        service().require_idle("selected", "original-job")
    else:
        with pytest.raises(AccountingUnavailable):
            service().require_idle("selected", "original-job")


@pytest.mark.parametrize("change", ["uid", "generation", "spec"])
def test_first_root_bind_preserves_post_readback_identity(
    provider_io, monkeypatch, change
):
    q = provider_io
    from app.services.providers import kubevirt

    original_post = kubevirt._origin_post

    def replace_after_post(api, kind, body):
        result = original_post(api, kind, body)
        if kind == "TroshkaProject":
            value = q.objects[(kind, body["metadata"]["name"])]
            if change == "uid":
                value["metadata"]["uid"] = "replacement-root"
            elif change == "generation":
                value["metadata"]["generation"] += 1
            else:
                value["spec"]["action"] = "destroy"
        return result

    monkeypatch.setattr(kubevirt, "_origin_post", replace_after_post)
    with q.sessions() as db, pytest.raises(AccountingUnavailable):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    with q.sessions() as db:
        intent = service().active(db, q.project_id)
        assert service().receipts(db, str(intent.id))[1] is None


@pytest.mark.parametrize("cluster_change", ["before_read", "after_absence"])
def test_absent_namespace_recovery_never_retries_into_changed_cluster(
    provider_io, monkeypatch, cluster_change
):
    q = provider_io
    from app.services.providers import kubevirt

    def interrupted_post(*_):
        raise TimeoutError("crash before namespace POST")

    with monkeypatch.context() as setup:
        setup.setattr(q.api, "post", interrupted_post)
        with q.sessions() as db, pytest.raises(TimeoutError):
            kubevirt.KubeVirtDriver().deploy_project(
                db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
            )
    with q.sessions() as db:
        intent = service().active(db, q.project_id)
    monkeypatch.setattr(service(), "require_idle", lambda *_: None)
    original_get = q.api.get
    cluster_reads = 0

    def changed_cluster(kind, ns, name):
        nonlocal cluster_reads
        if name == "kube-system":
            cluster_reads += 1
            uid = (
                "another-cluster"
                if cluster_change == "before_read" or cluster_reads > 1
                else intent.cluster_uid
            )
            return {"metadata": {"uid": uid}}
        return original_get(kind, ns, name)

    monkeypatch.setattr(q.api, "get", changed_cluster)
    with q.sessions() as db, pytest.raises(
        AccountingUnavailable, match="cluster|bracket"
    ):
        kubevirt.KubeVirtDriver().deploy_project(
            db.get(Provider, q.provider_id), q.project_id, {}, {}, db=db
        )
    assert q.events == []


@pytest.mark.parametrize("operation", ["recover", "retry"])
def test_postgres_tracking_wait_releases_all_identity_locks(
    origin_case, monkeypatch, operation
):
    q = origin_case
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from app.models.managed_group import ManagedGroup
    from app.services import quota_locks

    engine = q.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock contract")
    with q.sessions() as db:
        org = ManagedGroup(
            name="origin-tracking",
            normalized_name="origin-tracking",
            is_organization=True,
        )
        db.add(org)
        db.flush()
        project = db.get(Project, q.project_id)
        db.get(User, project.owner_id).organization_id = org.id
        db.commit()
    intent = prepare(q)
    with q.sessions() as db:
        token = service().begin_io(db, str(intent.id), "namespace")
        db.commit()
    tracking_started, tracking_release = Event(), Event()

    def blocked_tracking(*_):
        tracking_started.set()
        assert tracking_release.wait(timeout=8)

    monkeypatch.setattr(service(), "require_idle", blocked_tracking)

    def worker():
        with q.sessions() as db:
            if operation == "recover":
                return service().recover_handoff(db, intent)
            return service().retry_handoff(db, intent, "namespace", token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert tracking_started.wait(timeout=5)
        try:
            with q.sessions() as db:
                db.execute(text("SET LOCAL lock_timeout='500ms'"))
                quota_locks.lock_rows(
                    db,
                    user_ids=(str(intent.owner_id),),
                    organization_ids=(str(intent.organization_id),),
                    project_ids=(q.project_id,),
                )
                db.rollback()
        finally:
            tracking_release.set()
        future.result(timeout=8)


@pytest.mark.parametrize(
    "operation,change",
    [
        ("recover", "cancel"),
        ("retry", "cancel"),
        ("recover", "nonce"),
        ("retry", "nonce"),
    ],
)
def test_tracking_result_rechecks_original_handoff_snapshot(
    origin_case, monkeypatch, operation, change
):
    q = origin_case
    from app.models.native_deployment_origin import NativeDeploymentOriginActive

    intent = prepare(q)
    with q.sessions() as db:
        token = service().begin_io(db, str(intent.id), "namespace")
        db.commit()

    def changed_during_tracking(*_):
        with q.sessions() as other:
            if change == "cancel":
                service().cancel(other, q.project_id)
            else:
                pointer = other.get(NativeDeploymentOriginActive, q.project_id)
                pointer.io_token = str(uuid4())
            other.commit()

    monkeypatch.setattr(service(), "require_idle", changed_during_tracking)
    with q.sessions() as db, pytest.raises(
        AccountingUnavailable, match="changed|cancel"
    ):
        if operation == "recover":
            service().recover_handoff(db, intent)
        else:
            service().retry_handoff(db, intent, "namespace", token)
