"""Exact support receipts survive cancellation and uncertain transport outcomes."""

from copy import deepcopy

import pytest

from app.schemas.quota import AccountingUnavailable
from tests.test_native_deployment_origin import (
    isolated_quota_database as origin_database,
)
from tests.test_native_deployment_origin import (
    observation,
    prepare,
    service,
)
from tests.test_native_deployment_origin import (
    origin_case as origin_fixture,
)
from tests.test_native_origin_cleanup import rq_invocation

isolated_quota_database = origin_database
origin_case = origin_fixture


class Api:
    def __init__(self, intent):
        self.intent = intent
        self.items = {}
        self.posts = []
        self.deletes = []
        self.lost = False
        self.delayed_delete = False
        self.after_post = lambda: None

    def get(self, kind, namespace, name):
        if kind == "Namespace":
            if name == "kube-system":
                return {"metadata": {"uid": "cluster-uid"}}
            return observation(self.intent)["namespace"]
        return deepcopy(self.items.get((kind, name)))

    def post(self, kind, body):
        self.posts.append(deepcopy(body))
        value = deepcopy(body)
        value["metadata"].update(uid=f"uid-{len(self.posts)}", resourceVersion="1")
        self.items[kind, value["metadata"]["name"]] = value
        self.after_post()
        if self.lost:
            raise TimeoutError("lost POST response")
        return value

    def delete(self, kind, namespace, name, uid, rv):
        value = self.items[kind, name]
        assert value["metadata"]["uid"] == uid
        assert value["metadata"]["resourceVersion"] == rv
        self.deletes.append((kind, name, uid, rv))
        if self.delayed_delete:
            value["metadata"].update(
                deletionTimestamp="2026-11-17T00:00:00Z", resourceVersion="2"
            )
        else:
            del self.items[kind, name]


def body(intent, kind="Pod"):
    result = dict(
        apiVersion="v1",
        kind=kind,
        metadata=dict(
            name=f"troshka-{str(intent.project_id)[:8]}-ops", namespace=intent.namespace
        ),
    )
    if kind == "Pod":
        result["spec"] = {
            "restartPolicy": "Never",
            "containers": [{"name": "ops", "image": "test", "command": ["true"]}],
        }
    else:
        result.update(type="Opaque", stringData={"token": "not-retained"})
    return result


def setup_case(q, monkeypatch, *, root=False):
    from app.core import database

    if root:
        from app.models.project import Project
        from app.models.provider import Provider
        from app.services.kubevirt_storage_enrollment import digest_cr_spec
        from tests.test_native_deployment_origin import descriptor

        desc = descriptor()
        desc["cr_spec_digest"] = digest_cr_spec({"projectId": q.project_id})
        with q.sessions() as db:
            intent = service().prepare(
                db, db.get(Project, q.project_id), db.get(Provider, q.provider_id), desc
            )
            db.commit()
    else:
        intent = prepare(q)
    monkeypatch.setattr(database, "SessionLocal", q.sessions)
    with q.sessions() as db:
        service().bind_namespace(db, str(intent.id), observation(intent))
        if root:
            service().bind_root(db, str(intent.id), observation(intent, root=True))
        db.commit()
    return intent, Api(intent)


def test_lost_post_never_adopts_same_name_and_content(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    api.lost = True
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
    )
    assert not run()
    api.lost = False
    _, _, replay = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent)), "replay"
    )
    assert not replay()
    assert len(api.posts) == 1
    with q.sessions() as db:
        from app.services import native_origin_ops_state as state

        records = state.operations(db, str(intent.id))
        assert len(records) == 1
        assert state.fact(db, records[0], "returned") is None
        debt, boundaries = state.cleanup_obligations(db, str(intent.id))
        assert (
            debt == ["support_outcome_unresolved:" + records[0].id] and not boundaries
        )


