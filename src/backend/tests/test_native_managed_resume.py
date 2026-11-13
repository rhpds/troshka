"""Real builders, fresh physical baseline, admission and worker lifecycle.

Only Kubernetes transport is synthetic; quota readiness, spending, admission,
allocation confirmation and monetary source coverage execute their real paths.
"""

import importlib.util
import os
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.host import Host
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import QuotaPolicy, QuotaReservation
from app.services import metering_service as metering
from app.services import native_managed_power as native
from app.services import quota_accounting as accounting
from app.services import quota_capacity_baseline as capacity
from app.services import quota_operations as operations
from tests.test_quota_capacity_baseline import fresh, native_reviewed
from tests.test_quota_capacity_baseline import managed_inventory as empty_inventory
from tests.test_quota_enforcement import REAL_SPEND
from tests.test_quota_native_storage import NOW, ingest
from tests.test_quota_native_storage import native_case as native_case_fixture
from tests.test_quota_native_storage import native_ledger as native_ledger_fixture

native_case = native_case_fixture
native_ledger = native_ledger_fixture


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("condition", ["ready", "stale", "partial"])
def test_usage_matches_active_fresh_baseline(
    resume_case, monkeypatch, enabled, condition
):
    from app.models.quota import QuotaReadiness
    from app.models.user import User
    from app.services import quota_usage

    q = resume_case
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: enabled)
    if condition == "stale":
        FixedDatetime.current += timedelta(hours=2)
    with q.sessions() as db:
        if condition == "partial":
            marker = db.get(QuotaReadiness, "baseline")
            marker.details = {**marker.details, "capacity_ready": False}
            db.commit()
        ready = capacity.check_ready(db)["ready"]
        assert ready == (condition == "ready")
        actor = db.get(User, q.owner_id)
        body = quota_usage.read_usage(
            db, actor, "project", q.project_ids[0], FixedDatetime.current
        )
        assert body.baseline_ready == ready
        assert body.enforcement_status == (
            "disabled" if not enabled else "active" if ready else "baseline_unready"
        )


