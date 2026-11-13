"""Actual admission/enrollment/ledger paths, explicitly synthetic transports.

No fixture or test below establishes a physical initialization invariant.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from jsonschema import validate as validate_schema

from app.models.host import Host
from app.models.kubevirt_storage import NativeCollectionDomain, NativeEnrollment
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.quota import QuotaReservation
from app.schemas.kubevirt_storage import (
    ImageKey,
    ImageObservation,
    InitializationObservation,
    ReadResult,
    VolumeBinding,
)
from app.schemas.kubevirt_storage_enrollment import DomainExtension
from app.schemas.kubevirt_storage_initialization import InitializationEvidence
from app.services import native_initialization as initialization
from app.services import native_managed_collector as collector
from app.services import native_managed_deploy as deploy
from app.services import native_managed_power as power
from app.services import quota_accounting as accounting
from app.services import quota_operations as operations
from app.services.kubevirt_storage_enrollment import (
    digest,
    enroll_native_roots,
    prepare_native_enrollment,
    verify_native_enrollment,
)
from app.services.kubevirt_storage_reports import import_report
from tests.test_native_initialization_evidence import synthetic_profile
from tests.test_native_managed_resume import (
    FixedDatetime,
)
from tests.test_native_managed_resume import (
    isolated_quota_database as isolated_quota_database_fixture,
)
from tests.test_native_managed_resume import (
    native_ledger as native_ledger_fixture,
)
from tests.test_native_managed_resume import (
    resume_case as resume_case_fixture,
)
from tests.test_quota_native_storage import revised

REAL_IMPLEMENTATION = initialization.implementation
isolated_quota_database = isolated_quota_database_fixture
native_ledger = native_ledger_fixture
resume_case = resume_case_fixture


class SyntheticInitialization:
    """Test-only implementation selected by monkeypatch, never a real profile."""

    vnc_image = synthetic_profile()["vnc_image"]
    headroom_semantics = "synthetic-allocated-extents-headroom-v1"
    headroom = 1000000

    def observe(self, profile, kube, backend):
        actual = deepcopy(kube.target)
        sc = kube.storage_class(profile.target.storage_class_name)
        actual.update(
            storage_class_uid=sc["metadata"]["uid"],
            provisioner=sc["provisioner"],
            storage_class_parameters=sc["parameters"],
        )
        return InitializationObservation(
            version="native-initialization-observation-v1",
            metric=profile.metric,
            measurement_version=profile.measurement_version,
            profile_digest=digest(profile),
            target=actual,
            observed_at=FixedDatetime.current,
            headroom_bytes=self.headroom,
            headroom_semantics=self.headroom_semantics,
        )


class Reader:
    def __init__(self, q, target):
        self.q, self.target = q, deepcopy(target)

    def cluster_identity(self):
        return self.target["cluster_key"]

    def namespace(self, name):
        if name == "trusted-cache":
            return {"metadata": {"uid": "cache-ns"}}
        for api in self.q.apis.values():
            if ("Namespace", name) in api.values:
                return api.get("Namespace", None, name)
        raise KeyError(name)

    def projects(self, namespace):
        return [
            v
            for api in self.q.apis.values()
            for v in api.rows("TroshkaProject", namespace)
            if v["metadata"]["namespace"] == namespace
        ]

    def storage_class(self, name):
        assert name == self.target["storage_class_name"]
        return {
            "metadata": {"uid": self.target["storage_class_uid"]},
            "provisioner": self.target["provisioner"],
            "parameters": self.target["storage_class_parameters"],
        }

    def pending_root(self, target, operation_id, profile):
        return deploy.bind(
            self.q.apis[target["payload"]["spec"]["projectId"]],
            target,
            operation_id,
            profile,
        )


class Backend:
    def __init__(self, fsid):
        self.fsid = fsid

    def cluster_identity(self):
        return ReadResult(
            status="complete", value=self.fsid, collected_at=FixedDatetime.current
        )

    def pool_identity(self):
        return ReadResult(
            status="complete",
            value=[{"poolnum": 1}],
            collected_at=FixedDatetime.current,
        )


def project_spec_schema():
    path = Path(__file__).resolve().parents[2] / "operator/crds/troshkaproject.yaml"
    crd = yaml.safe_load(path.read_text())
    served = next(
        v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1" and v["served"]
    )
    return served["schema"]["openAPIV3Schema"]["properties"]["spec"]


def generic_api(q):
    class API(q.support.FakeAPI):
        def request(self, method, kind, namespace, name, body):
            if method == "DELETE":
                return super().request(method, kind, namespace, name, body)
            assert method == "POST"
            value = deepcopy(body)
            if kind == "TroshkaProject":
                if getattr(q, "require_namespace_grants", False):
                    from app.services.providers import kubevirt

                    for binding in (
                        kubevirt._PROVIDER_NS_BINDING,
                        kubevirt._OPERATOR_NS_BINDING,
                    ):
                        assert (
                            namespace,
                            binding,
                        ) in q.namespace_grants, (
                            "Forbidden: namespace write grant missing"
                        )
                schema = project_spec_schema()
                value["spec"] = {
                    key: item
                    for key, item in value["spec"].items()
                    if key in schema["properties"]
                }
                validate_schema(value["spec"], schema)
            name = value["metadata"]["name"]
            assert (kind, name) not in self.values
            self.calls.append((method, (kind, name), deepcopy(body)))
            value["metadata"].update(
                uid=kind + "-" + name + "-uid-" + str(len(self.calls)),
                resourceVersion="1",
                generation=1,
            )
            value.setdefault("status", {})
            if kind == "Service":
                value["spec"].update(
                    clusterIP="172.30.0.20",
                    clusterIPs=["172.30.0.20"],
                    ipFamilies=["IPv4"],
                    ipFamilyPolicy="SingleStack",
                    sessionAffinity="None",
                    internalTrafficPolicy="Cluster",
                )
            if kind == "Route":
                value["spec"].update(
                    host=name + ".apps.synthetic.example", wildcardPolicy="None"
                )
                value["spec"]["to"]["weight"] = 100
            if kind == "PersistentVolumeClaim":
                value["spec"]["volumeName"] = "pv-" + name
                value["status"] = {"phase": "Bound"}
                pv = q.support.resource(
                    "PersistentVolume",
                    "pv-" + name,
                    spec={
                        "claimRef": {
                            k: value["metadata"][k]
                            for k in ("name", "namespace", "uid")
                        }
                    },
                )
                pv["metadata"].pop("namespace")
                self.values["PersistentVolume", "pv-" + name] = pv
            self.values[kind, name] = value
            return deepcopy(value)

    system = q.support.resource("Namespace", "kube-system")
    system["metadata"]["uid"] = "cluster"
    return API([system])


@pytest.fixture
def deploy_case(resume_case, monkeypatch):
    q = resume_case
    monkeypatch.setattr(
        initialization, "implementation", lambda profile: SyntheticInitialization()
    )
    monkeypatch.setattr(collector, "datetime", FixedDatetime)
    with q.sessions() as db:
        old = db.get(NativeEnrollment, q.enrollment_id)
        from app.services.kubevirt_storage_enrollment import validated_enrollment

        original = validated_enrollment(old)
        host = db.query(Host).one()
        host.state = "active"
        host.agent_status = "connected"
        from app.models.provider import Provider

        db.get(Provider, host.provider_id).state = "active"
        q.host_id, q.provider_id = host.id, host.provider_id
        data = synthetic_profile()
        data["target"].update(
            provider_id=host.provider_id,
            cluster_key="cluster",
            ceph_fsid=q.report.pool_keys[0].ceph_fsid,
        )
        data.update(minimum_capacity_bytes=2**30, maximum_capacity_bytes=10 * 2**30)
        q.profile = InitializationEvidence.model_validate(data)
        q.reader, q.backend = Reader(q, data["target"]), Backend(
            data["target"]["ceph_fsid"]
        )
        request = prepare_native_enrollment(db, q.project_ids)
        pins = original.pins.model_copy(
            update={
                "initialization_evidence": q.profile,
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=old.id,
                    previous_enrollment_digest=old.digest,
                    initialization_authorization_digest=digest(q.profile),
                ),
            }
        )
        approved = verify_native_enrollment(
            request, {host.provider_id: q.reader}, q.backend, pins=pins
        )
        q.enrollment_id = enroll_native_roots(
            db, approved, expected_target_id=q.target_id
        )
        q.pins, q.digest = pins, approved.digest
        db.commit()
    advance_clock()
    ingest_current(q)
    q.new_id = str(uuid4())
    q.apis[q.new_id] = generic_api(q)
    with q.sessions() as db:
        db.add(
            Project(
                id=q.new_id,
                name="synthetic-new-native",
                owner_id=q.owner_id,
                state="draft",
                topology={
                    "placement": {"requires_kubevirt": True},
                    "nodes": [
                        {
                            "id": "first-container",
                            "type": "containerNode",
                            "data": {
                                "image": "synthetic/tenant:v1",
                                "cpus": 0.5,
                                "memory": 128,
                                "mounts": [
                                    {"diskNodeId": "new-disk", "mountPath": "/data"}
                                ],
                            },
                        },
                        {"id": "new-disk", "type": "storageNode", "data": {"size": 1}},
                    ],
                    "edges": [],
                },
            )
        )
        db.commit()
    from app.services.providers import kubevirt

    q.namespace_grants = {}

    class NamespaceRBAC:
        def __init__(self, api_client):
            pass

        def create_namespaced_role_binding(self, namespace, body):
            q.namespace_grants[namespace, body["metadata"]["name"]] = deepcopy(body)

        def read_namespaced_role_binding(self, namespace, name):
            return deepcopy(q.namespace_grants[namespace, name])

    monkeypatch.setattr("kubernetes.client.RbacAuthorizationV1Api", NamespaceRBAC)

    monkeypatch.setattr(
        kubevirt,
        "_get_k8s_clients",
        lambda provider: (
            None,
            SimpleNamespace(
                read_namespace=lambda name: q.apis[q.new_id].get(
                    "Namespace", None, name
                )
            ),
            SimpleNamespace(close=lambda: None),
        ),
    )

    class SyntheticClient:
        def __new__(cls, client=None):
            return q.apis[q.new_id]

    monkeypatch.setattr(power.helpers(), "PowerClient", SyntheticClient)

    def with_client(provider, function, *args):
        target = args[0]
        project_id = (
            target["payload"]["spec"]["projectId"]
            if "payload" in target
            else (target.get("context") or target)["root"]["project_id"]
        )
        return function(q.apis[project_id], *args)

    monkeypatch.setattr(power, "with_client", with_client)
    with q.sessions() as db:
        q.original_meters = monetary_identity(db, q.project_ids)
    return q


def test_pending_cr_requires_exact_namespace_write_grants(deploy_case):
    from app.services.providers import kubevirt

    q = deploy_case
    q.require_namespace_grants = True
    operation_id = start(q)
    target = current_target(q, operation_id)
    namespace = target["payload"]["metadata"]["namespace"]
    assert len(q.namespace_grants) == 2
    assert {ns for ns, _ in q.namespace_grants} == {namespace}
    assert (
        q.namespace_grants[namespace, kubevirt._PROVIDER_NS_BINDING]["roleRef"]["name"]
        == kubevirt.PROVIDER_NAMESPACED_ROLE
    )
    assert q.namespace_grants[namespace, kubevirt._OPERATOR_NS_BINDING]["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "troshka-operator",
            "namespace": "troshka-operator",
        }
    ]


@pytest.mark.parametrize("missing_grant", [False, True])
def test_pending_external_namespace_only_verifies_exact_grants(
    deploy_case, monkeypatch, missing_grant
):
    from kubernetes import client

    from app.models.provider import Provider
    from app.services.providers import kubevirt

    q = deploy_case
    api = q.apis[q.new_id]
    with q.sessions() as db:
        namespace = kubevirt._project_ns(db.get(Provider, q.provider_id), q.new_id)
    original = api.request(
        "POST",
        "Namespace",
        None,
        None,
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
    )
    rbac = client.RbacAuthorizationV1Api(None)
    kubevirt.ensure_troshka_namespaced_rbac(rbac, namespace)
    if missing_grant:
        del q.namespace_grants[namespace, kubevirt._OPERATOR_NS_BINDING]
    monkeypatch.setattr(kubevirt, "base_namespace_grants_mode", lambda: "external")
    writes = deepcopy(q.namespace_grants)
    if missing_grant:
        with pytest.raises(kubevirt.BaseNamespaceGrantsError):
            start(q)
        assert not api.rows("TroshkaProject", namespace)
    else:
        operation_id = start(q)
        target = current_target(q, operation_id)
        assert target["root"]["namespace_uid"] == original["metadata"]["uid"]
        assert target["base_grants_mode"] == "external"
    assert q.namespace_grants == writes
    assert len([c for c in api.calls if c[0] == "POST" and c[1][0] == "Namespace"]) == 1


def test_namespace_replacement_during_grants_retains_handoff_without_cr(
    deploy_case, monkeypatch
):
    from app.services.providers import kubevirt

    q = deploy_case
    original = kubevirt.ensure_troshka_namespaced_rbac

    def replace(rbac, namespace):
        original(rbac, namespace)
        q.apis[q.new_id].values["Namespace", namespace]["metadata"][
            "uid"
        ] = "replacement"

    monkeypatch.setattr(kubevirt, "ensure_troshka_namespaced_rbac", replace)
    with pytest.raises(accounting.AccountingUnavailable, match="namespace changed"):
        start(q)
    assert not q.apis[q.new_id].rows("TroshkaProject", None)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(project_id=q.new_id).one()
        assert row.dispatch_metadata["native_deploy"]["create_handoff"]
        assert (
            row.dispatch_metadata["native_deploy"]["create_namespace_uid"]
            != "replacement"
        )


def monetary_identity(db, project_ids):
    return sorted(
        (
            r.id,
            r.project_id,
            db.get(Project, r.project_id).owner_id,
            r.kind,
            str(r.qty),
            str(r.unit_rate),
            deepcopy(r.quota_attribution),
            r.started_at,
            r.ended_at,
        )
        for r in db.query(MeteringInterval).filter(
            MeteringInterval.project_id.in_(project_ids)
        )
    )


def advance_clock():
    FixedDatetime.current += timedelta(seconds=2)


def report_for(q, manifest=None, amount=16):
    roots = manifest.pins.project_roots if manifest else q.report.project_roots
    stamp = FixedDatetime.current
    images = [i.model_copy(update={"collected_at": stamp}) for i in q.report.images]
    bindings = list(q.report.volume_bindings)
    if getattr(q, "new_id", None) in q.apis:
        claims = q.apis[q.new_id].rows("PersistentVolumeClaim", None)
        if claims:
            root = next(r for r in roots if str(r.project_id) == q.new_id)
            for claim in claims:
                pv = q.apis[q.new_id].get(
                    "PersistentVolume", None, claim["spec"]["volumeName"]
                )
                image = ImageKey(
                    ceph_fsid=q.profile.target.ceph_fsid,
                    pool_id=1,
                    image_id="synthetic-" + claim["metadata"]["uid"],
                )
                bindings.append(
                    VolumeBinding(
                        root=root,
                        pvc_uid=claim["metadata"]["uid"],
                        pv_uid=pv["metadata"]["uid"],
                        csi_driver=q.profile.target.provisioner,
                        volume_handle="synthetic-" + claim["metadata"]["uid"],
                        image_key=image,
                        provisioned_bytes=2**30,
                        owner_reference_uids=[root.project_cr_uid],
                    )
                )
                images.append(
                    ImageObservation(
                        key=image,
                        collected_at=stamp,
                        allocated_bytes=amount,
                        provisioned_bytes=2**30,
                        measurement_status="complete",
                        command_version="synthetic-rbd-v1",
                        measurement_semantics_version="kubevirt-rbd-allocated-v1",
                    )
                )
    report = revised(
        q.report,
        project_roots=roots,
        started_at=stamp,
        completed_at=stamp,
        volume_bindings=bindings,
        images=images,
        initialization_observation=initialization.observe(
            q.profile, q.reader, q.backend, clock=lambda: stamp
        ),
    )
    return report


def ingest_current(q):
    report = report_for(q)
    with q.sessions() as db:
        import_report(
            db,
            report,
            enrollment_id=q.enrollment_id,
            expected_enrollment_digest=q.digest,
            now=FixedDatetime.current,
        )
        db.commit()
    collector.reconcile_allocations(q.sessions, SimpleNamespace(pins=q.pins))


def prepare_claim(q):
    from app.services import placement

    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        operation_id = operations.prepare(db, project, "deploy", host_id=q.host_id)
        db.commit()
        placement.place_project(
            db, project, host_id=q.host_id, quota_operation_id=operation_id
        )
        db.commit()
    with q.sessions() as db:
        operations.claim(db, q.new_id, operation_id, "deploy")
    return operation_id


def start(q):
    operation_id = prepare_claim(q)
    token = operations.current_operation.set(operation_id)
    try:
        with q.sessions() as db:
            deploy.dispatch(db, db.get(Project, q.new_id))
    finally:
        operations.current_operation.reset(token)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        row.state = "reconciling"
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "worker_finished": True,
            "worker_finished_at": FixedDatetime.current.isoformat(),
        }
        db.commit()
    advance_clock()
    deploy.reconcile(q.new_id)
    return operation_id


@pytest.mark.parametrize("job_status", ["finished", None])
@pytest.mark.parametrize("dry_run", [False, True])
def test_timer_preserves_durable_native_deploy(
    deploy_case, monkeypatch, job_status, dry_run
):
    from unittest.mock import Mock

    from app.services import project_timer

    q = deploy_case
    start(q)
    clear = Mock()
    monkeypatch.setattr(project_timer, "_notify", Mock())
    result = {}
    job_info = {"status": job_status, "job_id": "completed-job"} if job_status else None
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        project_timer._recover_one_stuck_project(
            db,
            project,
            result,
            dry_run,
            get_job_info=lambda _: job_info,
            get_progress=lambda _: None,
            clear_job_ref=clear,
        )
        assert project.state == "deploying"
        assert not project.deploy_error
    assert not result.get("stuck_recovered")
    clear.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        "failed",
        "canceled",
        "stopped",
        "cancelled-operation",
        "released",
        "wrong-generation",
        "wrong-host",
        "wrong-kind",
        "reserved",
        "unfinished",
        "ordinary-reservation",
    ],
)
def test_timer_does_not_hide_lost_native_deploy(deploy_case, monkeypatch, invalid):
    from unittest.mock import Mock

    from app.services import project_timer

    q = deploy_case
    operation_id = start(q)
    monkeypatch.setattr(project_timer, "_notify", Mock())
    result = {}
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        if invalid == "cancelled-operation":
            metadata["cancelled"] = True
        elif invalid == "released":
            row.state = "released"
        elif invalid == "wrong-generation":
            metadata["generation_id"] = str(uuid4())
        elif invalid == "wrong-host":
            project.host_id = None
        elif invalid == "wrong-kind":
            metadata["kind"] = "start"
        elif invalid == "reserved":
            metadata["native_deploy"]["phase"] = "reserved"
        elif invalid == "unfinished":
            metadata["worker_finished"] = False
        elif invalid == "ordinary-reservation":
            metadata.pop("native_deploy")
        row.dispatch_metadata = metadata
        db.commit()
        status = invalid if invalid in {"failed", "canceled", "stopped"} else "finished"
        project_timer._recover_one_stuck_project(
            db,
            project,
            result,
            False,
            get_job_info=lambda _: {"status": status, "job_id": "completed-job"},
            get_progress=lambda _: None,
            clear_job_ref=Mock(),
        )
        assert project.state == "error"
    assert result["stuck_recovered"] == [q.new_id]


def test_pending_cr_pins_survive_shipped_schema_pruning(deploy_case):
    q = deploy_case
    operation_id = start(q)
    target = current_target(q, operation_id)
    actual = q.apis[q.new_id].rows("TroshkaProject", None)[0]
    assert actual["spec"] == target["payload"]["spec"]
    for field in ("managedVncImage", "managedPlanDigest"):
        assert actual["spec"][field]
        invalid = deepcopy(actual["spec"])
        invalid[field] = 12
        with pytest.raises(Exception):
            validate_schema(invalid, project_spec_schema())
    invalid = deepcopy(actual["spec"])
    invalid["managedPlanDigest"] = "invalid-digest"
    with pytest.raises(Exception):
        validate_schema(invalid, project_spec_schema())
    source = Path(__file__).resolve().parents[3]
    for path in (
        "src/operator/kustomization.yaml",
        "src/operator/deploy/kustomization.yaml",
    ):
        kustomization = source / path
        packaged = [
            kustomization.parent / entry
            for entry in yaml.safe_load(kustomization.read_text())["resources"]
        ]
        assert source / "src/operator/crds/troshkaproject.yaml" in [
            entry.resolve() for entry in packaged
        ]
    assert (
        "COPY src/operator/crds/ /opt/app-root/operator/crds/"
        in (source / "deploy/containerfiles/Containerfile.backend").read_text()
    )


def test_ambiguous_pending_cr_create_is_bound_without_redispatch(
    deploy_case, monkeypatch
):
    q = deploy_case
    operation_id = prepare_claim(q)
    api = q.apis[q.new_id]
    request = api.request

    def ambiguous(method, kind, namespace, name, body):
        value = request(method, kind, namespace, name, body)
        if kind == "TroshkaProject":
            raise OSError("synthetic lost accepted create response")
        return value

    monkeypatch.setattr(api, "request", ambiguous)
    token = operations.current_operation.set(operation_id)
    try:
        with q.sessions() as db:
            with pytest.raises(OSError):
                deploy.dispatch(db, db.get(Project, q.new_id))
        calls = deepcopy(api.calls)
        with pytest.raises(Exception, match="Ambiguous native creation"):
            deploy.create_pending(None, q.new_id)
        assert api.calls == calls
    finally:
        operations.current_operation.reset(token)
    deploy.reconcile(q.new_id)
    assert current_target(q, operation_id)["phase"] == "bound"
    assert not api.rows("PersistentVolumeClaim", None)


def settle_runtime(q):
    """Synthetic API status/children only; production verifiers remain real."""
    api, support = q.apis[q.new_id], q.support
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
            rs["metadata"]["namespace"] = value["metadata"]["namespace"]
            api.values["ReplicaSet", rs["metadata"]["name"]] = rs
            for index in range(replicas):
                pod = support.resource(
                    "Pod",
                    "pod-" + rs["metadata"]["name"] + str(index),
                    rs,
                    spec=deepcopy(value["spec"]["template"]["spec"]),
                    status=deepcopy(ready),
                )
                pod["metadata"]["namespace"] = value["metadata"]["namespace"]
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
            vmi["metadata"]["namespace"] = value["metadata"]["namespace"]
            api.values["VirtualMachineInstance", vmi["metadata"]["name"]] = vmi
            launcher = support.resource(
                "Pod",
                "launcher-" + vmi["metadata"]["name"],
                vmi,
                status=deepcopy(ready),
            )
            launcher["metadata"]["namespace"] = value["metadata"]["namespace"]
            api.values["Pod", launcher["metadata"]["name"]] = launcher


def stop_and_resume(q, original_id, cpu, physical, monkeypatch):
    from app.models.quota import QuotaAction
    from app.services import deploy_service
    from app.services import quota_enforcement as enforcement

    api = q.apis[q.new_id]

    def execute(provider, context, request_id):
        try:
            return stop_transport(context, request_id)
        except Exception as error:
            q.stop_error = repr(error)
            raise

    def stop_transport(context, request_id):
        advance_clock()
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
                value = api.values[key]
                value["status"] = {
                    "observedGeneration": value["metadata"]["generation"]
                }
        power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
        return power.stop_and_observe(
            api, context, request_id, clock=lambda: FixedDatetime.current
        )

    monkeypatch.setattr(power, "execute", execute)
    monkeypatch.setattr(enforcement, "_now", lambda: FixedDatetime.current)
    operations.reconcile(q.new_id)
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        project.budget_usd = 0
        db.commit()
        enforcement.evaluate(db, FixedDatetime.current)
        db.commit()
        action = (
            db.query(QuotaAction).filter_by(project_id=q.new_id, kind="stop_run").one()
        )
        enforcement.reconcile_action(db, action.id, FixedDatetime.current)
        db.refresh(action)
        assert action.state == "completed", (
            action.last_error,
            getattr(q, "stop_error", None),
        )
        row = db.query(QuotaReservation).filter_by(operation_id=original_id).one()
        assert row.observed["cpu"] == 0 and row.observed["storage_bytes"] == physical
        project.budget_usd = 100
        db.commit()
    advance_clock()
    operations.reconcile(q.new_id)
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        resume_id = operations.prepare(db, project, "start")
        row = db.query(QuotaReservation).filter_by(operation_id=resume_id).one()
        assert row.requested["cpu"] == cpu and row.requested["storage_bytes"] == 0
        project.state = "starting"
        db.commit()
    deploy_service.start_project_async(q.new_id, quota_operation_id=resume_id)
    power.helpers().reconcile_stop(api, api.rows("TroshkaProject", None)[0])
    state = api.rows("TroshkaProject", None)[0]["status"]["managedPower"]
    assert state.get("producerDrained"), state.get("reason")
    settle_runtime(q)
    advance_clock()
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=resume_id).one()
        target = deepcopy(row.dispatch_metadata["native_resume"])
    power.observe_running(api, target, resume_id)
    operations.reconcile(q.new_id)
    with q.sessions() as db:
        rows = operations.current_generation(operations._rows(db, q.new_id))
        assert {r.operation_id for r in rows} == {original_id, resume_id}
        assert all(r.state == "confirmed" for r in rows)
        assert sum(r.observed["cpu"] for r in rows) == cpu
        assert sum(r.observed["storage_bytes"] for r in rows) == physical
    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        lambda manifest, previous, started: report_for(q, manifest, 32),
    )
    operations.reconcile(q.new_id)
    with q.sessions() as db:
        accounting.require_ready(db)
        assert monetary_identity(db, q.project_ids) == q.original_meters


@pytest.mark.parametrize("timer_error", [False, True])
@pytest.mark.parametrize("amount,with_vm", [(16, False), (32, True)])
def test_real_first_deploy_admission_pending_and_automatic_enrollment(
    deploy_case, amount, with_vm, monkeypatch, timer_error
):
    q = deploy_case
    if with_vm:
        with q.sessions() as db:
            project = db.get(Project, q.new_id)
            topology = deepcopy(project.topology)
            topology["nodes"].extend(
                [
                    {
                        "id": "new-vm-one",
                        "type": "vmNode",
                        "data": {"vcpus": 2, "ram": 1},
                    },
                    {"id": "vm-disk", "type": "storageNode", "data": {"size": 1}},
                    {
                        "id": "second-container",
                        "type": "containerNode",
                        "data": {
                            "image": "synthetic/tenant:v1",
                            "cpus": 0.25,
                            "memory": 128,
                        },
                    },
                ]
            )
            topology["edges"].append(
                {"id": "disk-vm", "source": "vm-disk", "target": "new-vm-one"}
            )
            project.topology = topology
            db.commit()
    cpu, claims = (3, 2) if with_vm else (1, 1)
    physical = amount * claims
    operation_id = start(q)
    with q.sessions() as db:
        if timer_error:
            project = db.get(Project, q.new_id)
            project.state = "error"
            project.deploy_error = "Background job lost while deploying — please retry"
            db.commit()
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert (
            row.requested["cpu"] == cpu
            and row.requested["storage_bytes"] == 32 * claims
            and row.requested["projects"] == 1
        )
        assert row.dispatch_metadata["native_deploy"]["phase"] == "bound"
    api = q.apis[q.new_id]
    assert not any(v["kind"] == "PersistentVolumeClaim" for v in api.values.values())
    advance_clock()

    def collect(manifest, previous, started):
        return report_for(q, manifest, amount)

    report = collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        collect,
    )
    assert len(report.project_roots) == 3
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert (
            row.dispatch_metadata["native_deploy"]["phase"] == "initialization-intent"
        )
    # Publish storage-only admission; operator initializes exact halted objects.
    deploy.reconcile(q.new_id)
    root = api.rows("TroshkaProject", None)[0]
    deploy.helpers().reconcile(api, root)
    assert api.rows("PersistentVolumeClaim", None)
    assert not api.rows("Pod", None)
    assert api.rows("Deployment", None)[0]["spec"]["replicas"] == 0
    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        collect,
    )
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        target = deepcopy(row.dispatch_metadata["native_deploy"])
    deploy.advance(q.new_id, operation_id, target, q.profile, SimpleNamespace())
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.dispatch_metadata["native_deploy"]["phase"] == "runtime-intent"
        assert row.observed["storage_bytes"] == physical
        from app.services.quota_admission import usage

        used, reserved = usage(db, "project", q.new_id)
        assert used.storage_bytes == physical and reserved.storage_bytes == 0
        assert reserved.cpu == cpu and reserved.projects == 1
    # Runtime publication alone never confirms compute or releases the slot.
    deploy.reconcile(q.new_id)
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    settle_runtime(q)
    advance_clock()
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        target = deepcopy(row.dispatch_metadata["native_deploy"])
        assert row.state == "reconciling"
    deploy.advance(q.new_id, operation_id, target, q.profile, SimpleNamespace())
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "confirmed"
        assert row.requested["storage_bytes"] == 32 * claims
        used, reserved = usage(db, "project", q.new_id)
        assert (used.cpu, used.storage_bytes, used.projects) == (cpu, physical, 1)
        assert not any(reserved.model_dump().values())
        assert db.get(Project, q.new_id).state == "active"
        assert db.get(Project, q.new_id).deploy_error is None
        disks = (
            db.query(MeteringInterval)
            .filter_by(project_id=q.new_id, kind="disk", ended_at=None)
            .all()
        )
        assert len(disks) == claims and all(float(d.qty) == 1 for d in disks)
    if with_vm:
        stop_and_resume(q, operation_id, cpu, physical, monkeypatch)


def initialized(q, amount=16, report_change=None):
    operation_id = start(q)

    def collect(manifest, previous, started):
        report = report_for(q, manifest, amount)
        return report_change(report) if report_change else report

    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        collect,
    )
    deploy.reconcile(q.new_id)
    deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    advance_clock()
    collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        collect,
    )
    return operation_id


def test_startup_allowance_reserves_protected_amount_not_virtual_capacity(deploy_case):
    q = deploy_case
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        operation_id = operations.prepare(db, project, "deploy", host_id=q.host_id)
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert (
            row.requested["storage_bytes"]
            == q.profile.initial_storage_bound_bytes
            == 32
        )
        target = row.dispatch_metadata["native_deploy"]
        assert target["initial_bound"] == 32
        assert target["profile_digest"] == digest(q.profile)
        planned = deploy.helpers().render(
            {
                **target["payload"],
                "metadata": {**target["payload"]["metadata"], "uid": "pending"},
            },
            q.profile.target.storage_class_name,
        )
        claims = [v for v in planned if v["kind"] == "PersistentVolumeClaim"]
        assert [v["spec"]["resources"]["requests"]["storage"] for v in claims] == [
            "1Gi"
        ]
        db.rollback()
    assert not q.apis[q.new_id].calls


@pytest.mark.parametrize("authorization", ["missing", "old", "current"])
def test_startup_allowance_changes_require_protected_enrollment_authorization(
    deploy_case, authorization
):
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    q = deploy_case
    with q.sessions() as db:
        old = db.get(NativeEnrollment, q.enrollment_id)
        manifest = validated_enrollment(old)
        changed = q.profile.model_copy(update={"initial_storage_bound_bytes": 64})
        reference = {
            "missing": None,
            "old": digest(q.profile),
            "current": digest(changed),
        }[authorization]
        pins = manifest.pins.model_copy(
            update={
                "initialization_evidence": changed,
                "domain_extension": DomainExtension(
                    version="native-domain-extension-v1",
                    previous_enrollment_id=old.id,
                    previous_enrollment_digest=old.digest,
                    initialization_authorization_digest=reference,
                ),
            }
        )
        approved = verify_native_enrollment(
            prepare_native_enrollment(db, q.project_ids),
            {q.provider_id: q.reader},
            q.backend,
            pins=pins,
        )
        if authorization == "current":
            record = enroll_native_roots(db, approved, expected_target_id=q.target_id)
            assert (
                validated_enrollment(
                    db.get(NativeEnrollment, record)
                ).pins.initialization_evidence
                == changed
            )
        else:
            with pytest.raises(ValueError, match="authorization"):
                enroll_native_roots(db, approved, expected_target_id=q.target_id)
        db.rollback()


def test_first_initialization_rejects_retained_source_but_charges_all_bytes(
    deploy_case,
):
    q = deploy_case

    def retained(report):
        bindings = [
            b for b in report.volume_bindings if str(b.root.project_id) == q.new_id
        ]
        return revised(report, retained_bindings=[*report.retained_bindings, *bindings])

    operation_id = initialized(q, report_change=retained)
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.observed["storage_bytes"] == 16 and row.observed["cpu"] == 0
        assert (
            row.dispatch_metadata["native_deploy"]["blocker"]
            == "initialization_not_blank"
        )
        assert "native_initialization_consumed" not in row.dispatch_metadata
    api = q.apis[q.new_id]
    assert not api.rows("Pod", None)
    assert api.rows("Deployment", None)[0]["spec"]["replicas"] == 0


def test_first_initialization_blank_guard_checks_current_image_graph(deploy_case):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    q = deploy_case
    operation_id = initialized(q)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        from app.services import quota_storage

        measurement = quota_storage._native_measurement(
            db, db.get(Project, q.new_id), [row], operations.now()
        )
        source = db.get(NativeStorageSnapshot, measurement.report_id).payload
    assert deploy.blank_initialization(source, q.new_id)
    binding = next(
        b for b in source["volume_bindings"] if b["root"]["project_id"] == q.new_id
    )
    for field in ("parent", "snapshots", "children"):
        changed = deepcopy(source)
        image = next(i for i in changed["images"] if i["key"] == binding["image_key"])
        image[field] = (
            {"unexpected": True} if field == "parent" else [{"unexpected": True}]
        )
        assert not deploy.blank_initialization(changed, q.new_id)
    changed = deepcopy(source)
    changed["snapshot_evidence"] = [{"root": binding["root"]}]
    assert not deploy.blank_initialization(changed, q.new_id)
    changed = deepcopy(source)
    changed["images"] = [
        i for i in changed["images"] if i["key"] != binding["image_key"]
    ]
    assert not deploy.blank_initialization(changed, q.new_id)


def test_above_bound_storage_held_and_billed_once_without_compute(deploy_case):
    q = deploy_case
    operation_id = initialized(q, 48)
    api = q.apis[q.new_id]
    for _ in range(2):
        advance_clock()
        collector.cycle(
            q.sessions,
            q.pins,
            q.target_id,
            q.report.collector_id,
            {q.provider_id: q.reader},
            q.backend,
            lambda manifest, previous, started: report_for(q, manifest, 48),
        )
        deploy.reconcile(q.new_id)
        with q.sessions() as db:
            row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
            assert (
                row.dispatch_metadata["native_deploy"]["blocker"]
                == "initialization_bound_exceeded"
            )
            assert row.observed["storage_bytes"] == 48 and row.observed["cpu"] == 0
            assert row.requested["storage_bytes"] == 32 and row.state == "reconciling"
            assert "native_initialization_consumed" not in row.dispatch_metadata
            intervals = (
                db.query(MeteringInterval)
                .filter_by(project_id=q.new_id, ended_at=None)
                .all()
            )
            assert [(r.kind, float(r.qty)) for r in intervals] == [("disk", 1)]
            assert monetary_identity(db, q.project_ids) == q.original_meters
        assert not api.rows("Pod", None)
        assert api.rows("Deployment", None)[0]["spec"]["replicas"] == 0


def test_pending_enrollment_failure_invalidates_latest_attempt(
    deploy_case, monkeypatch
):
    q = deploy_case
    start(q)

    def fail(*args):
        raise ValueError("synthetic independent read failed")

    monkeypatch.setattr(q.reader, "pending_root", fail)
    report = collector.cycle(
        q.sessions,
        q.pins,
        q.target_id,
        q.report.collector_id,
        {q.provider_id: q.reader},
        q.backend,
        fail,
    )
    assert report.collection_errors
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        assert domain.latest_report_id == str(report.report_id)
        assert domain.enrollment_id == q.enrollment_id
        with pytest.raises(Exception, match="unavailable|complete|partial"):
            initialization.current(db, q.provider_id, FixedDatetime.current)


@pytest.mark.parametrize(
    "change", ["proof", "generation", "report", "root", "reference"]
)
def test_consumption_requires_exact_complete_reference(deploy_case, change):
    q = deploy_case
    operation_id = initialized(q)
    deploy.reconcile(q.new_id)
    from app.services.quota_admission import remaining_vector

    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        metadata = deepcopy(row.dispatch_metadata)
        target = metadata["native_deploy"]
        if change == "proof":
            target.pop("initialization_proof")
        elif change == "generation":
            target["initialization_proof"]["generation"] = str(uuid4())
        elif change == "report":
            target["initialized_report"]["report_id"] = str(uuid4())
        elif change == "root":
            target["root"]["project_cr_uid"] = "replacement"
        else:
            metadata["native_initialization_consumed"]["pvc_uids"] = ["other"]
        row.dispatch_metadata = metadata
        with pytest.raises(Exception, match="consumption"):
            remaining_vector(row)


def test_real_profile_and_custom_network_close_before_reservation_or_io(
    deploy_case, monkeypatch
):
    q = deploy_case
    monkeypatch.setattr(initialization, "implementation", REAL_IMPLEMENTATION)
    with q.sessions() as db:
        with pytest.raises(Exception, match="physical acceptance"):
            operations.prepare(
                db, db.get(Project, q.new_id), "deploy", host_id=q.host_id
            )
        db.rollback()
        assert not db.query(QuotaReservation).filter_by(project_id=q.new_id).first()
    assert not q.apis[q.new_id].calls
    monkeypatch.setattr(
        initialization, "implementation", lambda profile: SyntheticInitialization()
    )
    with q.sessions() as db:
        project = db.get(Project, q.new_id)
        topology = deepcopy(project.topology)
        topology["nodes"].append({"id": "custom", "type": "networkNode", "data": {}})
        project.topology = topology
        db.commit()
        with pytest.raises(Exception):
            operations.prepare(db, project, "deploy", host_id=q.host_id)
        db.rollback()
        assert not db.query(QuotaReservation).filter_by(project_id=q.new_id).first()
    assert not q.apis[q.new_id].calls


def test_collector_concurrent_predecessor_cas_and_replay(deploy_case):
    q = deploy_case
    start(q)
    left = collector.snapshot(q.sessions, q.pins, q.target_id)
    right = collector.snapshot(q.sessions, q.pins, q.target_id)
    lv = collector.verify_new_roots(left, {q.provider_id: q.reader}, q.backend)
    rv = collector.verify_new_roots(right, {q.provider_id: q.reader}, q.backend)
    enrollment_id = collector.apply_extension(q.sessions, q.pins, left, lv, q.target_id)
    with pytest.raises(ValueError, match="predecessor changed"):
        collector.apply_extension(q.sessions, q.pins, right, rv, q.target_id)
    with q.sessions() as db:
        domain, manifest = collector.anchored(db, q.pins)
        assert domain.enrollment_id == enrollment_id
        assert len(manifest.pins.project_roots) == 3
        assert manifest.pins.initialization_evidence == q.pins.initialization_evidence
        assert (
            manifest.pins.domain_extension.initialization_authorization_digest is None
        )
    assert not collector.snapshot(q.sessions, q.pins, q.target_id)["selected"]


@pytest.mark.parametrize("failure", ["partial", "stale"])
def test_latest_incomplete_evidence_blocks_initialization_release(deploy_case, failure):
    q = deploy_case
    operation_id = initialized(q)
    if failure == "partial":

        def fail(*args):
            raise ValueError("synthetic failed collection")

        collector.cycle(
            q.sessions,
            q.pins,
            q.target_id,
            q.report.collector_id,
            {q.provider_id: q.reader},
            q.backend,
            fail,
        )
    else:
        FixedDatetime.current += timedelta(seconds=61)
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert (
            row.dispatch_metadata["native_deploy"]["phase"] == "initialization-intent"
        )
        assert "native_initialization_consumed" not in row.dispatch_metadata
        assert row.observed["storage_bytes"] == 16
        with pytest.raises(Exception):
            accounting.require_ready(db)
    assert not q.apis[q.new_id].rows("Pod", None)


@pytest.mark.parametrize(
    "change", ["fresh", "partial", "stale", "above", "profile", "limit", "retained"]
)
def test_delayed_first_runtime_publication_rechecks_exact_operation(
    deploy_case, change
):
    from app.models.quota import QuotaPolicy

    q = deploy_case
    operation_id = initialized(q)
    deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    advance_clock()
    if change in {"fresh", "above", "partial", "retained"}:

        def collect(manifest, previous, started):
            if change == "partial":
                raise ValueError("synthetic failed attempt")
            report = report_for(q, manifest, 48 if change == "above" else 16)
            if change == "retained":
                extra = [
                    b
                    for b in report.volume_bindings
                    if str(b.root.project_id) == q.new_id
                ]
                report = revised(
                    report, retained_bindings=[*report.retained_bindings, *extra]
                )
            return report

        if change == "above":
            # Real report import can precede ledger reconciliation. The delayed
            # coordinator must charge this report itself before rejecting release.
            from app.services.kubevirt_storage_enrollment import validated_enrollment

            with q.sessions() as db:
                domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
                enrollment = db.get(NativeEnrollment, domain.enrollment_id)
                manifest = validated_enrollment(enrollment)
                import_report(
                    db,
                    collect(manifest, None, FixedDatetime.current),
                    enrollment_id=enrollment.id,
                    expected_enrollment_digest=enrollment.digest,
                    now=FixedDatetime.current,
                )
                db.commit()
                row = (
                    db.query(QuotaReservation)
                    .filter_by(operation_id=operation_id)
                    .one()
                )
                assert row.observed["storage_bytes"] == 16
        else:
            collector.cycle(
                q.sessions,
                q.pins,
                q.target_id,
                q.report.collector_id,
                {q.provider_id: q.reader},
                q.backend,
                collect,
            )
    elif change == "stale":
        FixedDatetime.current += timedelta(seconds=61)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        if change == "profile":
            deploy.update(row, profile_digest="f" * 64)
        if change == "limit":
            db.add(QuotaPolicy(scope="project", scope_id=q.new_id, cpu=0))
        db.commit()
        target = deepcopy(row.dispatch_metadata["native_deploy"])
        with pytest.raises(Exception):
            accounting.require_ready(db)  # Runtime intent stays globally unready.
    if change == "fresh":
        deploy.release_runtime(q.new_id, operation_id, target, SimpleNamespace())
        deploy.release_runtime(
            q.new_id, operation_id, current_target(q, operation_id), SimpleNamespace()
        )
        assert (
            power.helpers().intent(api.rows("TroshkaProject", None)[0])["phase"]
            == "runtime"
        )
    else:
        with pytest.raises(Exception):
            deploy.release_runtime(q.new_id, operation_id, target, SimpleNamespace())
        assert (
            power.helpers().intent(api.rows("TroshkaProject", None)[0])["phase"]
            == "initializing"
        )
        assert not current_target(q, operation_id).get("release_handoff")
        if change == "above":
            from app.models.quota import QuotaAllocation

            with q.sessions() as db:
                row = (
                    db.query(QuotaReservation)
                    .filter_by(operation_id=operation_id)
                    .one()
                )
                allocation = (
                    db.query(QuotaAllocation).filter_by(operation_id=operation_id).one()
                )
                assert (
                    row.observed["storage_bytes"]
                    == allocation.observed["storage_bytes"]
                    == 48
                )
                assert row.observed["cpu"] == 0
                assert (
                    row.dispatch_metadata["native_deploy"]["blocker"]
                    == "initialization_bound_exceeded"
                )
            assert not api.rows("Pod", None)
            assert api.rows("Deployment", None)[0]["spec"]["replicas"] == 0
    assert deploy._release_recheck.get() is None


def current_target(q, operation_id):
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        return deepcopy(row.dispatch_metadata["native_deploy"])


@pytest.mark.parametrize("denial", ["quota", "unexpected"])
@pytest.mark.parametrize("stage", ["initialize", "release"])
def test_runtime_policy_denial_preserves_validated_measurement(
    deploy_case, monkeypatch, denial, stage
):
    from app.models.quota import QuotaAllocation, QuotaPolicy
    from app.schemas.quota import AccountingUnavailable
    from app.services.kubevirt_storage_enrollment import validated_enrollment

    q = deploy_case
    operation_id = initialized(q)
    if stage == "release":
        deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    advance_clock()
    with q.sessions() as db:
        domain = db.get(NativeCollectionDomain, str(q.pins.domain_id))
        enrollment = db.get(NativeEnrollment, domain.enrollment_id)
        manifest = validated_enrollment(enrollment)
        report = report_for(q, manifest, 24)
        import_report(
            db,
            report,
            enrollment_id=enrollment.id,
            expected_enrollment_digest=enrollment.digest,
            now=FixedDatetime.current,
        )
        db.add(QuotaPolicy(scope="project", scope_id=q.new_id, storage_bytes=20))
        db.commit()
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.observed["storage_bytes"] == 16
        assert row.requested["storage_bytes"] == 32
    # The imported domain also contains existing projects. Reconcile those real
    # retained ledgers so their freshness does not mask this operation's quota check.
    from app.services import quota_storage

    for project_id in q.project_ids:
        if project_id != q.new_id:
            quota_storage.reconcile_project(project_id)
    if denial == "unexpected":

        def fail(*args, **kwargs):
            raise ValueError("unexpected revalidation failure")

        monkeypatch.setattr(deploy.admission, "revalidate", fail)
    calls = deepcopy(api.calls)

    def attempt():
        target = current_target(q, operation_id)
        if stage == "release":
            deploy.release_runtime(q.new_id, operation_id, target, SimpleNamespace())
        else:
            proof = deploy.observe(api, target, operation_id)
            deploy.admit_runtime(q.new_id, operation_id, target, proof)

    for _ in range(2):
        expected = AccountingUnavailable if denial == "quota" else ValueError
        with pytest.raises(expected) as failure:
            attempt()
        with q.sessions() as db:
            row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
            allocation = (
                db.query(QuotaAllocation).filter_by(operation_id=operation_id).one()
            )
            amount = 24 if denial == "quota" else 16
            assert (
                row.observed["storage_bytes"]
                == allocation.observed["storage_bytes"]
                == amount
            ), str(failure.value)
            assert row.observed["cpu"] == 0
            target = row.dispatch_metadata["native_deploy"]
            assert not target.get("release_handoff")
            if denial == "quota":
                assert target["blocker"] == "initialization_quota_denied"
            else:
                assert "blocker" not in target
        assert deploy._release_recheck.get() is None
        assert api.calls == calls  # Original-root reads only, no runtime publication.
        assert not api.rows("Pod", None)
        assert api.rows("Deployment", None)[0]["spec"]["replicas"] == 0

    if denial == "quota":
        assert deploy.reconcile(q.new_id) is True
        assert not current_target(q, operation_id).get("release_handoff")
        assert api.calls == calls
        with q.sessions() as db:
            policy = (
                db.query(QuotaPolicy)
                .filter_by(scope="project", scope_id=q.new_id)
                .one()
            )
            policy.storage_bytes = 64
            db.commit()
        attempt()
        target = current_target(q, operation_id)
        assert not target.get("blocker")
        assert target["phase"] == "runtime-intent"
        assert bool(target.get("release_handoff")) == (stage == "release")


def test_ambiguous_accepted_runtime_cas_is_only_observed(deploy_case, monkeypatch):
    q = deploy_case
    operation_id = initialized(q)
    deploy.reconcile(q.new_id)
    api = q.apis[q.new_id]
    patch = api.patch
    calls = []

    def ambiguous(value, changes, **kwargs):
        result = patch(value, changes, **kwargs)
        if value["kind"] == "TroshkaProject" and any(
            c["path"] == "/metadata/annotations" for c in changes
        ):
            calls.append(changes)
            raise OSError("synthetic lost accepted response")
        return result

    monkeypatch.setattr(api, "patch", ambiguous)
    with pytest.raises(OSError):
        deploy.release_runtime(
            q.new_id, operation_id, current_target(q, operation_id), SimpleNamespace()
        )
    assert current_target(q, operation_id)["release_handoff"]
    deploy.release_runtime(
        q.new_id, operation_id, current_target(q, operation_id), SimpleNamespace()
    )
    assert len(calls) == 1
    deploy.helpers().reconcile(api, api.rows("TroshkaProject", None)[0])
    settle_runtime(q)
    advance_clock()
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        assert (
            db.query(QuotaReservation).filter_by(operation_id=operation_id).one().state
            == "confirmed"
        )
    assert len(calls) == 1


@pytest.mark.parametrize("scope", ["project", "user", "organization"])
@pytest.mark.parametrize("dimension", ["cpu", "storage_bytes", "spending_usd"])
def test_first_deploy_checks_every_capacity_and_spending_scope(
    deploy_case, scope, dimension
):
    from app.models.quota import QuotaPolicy

    q = deploy_case
    identity = {"project": q.new_id, "user": q.owner_id, "organization": q.org_id}[
        scope
    ]
    with q.sessions() as db:
        if scope == "project" and dimension == "spending_usd":
            db.get(Project, q.new_id).budget_usd = 0
        else:
            db.add(QuotaPolicy(scope=scope, scope_id=identity, **{dimension: 0}))
        db.commit()
        with pytest.raises(Exception) as error:
            operations.prepare(
                db, db.get(Project, q.new_id), "deploy", host_id=q.host_id
            )
        assert error.value.detail["scope"] == scope
        assert error.value.detail["dimension"] == dimension
        db.rollback()
        assert not db.query(QuotaReservation).filter_by(project_id=q.new_id).first()
    assert not q.apis[q.new_id].calls


def test_concurrent_new_deploys_reserve_tenant_storage_once(deploy_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    q = deploy_case
    with q.sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("requires disposable PostgreSQL row/advisory locks")
        project = db.get(Project, q.new_id)
        second_id = str(uuid4())
        db.add(
            Project(
                id=second_id,
                name="second-synthetic-new-native",
                owner_id=q.owner_id,
                state="draft",
                topology=deepcopy(project.topology),
            )
        )
        db.commit()
        from app.models.quota import QuotaPolicy
        from app.services.quota_admission import usage

        used, reserved = usage(db, "user", q.owner_id)
        db.add(
            QuotaPolicy(
                scope="user",
                scope_id=q.owner_id,
                storage_bytes=used.storage_bytes + reserved.storage_bytes + 32,
            )
        )
        db.commit()
    monkeypatch.setattr(SyntheticInitialization, "headroom", None)
    advance_clock()
    ingest_current(q)
    barrier = Barrier(2)

    def prepare(project_id):
        with q.sessions() as db:
            project = db.get(Project, project_id)
            barrier.wait(timeout=5)
            try:
                operation_id = operations.prepare(
                    db, project, "deploy", host_id=q.host_id
                )
                db.commit()
                return operation_id
            except Exception as error:
                db.rollback()
                assert error.detail["dimension"] == "storage_bytes"
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(prepare, [q.new_id, second_id]))
    assert sum(bool(value) for value in results) == 1
    with q.sessions() as db:
        rows = (
            db.query(QuotaReservation)
            .filter(QuotaReservation.project_id.in_([q.new_id, second_id]))
            .all()
        )
        assert len(rows) == 1 and rows[0].requested["storage_bytes"] == 32


def test_native_admission_does_not_require_pool_headroom(deploy_case, monkeypatch):
    q = deploy_case
    monkeypatch.setattr(SyntheticInitialization, "headroom", None)
    advance_clock()
    ingest_current(q)
    with q.sessions() as db:
        operation_id = operations.prepare(
            db, db.get(Project, q.new_id), "deploy", host_id=q.host_id
        )
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.requested["storage_bytes"] == 32
        from app.services.quota_admission import usage

        assert usage(db, "user", q.owner_id)[1].storage_bytes == 32


def test_csi_capacity_failure_keeps_native_reservation_and_assets(deploy_case):
    q = deploy_case
    operation_id = initialized(q)
    api = q.apis[q.new_id]
    for value in api.values.values():
        if value["kind"] == "PersistentVolumeClaim":
            value["status"] = {"phase": "Pending"}
    before = set(api.values)
    deploy.reconcile(q.new_id)
    with q.sessions() as db:
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        assert row.state == "reconciling"
        assert row.requested["storage_bytes"] == 32
        assert (
            row.dispatch_metadata["native_deploy"]["phase"] == "initialization-intent"
        )
        assert not row.dispatch_metadata.get("native_initialization_consumed")
    assert set(api.values) == before and not api.rows("Pod", None)


def test_unavailable_provider_host_still_rejects_native_admission(deploy_case):
    q = deploy_case
    with q.sessions() as db:
        db.get(Host, q.host_id).state = "disconnected"
        db.commit()
        with pytest.raises(Exception, match="available single native shared host"):
            operations.prepare(
                db, db.get(Project, q.new_id), "deploy", host_id=q.host_id
            )
        assert not db.query(QuotaReservation).filter_by(project_id=q.new_id).first()
    assert not q.apis[q.new_id].calls