def test_successful_secret_has_no_remote_execution_debt(origin_case, monkeypatch):
    from app.services import native_cleanup_authority as authority
    from app.services import native_origin_ops as ops
    from app.services import native_origin_writers as writers

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent, "Secret"))
    )
    assert run()
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        assert authority._remote_debt(db, str(intent.id)) == []
        assert writers.observe_terminal(db, str(intent.id))


def test_historical_untracked_remote_hold_is_not_erased_by_success(
    origin_case, monkeypatch
):
    from app.services import native_cleanup_authority as authority
    from app.services import native_origin_descendants as descendants
    from app.services import native_origin_ops as ops
    from app.services import native_origin_writers as writers

    q = origin_case
    intent, api = setup_case(q, monkeypatch)

    def execute():
        ops.create(q.project_id, api, body(intent, "Secret"))
        with q.sessions() as db:
            descendants.mark_support(db, q.project_id, remote_pending=True)

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        assert authority._remote_debt(db, str(intent.id))[0].startswith(
            "remote_outcome_unresolved:"
        )
        with pytest.raises(AccountingUnavailable, match="untracked"):
            writers.observe_terminal(db, str(intent.id))


def test_post_uid_retained_before_readback_and_after_cancel(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state

    q = origin_case
    intent, api = setup_case(q, monkeypatch)

    def cancel():
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()

    api.after_post = cancel
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
    )
    assert run()
    with q.sessions() as db:
        row = state.operations(db, str(intent.id))[0]
        assert state.fact(db, row, "returned")["uid"] == "uid-1"
        assert state.fact(db, row, "verified")["uid"] == "uid-1"
        assert "not-retained" not in str(row.payload)


def test_exact_graceful_removal_waits_for_controller_absence_without_finalizer(
    origin_case, monkeypatch
):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
    )
    assert run()
    name = body(intent)["metadata"]["name"]
    api.delayed_delete = True
    with pytest.raises(AccountingUnavailable, match="pending"):
        ops.remove(q.project_id, api, "Pod", name)
    assert api.deletes == [("Pod", name, "uid-1", "1")]
    value = api.items["Pod", name]
    assert not value["metadata"].get("finalizers")
    with pytest.raises(AccountingUnavailable, match="pending"):
        ops.remove(q.project_id, api, "Pod", name)
    del api.items["Pod", name]  # Normal controller finishes deletion.
    ops.remove(q.project_id, api, "Pod", name)
    assert ("Pod", name) not in api.items


def test_admission_cannot_change_command_or_add_container(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    original = api.post

    def post(kind, manifest):
        value = original(kind, manifest)
        value["spec"]["containers"].append({"name": "foreign", "image": "foreign"})
        return value

    api.post = post
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
    )
    assert not run()
    with q.sessions() as db:
        row = state.operations(db, str(intent.id))[0]
        # Retain the returned original UID even when admission is unsupported.
        assert state.fact(db, row, "returned")["uid"] == "uid-1"
        assert state.fact(db, row, "verified") is None


def test_actual_provider_routes_native_secret_pod_without_generic_replacement(
    origin_case, monkeypatch
):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services.providers import kubevirt

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    monkeypatch.setattr(ops, "client", lambda provider: api)
    monkeypatch.setattr(
        kubevirt,
        "_create_ops_pod_locked",
        lambda *args: pytest.fail("generic mutation"),
    )
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: kubevirt.create_ops_pod(
            SimpleNamespace(id=q.provider_id),
            q.project_id,
            body(intent),
            body(intent, "Secret"),
        ),
    )
    assert run()
    assert [v["kind"] for v in api.posts] == ["Secret", "Pod"]


def scaffold(intent):
    from app.services.ocp.ops_pod_scaffold import build_ops_pod_kubevirt_manifests

    return build_ops_pod_kubevirt_manifests(
        namespace=intent.namespace,
        project_id=str(intent.project_id),
        command=["bash", "-c", "true"],
        env={"EXISTING": "unchanged"},
        config_files={"/workdir/install.yaml": "config"},
        cluster_nads=["lab-network"],
        bmc_nad=None,
    )


