"""Original-generation cleanup: real ledger/API paths and bounded readers."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from app.schemas.kubevirt_storage import ImageKey, ReadResult
from app.services.kubevirt_storage_transport import (
    CapabilityProfile,
    PoolConfig,
    RbdReader,
)
from tests.test_kubevirt_storage_graph import NOW
from tests.test_native_managed_deploy import deploy_case as deploy_case_fixture
from tests.test_native_managed_resume import FixedDatetime
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_database_fixture,
)
from tests.test_native_managed_resume import resume_case as resume_case_fixture
from tests.test_quota_native_storage import native_ledger as native_ledger_fixture

isolated_quota_database = isolated_database_fixture
resume_case = resume_case_fixture
deploy_case = deploy_case_fixture
native_ledger = native_ledger_fixture


def test_cleanup_preparation_ignores_settled_binding_history(cleanup_case, monkeypatch):
    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.models.project import Project
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_enrollment import root_key
    from app.services.kubevirt_storage_reports import binding_key

    q = cleanup_case
    old = q.report.volume_bindings[0]
    old = old.model_copy(
        update={
            "pvc_uid": "already-settled-pvc",
            "root": old.root.model_copy(
                update={"namespace_uid": "already-settled-namespace"}
            ),
        }
    )
    retired = root_key(old.root)
    monkeypatch.setattr(quota_cleanup, "settled_root_keys", lambda db: {retired})
    with q.sessions() as db:
        current = db.query(NativeRetainedBinding).first()
        db.add(
            NativeRetainedBinding(
                key=binding_key(old),
                root_key=retired,
                domain_id=current.domain_id,
                payload=old.model_dump(mode="json"),
            )
        )
        db.commit()
        intent = quota_cleanup.prepare_native(
            db, db.get(Project, q.project_ids[0]), "undeploy"
        )
        assert intent.phase == "stopping"
        assert old not in intent.bindings
        db.rollback()


@pytest.fixture
def cleanup_case(resume_case, monkeypatch):
    from app.services import native_managed_power as power

    q = resume_case
    q.queued = []
    q.errors = []
    monkeypatch.setattr(
        "app.services.quota_enforcement._now", lambda: FixedDatetime.current
    )
    monkeypatch.setattr(
        "app.core.redis.enqueue_job",
        lambda fn, *args, **kwargs: q.queued.append((fn, args)),
    )
    for project_id, api in q.apis.items():
        binding = next(
            b for b in q.report.volume_bindings if str(b.root.project_id) == project_id
        )
        pv = api.values["PersistentVolume", "retained-pv"]
        pv["spec"].update(
            persistentVolumeReclaimPolicy="Delete",
            csi={"driver": binding.csi_driver, "volumeHandle": binding.volume_handle},
        )
        for value in api.values.values():
            value["metadata"].setdefault("resourceVersion", "1")
        api.find = lambda kind, namespace, name, api=api: deepcopy(
            api.values.get((kind, name))
        )
        original_request = api.request

        def request(
            method,
            kind,
            namespace,
            name,
            body,
            api=api,
            original_request=original_request,
        ):
            if method != "DELETE":
                return original_request(method, kind, namespace, name, body)
            value = api.values[kind, name]
            assert body["preconditions"]["uid"] == value["metadata"]["uid"]
            assert (
                body["preconditions"]["resourceVersion"]
                == value["metadata"]["resourceVersion"]
            )
            api.calls.append((method, (kind, name), deepcopy(body)))
            if kind == "TroshkaProject":
                value["metadata"]["generation"] += 1
                marker = value["metadata"]["annotations"][
                    power.helpers().CLEANUP_ANNOTATION
                ]
                assert all(
                    child["metadata"]
                    .get("annotations", {})
                    .get(power.helpers().CLEANUP_ANNOTATION)
                    == marker
                    for (child_kind, _), child in api.values.items()
                    if child_kind in {"TroshkaVM", "TroshkaNetwork"}
                )
                value["metadata"][
                    "deletionTimestamp"
                ] = FixedDatetime.current.isoformat()
            else:
                del api.values[kind, name]
            return {"kind": "Status", "status": "Success"}

        api.request = request
    from app.models.quota import QuotaReservation

    with q.sessions() as db:
        for row in db.query(QuotaReservation):
            identity = row.dispatch_metadata["native_storage_identity"]
            root = q.apis[row.project_id].rows("TroshkaProject", None)[0]
            row.dispatch_metadata = {
                **row.dispatch_metadata,
                "native_deploy": {
                    "base_grants_mode": "managed",
                    "create_namespace_uid": identity["root"]["namespace_uid"],
                    "runtime_request": power.helpers().intent(root),
                    "running_proof": {"observation_id": "reviewed-baseline-original"},
                },
            }
        db.commit()

    configure_cleanup_transport(q, monkeypatch)
    return q


def configure_cleanup_transport(q, monkeypatch):
    from app.services import native_managed_power as power

    def execute(provider, context, request_id):
        api = q.apis[context["root"]["project_id"]]
        FixedDatetime.current += timedelta(seconds=1)
        try:
            power.stop_and_observe(
                api, context, request_id, clock=lambda: FixedDatetime.current
            )
            power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
            for key in list(api.values):
                if key[0] in {"Pod", "VirtualMachineInstance"}:
                    del api.values[key]
                elif key[0] == "ReplicaSet":
                    api.values[key]["spec"]["replicas"] = 0
                elif key[0] == "Deployment":
                    api.values[key]["status"] = {
                        "observedGeneration": api.values[key]["metadata"]["generation"]
                    }
            power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
            return power.stop_and_observe(
                api, context, request_id, clock=lambda: FixedDatetime.current
            )
        except Exception as error:
            q.errors.append(repr(error))
            raise

    monkeypatch.setattr(power, "execute", execute)


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
@pytest.mark.parametrize("grant_shape", ["original", "foreign", "changed"])
def test_running_first_deploy_original_receipt_enters_cleanup(
    deploy_case, monkeypatch, mode, grant_shape
):
    from types import SimpleNamespace

    from app.api import projects
    from app.models.project import Project
    from app.models.quota import QuotaReservation
    from app.models.user import User
    from app.services import (
        native_managed_deploy as deploy,
    )
    from app.services import (
        native_managed_power as power,
    )
    from app.services import (
        quota_cleanup,
    )
    from tests.test_native_managed_deploy import (
        advance_clock,
        current_target,
        initialized,
        settle_runtime,
    )

    q = deploy_case
    operation_id = initialized(q)
    deploy.advance(
        q.new_id,
        operation_id,
        current_target(q, operation_id),
        q.profile,
        SimpleNamespace(),
    )
    deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    settle_runtime(q)
    advance_clock()
    deploy.advance(
        q.new_id,
        operation_id,
        current_target(q, operation_id),
        q.profile,
        SimpleNamespace(),
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed"
        assert row.dispatch_metadata["native_deploy"]["base_grants_mode"] == "managed"
        assert db.get(Project, q.new_id).state == "active"
    # Existing namespace grant helper creates these exact unowned base grants.
    for (namespace, name), body in q.namespace_grants.items():
        value = deepcopy(body)
        value["metadata"].update(uid=name + "-original", resourceVersion="1")
        api.values["RoleBinding", name] = value
    namespace = api.rows("TroshkaProject", None)[0]["metadata"]["namespace"]
    for name in ["system:deployers", "system:image-builders", "system:image-pullers"]:
        api.values["RoleBinding", name] = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": name + "-uid",
                "resourceVersion": "1",
            },
            **power.helpers().cleanup_grant_body(name, namespace),
        }
    for name in ["default", "builder", "deployer"]:
        api.values["ServiceAccount", name] = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": name + "-uid",
                "resourceVersion": "1",
            },
        }
    for name in ["default", "builder", "deployer"]:
        sa = api.values["ServiceAccount", name]
        secret_name = name + "-dockercfg-original"
        sa["metadata"]["annotations"] = {
            "openshift.io/internal-registry-pull-secret-ref": secret_name
        }
        api.values["Secret", secret_name] = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/dockercfg",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "uid": secret_name + "-uid",
                "resourceVersion": "1",
                "annotations": {
                    "openshift.io/internal-registry-auth-token.service-account": name,
                    "openshift.io/internal-registry-auth-token.binding": "bound",
                },
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "ServiceAccount",
                        "name": name,
                        "uid": sa["metadata"]["uid"],
                    }
                ],
            },
        }
    for name, annotations in {
        "kube-root-ca.crt": {
            "kubernetes.io/description": "Contains a CA bundle that can be used to verify the kube-apiserver when using internal endpoints such as the internal service IP or kubernetes.default.svc. No other usage is guaranteed across distributions of Kubernetes clusters."
        },
        "openshift-service-ca.crt": {
            "service.beta.openshift.io/inject-cabundle": "true"
        },
    }.items():
        api.values["ConfigMap", name] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": name + "-uid",
                "resourceVersion": "1",
                "annotations": annotations,
            },
            "data": {
                (
                    "ca.crt" if name == "kube-root-ca.crt" else "service-ca.crt"
                ): "synthetic public CA"
            },
        }
    if grant_shape == "foreign":
        foreign = deepcopy(value)
        foreign["metadata"].update(name="foreign-extra-grant", uid="foreign-grant")
        api.values["RoleBinding", "foreign-extra-grant"] = foreign
    elif grant_shape == "changed":
        value["subjects"][0]["name"] = "foreign-service-account"
    q.errors = []
    q.queued = []
    configure_cleanup_transport(q, monkeypatch)
    monkeypatch.setattr(
        "app.services.quota_enforcement._now", lambda: FixedDatetime.current
    )
    monkeypatch.setattr(
        "app.core.redis.enqueue_job",
        lambda fn, *args, **kwargs: q.queued.append((fn, args)),
    )
    api.find = lambda kind, namespace, name: deepcopy(api.values.get((kind, name)))
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        result = (
            projects.delete_project if mode == "delete" else projects.undeploy_project
        )(project.id, db.get(User, q.owner_id), db)
        assert result["status"] == "cleanup_pending"
    with q.sessions() as db:
        pending = quota_cleanup.native_intent(db, q.new_id)
        for binding in pending.bindings:
            claim = next(
                v
                for v in api.rows("PersistentVolumeClaim", None)
                if v["metadata"]["uid"] == binding.pvc_uid
            )
            pv = api.values["PersistentVolume", claim["spec"]["volumeName"]]
            pv["spec"].update(
                csi={
                    "driver": binding.csi_driver,
                    "volumeHandle": binding.volume_handle,
                },
                persistentVolumeReclaimPolicy="Delete",
            )
    original_request = api.request

    def original_root_delete(method, kind, namespace, name, body):
        if method == "DELETE" and kind == "TroshkaProject":
            live = api.values[kind, name]
            assert body["preconditions"] == {
                "uid": live["metadata"]["uid"],
                "resourceVersion": live["metadata"]["resourceVersion"],
            }
            api.calls.append((method, (kind, name), deepcopy(body)))
            live["metadata"]["deletionTimestamp"] = FixedDatetime.current.isoformat()
            return deepcopy(live)
        return original_request(method, kind, namespace, name, body)

    api.request = original_root_delete
    fn, args = q.queued.pop()
    fn(*args)
    with q.sessions() as db:
        receipt = quota_cleanup.native_intent(db, q.new_id)
        assert receipt.operation_id == operation_id
        assert receipt.stop_proof, (receipt.last_error, q.errors)
        assert db.get(Project, q.new_id).state == "cleanup_pending"
        if grant_shape != "original":
            assert receipt.phase == "stopping"
            assert not any(
                call[0] == "DELETE" and call[1][0] != "Pod" for call in api.calls
            )
            return
        assert receipt.phase == "pending", (receipt.last_error, q.errors)
        assert len([obj for obj in receipt.objects if obj.namespace_bootstrap]) == 13
        assert (
            row.dispatch_metadata["native_deploy"]["create_namespace_uid"]
            == receipt.root.namespace_uid
        )
    assert (
        power.helpers().intent(api.rows("TroshkaProject", None)[0])["mode"] == "stopped"
    )


def collect_cleanup(q, intent, *, gone=False, failed=False):
    """Collect through actual Kubernetes discovery and constrained RBD readers."""
    from app.schemas.kubevirt_storage import CollectionLimits
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_collector import collect_report
    from app.services.kubevirt_storage_enrollment import root_key
    from app.services.kubevirt_storage_inventory import discover_inventory
    from app.services.native_managed_collector import retained_inputs

    with q.sessions() as db:
        retired = quota_cleanup.settled_root_keys(db)
    roots = [r for r in q.report.project_roots if root_key(r) not in retired]
    retained, caches = retained_inputs(q.sessions, intent.domain_id)
    namespaces = {
        r.namespace: q.apis[str(r.project_id)] for r in q.report.project_roots
    }

    class Kube:
        collector_id = q.report.collector_id
        scan_started_at = FixedDatetime.current
        retiring_intents = [intent] if root_key(intent.root) not in retired else []
        cache_roots = [r.model_dump(mode="json") for r in q.pins.cache_roots]
        retained_cache_bindings = caches

        def cluster_identity(self):
            return q.report.project_roots[0].cluster_key

        def namespace_if_exists(self, name):
            if name in namespaces:
                return namespaces[name].find("Namespace", None, name)
            return {
                "metadata": {
                    "uid": next(
                        r.namespace_uid
                        for r in q.pins.cache_roots
                        if r.namespace == name
                    )
                }
            }

        namespace = namespace_if_exists

        def projects(self, namespace):
            return namespaces[namespace].rows("TroshkaProject", namespace)

        def pvcs(self, namespace):
            values = (
                namespaces[namespace].rows("PersistentVolumeClaim", namespace)
                if namespace in namespaces
                else []
            )
            for value in values:
                binding = next(
                    b
                    for b in q.report.volume_bindings
                    if b.pvc_uid == value["metadata"]["uid"]
                )
                value["spec"]["volumeName"] = binding.pv_uid
            return values

        def pvs(self):
            values = [
                v for api in q.apis.values() for v in api.rows("PersistentVolume", None)
            ]
            for value in values:
                value["metadata"]["name"] = value["metadata"]["uid"]
            return values

        def snapshots(self, namespace):
            return []

        def snapshot_contents(self):
            return []

        def resolve_csi(self, driver, handle, **kwargs):
            binding = next(
                b for b in q.report.volume_bindings if b.volume_handle == handle
            )
            return ReadResult(
                status="complete",
                value=binding.image_key,
                collected_at=FixedDatetime.current,
            )

        def cleanup_rows(self, value):
            return quota_cleanup._checked_cleanup_rows(
                namespaces[value.root.namespace], value
            )

    images = {
        image.key.image_id: image
        for image in q.report.images
        if not gone or image.key not in intent.owned_images
    }

    class Transport:
        def run(self, operation, **kwargs):
            if failed and operation == "directory_keys":
                return ReadResult(
                    status="unavailable", collected_at=FixedDatetime.current
                )
            values = {
                "fsid": q.report.images[0].key.ceph_fsid,
                "pools": [{"poolname": "pool", "poolnum": 1}],
                "version": "test",
                "active": list(images),
                "directory_keys": "\n".join("id_" + image for image in images),
                "trash_all": [],
                "trash": [],
                "namespaces": [],
                "snapshots": [],
                "children": [],
            }
            if operation in {"info", "info_id", "du"}:
                name = kwargs["image"]
                image = images[name]
                if operation == "du":
                    value = {
                        "images": [
                            {
                                "id": name,
                                "used_size": image.allocated_bytes,
                                "provisioned_size": image.provisioned_bytes,
                            }
                        ]
                    }
                else:
                    value = {"id": name, "name": name, "size": image.provisioned_bytes}
                    if image.parent:
                        value["parent"] = {
                            "pool": "pool",
                            "pool_namespace": "",
                            "id": image.parent.image_key.image_id,
                            "snap_id": image.parent.snapshot_id,
                        }
            else:
                value = values[operation]
            return ReadResult(
                status="complete",
                value=(
                    value
                    if operation in {"fsid", "version", "directory_keys"}
                    else json.dumps(value)
                ),
                collected_at=FixedDatetime.current,
            )

    backend = RbdReader(
        Transport(),
        q.report.images[0].key.ceph_fsid,
        [PoolConfig(pool_id=1, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(
            command_version="test",
            merge_snapshots_validated=True,
            validation_reference="synthetic-profile",
        ),
    )
    kube = Kube()
    backend.scan_started_at = FixedDatetime.current
    backend.recheck_inventory = lambda: discover_inventory(kube, retained, roots)
    inventory = discover_inventory(kube, retained, roots)
    return collect_report(
        inventory,
        backend,
        limits=CollectionLimits(),
        clock=lambda: FixedDatetime.current,
    )


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
@pytest.mark.parametrize(
    "condition",
    [
        "complete",
        "partial",
        "stale",
        "ambiguous",
        "namespace_ambiguous",
        "retain",
        "replacement",
        "namespace_replacement",
        "pv_replacement",
        "finalization_loss",
        "queue_loss",
        "root_presend",
        "partial_marker",
        "namespace_presend",
        "namespace_deleting",
        "quotas_disabled",
        "quotas_disabled_queue_loss",
    ],
)
def test_cleanup_worker_waits_for_original_resource_and_physical_settlement(
    cleanup_case, monkeypatch, mode, condition
):
    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import quota_cleanup

    q = cleanup_case
    if condition.startswith("quotas_disabled"):
        monkeypatch.setattr(
            "app.services.quota_accounting.quotas_enabled", lambda: False
        )
    api = q.apis[q.project_ids[0]]
    if condition == "root_presend":
        submit = quota_cleanup.submit_native_teardown
        failed = []

        def before_root_send(api, work):
            if not failed:
                failed.append(True)
                raise TimeoutError("synthetic worker loss before root submission")
            return submit(api, work)

        monkeypatch.setattr(quota_cleanup, "submit_native_teardown", before_root_send)
    if condition == "partial_marker":
        patch = api.patch
        seen = []

        def partial_markers(value, changes, **kwargs):
            if value["kind"] in {"TroshkaVM", "TroshkaNetwork"} and any(
                "troshka.redhat.com/managed-cleanup" in change.get("value", {})
                for change in changes
            ):
                seen.append(value["metadata"]["uid"])
                if len(seen) == 2:
                    raise TimeoutError("synthetic worker loss partway through markers")
            return patch(value, changes, **kwargs)

        api.patch = partial_markers
    if condition == "namespace_presend":
        request = api.request
        failed = []

        def before_namespace_send(method, kind, namespace, name, body):
            if method == "DELETE" and kind == "Namespace" and not failed:
                failed.append(True)
                raise TimeoutError("synthetic worker loss before namespace submission")
            return request(method, kind, namespace, name, body)

        api.request = before_namespace_send
    if condition == "namespace_deleting":
        request = api.request

        def accepted_namespace(method, kind, namespace, name, body):
            if method == "DELETE" and kind == "Namespace":
                value = api.values[kind, name]
                assert body["preconditions"]["uid"] == value["metadata"]["uid"]
                api.calls.append((method, (kind, name), deepcopy(body)))
                value["metadata"][
                    "deletionTimestamp"
                ] = FixedDatetime.current.isoformat()
                return deepcopy(value)
            return request(method, kind, namespace, name, body)

        api.request = accepted_namespace
    if condition == "retain":
        api.values["PersistentVolume", "retained-pv"]["spec"][
            "persistentVolumeReclaimPolicy"
        ] = "Retain"
    if condition in {"ambiguous", "namespace_ambiguous"}:
        original_request = api.request

        def lost_response(method, kind, namespace, name, body):
            result = original_request(method, kind, namespace, name, body)
            if method == "DELETE" and kind == (
                "TroshkaProject" if condition == "ambiguous" else "Namespace"
            ):
                raise TimeoutError("synthetic lost accepted-delete response")
            return result

        api.request = lost_response
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        result = (
            projects.delete_project if mode == "delete" else projects.undeploy_project
        )(project.id, db.get(User, q.owner_id), db)
        assert result["status"] == "cleanup_pending"
    fn, args = q.queued.pop()
    if condition in {"queue_loss", "quotas_disabled_queue_loss"}:
        from app.services.quota_enforcement import run_once

        run_once()  # The original queue delivery was lost; durable poll recovers it.
    else:
        fn(*args)
    if condition in {"root_presend", "partial_marker"}:
        with q.sessions() as db:
            assert quota_cleanup.native_intent(db, q.project_ids[0]).phase == "teardown"
        fn(*args)  # Observe the same unchanged original and complete missing steps.
    with q.sessions() as db:
        intent = quota_cleanup.native_intent(db, q.project_ids[0])
        from app.models.quota import QuotaAction

        action = db.get(QuotaAction, intent.stop_action_id)
        assert intent.phase == (
            "teardown" if condition == "ambiguous" else "pending"
        ), (
            q.errors,
            intent.last_error,
            action.state,
            action.last_error,
            action.details,
        )
        assert intent.objects and intent.inventory_digest and intent.stop_proof
        assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
    from app.services import native_managed_power as power
    from app.services.kubevirt_storage_reports import import_report

    if condition == "ambiguous":
        before = deepcopy(api.calls)
        fn(*args)  # Accepted/deleting parent is observed without duplicate mutation.
        assert api.calls == before
    root = api.rows("TroshkaProject", None)[0]
    if condition in {"replacement", "namespace_replacement"}:
        value = next(
            v
            for (kind, name), v in api.values.items()
            if kind
            == ("Namespace" if condition == "namespace_replacement" else "Deployment")
            and name != "kube-system"
        )
        value["metadata"]["uid"] = "replacement-helper"
        before = list(api.calls)
        with pytest.raises(power.helpers().ManagedPowerError):
            power.helpers().cleanup_delete(api, root)
        fn(*args)
        assert api.calls == before
        with q.sessions() as db:
            assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
        return
    for _ in range(8):
        if power.helpers().cleanup_delete(api, root):
            break
    else:
        raise AssertionError("original operator cleanup did not settle")
    del api.values["TroshkaProject", root["metadata"]["name"]]
    fn(*args)
    if condition == "namespace_presend":
        assert api.rows("Namespace", None)
        fn(*args)
    if condition == "namespace_deleting":
        before = deepcopy(api.calls)
        fn(*args)
        assert api.calls == before
        del api.values["Namespace", root["metadata"]["namespace"]]
    if condition == "pv_replacement":
        api.values["PersistentVolume", "retained-pv"]["metadata"][
            "uid"
        ] = "replacement-pv"
        FixedDatetime.current += timedelta(seconds=1)
        invalid = collect_cleanup(q, intent, gone=True)
        assert invalid.projects[0].allocated_bytes is None
        with q.sessions() as db:
            import_report(
                db,
                invalid,
                enrollment_id=q.enrollment_id,
                expected_enrollment_digest=q.digest,
                now=FixedDatetime.current,
            )
            db.commit()
        fn(*args)
        with q.sessions() as db:
            assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
        return
    # CSI/Kubernetes absence does not imply the original RBD graph is gone.
    for key in list(api.values):
        if key[0] == "PersistentVolume" and condition != "retain":
            del api.values[key]
    FixedDatetime.current += timedelta(seconds=1)
    retained_report = collect_cleanup(q, intent)
    assert retained_report.retiring_roots[0].objects_absent == (condition != "retain")
    assert (
        retained_report.projects[0].allocated_bytes == 30
    ), retained_report.collection_errors
    with q.sessions() as db:
        import_report(
            db,
            retained_report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    fn(*args)
    with q.sessions() as db:
        assert db.get(Project, q.project_ids[0]) is not None
    if condition == "retain":
        assert ("PersistentVolume", "retained-pv") in api.values
        assert not any(
            call[0] == "DELETE" and call[1][0] == "PersistentVolume"
            for call in api.calls
        )
        with q.sessions() as db:
            from app.services import quota_operations

            assert (
                sum(
                    r.observed["storage_bytes"]
                    for r in quota_operations._rows(db, q.project_ids[0])
                )
                == 30
            )
        return
    if condition in {"partial", "stale"}:
        FixedDatetime.current += timedelta(seconds=1)
        invalid = collect_cleanup(q, intent, gone=True, failed=condition == "partial")
        if condition == "stale":
            FixedDatetime.current += timedelta(minutes=16)
        with q.sessions() as db:
            import_report(
                db,
                invalid,
                enrollment_id=q.enrollment_id,
                expected_enrollment_digest=q.digest,
                now=FixedDatetime.current,
            )
            db.commit()
        fn(*args)
        with q.sessions() as db:
            assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
            from app.services import quota_operations

            assert (
                sum(
                    r.observed["storage_bytes"]
                    for r in quota_operations._rows(db, q.project_ids[0])
                )
                == 30
            )
    FixedDatetime.current += timedelta(seconds=1)
    zero_report = collect_cleanup(q, intent, gone=True)
    assert zero_report.projects[0].allocated_bytes == 0, zero_report.collection_errors
    with q.sessions() as db:
        import_report(
            db,
            zero_report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    if condition == "finalization_loss":
        from app.services import deploy_service

        original_eips = deploy_service._destroy_cleanup_eips
        failed = []

        def fail_once(db, project_id):
            if not failed:
                failed.append(True)
                raise RuntimeError("synthetic worker loss after durable settlement")
            return original_eips(db, project_id)

        monkeypatch.setattr(deploy_service, "_destroy_cleanup_eips", fail_once)
        fn(*args)
        with q.sessions() as db:
            retained = quota_cleanup.native_intent(db, q.project_ids[0])
            assert retained.phase == "settled" and retained.settlement_report_id
            assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
    fn(*args)
    fn(*args)  # Exact queue replay must not finalize or delete another generation.
    with q.sessions() as db:
        final = quota_cleanup.native_intent(db, q.project_ids[0])
        assert final.phase == "finalized", final.last_error
        project = db.get(Project, q.project_ids[0])
        assert (
            project is None
            if mode == "delete"
            else project.state == "draft" and project.host_id is None
        )
        assert db.get(Project, q.project_ids[1]) is not None
        from app.services.kubevirt_storage_usage import read_native_usage

        usage = read_native_usage(
            db, db.get(User, q.owner_id), "user", q.owner_id, FixedDatetime.current
        )
        assert usage.status == "complete", usage.reason_codes
        assert usage.expected_projects == 1
    assert (
        len(
            [
                call
                for call in api.calls
                if call[0] == "DELETE" and call[1][0] == "TroshkaProject"
            ]
        )
        == 1
    )
    if condition == "complete":
        from app.services import quota_capacity_baseline as capacity
        from app.services import quota_storage

        quota_storage.reconcile_project(q.project_ids[1])
        with q.sessions() as db:
            source = capacity.inventory(db)
            capacity._inventory_blockers(db, source, FixedDatetime.current)
            if mode == "undeploy":
                assert next(
                    p for p in source["projects"] if p["project_id"] == q.project_ids[0]
                )["empty"]
            readiness = capacity.check_ready(db)
            assert readiness["ready"], json.dumps(readiness, default=str)
            from app.schemas.quota import Resources
            from app.services import quota_admission

            next_project = (
                project
                if mode == "undeploy"
                else Project(name="next-draft", owner_id=q.owner_id)
            )
            if mode == "delete":
                db.add(next_project)
                db.flush()
            quota_admission.reserve(
                db,
                next_project.id,
                "next-admission",
                Resources(projects=1),
                FixedDatetime.current,
            )
            db.rollback()
        successor_id, new_operation = enroll_successor(q, final, mode)
        before_calls = deepcopy(api.calls)
        fn(*args)  # Exact old delivery after successor enrollment AND allocation.
        with q.sessions() as db:
            from app.models.quota import QuotaReservation

            successor = db.get(Project, successor_id)
            assert successor.state == "active"
            new_row = (
                db.query(QuotaReservation).filter_by(operation_id=new_operation).one()
            )
            assert new_row.observed == {
                "cpu": 2,
                "ram_bytes": 4096,
                "storage_bytes": 17,
                "projects": 1,
            }
        assert api.calls == before_calls


def enroll_successor(q, receipt, mode):
    """Settled historical roots do not block later same-record or domain deploys."""
    from app.models.kubevirt_storage import NativeEnrollment, NativeProjectRoot
    from app.models.project import Project
    from app.schemas.kubevirt_storage_enrollment import (
        DomainExtension,
        VerifiedEnrollment,
    )
    from app.services import quota_native
    from app.services.kubevirt_storage_enrollment import (
        digest,
        enroll_native_roots,
        prepare_native_enrollment,
        validated_enrollment,
    )

    with q.sessions() as db:
        project = (
            db.get(Project, q.project_ids[0])
            if mode == "undeploy"
            else Project(name="successor-project", owner_id=q.owner_id)
        )
        if mode == "delete":
            db.add(project)
        project.host_id = str(receipt.host_id)
        db.commit()
        successor = receipt.root.model_copy(
            update={
                "project_id": UUID(project.id),
                "namespace_uid": "successor-namespace-uid",
                "project_cr_uid": "successor-cr-uid",
                "deployment_generation": 1,
            }
        )
        request = prepare_native_enrollment(db, [project.id, q.project_ids[1]])
        previous = validated_enrollment(db.get(NativeEnrollment, q.enrollment_id))
        pins = q.pins.model_copy(
            update={
                "project_roots": [successor, q.report.project_roots[1]],
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=q.enrollment_id,
                    previous_enrollment_digest=previous.digest,
                ),
            }
        )
        manifest = VerifiedEnrollment(
            request=request,
            pins=pins,
            verified_at=FixedDatetime.current,
            digest="0" * 64,
        )
        manifest = manifest.model_copy(
            update={
                "digest": digest(manifest.model_dump(mode="json", exclude={"digest"}))
            }
        )
        enrolled = enroll_native_roots(
            db, manifest, expected_target_id=str(request.target_id)
        )
        db.commit()
        assert db.query(NativeProjectRoot).count() == 3
        identity = {
            "domain_id": str(q.pins.domain_id),
            "enrollment_id": enrolled,
            "host_id": str(receipt.host_id),
            "provider_id": str(receipt.provider_id),
            "root": successor.model_dump(mode="json"),
        }
        selected, _ = quota_native._measurement_domain(db, project.id, identity)
        assert selected.payload["namespace_uid"] == "successor-namespace-uid"
        from app.services import quota_capacity_baseline as capacity

        assert capacity._native_identity(db, project) == identity
        from app.models.quota import QuotaAllocation, QuotaReservation

        operation_id = "successor-generation"
        vector = {"cpu": 2, "ram_bytes": 4096, "storage_bytes": 17, "projects": 1}
        values = dict(
            operation_id=operation_id,
            project_id=project.id,
            owner_id=q.owner_id,
            organization_id=q.org_id,
            requested=vector,
            observed=vector,
            observation_id="successor-runtime",
        )
        db.add(
            QuotaReservation(
                **values,
                state="confirmed",
                dispatch_metadata={
                    "adapter": "kubevirt",
                    "generation_id": operation_id,
                    "native_storage_identity": identity,
                    "worker_finished": True,
                },
            )
        )
        db.add(
            QuotaAllocation(**values, state="active", observed_at=FixedDatetime.current)
        )
        project.state = "active"
        db.commit()
        return project.id, operation_id


def finish_original(q, project_id):
    """Drive original cleanup through operator absence and collected settlement."""
    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import native_managed_power as power
    from app.services import quota_cleanup
    from app.services.kubevirt_storage_reports import import_report

    api = q.apis[project_id]
    with q.sessions() as db:
        projects.undeploy_project(project_id, db.get(User, q.owner_id), db)
    fn, args = q.queued.pop()
    fn(*args)
    with q.sessions() as db:
        intent = quota_cleanup.native_intent(db, project_id)
        assert intent.phase == "pending", intent.last_error
    root = api.rows("TroshkaProject", None)[0]
    for _ in range(8):
        if power.helpers().cleanup_delete(api, root):
            break
    else:
        raise AssertionError("Original cleanup did not settle")
    del api.values["TroshkaProject", root["metadata"]["name"]]
    fn(*args)
    for key in list(api.values):
        if key[0] == "PersistentVolume":
            del api.values[key]
    FixedDatetime.current += timedelta(seconds=1)
    report = collect_cleanup(q, intent, gone=True)
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    fn(*args)
    with q.sessions() as db:
        final = quota_cleanup.native_intent(db, project_id)
        assert final.phase == "finalized", final.last_error
        assert db.get(Project, project_id).state == "draft"
    # Later scans cannot rediscover the absent original images.
    q.report = q.report.model_copy(
        update={
            "images": [i for i in q.report.images if i.key not in intent.owned_images]
        }
    )
    return final


@pytest.mark.parametrize(
    "condition",
    ["complete", "stale", "unknown_binding", "changed_binding", "unsettled"],
)
def test_last_retirement_preserves_cache_and_next_admission(cleanup_case, condition):
    from app.models.kubevirt_storage import NativeProjectRoot, NativeRetainedBinding
    from app.models.project import Project
    from app.schemas.quota import Resources
    from app.services import quota_admission, quota_cleanup, quota_storage
    from app.services import quota_capacity_baseline as capacity

    q = cleanup_case
    first = finish_original(q, q.project_ids[0])
    quota_storage.reconcile_project(q.project_ids[1])
    final = finish_original(q, q.project_ids[1])
    with q.sessions() as db:
        assert db.query(NativeProjectRoot).count() == 2
        bindings_before = db.query(NativeRetainedBinding).count()
        if condition == "stale":
            FixedDatetime.current += timedelta(minutes=16)
        elif condition == "unknown_binding":
            from uuid import uuid4

            db.add(
                NativeRetainedBinding(
                    key=str(uuid4()),
                    root_key="unknown",
                    domain_id=str(final.domain_id),
                    payload={"unknown": "retained-storage"},
                )
            )
            db.commit()
        elif condition == "unsettled":
            quota_cleanup._save_native(
                db, first, phase="pending", settlement_report_id=None, settled_at=None
            )
            db.commit()
        elif condition == "changed_binding":
            binding = (
                db.query(NativeRetainedBinding)
                .filter(NativeRetainedBinding.root_key != "cache")
                .first()
            )
            binding.payload = {**binding.payload, "volume_handle": "replacement"}
            db.commit()
        source = capacity.inventory(db)
        readiness = capacity.check_ready(db)
        if condition != "complete":
            assert not readiness["ready"], readiness
            return
        assert not source["native_roots"]
        assert source["native_retained_bindings"]
        assert all(
            b["coverage"] == "platform_cache"
            for b in source["native_retained_bindings"]
        )
        assert all(p["empty"] for p in source["projects"])
        assert readiness["ready"], json.dumps(readiness, default=str)
        assert db.query(NativeRetainedBinding).count() == bindings_before
        next_project = Project(name="after-last-retirement", owner_id=q.owner_id)
        db.add(next_project)
        db.flush()
        quota_admission.reserve(
            db,
            next_project.id,
            "after-retirement",
            Resources(projects=1),
            FixedDatetime.current,
        )


@pytest.mark.parametrize("mode", ["delete", "undeploy"])
@pytest.mark.parametrize("enabled", [True, False])
def test_native_cleanup_routes_persist_original_intent_and_visible_pending(
    cleanup_case, monkeypatch, mode, enabled
):
    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import quota_cleanup, quota_operations

    q = cleanup_case
    monkeypatch.setattr("app.services.quota_accounting.quotas_enabled", lambda: enabled)
    queued = []
    monkeypatch.setattr(
        "app.core.redis.enqueue_job",
        lambda fn, *args, **kwargs: queued.append((fn, args)),
    )
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        result = (
            projects.delete_project if mode == "delete" else projects.undeploy_project
        )(project.id, db.get(User, q.owner_id), db)
        assert result["status"] == "cleanup_pending"
        db.expire_all()
        project = db.get(Project, q.project_ids[0])
        assert project is not None and project.state == "cleanup_pending"
        intent = quota_cleanup.native_intent(db, project.id)
        assert (
            intent.mode == mode
            and intent.root.project_id == q.report.project_roots[0].project_id
        )
        assert (
            sum(
                r.observed["storage_bytes"]
                for r in quota_operations._rows(db, project.id)
            )
            > 0
        )
        assert len(queued) == 1


@pytest.mark.parametrize("entry", ["delete", "undeploy", "worker"])
def test_quota_off_cleanup_retains_project_without_original_ownership(
    cleanup_case, monkeypatch, entry
):
    from fastapi import HTTPException

    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import deploy_service, quota_operations

    q = cleanup_case
    monkeypatch.setattr("app.services.quota_accounting.quotas_enabled", lambda: False)
    pid = q.project_ids[0]
    with q.sessions() as db:
        for row in quota_operations._rows(db, pid):
            row.dispatch_metadata = {**row.dispatch_metadata, "native_deploy": {}}
        db.commit()
        before = db.get(Project, pid).topology
        if entry == "worker":
            deploy_service._destroy_project_inner(
                {"project_id": pid}, delete_record=True
            )
        else:
            endpoint = (
                projects.delete_project
                if entry == "delete"
                else projects.undeploy_project
            )
            with pytest.raises(HTTPException) as error:
                endpoint(pid, db.get(User, q.owner_id), db)
            assert error.value.status_code == 409
        db.expire_all()
        project = db.get(Project, pid)
        assert project.owner_id == q.owner_id
        assert project.topology == before
        assert not any(call[0] == "DELETE" for call in q.apis[pid].calls)


@pytest.mark.parametrize("entry", ["worker", "helper"])
def test_old_destroy_worker_uses_strong_cleanup_with_quotas_off(
    cleanup_case, monkeypatch, entry
):
    from app.models.project import Project
    from app.services import deploy_service, quota_cleanup

    q = cleanup_case
    monkeypatch.setattr("app.services.quota_accounting.quotas_enabled", lambda: False)
    pid = q.project_ids[0]
    if entry == "worker":
        deploy_service._destroy_project_inner({"project_id": pid}, delete_record=True)
    else:
        with q.sessions() as db:
            deploy_service._destroy_kubevirt_native(pid, None, db, True)
    with q.sessions() as db:
        project = db.get(Project, pid)
        assert project.state == "cleanup_pending"
        assert quota_cleanup.native_intent(db, pid).mode == "delete"


def test_pending_cleanup_rejects_organization_transfer_without_reassigning_receipt(
    cleanup_case,
):
    from fastapi import HTTPException

    from app.models.managed_group import ManagedGroup
    from app.models.project import Project
    from app.models.user import User
    from app.services import organization_service, quota_cleanup

    q = cleanup_case
    with q.sessions() as db:
        actor = db.get(User, q.owner_id)
        actor.role = "admin"
        destination = ManagedGroup(
            name="cleanup-transfer-destination",
            normalized_name="cleanup-transfer-destination",
            is_organization=True,
        )
        db.add(destination)
        db.commit()
        intent = quota_cleanup.prepare_native(
            db, db.get(Project, q.project_ids[0]), "delete"
        )
        db.commit()
        with pytest.raises(HTTPException, match="Original native cleanup"):
            organization_service.transfer_user(
                db, actor, actor.id, destination.id, FixedDatetime.current
            )
        db.rollback()
        assert db.get(User, q.owner_id).organization_id == q.org_id
        assert (
            quota_cleanup.native_intent(db, q.project_ids[0]).organization_id
            == intent.organization_id
        )


@pytest.mark.parametrize("boundary", ["operation", "owner"])
def test_postgres_cleanup_serializes_operation_and_owner_boundaries(
    cleanup_case, boundary
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.models.project import Project
    from app.services import (
        quota_admission,
        quota_cleanup,
        quota_locks,
        quota_operations,
    )

    q = cleanup_case
    with q.sessions() as holder:
        if holder.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL lock boundary only")
        if boundary == "operation":
            quota_admission.lock_operations(
                holder,
                [
                    r.operation_id
                    for r in quota_operations._rows(holder, q.project_ids[0])
                ],
            )
        else:
            quota_locks.lock_rows(holder, user_ids=[q.owner_id])

        def contend():
            with q.sessions() as contender:
                contender.execute(text("SET LOCAL lock_timeout = '150ms'"))
                try:
                    quota_cleanup.prepare_native(
                        contender, contender.get(Project, q.project_ids[0]), "delete"
                    )
                except OperationalError as error:
                    assert error.orig.pgcode == "55P03"
                    assert (
                        "pg_advisory_xact_lock" if boundary == "operation" else "users"
                    ) in error.statement
                    contender.rollback()
                    return
                raise AssertionError("cleanup bypassed held original identity boundary")

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contend).result(timeout=5)
        holder.rollback()
    with q.sessions() as db:
        assert quota_cleanup.native_intent(db, q.project_ids[0]) is None
        receipt = quota_cleanup.prepare_native(
            db, db.get(Project, q.project_ids[0]), "delete"
        )
        db.commit()
        assert receipt.phase == "stopping"


@pytest.mark.parametrize("condition", ["ambiguous", "replacement"])
def test_child_marker_uncertainty_blocks_parent_delete_and_is_not_replayed(
    cleanup_case, condition
):
    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import quota_cleanup, quota_operations

    q = cleanup_case
    api = q.apis[q.project_ids[0]]
    original_patch = api.patch
    faulted = []

    def patch(value, changes, **kwargs):
        result = original_patch(value, changes, **kwargs)
        if (
            not faulted
            and value["kind"] == "TroshkaVM"
            and any(
                "troshka.redhat.com/managed-cleanup" in change.get("value", {})
                for change in changes
            )
        ):
            faulted.append(True)
            if condition == "ambiguous":
                raise TimeoutError("synthetic accepted marker response loss")
            api.values[value["kind"], value["metadata"]["name"]]["metadata"][
                "uid"
            ] = "replacement-vm-cr"
        return result

    api.patch = patch
    with q.sessions() as db:
        projects.delete_project(q.project_ids[0], db.get(User, q.owner_id), db)
    fn, args = q.queued.pop()
    fn(*args)
    before = deepcopy(api.calls)
    fn(*args)
    if condition == "replacement":
        assert api.calls == before
        assert not any(call[0] == "DELETE" for call in api.calls)
    else:
        assert (
            len(
                [
                    call
                    for call in api.calls
                    if call[0] == "DELETE" and call[1][0] == "TroshkaProject"
                ]
            )
            == 1
        )
        # Accepted marker write is independently observed, not submitted twice.
        markers = [
            call[1]
            for call in api.calls
            if call[0] == "PATCH"
            and call[1][0] == "TroshkaVM"
            and any(
                "troshka.redhat.com/managed-cleanup" in change.get("value", {})
                for change in call[2]
            )
        ]
        assert len(markers) == len(set(markers)) == 2
    with q.sessions() as db:
        assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
        assert quota_cleanup.native_intent(db, q.project_ids[0]).phase == (
            "teardown" if condition == "replacement" else "pending"
        )
        assert (
            sum(
                r.observed["storage_bytes"]
                for r in quota_operations._rows(db, q.project_ids[0])
            )
            == 30
        )


@pytest.mark.parametrize("change", ["replacement", "marker", "running", "unavailable"])
def test_presend_recovery_rejects_replacements_contradictions_and_unknown_reads(
    cleanup_case, monkeypatch, change
):
    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import quota_cleanup, quota_operations

    q = cleanup_case
    api = q.apis[q.project_ids[0]]
    submit = quota_cleanup.submit_native_teardown

    def lost_before_send(api, work):
        raise TimeoutError("synthetic original submission never sent")

    monkeypatch.setattr(quota_cleanup, "submit_native_teardown", lost_before_send)
    with q.sessions() as db:
        projects.delete_project(q.project_ids[0], db.get(User, q.owner_id), db)
    fn, args = q.queued.pop()
    fn(*args)
    with q.sessions() as db:
        assert quota_cleanup.native_intent(db, q.project_ids[0]).phase == "teardown"
    monkeypatch.setattr(quota_cleanup, "submit_native_teardown", submit)
    root = api.values["TroshkaProject", "project"]
    if change == "replacement":
        root["metadata"]["uid"] = "replacement-original-name"
    elif change == "marker":
        root["metadata"].setdefault("annotations", {})[
            "troshka.redhat.com/managed-cleanup"
        ] = "unknown-intent"
    elif change == "running":
        api.rows("VirtualMachine", None)
        next(v for (kind, _), v in api.values.items() if kind == "VirtualMachine")[
            "spec"
        ]["runStrategy"] = "Always"
    else:

        def unavailable(*args):
            raise TimeoutError("synthetic unresolved independent read")

        api.find = unavailable
    before = deepcopy(api.calls)
    fn(*args)
    assert api.calls == before
    with q.sessions() as db:
        assert db.get(Project, q.project_ids[0]).state == "cleanup_pending"
        assert quota_cleanup.native_intent(db, q.project_ids[0]).phase == "teardown"
        assert (
            sum(
                r.observed["storage_bytes"]
                for r in quota_operations._rows(db, q.project_ids[0])
            )
            == 30
        )


@pytest.mark.parametrize("mode", ["external", "unknown"])
def test_unowned_namespace_rejects_cleanup_before_any_mutation(
    cleanup_case, monkeypatch, mode
):
    from fastapi import HTTPException

    from app.api import projects
    from app.models.project import Project
    from app.models.user import User
    from app.services import quota_operations

    q = cleanup_case
    monkeypatch.setattr(
        "app.services.providers.base_namespace_grants.base_namespace_grants_mode",
        lambda: "managed",
    )
    with q.sessions() as db:
        row = quota_operations._rows(db, q.project_ids[0])[0]
        target = dict(row.dispatch_metadata["native_deploy"])
        if mode == "unknown":
            target.pop("base_grants_mode")
        else:
            target["base_grants_mode"] = "external"
        row.dispatch_metadata = {**row.dispatch_metadata, "native_deploy": target}
        db.commit()
        before = deepcopy(row.dispatch_metadata)
        with pytest.raises(HTTPException, match="namespace ownership"):
            projects.delete_project(q.project_ids[0], db.get(User, q.owner_id), db)
        db.rollback()
        assert (
            quota_operations._rows(db, q.project_ids[0])[0].dispatch_metadata == before
        )
        assert db.get(Project, q.project_ids[0]).state != "cleanup_pending"
    assert not q.queued
    assert all(not api.calls for api in q.apis.values())


@pytest.mark.parametrize(
    "condition", ["complete", "trash", "unresolved", "changed", "pool_changed"]
)
def test_original_image_absence_requires_complete_stable_identity_inventory(condition):
    class Transport:
        reads = 0

        def run(self, operation, **kwargs):
            values = {
                "fsid": "fsid",
                "pools": [{"poolname": "pool", "poolnum": 1}],
                "namespaces": [],
                "active": ["other"],
                "directory_keys": "id_other-id\nname_other",
                "trash_all": [],
            }
            if condition == "pool_changed":
                values["pools"][0]["poolnum"] = 2
            if condition == "trash":
                values["trash_all"] = [{"id": "original", "name": "old"}]
            if operation == "directory_keys":
                self.reads += 1
                if condition == "unresolved":
                    return ReadResult(status="unavailable", collected_at=NOW)
                value = (
                    "id_original"
                    if condition == "changed" and self.reads > 1
                    else values[operation]
                )
            else:
                value = values[operation]
            return ReadResult(
                status="complete",
                value=(
                    value
                    if operation in {"fsid", "directory_keys"}
                    else json.dumps(value)
                ),
                collected_at=NOW,
            )

    reader = RbdReader(
        Transport(),
        "fsid",
        [PoolConfig(pool_id=1, name="pool", csi_cluster_id="rook")],
        CapabilityProfile(command_version="rbd version 19.2.1"),
    )
    result = reader.image_inventory(
        ImageKey(ceph_fsid="fsid", pool_id=1, image_id="original"), max_objects=10
    )
    if condition in {"unresolved", "changed", "pool_changed"}:
        assert result.status != "complete"
    else:
        assert result.status == "complete"
        assert result.value.trash == (condition == "trash")
        assert not result.value.active
