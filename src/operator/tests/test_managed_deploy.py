"""Synthetic transport exercises real guards/builders, not physical calibration."""

import asyncio
import json
from copy import deepcopy

import pytest
from helpers import managed_deploy as deploy
from helpers import managed_power as power

from tests.test_managed_power import FakeAPI, resource


class API(FakeAPI):
    fail_create = None

    def request(self, method, kind, namespace, name, body):
        assert method == "POST"
        value = deepcopy(body)
        key = kind, value["metadata"]["name"]
        assert key not in self.values
        self.calls.append((method, key, deepcopy(body)))
        if self.fail_create == "before":
            self.fail_create = None
            raise TimeoutError("synthetic create before server acceptance")
        value["metadata"].update(
            uid=kind + "-" + key[1] + "-new", resourceVersion="1", generation=1
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
                host=key[1] + ".apps.synthetic.example", wildcardPolicy="None"
            )
            value["spec"]["to"]["weight"] = 100
        if kind == "PersistentVolumeClaim":
            value["spec"]["volumeName"] = "pv-" + key[1]
            value["status"] = {"phase": "Bound"}
            pv = resource(
                "PersistentVolume",
                value["spec"]["volumeName"],
                spec={
                    "claimRef": {
                        k: value["metadata"][k] for k in ("name", "namespace", "uid")
                    },
                },
            )
            pv["metadata"].pop("namespace")
            self.values["PersistentVolume", pv["metadata"]["name"]] = pv
        self.values[key] = value
        if self.fail_create == "after":
            self.fail_create = None
            raise TimeoutError("synthetic create after server acceptance")
        return deepcopy(value)


def topology():
    return {
        "nodes": [
            {"id": "vm-one", "type": "vmNode", "data": {"vcpus": 2, "ram": 1}},
            {"id": "disk-one", "type": "storageNode", "data": {"size": 2}},
            {
                "id": "first-container",
                "type": "containerNode",
                "data": {
                    "image": "synthetic/test",
                    "cpus": 0.5001,
                    "memory": 128,
                },
            },
            {
                "id": "second-container",
                "type": "containerNode",
                "data": {
                    "image": "synthetic/test",
                    "cpus": 0.4998,
                    "memory": 128,
                },
            },
        ],
        "edges": [{"source": "disk-one", "target": "vm-one"}],
    }


@pytest.fixture
def api():
    body = resource(
        "TroshkaProject",
        "project",
        spec={
            "projectId": "full-project-id",
            "topology": topology(),
            "action": "deploy",
        },
    )
    body["metadata"]["annotations"] = {
        power.ANNOTATION: json.dumps(
            {
                "version": 1,
                "mode": "deploy",
                "phase": "pending",
                "profile": deploy.PROFILE,
                "profileDigest": "1" * 64,
                "storageClass": "synthetic-reviewed-storage",
                "requestId": "operation-1",
                "allocationGeneration": "operation-1",
                "payloadDigest": power.digest(body["spec"]),
            }
        )
    }
    return API(
        [body, resource("Namespace", "ns"), resource("Namespace", "kube-system")]
    )


def root(api):
    return api.get("TroshkaProject", "ns", "project")


def phase(api, value, **updates):
    current = root(api)
    request = power.intent(current)
    request.update(
        {
            "phase": value,
            "rootUid": current["metadata"]["uid"],
            "rootGeneration": current["metadata"]["generation"],
            "projectId": current["spec"]["projectId"],
            "namespaceUid": api.get("Namespace", None, "ns")["metadata"]["uid"],
            "clusterKey": api.get("Namespace", None, "kube-system")["metadata"]["uid"],
            **updates,
        }
    )
    api.patch(
        current,
        [
            {
                "op": "add",
                "path": "/metadata/annotations",
                "value": {
                    power.ANNOTATION: json.dumps(request),
                },
            }
        ],
    )


def release(api):
    state = root(api)["status"]["managedDeployment"]
    phase(
        api,
        "runtime",
        initializationDigest=state["initializationDigest"],
        physicalReportId="synthetic-complete-report",
        physicalObservationId="synthetic-complete-graph",
    )