def admit_service_account(pod):
    name = "kube-api-access-abcde"
    pod["spec"]["volumes"].append(
        {
            "name": name,
            "projected": {
                "defaultMode": 420,
                "sources": [
                    {
                        "serviceAccountToken": {
                            "expirationSeconds": 3607,
                            "path": "token",
                        }
                    },
                    {
                        "configMap": {
                            "name": "kube-root-ca.crt",
                            "items": [{"key": "ca.crt", "path": "ca.crt"}],
                        }
                    },
                    {
                        "downwardAPI": {
                            "items": [
                                {
                                    "path": "namespace",
                                    "fieldRef": {
                                        "apiVersion": "v1",
                                        "fieldPath": "metadata.namespace",
                                    },
                                }
                            ]
                        }
                    },
                ],
            },
        }
    )
    pod["spec"]["containers"][0]["volumeMounts"].append(
        {
            "name": name,
            "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
            "readOnly": True,
        }
    )


def test_actual_scaffold_accepts_service_account_admission(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    manifest, _ = scaffold(intent)
    original = deepcopy(manifest)
    post = api.post

    def admitted(kind, requested):
        value = post(kind, requested)
        admit_service_account(value)
        return value

    api.post = admitted

    def execute():
        operation = ops.create(q.project_id, api, manifest)
        with q.sessions() as db:
            assert state.fact(db, operation, "verified")["uid"] == "uid-1"
        assert (
            remote.exec_command(q.project_id, api, ["true"], lambda argv: "OK") == "OK"
        )

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert manifest == original
    assert api.posts[0]["spec"] == original["spec"]


@pytest.mark.parametrize(
    "change",
    [
        "extra_mount",
        "mount_path",
        "token_source",
        "extra_volume",
        "command",
        "container",
    ],
)
def test_admission_allowance_rejects_unexpected_changes(change):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops

    requested, _ = scaffold(
        SimpleNamespace(namespace="project", project_id="12345678-project")
    )
    observed = deepcopy(requested)
    admit_service_account(observed)
    spec = observed["spec"]
    container = spec["containers"][0]
    if change == "extra_mount":
        container["volumeMounts"].append(
            {"name": "ops-config", "mountPath": "/unexpected"}
        )
    elif change == "mount_path":
        container["volumeMounts"][-1]["mountPath"] = "/unexpected"
    elif change == "token_source":
        spec["volumes"][-1]["projected"]["sources"][1]["configMap"][
            "name"
        ] = "unexpected"
    elif change == "extra_volume":
        spec["volumes"].append({"name": "unexpected", "emptyDir": {}})
    elif change == "command":
        container["command"] = ["unexpected"]
    else:
        spec["containers"].append({"name": "unexpected", "image": "unexpected"})
    with pytest.raises(AccountingUnavailable, match="admitted"):
        ops._admitted(requested, observed)


def test_initial_pair_retry_after_heal_never_touches_successor(
    origin_case, monkeypatch
):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    provider = SimpleNamespace(id=q.provider_id)
    manifest, secret = scaffold(intent)
    monkeypatch.setattr(ops, "client", lambda provider: api)

    def execute():
        ops.create_pair(provider, q.project_id, manifest, secret)
        ops.replacement(q.project_id, api, manifest)
        with q.sessions() as db:
            successor = state.latest(
                db, str(intent.id), "Pod", manifest["metadata"]["name"]
            )
            successor_id = successor.id
        deletes, posts = deepcopy(api.deletes), deepcopy(api.posts)
        with pytest.raises(AccountingUnavailable):
            ops.create_pair(provider, q.project_id, manifest, secret)
        assert api.deletes == deletes
        assert api.posts == posts
        with q.sessions() as db:
            assert (
                state.fact(db, db.get(state.Operation, successor_id), "exec_closed")
                is None
            )

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()


def test_exec_normal_restarts_supported_without_resolving_remote_liability(
    origin_case, monkeypatch
):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_remote as remote
    from app.services import native_origin_writers as writers

    q = origin_case
    intent, api = setup_case(q, monkeypatch)

    def execute():
        ops.create(q.project_id, api, body(intent))
        pod = api.items["Pod", body(intent)["metadata"]["name"]]
        pod["status"] = {
            "containerStatuses": [
                {
                    "name": "ops",
                    "containerID": "cri://seven",
                    "restartCount": 7,
                    "state": {"running": {"startedAt": "2026-11-17T00:00:00Z"}},
                }
            ]
        }
        invoked = []
        assert (
            remote.exec_command(
                q.project_id, api, ["true"], lambda argv: invoked.append(argv) or "OK"
            )
            == "OK"
        )
        assert invoked == [["true"]]

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    with q.sessions() as db:
        service().cancel(db, q.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="remote operations"):
            writers.observe_terminal(db, str(intent.id))


@pytest.mark.parametrize(
    "outcome", ["complete", "timeout", "missing_status", "transport"]
)
def test_actual_exec_transport_retains_unknown_outcomes(
    origin_case, monkeypatch, outcome
):
    from types import SimpleNamespace

    from kubernetes import stream

    from app.services import deploy_service
    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    api.connect_get_namespaced_pod_exec = lambda *a: None
    closed = []
    response = SimpleNamespace(
        run_forever=lambda **kwargs: None,
        is_open=lambda: outcome == "timeout",
        returncode=None if outcome == "missing_status" else 0,
        read_all=lambda: "data",
        close=lambda: closed.append(True),
    )

    def transport(*args, **kwargs):
        if outcome == "transport":
            raise TimeoutError("ambiguous exec dispatch")
        return "data" if kwargs["_preload_content"] else response

    monkeypatch.setattr(stream, "stream", transport)
    monkeypatch.setattr(ops, "for_core", lambda _: api)
    monkeypatch.setattr(
        deploy_service,
        "_kubevirt_ops_pod_ctx",
        lambda *a: (api, intent.namespace, body(intent)["metadata"]["name"]),
    )

    def execute():
        ops.create(q.project_id, api, body(intent))
        result = deploy_service._ops_pod_exec(
            SimpleNamespace(host_type="kubevirt-cluster"), q.project_id, "ops", ["true"]
        )
        assert result == "data"

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run() is (outcome == "complete")
    with q.sessions() as db:
        debt, boundaries = state.cleanup_obligations(db, str(intent.id))
        assert bool(debt) is (outcome != "complete")
        assert len(boundaries) == (2 if outcome == "complete" else 1)
    if outcome != "transport":
        assert closed == [True]


def test_native_pod_preserves_always_policy_command_and_environment(
    origin_case, monkeypatch
):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    manifest = body(intent)
    manifest["spec"]["restartPolicy"] = "Always"
    manifest["spec"]["containers"][0].update(
        args=["a b;$(false)"], env=[{"name": "EXISTING", "value": "unchanged"}]
    )
    original = deepcopy(manifest)
    _, _, run = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, manifest)
    )
    assert run()
    assert api.posts[0]["spec"] == original["spec"]
    assert not api.posts[0]["metadata"].get("finalizers")


