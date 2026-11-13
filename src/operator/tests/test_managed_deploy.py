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