@pytest.fixture
def clone_api(api, monkeypatch):
    body = api.values["TroshkaProject", "project"]
    body["spec"]["topology"]["nodes"][1]["data"].update(
        source="library",
        libraryItemId="approved-item",
        resolvedS3Path="library/rhel.qcow2",
        centralSource=True,
        size=30,
    )
    body["spec"]["managedSources"] = {
        "disk-one": {
            "name": "managed-cache-original",
            "namespace": "platform-cache",
            "sourceCapacityGiB": 23,
            "storage_request": "40Gi",
        }
    }
    wanted = power.intent(body)
    wanted.update(
        profile=deploy.CLONE_PROFILE, payloadDigest=power.digest(body["spec"])
    )
    body["metadata"]["annotations"][power.ANNOTATION] = json.dumps(wanted)
    original = api.request

    def create(method, kind, namespace, name, desired):
        if kind == "DataVolume":
            # Installed v1beta1 spec is structural and prunes unknown fields.
            allowed = {
                "checkpoints",
                "contentType",
                "finalCheckpoint",
                "preallocation",
                "priorityClassName",
                "pvc",
                "source",
                "sourceRef",
                "storage",
            }
            desired = deepcopy(desired)
            desired["spec"] = {k: v for k, v in desired["spec"].items() if k in allowed}
        value = original(method, kind, namespace, name, desired)
        if kind == "DataVolume":
            value["status"] = {"phase": "Succeeded"}
            api.values[kind, value["metadata"]["name"]] = deepcopy(value)
            pvc = resource(
                "PersistentVolumeClaim",
                value["metadata"]["name"],
                value,
                deepcopy(value["spec"]["pvc"]),
            )
            pvc["spec"]["volumeName"] = "clone-pv"
            source_ref = {
                "apiGroup": "cdi.kubevirt.io",
                "kind": "VolumeCloneSource",
                "name": "volume-clone-source-" + value["metadata"]["uid"],
            }
            pvc["spec"].update(
                dataSource=deepcopy(source_ref), dataSourceRef=source_ref
            )
            pvc["metadata"]["annotations"] = {
                "cdi.kubevirt.io/storage.usePopulator": "true",
                "cdi.kubevirt.io/dataSourceNamespace": "platform-cache",
            }
            pvc["status"] = {"phase": "Bound"}
            api.values["PersistentVolumeClaim", pvc["metadata"]["name"]] = pvc
            pv = resource(
                "PersistentVolume",
                "clone-pv",
                spec={
                    "claimRef": {
                        k: pvc["metadata"][k] for k in ("uid", "name", "namespace")
                    }
                },
            )
            pv["metadata"].pop("namespace")
            api.values["PersistentVolume", "clone-pv"] = pv
        return value

    monkeypatch.setattr(api, "request", create)
    return api


def test_clone_real_render_journals_only_dv_and_holds_runtime_until_release(clone_api):
    api = clone_api
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    rows, _, plan = deploy.observe(api, root(api))
    assert len(rows["DataVolume"]) == len(rows["PersistentVolumeClaim"]) == 1
    assert rows["VirtualMachine"][0]["spec"]["runStrategy"] == "Halted"
    assert deploy.demand(plan)["cpu"] == 3
    assert not any(
        c[0] == "POST" and c[1][0] == "PersistentVolumeClaim" for c in api.calls
    )
    dv = rows["DataVolume"][0]
    assert (
        dv["metadata"]["annotations"]["cdi.kubevirt.io/storage.usePopulator"] == "true"
    )
    assert dv["spec"]["pvc"]["resources"]["requests"]["storage"] == "40Gi"
    assert "storageClassName" not in dv["spec"]
    assert dv["spec"]["pvc"]["storageClassName"] == "synthetic-reviewed-storage"
    assert dv["spec"]["source"]["pvc"] == {
        "name": "managed-cache-original",
        "namespace": "platform-cache",
    }
    state = root(api)["status"]["managedDeployment"]
    assert (
        state["creates"]["DataVolume:" + dv["metadata"]["name"]]["uid"]
        == dv["metadata"]["uid"]
    )
    release(api)
    deploy.reconcile(api, root(api))
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] != "Halted"


@pytest.mark.parametrize("drift", [False, True])
def test_frozen_legacy_clone_plan_is_read_only_compatible(clone_api, drift):
    body = root(clone_api)
    storage_class = power.intent(body)["storageClass"]
    legacy = deploy.render(body, storage_class)
    dv = next(v for v in legacy if v["kind"] == "DataVolume")
    dv["metadata"]["annotations"]["cdi.kubevirt.io/storage.usePopulator"] = "false"
    body["spec"]["managedPlanDigest"] = deploy.plan_digest(legacy)
    if drift:
        body["spec"]["managedSources"]["disk-one"]["name"] = "unrelated-source"
        with pytest.raises(power.ManagedPowerError):
            deploy.render_observed(body, storage_class)
    else:
        assert deploy.render_observed(body, storage_class) == legacy
        current = deploy.render(body, storage_class)
        assert deploy.plan_digest(current) != body["spec"]["managedPlanDigest"]
        assert (
            next(v for v in current if v["kind"] == "DataVolume")["metadata"][
                "annotations"
            ]["cdi.kubevirt.io/storage.usePopulator"]
            == "true"
        )


@pytest.mark.parametrize("interrupted", [False, True])
def test_clone_runtime_reconcile_and_interrupted_phase_write_retain_generated_claim(
    clone_api, monkeypatch, interrupted
):
    api = clone_api
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    original_objects = {
        (v["kind"], v["metadata"]["name"]): deepcopy(v["metadata"]["uid"])
        for kind in ("DataVolume", "PersistentVolumeClaim")
        for v in api.rows(kind, "ns")
    }
    release(api)
    original_write = deploy.write_state
    failed = []

    def write(current_api, body, state):
        result = original_write(current_api, body, state)
        if interrupted and state.get("phase") == "runtime" and not failed:
            failed.append(True)
            raise TimeoutError("interrupted immediately after runtime-phase write")
        return result

    monkeypatch.setattr(deploy, "write_state", write)
    if interrupted:
        with pytest.raises(TimeoutError):
            deploy.reconcile(api, root(api))
        assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] == "Halted"
    else:
        deploy.reconcile(api, root(api))
    deploy.reconcile(api, root(api))
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] != "Halted"
    actual = {
        (v["kind"], v["metadata"]["name"]): v["metadata"]["uid"]
        for kind in ("DataVolume", "PersistentVolumeClaim")
        for v in api.rows(kind, "ns")
    }
    assert actual == original_objects
    assert (
        len([c for c in api.calls if c[0] == "POST" and c[1][0] == "DataVolume"]) == 1
    )
    assert not any(
        c[0] == "POST" and c[1][0] == "PersistentVolumeClaim" for c in api.calls
    )