def test_replacement_restart_never_deletes_successor_after_lost_publication(
    origin_case, monkeypatch
):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    _, _, create = rq_invocation(
        monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
    )
    assert create()
    record = ops._record

    def lost(operation, stage, value):
        if stage == "successor":
            raise TimeoutError("lost successor publication")
        return record(operation, stage, value)

    monkeypatch.setattr(ops, "_record", lost)
    _, _, replace = rq_invocation(
        monkeypatch, lambda: ops.replacement(q.project_id, api, body(intent)), "replace"
    )
    assert not replace()
    assert len(api.posts) == 2
    monkeypatch.setattr(ops, "_record", record)
    _, _, resume = rq_invocation(
        monkeypatch, lambda: ops.resume_replacement(q.project_id, api), "resume"
    )
    assert resume()
    assert len(api.posts) == 2
    assert len(api.deletes) == 1


def test_filepull_actual_driver_retains_async_debt_after_ready(
    origin_case, monkeypatch
):
    import base64
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state
    from app.services import native_origin_writers as writers
    from app.services.providers import kubevirt

    q = origin_case
    intent, api = setup_case(q, monkeypatch, root=True)
    root = observation(intent, root=True)["root"]
    root["metadata"]["resourceVersion"] = "19"
    requested = []
    api.root = lambda *args: root
    api.request_file = lambda *args: requested.append(args)
    monkeypatch.setattr(ops, "client", lambda provider: api)

    def read(**kwargs):
        import json

        payload = json.loads(requested[0][-1]["troshka.redhat.com/file-pull-request"])
        return {
            "metadata": root["metadata"],
            "status": {
                "filePull": {
                    "requestId": payload["requestId"],
                    "ready": True,
                    "contentB64": base64.b64encode(b"data").decode(),
                }
            },
        }

    custom = SimpleNamespace(get_namespaced_custom_object=read)
    monkeypatch.setattr(
        kubevirt, "_get_k8s_clients", lambda provider: (custom, object(), object())
    )
    monkeypatch.setattr(kubevirt, "_project_ns", lambda *args: intent.namespace)
    monkeypatch.setattr(
        kubevirt, "_resolve_vm_root_pvc", lambda *args: ("source-pvc", 20)
    )

    def invoke():
        assert (
            kubevirt.KubeVirtDriver().pull_file(
                SimpleNamespace(id=q.provider_id), q.project_id, "vm-id", "/guest/file"
            )
            == b"data"
        )

    _, _, run = rq_invocation(monkeypatch, invoke)
    assert run()
    assert requested[0][2:4] == ("root-uid", "19")
    with q.sessions() as db:
        rows = state.operations(db, str(intent.id))
        assert len(rows) == 1
        assert rows[0].payload["request"]["operation_digest"]
        service().cancel(db, q.project_id)
        db.commit()
        with pytest.raises(AccountingUnavailable, match="remote operations"):
            writers.observe_terminal(db, str(intent.id))