@pytest.fixture(params=["sqlite", "postgres"])
def isolated_quota_database(request, tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker

    from app.core import database
    from tests import conftest

    postgres = request.param == "postgres"
    url = (
        os.getenv("QUOTA_TEST_POSTGRES_URL")
        if postgres
        else f"sqlite:///{tmp_path / 'resume.db'}"
    )
    if not url:
        pytest.skip("disposable PostgreSQL URL not configured")
    engine = create_engine(url)
    schema = "managed_resume_" + uuid4().hex
    if postgres:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        @event.listens_for(engine, "checkout")
        def search_path(connection, _record, _proxy):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            cursor.close()

    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(conftest, "TestSession", sessions)
    try:
        yield sessions
    finally:
        if postgres:
            with engine.begin() as connection:
                connection.execute(text("SET search_path TO public"))
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


class FixedDatetime(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


def support_module():
    native.helpers()
    path = Path(__file__).resolve().parents[2] / "operator/tests/test_managed_power.py"
    spec = importlib.util.spec_from_file_location("managed_resume_support", path)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    return support


def builder_inventory(project, identity, generation, support, binding):
    """Construct persisted originals from the actual VM and container builders."""
    from app.services.quota_native import _helpers

    power = native.helpers()
    supplied = empty_inventory(
        SimpleNamespace(id=project.id, topology={}), identity, generation
    )
    root = supplied["TroshkaProject"][0]
    root["spec"]["topology"] = project.topology
    root["metadata"]["resourceVersion"] = "1"
    topo, k8s, kv, _, vnc, *_ = _helpers()
    for vm in topo.extract_vms(project.topology):
        cr = topo.build_vm_cr(
            vm,
            {},
            {},
            {},
            None,
            root["metadata"]["namespace"],
            "project",
            root,
            project.topology,
        )
        cr["metadata"].update(
            uid=cr["metadata"]["name"] + "-uid",
            resourceVersion="1",
            generation=1,
            ownerReferences=[k8s.owner_ref(root)],
        )
        cr["status"] = {"observedGeneration": 1}
        supplied["TroshkaVM"].append(cr)
        body = kv.build_kubevirt_vm(cr, {}, {}, None)
        body["metadata"].update(
            uid=body["metadata"]["name"] + "-uid",
            resourceVersion="1",
            generation=1,
            ownerReferences=[k8s.owner_ref(cr)],
        )
        supplied["VirtualMachine"].append(body)
    pvc = support.resource(
        "PersistentVolumeClaim",
        "retained-disk",
        root,
        {"volumeName": "retained-pv"},
        {"phase": "Bound"},
    )
    pvc["metadata"].update(namespace=root["metadata"]["namespace"], uid=binding.pvc_uid)
    pv = support.resource(
        "PersistentVolume",
        "retained-pv",
        spec={
            "claimRef": {k: pvc["metadata"][k] for k in ("name", "namespace", "uid")}
        },
    )
    pv["metadata"]["uid"] = binding.pv_uid
    pv["metadata"].pop("namespace")
    supplied["PersistentVolumeClaim"].append(pvc)
    supplied["PersistentVolume"].append(pv)
    network = support.resource(
        "TroshkaNetwork",
        "net-basic",
        root,
        {"cidr": "10.1.0.0/24"},
        {"nadName": "net-basic-nad"},
    )
    network["metadata"]["namespace"] = root["metadata"]["namespace"]
    supplied["TroshkaNetwork"] = [network]
    supplied["Deployment"] = [
        k8s.build_dnsmasq_deployment(network),
        k8s.build_exec_deployment(root, "net-basic-nad"),
        k8s.build_gateway_deployment(root, ["net-basic-nad"]),
        vnc.build_vnc_proxy_deployment(
            "project", root["metadata"]["namespace"], owner_body=root
        ),
    ]
    for deployment in supplied["Deployment"]:
        deployment["metadata"].update(
            uid=deployment["metadata"]["name"] + "-uid", generation=1
        )
        deployment["status"] = {"observedGeneration": 1}
        deployment["metadata"]["resourceVersion"] = "1"
        deployment["spec"]["replicas"] = 1
    rows = {kind: supplied[kind] for kind in power.INVENTORY}
    manifests = power.container_manifests(root, rows)
    for pod in manifests.values():
        pod["metadata"].update(
            uid=pod["metadata"]["name"] + "-original", resourceVersion="1", generation=1
        )
        rows["Pod"].append(pod)
    by_uid = {v["metadata"]["uid"]: v for values in supplied.values() for v in values}
    state = power.snapshot(root, rows, by_uid, power.intent(root))
    state.update(producerDrained=True, state="off")
    root["status"]["managedPower"] = state
    for deployment in rows["Deployment"]:
        deployment["spec"]["replicas"] = 0
    for vm in rows["VirtualMachine"]:
        vm["spec"]["runStrategy"] = "Halted"
    supplied["Pod"] = []
    return supplied


@pytest.fixture
def resume_case(native_ledger, isolated_quota_database, monkeypatch, request):
    q = native_ledger
    q.vm_only = getattr(request, "param", None) == "vm-only"
    FixedDatetime.current = NOW
    support = support_module()
    monkeypatch.setattr(accounting, "spend", REAL_SPEND)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(capacity, "datetime", FixedDatetime)
    monkeypatch.setattr(native, "datetime", FixedDatetime)
    monkeypatch.setattr(operations, "now", lambda: FixedDatetime.current)
    monkeypatch.setattr(metering, "metering_enabled", lambda: True)
    inventories, apis = {}, {}
    with q.sessions() as db:
        for host in db.query(Host):
            host.host_type, host.billing_mode = "kubevirt-cluster", "shared"
            host.metering_rates = {
                "vcpu_hour": 0.1,
                "ram_gib_hour": 0.01,
                "disk_gib_hour": 0.001,
            }
        for project in db.query(Project):
            project.topology = {
                "nodes": [
                    {
                        "id": "tenant-vm-one",
                        "type": "vmNode",
                        "data": {"vcpus": 2, "ram": 1},
                    },
                    {
                        "id": "halted-vm-two",
                        "type": "vmNode",
                        "data": {"vcpus": 4, "ram": 2, "powerOnAtDeploy": False},
                    },
                    {
                        "id": "first-container",
                        "type": "containerNode",
                        "data": {
                            "image": "example/tenant",
                            "cpus": 0.25,
                            "memory": 128,
                        },
                    },
                    {
                        "id": "second-container",
                        "type": "containerNode",
                        "data": {
                            "image": "example/tenant",
                            "cpus": 0.25,
                            "memory": 128,
                        },
                    },
                    {"id": "retained-disk", "type": "storageNode", "data": {"size": 1}},
                ],
                "edges": [],
            }
            if q.vm_only:
                project.topology = {
                    **project.topology,
                    "nodes": [
                        n
                        for n in project.topology["nodes"]
                        if n["type"] != "containerNode"
                    ],
                }
        db.commit()
        fresh(db, NOW)
    ingest(q)

    def recorded(project, identity, generation):
        binding = next(
            b for b in q.report.volume_bindings if str(b.root.project_id) == project.id
        )
        return builder_inventory(project, identity, generation, support, binding)

    monkeypatch.setattr(
        "tests.test_quota_capacity_baseline.managed_inventory", recorded
    )
    with q.sessions() as db:
        manifest = native_reviewed(db)
        for project_id, entry in manifest["projects"].items():
            project = db.get(Project, project_id)
            row = db.query(QuotaReservation).filter_by(project_id=project_id).one()
            identity = row.dispatch_metadata["native_storage_identity"]
            binding = next(
                b
                for b in q.report.volume_bindings
                if str(b.root.project_id) == project_id
            )
            supplied = builder_inventory(
                project, identity, entry["generation_id"], support, binding
            )
            inventories[project_id] = supplied
            entry["compute_observation"]["managed_inventory"] = supplied
            # Existing monetary disk source is established in the fresh era.
            db.add(
                MeteringInterval(
                    project_id=project_id,
                    kind="disk",
                    resource_id="retained-disk",
                    qty=1,
                    unit_rate=0.001,
                    host_id=project.host_id,
                    provider_type="kubevirt",
                    started_at=NOW - timedelta(minutes=1),
                    quota_attribution=accounting.attribution(db, project),
                )
            )
        db.commit()
        # Adding current billing changes the review fingerprint; independently
        # reread the source immediately before the actual baseline import.
        manifest["inventory_digest"] = capacity.inventory(db)["inventory_digest"]
        report = capacity.run(db, evidence=manifest, now=NOW, apply=True)
        if not report["ready"]:
            import json

            raise AssertionError(json.dumps(report, indent=2, default=str))
        db.commit()

    class LifecycleAPI(support.FakeAPI):
        def request(self, method, kind, namespace, name, body):
            value = super().request(method, kind, namespace, name, body)
            if method == "POST" and kind == "Pod":
                # Kubernetes creates a new UID on every recreation. The shared
                # one-cycle fixture deliberately uses a fixed restored UID.
                key = kind, value["metadata"]["name"]
                value["metadata"]["uid"] = str(uuid4())
                self.values[key] = deepcopy(value)
            return value

    for project_id, supplied in inventories.items():
        apis[project_id] = LifecycleAPI(
            [v for values in supplied.values() for v in values]
        )

    def with_client(provider, function, *args):
        context = args[0]["context"] if "context" in args[0] else args[0]
        api = apis[context["root"]["project_id"]]
        return function(api, *args)

    monkeypatch.setattr(native, "with_client", with_client)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    # metering_service imports quotas_enabled directly.
    monkeypatch.setattr(metering, "quotas_enabled", lambda: True)
    q.apis, q.support = apis, support
    return q


def start_worker(q, expected_state="starting"):
    from app.services import deploy_service

    with q.sessions() as db:
        accounting.require_ready(db)
        project = db.get(Project, q.project_ids[0])
        project.state = "stopped"
        db.commit()
        operation_id = operations.prepare(db, project, "start")
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.requested == {
            "cpu": 2 if q.vm_only else 3,
            "ram_bytes": (1024 if q.vm_only else 1280) * 2**20,
            "storage_bytes": 0,
            "projects": 0,
        }
        assert (
            not db.query(MeteringInterval)
            .filter(
                MeteringInterval.kind.in_(("vcpu", "ram")),
                MeteringInterval.ended_at.is_(None),
            )
            .count()
        )
        project.state = "starting"
        db.commit()
    deploy_service.start_project_async(
        q.project_ids[0], quota_operation_id=operation_id
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["claim"]
        assert row.dispatch_metadata["native_handoff"]["state"] == "intent"
        assert row.state == "reconciling" and row.observed["cpu"] == 0
        assert db.get(Project, q.project_ids[0]).state == expected_state
    api = q.apis[q.project_ids[0]]
    root = api.rows("TroshkaProject", None)[0]
    assert native.helpers().intent(root)["requestId"] == operation_id
    return operation_id


def settle_running(q, project_id):
    api, support = q.apis[project_id], q.support
    power = native.helpers()
    root = api.rows("TroshkaProject", None)[0]
    power.reconcile_stop(api, root)
    state = api.get("TroshkaProject", None, "project")["status"]["managedPower"]
    assert state["state"] == "restoring", state.get("reason", state)
    ready = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    for value in list(api.values.values()):
        if value["kind"] == "Pod":
            value["status"] = deepcopy(ready)
        elif value["kind"] == "Deployment":
            replicas = value["spec"]["replicas"]
            value["status"] = {
                "observedGeneration": value["metadata"]["generation"],
                **{
                    k: replicas
                    for k in (
                        "replicas",
                        "readyReplicas",
                        "availableReplicas",
                        "updatedReplicas",
                    )
                },
            }
            rs = support.resource(
                "ReplicaSet",
                "rs-" + value["metadata"]["name"],
                value,
                {"replicas": replicas, "template": deepcopy(value["spec"]["template"])},
            )
            api.values["ReplicaSet", rs["metadata"]["name"]] = rs
            for index in range(replicas):
                pod = support.resource(
                    "Pod",
                    "pod-" + rs["metadata"]["name"] + str(index),
                    rs,
                    spec=deepcopy(value["spec"]["template"]["spec"]),
                    status=deepcopy(ready),
                )
                api.values["Pod", pod["metadata"]["name"]] = pod
        elif (
            value["kind"] == "VirtualMachine"
            and value["spec"].get("runStrategy") != "Halted"
        ):
            vmi = support.resource(
                "VirtualMachineInstance",
                "vmi-" + value["metadata"]["name"],
                value,
                spec=deepcopy(value["spec"]["template"]["spec"]),
                status=deepcopy(ready),
            )
            api.values["VirtualMachineInstance", vmi["metadata"]["name"]] = vmi
            pod = support.resource(
                "Pod",
                "launcher-" + vmi["metadata"]["name"],
                vmi,
                status=deepcopy(ready),
            )
            api.values["Pod", pod["metadata"]["name"]] = pod
    return api


def test_real_admission_claim_restore_observation_and_atomic_billing(resume_case):
    q = resume_case
    with q.sessions() as db:
        db.get(Project, q.project_ids[0]).auto_stop_minutes = 30
        db.commit()
    operation_id = start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed", row.dispatch_metadata
        assert row.observed == row.requested
        assert row.observed["storage_bytes"] == row.observed["projects"] == 0
        project = db.get(Project, q.project_ids[0])
        assert project.state == "active"
        assert accounting.utc(project.auto_stop_started_at) == FixedDatetime.current
        assert accounting.utc(
            project.auto_stop_expires_at
        ) == FixedDatetime.current + timedelta(minutes=30)
        accounting.require_ready(db)
        meters = (
            db.query(MeteringInterval)
            .filter_by(project_id=q.project_ids[0], ended_at=None)
            .all()
        )
        assert len(meters) == 7  # 3 configured running nodes, retained disk.
        assert {m.resource_id for m in meters} == {
            "tenant-vm-one",
            "first-container",
            "second-container",
            "retained-disk",
        }
        assert sorted(float(m.qty) for m in meters if m.kind == "vcpu") == [
            0.25,
            0.25,
            2,
        ]
        disk = next(m for m in meters if m.kind == "disk")
        assert float(disk.qty) == 1 and float(disk.unit_rate) == 0.001
        before = [(m.id, m.resource_id, m.kind) for m in meters]
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        after = [
            (m.id, m.resource_id, m.kind)
            for m in db.query(MeteringInterval).filter_by(
                project_id=q.project_ids[0], ended_at=None
            )
        ]
        assert sorted(after) == sorted(before)
    api = q.apis[q.project_ids[0]]
    root = api.get("TroshkaProject", None, "project")
    assert root["status"]["managedPower"]["state"] == "running"
    targets = deepcopy(root["status"]["managedPower"]["targets"])
    calls = deepcopy(api.calls)
    native.helpers().reconcile_stop(api, root)
    assert api.calls == calls
    assert (
        api.get("TroshkaProject", None, "project")["status"]["managedPower"]["targets"]
        == targets
    )


@pytest.mark.parametrize("scope", ["project", "user", "organization"])
def test_each_scope_denies_compute_before_handoff(resume_case, scope):
    from fastapi import HTTPException

    q = resume_case
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        identity = {
            "project": project.id,
            "user": q.owner_id,
            "organization": q.org_id,
        }[scope]
        db.add(QuotaPolicy(scope=scope, scope_id=identity, cpu=2))
        db.commit()
        with pytest.raises(HTTPException) as error:
            operations.prepare(db, project, "start")
        assert error.value.detail["dimension"] == "cpu"
        assert error.value.detail["scope"] == scope
        assert db.query(QuotaReservation).count() == 2
    assert not q.apis[q.project_ids[0]].calls


@pytest.mark.parametrize(
    "changed",
    [
        "helper_pending",
        "container_resources",
        "vm_resources",
        "new_stop",
        "extra_pod",
        "paused",
    ],
)
def test_partial_or_changed_runtime_never_confirms(resume_case, changed):
    q = resume_case
    operation_id = start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    api = settle_running(q, q.project_ids[0])
    if changed == "helper_pending":
        next(v for (kind, _), v in api.values.items() if kind == "Deployment")[
            "status"
        ]["readyReplicas"] = 0
    elif changed == "container_resources":
        api.values["Pod", "ctr-first-co"]["spec"]["containers"][0]["resources"][
            "requests"
        ]["cpu"] = "4"
    elif changed == "vm_resources":
        next(
            v for (kind, _), v in api.values.items() if kind == "VirtualMachineInstance"
        )["spec"]["domain"]["cpu"]["sockets"] = 2
    elif changed == "new_stop":
        root = api.values["TroshkaProject", "project"]
        import json

        root["metadata"]["annotations"][native.helpers().ANNOTATION] = json.dumps(
            {
                "version": 1,
                "mode": "stopped",
                "requestId": "new-stop",
                "allocationGeneration": "native-deploy-0",
            }
        )
    elif changed == "extra_pod":
        root = api.values["TroshkaProject", "project"]
        api.values["Pod", "unexpected"] = q.support.resource("Pod", "unexpected", root)
    else:
        next(
            v for (kind, _), v in api.values.items() if kind == "VirtualMachineInstance"
        )["status"]["conditions"].append({"type": "Paused", "status": "True"})
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "reconciling" and not any(row.observed.values())
        assert (
            not db.query(MeteringInterval)
            .filter(MeteringInterval.kind.in_(("vcpu", "ram")))
            .count()
        )


def test_direct_worker_and_duplicate_claim_cannot_dispatch(resume_case):
    q = resume_case
    from app.services.deploy_service import start_project_async

    with pytest.raises(
        accounting.AccountingUnavailable, match="original quota operation"
    ):
        start_project_async(q.project_ids[0])
    operation_id = start_worker(q)
    calls = deepcopy(q.apis[q.project_ids[0]].calls)
    with pytest.raises(accounting.AccountingUnavailable, match="observed"):
        start_project_async(q.project_ids[0], quota_operation_id=operation_id)
    assert q.apis[q.project_ids[0]].calls == calls


def test_ambiguous_handoff_is_observed_without_redispatch(resume_case, monkeypatch):
    q = resume_case
    original = native.with_client

    def lose_response(provider, function, *args):
        result = original(provider, function, *args)
        if function is native.publish_resume:
            raise OSError("synthetic response loss after accepted patch")
        return result

    monkeypatch.setattr(native, "with_client", lose_response)
    operation_id = start_worker(q, expected_state="error")
    FixedDatetime.current += timedelta(seconds=1)
    api = settle_running(q, q.project_ids[0])
    publications = len(
        [
            call
            for call in api.calls
            if call[1][0] == "TroshkaProject"
            and any(c["path"] == "/metadata/annotations" for c in call[2])
        ]
    )
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed"
        assert db.get(Project, q.project_ids[0]).deploy_error is None
    assert publications == 1
    assert (
        len(
            [
                call
                for call in api.calls
                if call[1][0] == "TroshkaProject"
                and any(c["path"] == "/metadata/annotations" for c in call[2])
            ]
        )
        == 1
    )


def test_missing_future_compute_rate_denies_start_without_repricing(
    resume_case, monkeypatch
):
    from fastapi import HTTPException

    q = resume_case
    monkeypatch.setattr(metering, "load_catalog", lambda: {})
    monkeypatch.setattr(metering, "load_type_rates", lambda db: {})
    monkeypatch.setattr(metering, "kubevirt_unit_rates", lambda: {})
    with q.sessions() as db:
        for host in db.query(Host):
            host.metering_rates = {"disk_gib_hour": 0.001}
        db.commit()
        before = [(r.id, r.qty, r.unit_rate) for r in db.query(MeteringInterval)]
        with pytest.raises(HTTPException) as error:
            operations.prepare(db, db.get(Project, q.project_ids[0]), "start")
        assert error.value.detail["reason"] == "accounting_unavailable"
        assert db.query(QuotaReservation).count() == 2
        assert [
            (r.id, r.qty, r.unit_rate) for r in db.query(MeteringInterval)
        ] == before


@pytest.mark.parametrize("trigger", ["overage", "api", "force", "timer"])
@pytest.mark.parametrize("failed_observation", [False, True])
@pytest.mark.parametrize("resume_case", ["mixed", "vm-only"], indirect=True)
def test_next_overage_stop_retains_storage_and_original_power(
    resume_case, monkeypatch, trigger, failed_observation
):
    from app.models.quota import QuotaAction
    from app.services import quota_enforcement as enforcement

    q = resume_case
    operation_id = start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    api = settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    FixedDatetime.current += timedelta(seconds=1)

    def execute(provider, context, request_id):
        FixedDatetime.current += timedelta(seconds=1)
        native.stop_and_observe(
            api, context, request_id, clock=lambda: FixedDatetime.current
        )
        power = native.helpers()
        power.reconcile_stop(api, api.get("TroshkaProject", None, "project"))
        for key in list(api.values):
            if key[0] in {"Pod", "VirtualMachineInstance"}:
                del api.values[key]
            elif key[0] == "ReplicaSet":
                api.values[key]["spec"]["replicas"] = 0
            elif key[0] == "Deployment":
                value = api.values[key]
                value["status"] = {
                    "observedGeneration": value["metadata"]["generation"]
                }
        power.reconcile_stop(api, api.get("TroshkaProject", None, "project"))
        return native.stop_and_observe(
            api, context, request_id, clock=lambda: FixedDatetime.current
        )

    monkeypatch.setattr(native, "execute", execute)
    monkeypatch.setattr(enforcement, "_now", lambda: FixedDatetime.current)
    if failed_observation:

        def unavailable(*args):
            raise accounting.AccountingUnavailable("synthetic observation failure")

        monkeypatch.setattr(native, "execute", unavailable)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        if trigger == "overage":
            project.budget_usd = 0
            db.commit()
            enforcement.evaluate(db, FixedDatetime.current)
            db.commit()
        else:
            from app.api import projects
            from app.models.user import User
            from app.services import project_timer

            queued = []
            monkeypatch.setattr(
                "app.core.redis.enqueue_job",
                lambda fn, *args, **kwargs: queued.append((fn, args)),
            )
            if trigger == "timer":
                project.auto_stop_expires_at = FixedDatetime.current - timedelta(
                    seconds=1
                )
                db.commit()
                project_timer._process_expired_timers(
                    db,
                    FixedDatetime.current,
                    {"auto_stop": [], "auto_delete": []},
                    False,
                )
            else:
                route = (
                    projects.force_stop_project
                    if trigger == "force"
                    else projects.stop_project
                )
                route(project.id, db.get(User, q.owner_id), db)
            for fn, args in queued:
                fn(*args)
            db.expire_all()
        action = (
            db.query(QuotaAction)
            .filter_by(project_id=project.id, kind="stop_run")
            .one()
        )
        if failed_observation and trigger != "overage":
            db.refresh(project)
            assert project.state == "stopping"
            project_timer._recover_one_stuck_project(
                db,
                project,
                {},
                False,
                get_job_info=lambda *args: None,
                get_progress=lambda *args: None,
                clear_job_ref=lambda *args: None,
            )
            assert project.state == "stopping"
            db.refresh(action)
            assert action.state == "failed" and action.last_error
            rows = operations.current_generation(operations._rows(db, project.id))
            assert sum(r.observed["cpu"] for r in rows) == (2 if q.vm_only else 3)
            assert sum(r.observed["storage_bytes"] for r in rows) == 30
            assert enforcement.fence_action(db, action.id, FixedDatetime.current)
            action.next_attempt_at = None
            db.commit()
        monkeypatch.setattr(native, "execute", execute)
        enforcement.reconcile_action(db, action.id, FixedDatetime.current)
        db.refresh(action)
        assert action.state == "completed", action.last_error
        rows = operations.current_generation(operations._rows(db, project.id))
        assert {r.operation_id for r in rows} == {"native-deploy-0", operation_id}
        assert all(r.observed["cpu"] == r.observed["ram_bytes"] == 0 for r in rows)
        assert sum(r.observed["storage_bytes"] for r in rows) == 30
        assert sum(r.observed["projects"] for r in rows) == 1
        meters = db.query(MeteringInterval).filter_by(project_id=project.id).all()
        assert all((m.ended_at is None) == (m.kind == "disk") for m in meters)
    snapshot = api.get("TroshkaProject", None, "project")["status"]["managedPower"]
    halted = next(t for t in snapshot["targets"] if "halted" in t["name"])
    assert halted["desired"] == {"runStrategy": "Halted"}
    if trigger != "overage":
        FixedDatetime.current += timedelta(seconds=1)
        operations.reconcile(q.project_ids[0])
        restart_id = start_worker(q)
        assert restart_id != operation_id
        FixedDatetime.current += timedelta(seconds=1)
        settle_running(q, q.project_ids[0])
        operations.reconcile(q.project_ids[0])
        with q.sessions() as db:
            assert db.get(Project, q.project_ids[0]).state == "active"
        if trigger == "timer":
            # Replay the actual delayed timer delivery after an admitted resume.
            # It belongs to the completed stop, never to the current generation.
            for fn, args in queued:
                fn(*args)
            with q.sessions() as db:
                assert db.get(Project, q.project_ids[0]).state == "active"


def test_concurrent_user_native_stops_share_one_durable_episode(
    resume_case, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.services import quota_enforcement as enforcement

    q = resume_case
    with q.sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL operation/identity locking contract")
    start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    monkeypatch.setattr(enforcement, "_now", lambda: FixedDatetime.current)
    barrier = Barrier(2)

    def request_stop():
        with q.sessions() as db:
            project = db.get(Project, q.project_ids[0])
            barrier.wait(timeout=10)
            action_id = enforcement.request_native_stop(db, project)
            db.commit()
            return action_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_stop) for _ in range(2)]
        assert len({f.result(timeout=15) for f in futures}) == 1
    with q.sessions() as db:
        rows = operations.current_generation(operations._rows(db, q.project_ids[0]))
        assert all(r.dispatch_metadata.get("cancelled") for r in rows)
        assert sum(r.observed["cpu"] for r in rows) == 3
        assert sum(r.observed["storage_bytes"] for r in rows) == 30


def test_pending_resume_has_dedicated_stop_context_and_preserves_original_targets(
    resume_case, monkeypatch
):
    from app.models.quota import QuotaAction
    from app.services import quota_enforcement as enforcement

    q = resume_case
    operation_id = start_worker(q)
    api = q.apis[q.project_ids[0]]
    patch = api.patch

    def stop_after_vm(value, changes, **kwargs):
        result = patch(value, changes, **kwargs)
        if value["kind"] == "VirtualMachine":
            raise RuntimeError("restore interrupted after first VM")
        return result

    monkeypatch.setattr(api, "patch", stop_after_vm)
    with pytest.raises(RuntimeError):
        native.helpers().reconcile_stop(api, api.get("TroshkaProject", None, "project"))
    monkeypatch.setattr(api, "patch", patch)
    FixedDatetime.current += timedelta(seconds=1)
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        project.budget_usd = 0
        db.commit()
        enforcement.evaluate(db, FixedDatetime.current)
        db.commit()
        run = (
            db.query(QuotaAction)
            .filter_by(project_id=project.id, kind="stop_run")
            .one()
        )
        assert enforcement._prepare_stop(db, run, FixedDatetime.current), run.last_error
        rows = operations.current_generation(operations._rows(db, project.id))
        with pytest.raises(accounting.AccountingUnavailable, match="unsettled"):
            native.retained_context(db, project, rows)
        context, request_id = (
            run.details["managed_context"],
            run.details["managed_stop_id"],
        )
        assert context["supersedes"] == "reviewed-native-off"
        db.commit()
    native.stop_and_observe(
        api, context, request_id, clock=lambda: FixedDatetime.current
    )
    calls = len(api.calls)
    native.helpers().reconcile_stop(api, api.get("TroshkaProject", None, "project"))
    state = api.get("TroshkaProject", None, "project")["status"]["managedPower"]
    assert state["requestId"] == request_id
    assert all(
        t["desired"]["replicas"] == 1
        for t in state["targets"]
        if t["kind"] == "Deployment"
    )
    assert not any(method == "POST" for method, _, _ in api.calls[calls:])
    assert all(v["spec"]["replicas"] == 0 for v in api.rows("Deployment", None))
    assert all(
        v["spec"]["runStrategy"] == "Halted" for v in api.rows("VirtualMachine", None)
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["cancelled"] and row.state == "reconciling"


def test_current_physical_reports_reconcile_independently_during_resume(resume_case):
    from app.services import quota_storage
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_quota_native_storage import revised

    q = resume_case
    operation_id = start_worker(q)
    FixedDatetime.current += timedelta(seconds=2)
    observed = FixedDatetime.current - timedelta(seconds=1)
    report = revised(
        q.report,
        started_at=observed,
        completed_at=observed,
        images=[
            image.model_copy(
                update={
                    "collected_at": observed,
                    "allocated_bytes": 40
                    if image.key.image_id == "child-1"
                    else image.allocated_bytes,
                }
            )
            for image in q.report.images
        ],
    )
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    for project_id in q.project_ids:
        quota_storage.reconcile_project(project_id)
    settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed"
        assert row.observed["storage_bytes"] == 0
        root = (
            db.query(QuotaReservation).filter_by(operation_id="native-deploy-0").one()
        )
        assert root.observed["storage_bytes"] == 40
        assert root.dispatch_metadata["native_storage_evidence"]["report_id"] == str(
            report.report_id
        )


def test_domain_extension_preserves_claimed_resume_target_and_confirms_once(
    resume_case,
):
    from app.models.kubevirt_storage import NativeEnrollment
    from app.schemas.quota import AccountingUnavailable
    from app.services import quota_storage
    from app.services.kubevirt_storage_enrollment import enroll_native_roots
    from app.services.kubevirt_storage_reports import import_report
    from tests.test_quota_native_storage import extension_manifest, revised

    q = resume_case
    operation_id = start_worker(q)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        target = deepcopy(row.dispatch_metadata["native_resume"])
        originals = deepcopy(row.dispatch_metadata["native_originals"])
        old_enrollment = deepcopy(db.get(NativeEnrollment, q.enrollment_id).payload)
        manifest, new_root = extension_manifest(db, q)
        enrollment_id = enroll_native_roots(
            db, manifest, expected_target_id=q.target_id
        )
        db.commit()
        with pytest.raises(AccountingUnavailable):
            quota_storage.scope_evidence(db, "user", q.owner_id, FixedDatetime.current)
        assert db.get(NativeEnrollment, q.enrollment_id).payload == old_enrollment
    FixedDatetime.current += timedelta(seconds=2)
    observed = FixedDatetime.current - timedelta(seconds=1)
    report = revised(
        q.report,
        project_roots=[*q.report.project_roots, new_root],
        started_at=observed,
        completed_at=observed,
        images=[
            i.model_copy(update={"collected_at": observed}) for i in q.report.images
        ],
    )
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=enrollment_id,
            expected_enrollment_digest=manifest.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    for project_id in q.project_ids:
        quota_storage.reconcile_project(project_id)
    settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed"
        assert row.dispatch_metadata["native_resume"] == target
        assert row.dispatch_metadata["native_originals"] == originals
        assert row.observed["cpu"] == 3 and row.observed["storage_bytes"] == 0
        assert (
            db.query(MeteringInterval)
            .filter_by(project_id=q.project_ids[0], kind="vm")
            .count()
            == 0
        )


@pytest.mark.parametrize("missing_extension", [False, True])
def test_concurrent_domain_extensions_compare_current_pointer(
    native_ledger, isolated_quota_database, missing_extension
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import text

    from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import digest, enroll_native_roots
    from tests.test_quota_native_storage import extension_manifest

    q = native_ledger
    with q.sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("row-lock concurrency requires disposable PostgreSQL")
        manifests = []
        for _ in range(2):
            manifest, _ = extension_manifest(db, q)
            if missing_extension:
                body = manifest.model_dump(mode="json", exclude={"digest"})
                body["pins"].pop("domain_extension")
                manifest = VerifiedEnrollment.model_validate(
                    {**body, "digest": digest(body)}
                )
            manifests.append(manifest)
        db.commit()
    barrier = Barrier(2)

    def apply(manifest):
        with q.sessions() as db:
            db.execute(text("SET LOCAL statement_timeout = '8000ms'"))
            cached = db.get(NativeCollectionDomain, str(manifest.pins.domain_id))
            assert cached.enrollment_id == q.enrollment_id
            barrier.wait(timeout=5)
            try:
                result = enroll_native_roots(
                    db, manifest, expected_target_id=q.target_id
                )
                db.commit()
                return result
            except ValueError as error:
                db.rollback()
                assert (
                    "explicit domain extension"
                    if missing_extension
                    else "no longer current"
                ) in str(error)
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, manifests))
    assert sum(result is not None for result in results) == (
        0 if missing_extension else 1
    )
    with q.sessions() as db:
        assert db.query(NativeEnrollment).count() == (1 if missing_extension else 2)
        assert db.query(QuotaReservation).count() == 2


def current_meters(db):
    return sorted(
        (m.id, m.kind, m.resource_id, str(m.qty), str(m.unit_rate), m.ended_at)
        for m in db.query(MeteringInterval)
    )


def test_settled_running_proof_is_refreshed_for_current_accounting(resume_case):
    q = resume_case
    operation_id = start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        meters = current_meters(db)
        accepted = deepcopy(
            db.query(QuotaReservation)
            .filter_by(operation_id=operation_id)
            .one()
            .dispatch_metadata["native_running_proof"]
        )
    FixedDatetime.current += timedelta(seconds=max(30, metering.poll_seconds()) + 1)
    with q.sessions() as db:
        assert not capacity.check_ready(db)["ready"]
        with pytest.raises(accounting.AccountingUnavailable):
            accounting.refresh(db, "project", q.project_ids[0], FixedDatetime.current)
    for project_id in q.project_ids:
        operations.reconcile(project_id)
    with q.sessions() as db:
        assert capacity.check_ready(db)["ready"]
        accounting.refresh(db, "project", q.project_ids[0], FixedDatetime.current)
        assert current_meters(db) == meters
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["native_running_proof"] == accepted
        assert all(
            accounting.utc(
                datetime.fromisoformat(
                    r.dispatch_metadata["native_current_power"]["proof"]["observed_at"]
                )
            )
            == FixedDatetime.current
            for r in db.query(QuotaReservation).filter(
                QuotaReservation.operation_id.in_(
                    ("native-deploy-0", "native-deploy-1")
                )
            )
        )


def test_fresh_start_preflight_updates_stopped_billing_proof(resume_case):
    q = resume_case
    FixedDatetime.current += timedelta(seconds=max(30, metering.poll_seconds()) + 1)
    operations.reconcile(q.project_ids[1])
    with q.sessions() as db:
        project = db.get(Project, q.project_ids[0])
        project.state = "stopped"
        db.commit()
        operation_id = operations.prepare(db, project, "start")
        assert operation_id
        assert capacity.check_ready(db)["ready"]
        project.state = "starting"
        db.commit()
    FixedDatetime.current += timedelta(seconds=max(30, metering.poll_seconds()) + 1)
    operations.reconcile(q.project_ids[1])
    from app.services.deploy_service import start_project_async

    start_project_async(q.project_ids[0], quota_operation_id=operation_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert (
            row.dispatch_metadata["claim"] and row.dispatch_metadata["native_handoff"]
        )


@pytest.mark.parametrize("failure", ["unavailable", "partial", "changed"])
@pytest.mark.parametrize("expired", [False, True])
def test_failed_periodic_collection_does_not_renew_stale_power_proof(
    resume_case, monkeypatch, failure, expired
):
    q = resume_case
    start_worker(q)
    FixedDatetime.current += timedelta(seconds=1)
    api = settle_running(q, q.project_ids[0])
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        before = sorted(
            (r.operation_id, r.observation_id, deepcopy(r.observed))
            for r in db.query(QuotaReservation)
        )
        meters = current_meters(db)
    FixedDatetime.current += timedelta(
        seconds=max(30, metering.poll_seconds()) + 1 if expired else 1
    )
    if failure == "partial":
        next(v for (k, _), v in api.values.items() if k == "VirtualMachineInstance")[
            "status"
        ]["phase"] = "Pending"
    elif failure == "changed":
        next(v for (k, _), v in api.values.items() if k == "PersistentVolumeClaim")[
            "metadata"
        ]["uid"] = "replaced-pvc"
    else:
        monkeypatch.setattr(
            native,
            "with_client",
            lambda *args: (_ for _ in ()).throw(OSError("collection unavailable")),
        )
    operations.reconcile(q.project_ids[0])
    with q.sessions() as db:
        assert not capacity.check_ready(db)["ready"]
        assert (
            sorted(
                (r.operation_id, r.observation_id, r.observed)
                for r in db.query(QuotaReservation)
            )
            == before
        )
        assert current_meters(db) == meters


def test_pre_4a_reviewed_baseline_profile_upgrade_is_read_only(
    resume_case, monkeypatch
):
    from app.models.quota import QuotaReadiness

    q = resume_case
    with q.sessions() as db:
        for row in db.query(QuotaReservation):
            row.dispatch_metadata = {
                k: v
                for k, v in row.dispatch_metadata.items()
                if k != "native_baseline_profile"
            }
        marker = db.get(QuotaReadiness, "baseline")
        marker.ready = False
        details = deepcopy(marker.details)
        details.update(ready=False, runtime_ready=False, accounting_ready=False)
        marker.details = details
        db.flush()
        marker.details = {
            **marker.details,
            "post_import_digest": capacity.source_digest(db),
        }
        db.commit()
        before = (
            capacity.source_digest(db),
            deepcopy(marker.details),
            capacity.preservation(db),
            current_meters(db),
        )
        for enabled in (False, True):
            monkeypatch.setattr(accounting, "quotas_enabled", lambda: enabled)
            if not enabled:
                native.refresh_settled(q.project_ids[0])
            assert capacity.check_ready(db)["ready"]
            assert (
                capacity.source_digest(db),
                marker.details,
                capacity.preservation(db),
                current_meters(db),
            ) == before
    FixedDatetime.current += timedelta(seconds=max(30, metering.poll_seconds()) + 1)
    for project_id in q.project_ids:
        operations.reconcile(project_id)
    with q.sessions() as db:
        assert capacity.check_ready(db)["ready"]
        marker = db.get(QuotaReadiness, "baseline")
        assert (
            marker.details,
            capacity.preservation(db),
            current_meters(db),
        ) == before[1:]