@pytest.mark.parametrize("changed", ["pvc_uid", "pv_uid", "csi", "journal_uid"])
def test_clone_runtime_replay_rejects_generated_storage_identity_changes(
    clone_api, changed
):
    api = clone_api
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    release(api)
    deploy.reconcile(api, root(api))
    pvc = api.values[next(k for k in api.values if k[0] == "PersistentVolumeClaim")]
    pv = api.values["PersistentVolume", "clone-pv"]
    if changed == "pvc_uid":
        pvc["metadata"]["uid"] = "replacement-pvc"
        pv["spec"]["claimRef"]["uid"] = "replacement-pvc"
    elif changed == "pv_uid":
        pv["metadata"]["uid"] = "replacement-pv"
    elif changed == "csi":
        pv["spec"]["csi"] = {"driver": "unexpected", "volumeHandle": "replacement"}
    else:
        state = api.values["TroshkaProject", "project"]["status"]["managedDeployment"]
        state["creates"]["DataVolume:" + pvc["metadata"]["name"]][
            "uid"
        ] = "replacement-dv"
    before = len([c for c in api.calls if c[0] == "POST"])
    with pytest.raises(power.ManagedPowerError):
        deploy.reconcile(api, root(api))
    assert len([c for c in api.calls if c[0] == "POST"]) == before


@pytest.mark.parametrize(
    "change",
    [
        "unknown_source",
        "missing_ref",
        "source_namespace",
        "owner",
        "extra_pvc",
        "writer",
        "failed",
    ],
)
def test_clone_unknown_generated_shape_or_writer_stays_off(clone_api, change):
    api = clone_api
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    pvc = api.values[next(k for k in api.values if k[0] == "PersistentVolumeClaim")]
    if change == "unknown_source":
        pvc["spec"]["dataSourceRef"] = {
            "kind": "VolumeCloneSource",
            "apiGroup": "cdi.kubevirt.io",
            "name": "unexpected",
        }
    elif change == "missing_ref":
        pvc["spec"].pop("dataSource")
    elif change == "source_namespace":
        pvc["metadata"]["annotations"][
            "cdi.kubevirt.io/dataSourceNamespace"
        ] = "unrelated"
    elif change == "owner":
        pvc["metadata"]["ownerReferences"][0]["uid"] = "replacement"
    elif change == "extra_pvc":
        extra = resource(
            "PersistentVolumeClaim", "unplanned", root(api), deepcopy(pvc["spec"])
        )
        api.values["PersistentVolumeClaim", "unplanned"] = extra
    elif change == "writer":
        helper = resource(
            "Pod",
            "cdi-writer",
            root(api),
            {
                "volumes": [
                    {"persistentVolumeClaim": {"claimName": pvc["metadata"]["name"]}}
                ]
            },
        )
        api.values["Pod", "cdi-writer"] = helper
    else:
        api.values[next(k for k in api.values if k[0] == "DataVolume")]["status"][
            "phase"
        ] = "Failed"
    with pytest.raises(power.ManagedPowerError):
        deploy.observe(api, root(api))
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] == "Halted"


def test_clone_lost_original_dv_response_cannot_be_adopted(clone_api, monkeypatch):
    api = clone_api
    original = api.request

    def lost(method, kind, namespace, name, desired):
        value = original(method, kind, namespace, name, desired)
        if kind == "DataVolume":
            raise TimeoutError("lost original DV response")
        return value

    monkeypatch.setattr(api, "request", lost)
    phase(api, "initializing")
    with pytest.raises(TimeoutError):
        deploy.reconcile(api, root(api))
    with pytest.raises(power.ManagedPowerError, match="managed_create_unresolved"):
        deploy.reconcile(api, root(api))
    assert (
        len([c for c in api.calls if c[0] == "POST" and c[1][0] == "DataVolume"]) == 1
    )