def file_api(api, intent):
    root = observation(intent, root=True)["root"]
    root["metadata"]["resourceVersion"] = "19"
    api.root = lambda *args: deepcopy(root)
    calls = []
    api.request_file = lambda *args: calls.append(args)
    return calls


@pytest.mark.parametrize("boundary", ["create", "exec", "filepull"])
def test_cancellation_at_actual_dispatch_suppresses_remote_mutation(
    origin_case, monkeypatch, boundary
):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch, root=True)
    calls = file_api(api, intent)
    monkeypatch.setattr(ops, "client", lambda provider: api)
    if boundary == "exec":
        _, _, create = rq_invocation(
            monkeypatch, lambda: ops.create(q.project_id, api, body(intent))
        )
        assert create()
    posted = len(api.posts)
    original = ops._dispatch

    def cancel(operation):
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()
        return original(operation)

    monkeypatch.setattr(ops, "_dispatch", cancel)
    functions = {
        "create": lambda: ops.create(q.project_id, api, body(intent)),
        "exec": lambda: remote.exec_command(
            q.project_id, api, ["true"], lambda argv: calls.append(argv)
        ),
        "filepull": lambda: remote.request_file(
            SimpleNamespace(id=q.provider_id), q.project_id, {"requestId": "request"}
        ),
    }
    _, _, run = rq_invocation(monkeypatch, functions[boundary], "cancel-boundary")
    assert not run()
    assert not calls
    assert len(api.posts) == posted
    with q.sessions() as db:
        facts = state.fence_snapshot(db, str(intent.id))
        assert any(v["kind"] == boundary and v["dispatched"] is None for v in facts)


def test_exec_in_flight_blocks_heal_then_same_plan_resumes(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch)

    def execute():
        ops.create(q.project_id, api, body(intent))

        def invoke(argv):
            with pytest.raises(AccountingUnavailable, match="in flight"):
                ops.replacement(q.project_id, api, body(intent))
            assert len(api.posts) == 1 and not api.deletes
            return "OK"

        assert remote.exec_command(q.project_id, api, ["true"], invoke) == "OK"
        assert ops.resume_replacement(q.project_id, api)["action"] == "reschedule"

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert len(api.posts) == 2
    assert api.deletes == [("Pod", body(intent)["metadata"]["name"], "uid-1", "1")]


def test_prepared_exec_cannot_start_against_healed_successor(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    original = ops._dispatch
    invoked = []

    def heal(operation):
        if operation.payload["kind"] == "exec":
            ops.replacement(q.project_id, api, body(intent))
        return original(operation)

    def execute():
        ops.create(q.project_id, api, body(intent))
        monkeypatch.setattr(ops, "_dispatch", heal)
        with pytest.raises(AccountingUnavailable, match="closed"):
            remote.exec_command(
                q.project_id, api, ["true"], lambda argv: invoked.append(argv)
            )

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert not invoked
    assert len(api.posts) == 2


def test_cancel_during_predecessor_delete_never_posts_successor(
    origin_case, monkeypatch
):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    delete = api.delete

    def cancel(*args):
        delete(*args)
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()

    api.delete = cancel

    def execute():
        ops.create(q.project_id, api, body(intent))
        with pytest.raises(AccountingUnavailable, match="cancel"):
            ops.replacement(q.project_id, api, body(intent))
        assert ops.resume_replacement(q.project_id, api)["action"] == "cancelled"

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert len(api.posts) == 1


def test_actual_healer_resumes_saved_body_after_delayed_delete(
    origin_case, monkeypatch
):
    from types import SimpleNamespace

    from kubernetes.client import ApiClient

    from app.services import native_origin_ops as ops
    from app.services.ocp import ops_pod_heal as heal

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    name = body(intent)["metadata"]["name"]
    core = SimpleNamespace(
        read_namespaced_pod=lambda **kwargs: ApiClient()._ApiClient__deserialize(
            deepcopy(api.items["Pod", name]), "V1Pod"
        )
    )
    monkeypatch.setattr(ops, "for_core", lambda value: api)
    planned = []
    monkeypatch.setattr(
        heal,
        "_needs_ops_pod_heal",
        lambda *args, **kwargs: planned.append(True) or (True, "stuck-creating"),
    )

    def execute():
        ops.create(q.project_id, api, body(intent))
        api.delayed_delete = True
        assert (
            heal.heal_stuck_ops_pod(
                core, intent.namespace, name, project_id=q.project_id
            )["action"]
            == "error"
        )
        del api.items["Pod", name]
        assert (
            heal.heal_stuck_ops_pod(
                core, intent.namespace, name, project_id=q.project_id
            )["action"]
            == "reschedule"
        )

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert len(planned) == 1
    assert len(api.posts) == 2 and len(api.deletes) == 1
    assert api.posts[1]["metadata"]["annotations"][heal.ANN_ATTEMPTS] == "1"


def test_filepull_retry_after_cleared_annotation_keeps_prior_debt(
    origin_case, monkeypatch
):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch, root=True)
    calls = file_api(api, intent)
    monkeypatch.setattr(ops, "client", lambda provider: api)

    def execute():
        provider = SimpleNamespace(id=q.provider_id)
        remote.request_file(provider, q.project_id, {"requestId": "one"})
        remote.request_file(provider, q.project_id, {"requestId": "two"})

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()
    assert len(calls) == 2
    with q.sessions() as db:
        rows = state.operations(db, str(intent.id))
        assert {r.payload["request"]["request_id"] for r in rows} == {"one", "two"}
        assert len({r.payload["request"]["operation_digest"] for r in rows}) == 2