@pytest.mark.parametrize(
    "outcome", ["absent", "appeared", "known_uid", "changed_journal"]
)
def test_pending_clone_cancellation_never_replays_unknown_create(
    clone_api, monkeypatch, outcome
):
    api = clone_api
    body = api.values["TroshkaProject", "project"]
    plan = deploy.plan_digest(deploy.render(body, "synthetic-reviewed-storage"))
    body["spec"]["managedPlanDigest"] = plan
    phase(
        api, "initializing", planDigest=plan, payloadDigest=power.digest(body["spec"])
    )
    original = api.request

    def lost(method, kind, namespace, name, desired):
        if kind == "DataVolume":
            if outcome == "appeared":
                original(method, kind, namespace, name, desired)
            raise TimeoutError("original DV POST has no returned UID")
        return original(method, kind, namespace, name, desired)

    monkeypatch.setattr(api, "request", lost)
    with pytest.raises(TimeoutError):
        deploy.reconcile(api, root(api))
    body = api.values["TroshkaProject", "project"]
    entry = next(
        v
        for k, v in body["status"]["managedDeployment"]["creates"].items()
        if k.startswith("DataVolume:")
    )
    if outcome == "known_uid":
        entry["uid"] = "previous-known-disk"
    if outcome == "changed_journal":
        body["status"]["managedDeployment"]["creates"]["DataVolume:unknown"] = entry
    original_journal = deepcopy(body["status"]["managedDeployment"])
    body["metadata"]["annotations"][power.ANNOTATION] = json.dumps(
        {
            "version": 1,
            "mode": "stopped",
            "requestId": "cancel-original",
            "allocationGeneration": "operation-1",
            "pendingInitialization": power.intent(body),
        }
    )
    calls = deepcopy(api.calls)
    power.reconcile_stop(api, root(api))
    state = root(api)["status"]["managedPower"]
    assert state["producerDrained"] == (outcome == "absent")
    assert state["state"] == ("off" if outcome == "absent" else "blocked")
    assert root(api)["status"]["managedDeployment"] == original_journal
    assert all(call[0] == "PATCH" for call in api.calls[len(calls) :])


def test_pending_has_no_children_and_configured_fractional_demand(api):
    deploy.reconcile(api, root(api))
    assert api.calls == []
    manifests = deploy.render(root(api), "synthetic-reviewed-storage")
    vector = deploy.demand(manifests)
    assert vector["cpu"] == 3
    assert vector["ram_bytes"] == 1280 * 2**20
    assert any(v["kind"] == "Deployment" for v in manifests)
    assert any(v["kind"] == "PersistentVolumeClaim" for v in manifests)


def test_initialization_holds_writers_and_replay_preserves_uids(api):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    rows, by_uid, planned = deploy.observe(api, root(api))
    assert not rows["Pod"] and not rows["VirtualMachineInstance"]
    assert rows["VirtualMachine"][0]["spec"]["runStrategy"] == "Halted"
    assert rows["Deployment"][0]["spec"]["replicas"] == 0
    assert len(rows["PersistentVolumeClaim"]) == 1
    power.verify_virtual_machines(rows, by_uid)
    initial = deepcopy(api.values)
    deploy.reconcile(api, root(api))
    assert {(k, v["metadata"]["uid"]) for k, v in initial.items()} == {
        (k, v["metadata"]["uid"]) for k, v in api.values.items()
    }
    assert len([c for c in api.calls if c[0] == "POST"]) == 9
    assert deploy.demand(planned)["cpu"] == 3


@pytest.mark.parametrize(
    "kind,field,new",
    [
        ("Service", "clusterIP", "172.30.0.21"),
        ("Service", "clusterIPs", ["172.30.0.21"]),
        ("Service", "ipFamilies", ["IPv6"]),
        ("Route", "host", "replacement.apps.synthetic.example"),
    ],
)
def test_server_support_allocations_bind_on_initialization_and_reject_replay_drift(
    api, kind, field, new
):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    state = root(api)["status"]["managedDeployment"]
    actual = api.rows(kind, "ns")[0]
    key = kind + ":" + actual["metadata"]["name"]
    assert state["creates"][key]["allocations"][field] == actual["spec"][field]
    api.values[kind, actual["metadata"]["name"]]["spec"][field] = new
    before = len(api.calls)
    with pytest.raises(
        power.ManagedPowerError, match="managed_support_allocation_changed"
    ):
        deploy.reconcile(api, root(api))
    assert len(api.calls) == before
    with pytest.raises(
        power.ManagedPowerError, match="managed_support_allocation_changed"
    ):
        deploy.observe(api, root(api))


@pytest.mark.parametrize(
    "kind,path,new",
    [
        ("Service", ("selector", "app"), "foreign"),
        ("Service", ("ports", 0, "targetPort"), 9090),
        ("Service", ("ipFamilyPolicy",), "PreferDualStack"),
        ("Route", ("to", "name"), "foreign"),
        ("Route", ("tls", "termination"), "passthrough"),
        ("Route", ("to", "weight"), 50),
        ("Route", ("wildcardPolicy",), "Subdomain"),
    ],
)
def test_server_allocations_do_not_ignore_support_configuration_drift(
    api, kind, path, new
):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    actual = api.rows(kind, "ns")[0]
    spec = api.values[kind, actual["metadata"]["name"]]["spec"]
    for part in path[:-1]:
        spec = spec[part]
    spec[path[-1]] = new
    with pytest.raises(power.ManagedPowerError, match="managed_configuration_changed"):
        deploy.observe(api, root(api))


@pytest.mark.parametrize("kind,field", [("Service", "clusterIP"), ("Route", "host")])
def test_explicit_support_address_is_exact_and_retained_allocations_do_not_rebind(
    api, kind, field
):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    body = root(api)
    planned = deploy.render(body, "synthetic-reviewed-storage")
    expected = deepcopy(next(v for v in planned if v["kind"] == kind))
    actual = api.rows(kind, "ns")[0]
    expected["spec"][field] = "explicit-other-address"
    with pytest.raises(power.ManagedPowerError, match="managed_configuration_changed"):
        deploy.verify_spec(expected, actual)
    body["spec"]["managedPlanDigest"] = deploy.plan_digest(planned)
    rows, by_uid = power.inventory(api, body, deployment_support=False)
    deploy.retained_support(api, body, rows, by_uid)
    api.values[kind, actual["metadata"]["name"]]["spec"][field] = "replacement-address"
    rows, by_uid = power.inventory(api, body, deployment_support=False)
    with pytest.raises(
        power.ManagedPowerError, match="managed_support_allocation_changed"
    ):
        deploy.retained_support(api, body, rows, by_uid)


def test_ambiguous_support_create_binds_returned_allocation_without_recreation(
    api, monkeypatch
):
    request = api.request
    lost = []

    def ambiguous(method, kind, namespace, name, body):
        result = request(method, kind, namespace, name, body)
        if kind == "Service" and not lost:
            lost.append(True)
            raise TimeoutError("synthetic accepted service response lost")
        return result

    monkeypatch.setattr(api, "request", ambiguous)
    phase(api, "initializing")
    with pytest.raises(TimeoutError):
        deploy.reconcile(api, root(api))
    deploy.reconcile(api, root(api))
    value = api.rows("Service", "ns")[0]
    saved = root(api)["status"]["managedDeployment"]["creates"][
        "Service:" + value["metadata"]["name"]
    ]
    assert saved["uid"] == value["metadata"]["uid"]
    assert saved["allocations"]["clusterIP"] == value["spec"]["clusterIP"]
    assert (
        len(
            [
                call
                for call in api.calls
                if call[0] == "POST" and call[1][0] == "Service"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("kind", ["Role", "RoleBinding"])
def test_allocated_support_does_not_weaken_exact_rbac(api, kind):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    actual = api.rows(kind, "ns")[0]
    value = api.values[kind, actual["metadata"]["name"]]
    if kind == "Role":
        value["rules"][0]["verbs"].append("delete")
    else:
        value["roleRef"]["name"] = "foreign-role"
    with pytest.raises(power.ManagedPowerError, match="managed_configuration_changed"):
        deploy.observe(api, root(api))


def test_runtime_requires_prior_initialization_and_physical_receipt(api):
    phase(
        api,
        "runtime",
        initializationDigest="arbitrary",
        physicalReportId="report",
        physicalObservationId="observation",
    )
    with pytest.raises(power.ManagedPowerError, match="initialization_pending"):
        deploy.reconcile(api, root(api))
    assert not any(c[0] == "POST" for c in api.calls)


def test_runtime_releases_only_saved_workload_and_helper_plan(api):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    release(api)
    deploy.reconcile(api, root(api))
    rows, _, _ = deploy.observe(api, root(api), runtime=True)
    assert rows["VirtualMachine"][0]["spec"]["runStrategy"] == "Always"
    assert rows["Deployment"][0]["spec"]["replicas"] == 1
    assert len(rows["Pod"]) == 2
    before = len([c for c in api.calls if c[0] == "POST"])
    deploy.reconcile(api, root(api))
    assert len([c for c in api.calls if c[0] == "POST"]) == before


@pytest.mark.parametrize("timing", ["before", "after"])
def test_ambiguous_create_never_redispatches(api, timing):
    phase(api, "initializing")
    api.fail_create = timing
    with pytest.raises(TimeoutError):
        deploy.reconcile(api, root(api))
    if timing == "before":
        with pytest.raises(power.ManagedPowerError, match="create_unresolved"):
            deploy.reconcile(api, root(api))
        assert len([c for c in api.calls if c[0] == "POST"]) == 1
    else:
        deploy.reconcile(api, root(api))
        assert len([c for c in api.calls if c[0] == "POST"]) == 9


def test_unrelated_resource_is_not_adopted(api):
    phase(api, "initializing")
    child = deploy.render(root(api), "synthetic-reviewed-storage")[0]
    child["metadata"].update(uid="unrelated", resourceVersion="1", generation=1)
    api.values[child["kind"], child["metadata"]["name"]] = child
    with pytest.raises(power.ManagedPowerError, match="unrelated_resource"):
        deploy.reconcile(api, root(api))
    assert not any(c[0] == "POST" for c in api.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rootUid", "other"),
        ("rootGeneration", 2),
        ("projectId", "short-id"),
        ("namespaceUid", "other"),
        ("clusterKey", "other"),
        ("payloadDigest", "0" * 64),
    ],
)
def test_identity_drift_blocks_before_children(api, field, value):
    phase(api, "initializing", **{field: value})
    with pytest.raises(power.ManagedPowerError):
        deploy.reconcile(api, root(api))
    assert not any(c[0] == "POST" for c in api.calls)


@pytest.mark.parametrize(
    "node",
    [
        {"id": "net", "type": "networkNode", "data": {}},
        {"id": "disk", "type": "storageNode", "data": {"libraryItemId": "image"}},
        {"id": "pod", "type": "containerNode", "data": {"isPod": True}},
        {"id": "vm", "type": "vmNode", "data": {"tpmEnabled": True}},
    ],
)
def test_unsupported_shape_rejects_real_renderer(api, node):
    body = root(api)
    body["spec"]["topology"]["nodes"].append(node)
    with pytest.raises(power.ManagedPowerError):
        deploy.render(body, "synthetic-reviewed-storage")


def test_project_and_child_guards_never_enter_ordinary_producer(api, monkeypatch):
    monkeypatch.setattr(power, "PowerClient", lambda: api)
    entered = []

    @power.producer(project=True)
    async def project_handler(namespace, body):
        entered.append("project")

    asyncio.run(project_handler("ns", root(api)))
    assert entered == []
    assert not any(c[0] == "POST" for c in api.calls)
    phase(api, "initializing")
    asyncio.run(project_handler("ns", root(api)))
    child = api.rows("TroshkaVM", "ns")[0]

    @power.producer()
    async def child_handler(namespace, body):
        entered.append("child")

    asyncio.run(child_handler("ns", child))
    assert entered == []


def test_truncated_resource_name_collision_rejects_before_children(api):
    body = root(api)
    body["spec"]["topology"]["nodes"][2]["id"] = "container-a"
    body["spec"]["topology"]["nodes"][3]["id"] = "container-b"
    api.values["TroshkaProject", "project"] = body
    phase(api, "initializing", payloadDigest=power.digest(body["spec"]))
    with pytest.raises(power.ManagedPowerError, match="resource_collision"):
        deploy.reconcile(api, root(api))
    assert not any(c[0] == "POST" for c in api.calls)


def test_replaced_initialized_helper_blocks_all_runtime_mutations(api):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    release(api)
    helper = api.rows("Deployment", "ns")[0]
    api.values["Deployment", helper["metadata"]["name"]]["metadata"][
        "uid"
    ] = "replacement"
    before = len(api.calls)
    with pytest.raises(power.ManagedPowerError, match="initialization_changed"):
        deploy.reconcile(api, root(api))
    assert len(api.calls) == before
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] == "Halted"


def test_release_receipt_is_required_and_pinned_after_handoff(api):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    phase(
        api,
        "runtime",
        initializationDigest=root(api)["status"]["managedDeployment"][
            "initializationDigest"
        ],
    )
    with pytest.raises(power.ManagedPowerError, match="physical_proof_required"):
        deploy.reconcile(api, root(api))
    release(api)
    deploy.reconcile(api, root(api))
    phase(api, "runtime", physicalReportId="changed-report")
    with pytest.raises(power.ManagedPowerError, match="release_changed"):
        deploy.reconcile(api, root(api))


def persist_runtime_handoff(api):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    release(api)
    current = root(api)
    wanted = deploy.request(current)
    state = deploy.state_for(current, wanted)
    deploy.require_release(state, wanted)
    state["phase"] = "runtime"
    deploy.write_state(api, current, state)


@pytest.mark.parametrize(
    "kind,path,value",
    [
        ("VirtualMachine", ("template", "spec", "domain", "cpu", "sockets"), 4),
        ("VirtualMachine", ("template", "spec", "domain", "cpu", "threads"), 2),
        (
            "VirtualMachine",
            ("template", "spec", "domain", "cpu", "model"),
            "host-passthrough",
        ),
        ("Deployment", ("template", "spec", "hostNetwork"), True),
        ("Deployment", ("template", "spec", "nodeSelector"), {"unadmitted": "node"}),
        (
            "Deployment",
            ("template", "spec", "containers"),
            [{"name": "injected", "image": "unadmitted/image"}],
        ),
    ],
)
def test_runtime_recovery_rejects_added_configuration_before_any_writer(
    api, kind, path, value
):
    persist_runtime_handoff(api)
    live = api.rows(kind, "ns")[0]
    spec = api.values[kind, live["metadata"]["name"]]["spec"]
    for part in path[:-1]:
        spec = spec[part]
    spec[path[-1]] = value
    before = len(api.calls)
    with pytest.raises(power.ManagedPowerError, match="configuration_changed"):
        deploy.reconcile(api, root(api))
    assert len(api.calls) == before
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] == "Halted"
    assert api.rows("Deployment", "ns")[0]["spec"]["replicas"] == 0
    assert not api.rows("Pod", "ns")