def test_filepull_response_after_cancellation_is_retained_without_settlement(
    origin_case, monkeypatch
):
    from types import SimpleNamespace

    from app.services import native_origin_ops as ops
    from app.services import native_origin_ops_state as state
    from app.services import native_origin_remote as remote

    q = origin_case
    intent, api = setup_case(q, monkeypatch, root=True)
    file_api(api, intent)
    monkeypatch.setattr(ops, "client", lambda provider: api)

    def cancel(*args):
        with q.sessions() as db:
            service().cancel(db, q.project_id)
            db.commit()

    api.request_file = cancel
    _, _, run = rq_invocation(
        monkeypatch,
        lambda: remote.request_file(
            SimpleNamespace(id=q.provider_id), q.project_id, {"requestId": "late"}
        ),
    )
    assert run()
    with q.sessions() as db:
        row = state.operations(db, str(intent.id))[0]
        assert state.fact(db, row, "submitted")["request_id"] == "late"
        assert state.fence_snapshot(db, str(intent.id))[0]["local_exit"]


def test_provider_io_never_holds_identity_transaction(origin_case, monkeypatch):
    from app.services import native_origin_ops as ops

    q = origin_case
    intent, api = setup_case(q, monkeypatch)
    locked_sessions = set()
    original_lock = service()._locked

    def locked(db, *args, **kwargs):
        locked_sessions.add(db)
        return original_lock(db, *args, **kwargs)

    monkeypatch.setattr(service(), "_locked", locked)
    for method in ("get", "post", "delete"):
        original = getattr(api, method)

        def checked(*args, _original=original, **kwargs):
            assert not any(db.in_transaction() for db in locked_sessions)
            return _original(*args, **kwargs)

        monkeypatch.setattr(api, method, checked)

    def execute():
        ops.create(q.project_id, api, body(intent))
        ops.replacement(q.project_id, api, body(intent))

    _, _, run = rq_invocation(monkeypatch, execute)
    assert run()


def test_postgres_dispatch_wait_rechecks_cancellation(origin_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services import native_origin_ops as ops
    from app.services import native_origin_remote as remote

    q = origin_case
    if q.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL dispatch/cancellation lock race")
    intent, api = setup_case(q, monkeypatch)
    prepared, gate, entering = Event(), Event(), Event()
    dispatch, locked = ops._dispatch, service()._locked
    invoked = []

    def wait(operation):
        if operation.payload["kind"] == "exec":
            prepared.set()
            assert gate.wait(timeout=5)
        return dispatch(operation)

    def lock(db, *args, **kwargs):
        if gate.is_set():
            entering.set()
        return locked(db, *args, **kwargs)

    monkeypatch.setattr(ops, "_dispatch", wait)
    monkeypatch.setattr(service(), "_locked", lock)

    def execute():
        ops.create(q.project_id, api, body(intent))
        remote.exec_command(
            q.project_id, api, ["true"], lambda argv: invoked.append(argv)
        )

    _, _, run = rq_invocation(monkeypatch, execute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert prepared.wait(timeout=5)
            with q.sessions() as db:
                service().cancel(db, q.project_id)
                gate.set()
                assert entering.wait(timeout=5)
                assert not future.done()
                db.commit()
            assert not future.result(timeout=5)
        finally:
            gate.set()
    assert not invoked


def test_postgres_support_evidence_ddl_is_immutable(origin_case):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine = origin_case.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL support evidence DDL")
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/e06fd54ac283_native_origin_ops_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("support_migration", path)
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
        for table, columns, values in [
            ("native_origin_ops_operations", ",intent_id,writer_id", ",:id,:id"),
            ("native_origin_ops_facts", ",operation_id", ",:id"),
        ]:
            conn.execute(
                text(
                    f"INSERT INTO {table} (id,digest,payload{columns}) VALUES (:id,'digest','{{}}'{values})"
                ),
                {"id": str(uuid4())},
            )
            for statement in (
                f"UPDATE {table} SET digest='changed'",
                f"DELETE FROM {table}",
            ):
                with pytest.raises(DBAPIError, match="immutable"), conn.begin_nested():
                    conn.execute(text(statement))
        migration.downgrade()
        migration.upgrade()