@pytest.mark.parametrize("handoff", [False, True])
def test_explicit_static_api_defaults_and_quantities_preserve_admitted_plan(
    api, handoff
):
    if handoff:
        persist_runtime_handoff(api)
    else:
        phase(api, "initializing")
        deploy.reconcile(api, root(api))
    troshkavm = api.rows("TroshkaVM", "ns")[0]
    api.values["TroshkaVM", troshkavm["metadata"]["name"]]["spec"].update(
        nestedVirt=False, recertEnabled=False
    )
    vm = api.rows("VirtualMachine", "ns")[0]
    spec = api.values["VirtualMachine", vm["metadata"]["name"]]["spec"]
    domain = spec["template"]["spec"]["domain"]
    domain["cpu"].update(sockets=1, threads=1)
    domain["resources"]["requests"]["memory"] = "1Gi"
    spec["template"]["metadata"]["creationTimestamp"] = None
    helper = api.rows("Deployment", "ns")[0]
    spec = api.values["Deployment", helper["metadata"]["name"]]["spec"]
    spec.update(
        revisionHistoryLimit=10,
        progressDeadlineSeconds=600,
        strategy={
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
        },
    )
    spec["template"]["spec"].update(
        restartPolicy="Always",
        dnsPolicy="ClusterFirst",
        schedulerName="default-scheduler",
        terminationGracePeriodSeconds=30,
        hostNetwork=False,
        securityContext={},
    )
    for ctr in spec["template"]["spec"]["containers"]:
        ctr.update(
            terminationMessagePath="/dev/termination-log",
            terminationMessagePolicy="File",
        )
    if not handoff:
        release(api)
    deploy.reconcile(api, root(api))
    assert api.rows("VirtualMachine", "ns")[0]["spec"]["runStrategy"] == "Always"
    assert api.rows("Deployment", "ns")[0]["spec"]["replicas"] == 1
    deploy.reconcile(api, root(api))


@pytest.mark.parametrize(
    "kind,path,value",
    [
        ("TroshkaVM", ("nestedVirt",), True),
        ("TroshkaVM", ("recertEnabled",), True),
        ("TroshkaVM", ("nestedVirt",), 0),
        ("TroshkaVM", ("recertEnabled",), "false"),
        ("TroshkaVM", ("unknownBoolean",), False),
        ("VirtualMachine", ("template", "spec", "domain", "cpu", "sockets"), True),
        ("Deployment", ("template", "spec", "terminationGracePeriodSeconds"), 0),
        ("Deployment", ("strategy",), {"type": "Recreate"}),
    ],
)
def test_nondefault_or_wrong_type_additions_are_not_api_defaults(
    api, kind, path, value
):
    expected = next(
        v
        for v in deploy.render(root(api), "synthetic-reviewed-storage")
        if v["kind"] == kind
    )
    actual = deepcopy(expected)
    spec = actual["spec"]
    for part in path[:-1]:
        spec = spec.setdefault(part, {})
    spec[path[-1]] = value
    with pytest.raises(power.ManagedPowerError, match="configuration_changed"):
        deploy.verify_spec(expected, actual)


def test_troshkavm_api_defaults_preserve_immutable_plan(api):
    planned = deploy.render(root(api), "synthetic-reviewed-storage")
    original = deepcopy(planned)
    original_digest = deploy.plan_digest(planned)
    expected = next(value for value in planned if value["kind"] == "TroshkaVM")
    actual = deepcopy(expected)
    actual["spec"].update(nestedVirt=False, recertEnabled=False)
    original_actual = deepcopy(actual)

    deploy.verify_spec(expected, actual)

    assert deploy.configuration(expected) == deploy.configuration(actual)
    assert deploy.plan_digest(planned) == original_digest
    assert planned == original
    assert actual == original_actual


@pytest.mark.parametrize("field", ["nestedVirt", "recertEnabled"])
def test_troshkavm_explicit_true_cannot_change_to_default(field):
    expected = resource("TroshkaVM", "vm", spec={field: True})
    actual = deepcopy(expected)
    actual["spec"][field] = False

    with pytest.raises(power.ManagedPowerError, match="configuration_changed"):
        deploy.verify_spec(expected, actual)


def injected_vm_defaults(value):
    """Sanitized installed shape; identifiers are synthetic, not a live receipt."""
    template = value["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("annotations", {})[
        "kubevirt.io/pci-topology-version"
    ] = "v3"
    template["spec"]["architecture"] = "amd64"
    template["spec"]["domain"].update(
        machine={"type": "pc-q35-rhel9.6.0"},
        firmware={
            "serial": "da5bc70b-b4eb-47f3-8e1a-795ec3571258",
            "uuid": "179455e0-cc90-4ff8-a84a-34a7af551a5b",
        },
    )


def test_vm_injected_create_values_are_atomically_receipted(api, monkeypatch):
    original = api.request
    snapshots = []
    original_write = deploy.write_state

    def write(client, body, state):
        snapshots.append(deepcopy(state))
        return original_write(client, body, state)

    def created(method, kind, namespace, name, desired):
        value = original(method, kind, namespace, name, desired)
        if kind == "VirtualMachine":
            injected_vm_defaults(value)
            api.values[kind, value["metadata"]["name"]] = deepcopy(value)
        return value

    monkeypatch.setattr(api, "request", created)
    monkeypatch.setattr(deploy, "write_state", write)
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    deploy.observe(api, root(api))
    entries = [
        entry
        for state in snapshots
        for key, entry in state.get("creates", {}).items()
        if key.startswith("VirtualMachine:") and entry.get("uid")
    ]
    assert entries and all(entry.get("allocations") for entry in entries)
    vm = api.rows("VirtualMachine", "ns")[0]
    assert vm["spec"]["runStrategy"] == "Halted"
    vm["spec"]["template"]["spec"]["domain"]["machine"]["type"] = "changed-machine"
    api.values["VirtualMachine", vm["metadata"]["name"]] = vm
    with pytest.raises(power.ManagedPowerError, match="allocation_changed"):
        deploy.observe(api, root(api))


@pytest.mark.parametrize("field", list(deploy._VM_ALLOCATIONS))
def test_vm_injected_values_cannot_change_or_override_explicit_plan(api, field):
    expected = next(
        v
        for v in deploy.render(root(api), "synthetic-reviewed-storage")
        if v["kind"] == "VirtualMachine"
    )
    actual = deepcopy(expected)
    injected_vm_defaults(actual)
    saved = deploy.server_allocations(expected, actual)
    deploy.verify_spec(expected, actual, allocations=saved)
    path = deploy._VM_ALLOCATIONS[field]
    changed = deepcopy(actual)
    node = changed["spec"]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = {
        "architecture": "arm64",
        "machine": "changed-machine",
        "pciTopology": "v4",
        "firmwareSerial": "057c26a0-9664-47f3-9865-7cd1206a782d",
        "firmwareUUID": "057c26a0-9664-47f3-9865-7cd1206a782d",
    }[field]
    with pytest.raises(power.ManagedPowerError, match="allocation_changed"):
        deploy.verify_spec(expected, changed, allocations=saved)
    with pytest.raises(power.ManagedPowerError, match="configuration_changed"):
        deploy.verify_spec(actual, changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("architecture", True),
        ("architecture", ""),
        ("machine", "bad value"),
        ("machine", "x" * 129),
        ("pciTopology", "latest"),
        ("firmwareSerial", "not-a-uuid"),
        ("firmwareUUID", "not-a-uuid"),
    ],
)
def test_vm_injected_value_shapes_are_bounded(api, field, value):
    expected = next(
        v
        for v in deploy.render(root(api), "synthetic-reviewed-storage")
        if v["kind"] == "VirtualMachine"
    )
    actual = deepcopy(expected)
    injected_vm_defaults(actual)
    node = actual["spec"]
    path = deploy._VM_ALLOCATIONS[field]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(power.ManagedPowerError, match="allocation_invalid"):
        deploy.verify_spec(expected, actual)


@pytest.mark.parametrize(
    "path",
    [
        ("template", "spec", "domain", "firmware", "unknown"),
        ("template", "spec", "domain", "machine", "unknown"),
        ("template", "metadata", "annotations", "unknown"),
    ],
)
def test_vm_allocation_allowlist_does_not_remove_unknown_siblings(api, path):
    expected = next(
        v
        for v in deploy.render(root(api), "synthetic-reviewed-storage")
        if v["kind"] == "VirtualMachine"
    )
    actual = deepcopy(expected)
    injected_vm_defaults(actual)
    node = actual["spec"]
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = "unknown"
    with pytest.raises(power.ManagedPowerError, match="configuration_changed"):
        deploy.verify_spec(expected, actual)


@pytest.mark.parametrize("injected", [False, True])
def test_vm_observation_requires_receipt_only_for_injected_values(api, injected):
    phase(api, "initializing")
    deploy.reconcile(api, root(api))
    body = api.values["TroshkaProject", "project"]
    for key, entry in body["status"]["managedDeployment"]["creates"].items():
        if key.startswith("VirtualMachine:"):
            entry.pop("allocations")
    if injected:
        vm = next(v for (kind, _), v in api.values.items() if kind == "VirtualMachine")
        injected_vm_defaults(vm)
        with pytest.raises(power.ManagedPowerError, match="allocation_unbound"):
            deploy.observe(api, root(api))
    else:
        deploy.observe(api, root(api))


@pytest.mark.parametrize("source", ["library", "snapshot", "pattern", "unknown"])
def test_nonblank_storage_source_rejects_even_without_resolved_path(api, source):
    body = root(api)
    body["spec"]["topology"]["nodes"][1]["data"]["source"] = source
    with pytest.raises(power.ManagedPowerError):
        deploy.render(body, "synthetic-reviewed-storage")


@pytest.mark.parametrize(
    "selector,value",
    [
        ("resolvedS3Path", "snapshots/retained.qcow2"),
        ("pxeBootIsoId", "installer-id"),
        ("pxeBootIsoS3Path", "library/installer.iso"),
        ("sourceSizeGb", 20),
        ("diskSource", "central"),
        ("disks", [{"source": "snapshot", "resolvedS3Path": "retained.qcow2"}]),
    ],
)
def test_standalone_import_selectors_reject_without_companion_fields(
    api, selector, value
):
    body = root(api)
    body["spec"]["topology"]["nodes"][0]["data"][selector] = value
    with pytest.raises(power.ManagedPowerError):
        deploy.render(body, "synthetic-reviewed-storage")


@pytest.mark.parametrize("source", ["", "blank"])
def test_explicit_blank_storage_source_remains_supported(api, source):
    body = root(api)
    body["spec"]["topology"]["nodes"][1]["data"]["source"] = source
    manifests = deploy.render(body, "synthetic-reviewed-storage")
    claims = [v for v in manifests if v["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 1
    assert "dataSource" not in claims[0]["spec"]


@pytest.mark.parametrize("case", ["container-snapshot", "vm-local-iso"])
def test_unsupported_source_selectors_reject_before_child_io(api, case):
    body = root(api)
    nodes = body["spec"]["topology"]["nodes"]
    if case == "container-snapshot":
        nodes[1]["data"].update(
            source="snapshot", resolvedS3Path="snapshots/retained.qcow2"
        )
        nodes[2]["data"]["mounts"] = [{"diskNodeId": "disk-one", "mountPath": "/data"}]
        body["spec"]["topology"]["edges"] = []
    else:
        nodes[0]["data"].update(
            pxeBootIsoId="installer-iso", pxeBootIsoS3Path="library/installer.iso"
        )
    api.values["TroshkaProject", "project"] = body
    phase(api, "initializing", payloadDigest=power.digest(body["spec"]))
    with pytest.raises(power.ManagedPowerError):
        deploy.reconcile(api, root(api))
    assert not any(c[0] == "POST" for c in api.calls)
