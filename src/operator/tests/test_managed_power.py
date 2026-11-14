"""Synthetic lifecycle evidence: no real Kubernetes credentials or transport."""

import asyncio
import json
from copy import deepcopy

import pytest
from helpers import managed_power as power


def resource(kind, name, owner=None, spec=None, status=None):
    value = {
        "apiVersion": (power.KINDS | power.CLEANUP_KINDS)[kind][0],
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "ns",
            "uid": name + "-uid",
            "resourceVersion": "1",
            "generation": 1,
        },
        "spec": spec or {},
        "status": status or {},
    }
    if owner:
        value["metadata"]["ownerReferences"] = [
            {
                "apiVersion": owner["apiVersion"],
                "kind": owner["kind"],
                "name": owner["metadata"]["name"],
                "uid": owner["metadata"]["uid"],
                "controller": True,
            }
        ]
    return value


class FakeAPI:
    def __init__(self, values):
        self.values = {(v["kind"], v["metadata"]["name"]): deepcopy(v) for v in values}
        self.calls = []

    def get(self, kind, namespace, name):
        return deepcopy(self.values[kind, name])

    def rows(self, kind, namespace):
        return [deepcopy(v) for (k, _), v in self.values.items() if k == kind]

    def patch(self, value, changes, *, status=False):
        key = value["kind"], value["metadata"]["name"]
        live = self.values[key]
        assert live["metadata"]["uid"] == value["metadata"]["uid"]
        assert (
            live["metadata"]["resourceVersion"] == value["metadata"]["resourceVersion"]
        )
        self.calls.append(("PATCH", key, deepcopy(changes)))
        for change in changes:
            parts = change["path"].strip("/").split("/")
            target = live
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if change["op"] == "remove":
                del target[parts[-1]]
            else:
                target[parts[-1]] = deepcopy(change["value"])
        live["metadata"]["resourceVersion"] = str(
            int(live["metadata"]["resourceVersion"]) + 1
        )
        if any(c["path"].startswith("/spec/") for c in changes):
            live["metadata"]["generation"] += 1
        return deepcopy(live)

    def request(self, method, kind, namespace, name, body):
        if method == "POST" and kind == "Pod":
            value = deepcopy(body)
            value["metadata"].update(
                uid=value["metadata"]["name"] + "-restored",
                resourceVersion="1",
                generation=1,
            )
            key = kind, value["metadata"]["name"]
            assert key not in self.values
            self.values[key] = value
            self.calls.append((method, key, deepcopy(body)))
            return deepcopy(value)
        assert method == "DELETE" and kind == "Pod"
        self.calls.append((method, (kind, name), deepcopy(body)))
        value = self.values[kind, name]
        assert body["preconditions"]["uid"] == value["metadata"]["uid"]
        value["metadata"]["deletionTimestamp"] = "2026-11-12T00:00:00Z"
        return deepcopy(value)


@pytest.mark.parametrize("extra", [False, True])
def test_cleanup_deletes_only_sealed_originals_and_retains_replacements(extra):
    root = resource("TroshkaProject", "project", spec={"projectId": "project-id"})
    child = resource("Service", "original-service", root)
    payload = {
        "version": "native-cleanup-v1", "namespace_mode": "managed", "intent_id": "cleanup-1", "generation": "allocation-1",
        "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": root["metadata"]["uid"], "deployment_generation": 1},
        "stop_proof": {"request_id": "stop-1", "generation": "allocation-1"},
        "objects": [{"kind": "Service", "name": child["metadata"]["name"], "namespace": "ns", "uid": child["metadata"]["uid"]}],
    }
    payload["inventory_digest"] = power.digest(payload["objects"])
    root["metadata"]["annotations"] = {power.CLEANUP_ANNOTATION: json.dumps(payload), power.ANNOTATION: json.dumps({"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "allocation-1"})}

    class API(FakeAPI):
        def request(self, method, kind, namespace, name, body):
            assert method == "DELETE" and kind == "Service"
            assert body["preconditions"]["uid"] == self.values[kind, name]["metadata"]["uid"]
            self.calls.append((method, kind, name))
            del self.values[kind, name]
            return {"kind": "Status", "status": "Success"}

    values = [root, child, resource("Namespace", "ns")]
    if extra:
        values.append(resource("Service", "foreign-service"))
    api = API(values)
    if extra:
        with pytest.raises(power.ManagedPowerError):
            power.cleanup_delete(api, root)
        assert not api.calls
    else:
        assert power.cleanup_delete(api, root) is False
        assert api.calls == [("DELETE", "Service", "original-service")]
        assert power.cleanup_delete(api, root) is True


@pytest.mark.parametrize("change", ["ordinary", "original", "child_uid", "parent_uid", "foreign", "marker"])
def test_cleanup_child_authority_requires_original_parent_intent_and_child(change):
    parent = resource("TroshkaProject", "project")
    child = resource("TroshkaVM", "vm", parent)
    payload = {
        "version": "native-cleanup-v1", "namespace_mode": "managed",
        "intent_id": "cleanup-1", "generation": "allocation-1",
        "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": parent["metadata"]["uid"], "deployment_generation": 1},
        "stop_proof": {"request_id": "stop-1", "generation": "allocation-1"},
        "objects": [{"kind": "TroshkaVM", "name": "vm", "namespace": "ns", "uid": child["metadata"]["uid"]}],
    }
    payload["inventory_digest"] = power.digest(payload["objects"])
    parent["metadata"]["annotations"] = {
        power.CLEANUP_ANNOTATION: json.dumps(payload),
        power.ANNOTATION: json.dumps({"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "allocation-1"}),
    }
    child["metadata"]["annotations"] = {power.CLEANUP_ANNOTATION: json.dumps(payload)}
    if change == "ordinary":
        child["metadata"]["annotations"] = {}
    elif change == "child_uid":
        child["metadata"]["uid"] = "replacement"
    elif change == "parent_uid":
        parent["metadata"]["uid"] = "replacement"
    elif change == "foreign":
        child["metadata"]["ownerReferences"] = []
    elif change == "marker":
        child["metadata"]["annotations"][power.CLEANUP_ANNOTATION] = "{}"
    api = FakeAPI([parent, child, resource("Namespace", "ns")])
    if change in {"ordinary", "original"}:
        assert power.cleanup_child(api, child["metadata"], "ns") == (change == "original")
    else:
        with pytest.raises(power.ManagedPowerError):
            power.cleanup_child(api, child["metadata"], "ns")
    assert not api.calls


@pytest.mark.parametrize("change", ["original", "grant_subject", "sa_uid", "secret_owner"])
def test_cleanup_preserves_exact_openshift_bootstrap_and_blocks_changed_ownership(change):
    parent = resource("TroshkaProject", "project")
    sa = resource("ServiceAccount", "default")
    sa["metadata"]["annotations"] = {"openshift.io/internal-registry-pull-secret-ref": "default-dockercfg-original"}
    secret = resource("Secret", "default-dockercfg-original")
    secret["type"] = "kubernetes.io/dockercfg"
    secret["metadata"].update(annotations={"openshift.io/internal-registry-auth-token.service-account": "default", "openshift.io/internal-registry-auth-token.binding": "bound"}, ownerReferences=[{"apiVersion": "v1", "kind": "ServiceAccount", "name": "default", "uid": sa["metadata"]["uid"]}])
    grant = resource("RoleBinding", "system:image-pullers")
    grant.update(power.cleanup_grant_body("system:image-pullers", "ns"))
    by_uid = {v["metadata"]["uid"]: v for v in [sa, secret, grant]}
    objects = [{"kind": v["kind"], "name": v["metadata"]["name"], "namespace": "ns", "uid": v["metadata"]["uid"], "namespace_bootstrap": power.cleanup_bootstrap(v, by_uid)} for v in [sa, secret, grant]]
    payload = {"version": "native-cleanup-v1", "namespace_mode": "managed", "intent_id": "cleanup-1", "generation": "allocation-1", "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": parent["metadata"]["uid"], "deployment_generation": 1}, "stop_proof": {"request_id": "stop-1", "generation": "allocation-1"}, "objects": objects, "inventory_digest": power.digest(sorted(objects, key=lambda v: (v["kind"], v["name"])))}
    parent["metadata"]["annotations"] = {power.CLEANUP_ANNOTATION: json.dumps(payload), power.ANNOTATION: json.dumps({"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "allocation-1"})}
    if change == "grant_subject":
        grant["subjects"][0]["name"] = "system:serviceaccounts:foreign"
    elif change == "sa_uid":
        sa["metadata"]["uid"] = "replacement"
    elif change == "secret_owner":
        secret["metadata"]["ownerReferences"][0]["uid"] = "replacement"
    api = FakeAPI([parent, sa, secret, grant, resource("Namespace", "ns")])
    if change == "original":
        assert power.cleanup_delete(api, parent) is True
    else:
        with pytest.raises(power.ManagedPowerError):
            power.cleanup_delete(api, parent)
    assert not api.calls


@pytest.mark.parametrize("name", ["kube-root-ca.crt", "openshift-service-ca.crt", "odh-kserve-custom-ca-bundle"])
@pytest.mark.parametrize("change", ["original", "annotation", "foreign", "replacement"])
def test_cleanup_preserves_original_ca_publishers_until_namespace_deletion(name, change):
    parent = resource("TroshkaProject", "project")
    ca = resource("ConfigMap", name)
    annotation = {"kubernetes.io/description": "Contains a CA bundle that can be used to verify the kube-apiserver when using internal endpoints such as the internal service IP or kubernetes.default.svc. No other usage is guaranteed across distributions of Kubernetes clusters."} if name == "kube-root-ca.crt" else {"service.beta.openshift.io/inject-cabundle": "true"}
    ca["metadata"]["annotations"] = annotation
    ca["data"] = {"ca.crt" if name == "kube-root-ca.crt" else "service-ca.crt": "synthetic public CA"}
    if name == "odh-kserve-custom-ca-bundle":
        ca["metadata"].pop("annotations")
        ca["metadata"]["labels"] = {"opendatahub.io/managed": "true"}
        ca["data"] = {"cabundle.crt": "synthetic public CA"}
    objects = [{"kind": "ConfigMap", "name": name, "namespace": "ns", "uid": ca["metadata"]["uid"], "namespace_bootstrap": power.cleanup_bootstrap(ca)}]
    assert objects[0]["namespace_bootstrap"]
    payload = {"version": "native-cleanup-v1", "namespace_mode": "managed", "intent_id": "cleanup-1", "generation": "allocation-1", "root": {"namespace": "ns", "namespace_uid": "ns-uid", "project_cr_uid": parent["metadata"]["uid"], "deployment_generation": 1}, "stop_proof": {"request_id": "stop-1", "generation": "allocation-1"}, "objects": objects, "inventory_digest": power.digest(objects)}
    parent["metadata"]["annotations"] = {power.CLEANUP_ANNOTATION: json.dumps(payload), power.ANNOTATION: json.dumps({"version": 1, "mode": "stopped", "requestId": "stop-1", "allocationGeneration": "allocation-1"})}
    if change == "annotation":
        if name == "odh-kserve-custom-ca-bundle":
            ca["metadata"]["labels"] = {}
        else:
            ca["metadata"]["annotations"] = {}
    elif change == "foreign":
        ca["metadata"]["name"] = "foreign-ca"
    elif change == "replacement":
        ca["metadata"]["uid"] = "replacement"
    api = FakeAPI([parent, ca, resource("Namespace", "ns")])
    if change == "original":
        assert power.cleanup_delete(api, parent) is True
        assert power.cleanup_delete(api, parent) is True
    else:
        with pytest.raises(power.ManagedPowerError):
            power.cleanup_delete(api, parent)
    assert not api.calls  # Never individually delete a live publisher's CA map.


@pytest.mark.parametrize("change", ["owner", "annotation", "extra_label", "extra_data", "binary", "immutable", "unknown"])
def test_cleanup_rejects_nonstandard_odh_configmaps(change):
    value = resource("ConfigMap", "odh-kserve-custom-ca-bundle")
    value["metadata"]["labels"] = {"opendatahub.io/managed": "true"}
    value["data"] = {"cabundle.crt": "synthetic public CA"}
    if change == "owner":
        value["metadata"]["ownerReferences"] = [{"uid": "another-owner"}]
    elif change == "annotation":
        value["metadata"]["annotations"] = {"unknown": "true"}
    elif change == "extra_label":
        value["metadata"]["labels"]["unknown"] = "true"
    elif change == "extra_data":
        value["data"]["unknown"] = "value"
    elif change == "binary":
        value["binaryData"] = {"unknown": "dmFsdWU="}
    elif change == "immutable":
        value["immutable"] = True
    else:
        value["metadata"]["name"] = "unknown-configmap"
    assert power.cleanup_bootstrap(value) is None


@pytest.fixture
def lifecycle():
    root = resource(
        "TroshkaProject",
        "project",
        spec={"projectId": "project-id", "topology": {"nodes": []}},
        status={"phase": "Running"},
    )
    request = {
        "version": 1,
        "mode": "stopped",
        "requestId": "stop-1",
        "allocationGeneration": "allocation-1",
    }
    root["metadata"]["annotations"] = {
        power.ANNOTATION: json.dumps(request),
        "unrelated": "preserved",
    }
    owner = resource("TroshkaVM", "vm", root, status={"observedGeneration": 1})
    vm = resource("VirtualMachine", "vm-runtime", owner, {"runStrategy": "Always"})
    net = resource("TroshkaNetwork", "network", root, status={"nadName": "network-nad"})
    values = [root, owner, vm, net]
    for prefix in ("dnsmasq", "gateway", "exec", "vnc"):
        values.append(
            resource(
                "Deployment",
                prefix + "-project",
                net if prefix == "dnsmasq" else root,
                {"replicas": 1},
                {"observedGeneration": 1},
            )
        )
    return FakeAPI(values)


def root(api):
    return api.get("TroshkaProject", "ns", "project")


def reconcile(api):
    power.reconcile_stop(api, root(api))
    return root(api)["status"]["managedPower"]


def test_all_helpers_stop_and_originals_survive_restart(lifecycle):
    state = reconcile(lifecycle)
    assert state["producerDrained"] is True
    assert state["targets"][0]["desired"] == {"runStrategy": "Always"}
    assert lifecycle.get("VirtualMachine", "ns", "vm-runtime")["spec"] == {
        "runStrategy": "Halted"
    }
    assert all(v["spec"]["replicas"] == 0 for v in lifecycle.rows("Deployment", "ns"))
    before = deepcopy(state["targets"])
    power._locks.clear()  # New process retains snapshot in the Kubernetes status.
    assert reconcile(lifecycle)["targets"] == before
    assert (
        len(lifecycle.calls) == 8
    )  # initial snapshot, five targets, two acknowledgements
    assert root(lifecycle)["metadata"]["generation"] == 1


@pytest.mark.parametrize("mode", [{"runStrategy": "Halted"}, {"running": False}])
def test_intentionally_halted_vm_is_preserved(lifecycle, mode):
    lifecycle.values["VirtualMachine", "vm-runtime"]["spec"] = deepcopy(mode)
    assert reconcile(lifecycle)["targets"][0]["desired"] == mode


def admitted_resume(api):
    state = root(api)["status"]["managedPower"]
    request = {
        "version": 1,
        "mode": "running",
        "requestId": "start-1",
        "allocationGeneration": "allocation-1",
        "resumeOf": "stop-1",
        "executionDigest": "execution",
        "profile": "configured-workloads-v1",
        "targetDigest": state["inventoryDigest"],
        "configurationDigest": power.configuration_digest(
            power.inventory(api, root(api))[0]
        ),
    }
    api.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ] = json.dumps(request)
    return request


def test_admitted_restore_preserves_originals_and_halted_vm(lifecycle):
    lifecycle.values["VirtualMachine", "vm-runtime"]["spec"] = {"running": False}
    original = reconcile(lifecycle)
    admitted_resume(lifecycle)
    restored = reconcile(lifecycle)
    assert restored["state"] == "restoring"
    assert restored["targets"] == original["targets"]
    assert lifecycle.get("VirtualMachine", "ns", "vm-runtime")["spec"] == {
        "running": False
    }
    assert all(v["spec"]["replicas"] == 1 for v in lifecycle.rows("Deployment", "ns"))
    assert reconcile(lifecycle)["targets"] == original["targets"]


def test_running_intent_requires_exact_prior_snapshot(lifecycle):
    reconcile(lifecycle)
    request = admitted_resume(lifecycle)
    request["targetDigest"] = "changed"
    lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ] = json.dumps(request)
    assert reconcile(lifecycle)["state"] == "blocked"
    assert all(v["spec"]["replicas"] == 0 for v in lifecycle.rows("Deployment", "ns"))


def test_restore_restarts_from_originals_after_lost_patch_response(
    lifecycle, monkeypatch
):
    original = reconcile(lifecycle)["targets"]
    admitted_resume(lifecycle)
    patch = lifecycle.patch
    failed = False

    def lose_response(value, changes, **kwargs):
        nonlocal failed
        result = patch(value, changes, **kwargs)
        if value["kind"] == "VirtualMachine" and not failed:
            failed = True
            raise RuntimeError("synthetic restore response loss")
        return result

    monkeypatch.setattr(lifecycle, "patch", lose_response)
    with pytest.raises(RuntimeError):
        reconcile(lifecycle)
    power._locks.clear()
    assert reconcile(lifecycle)["targets"] == original
    assert all(v["spec"]["replicas"] == 1 for v in lifecycle.rows("Deployment", "ns"))


def test_new_stop_supersedes_restore_before_next_mutation(lifecycle, monkeypatch):
    reconcile(lifecycle)
    admitted_resume(lifecycle)
    patch = lifecycle.patch

    def supersede(value, changes, **kwargs):
        result = patch(value, changes, **kwargs)
        if value["kind"] == "VirtualMachine":
            request = {
                "version": 1,
                "mode": "stopped",
                "requestId": "stop-2",
                "allocationGeneration": "allocation-1",
            }
            lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
                power.ANNOTATION
            ] = json.dumps(request)
        return result

    monkeypatch.setattr(lifecycle, "patch", supersede)
    assert reconcile(lifecycle)["state"] == "blocked"
    assert all(v["spec"]["replicas"] == 0 for v in lifecycle.rows("Deployment", "ns"))
    monkeypatch.setattr(lifecycle, "patch", patch)
    state = reconcile(lifecycle)
    assert state["requestId"] == "stop-2"
    assert all(
        t["desired"] == {"replicas": 1}
        for t in state["targets"]
        if t["kind"] == "Deployment"
    )
    assert (
        lifecycle.get("VirtualMachine", "ns", "vm-runtime")["spec"]["runStrategy"]
        == "Halted"
    )


@pytest.mark.parametrize(
    "kind", ["Job", "CronJob", "StatefulSet", "DaemonSet", "TroshkaCeph"]
)
def test_unsupported_producers_block_before_any_compute_change(lifecycle, kind):
    value = resource(kind, "unsupported", root(lifecycle))
    lifecycle.values[kind, "unsupported"] = value
    assert reconcile(lifecycle)["state"] == "blocked"
    assert (
        lifecycle.get("VirtualMachine", "ns", "vm-runtime")["spec"]["runStrategy"]
        == "Always"
    )


def test_uid_change_never_overwrites_originals(lifecycle):
    original = reconcile(lifecycle)["targets"]
    lifecycle.values["VirtualMachine", "vm-runtime"]["metadata"]["uid"] = "replacement"
    assert reconcile(lifecycle)["state"] == "blocked"
    assert root(lifecycle)["status"]["managedPower"]["targets"] == original


def test_partial_failure_keeps_snapshot_for_readback_retry(lifecycle, monkeypatch):
    patch = lifecycle.patch
    failed = False

    def lose_response(value, changes, **kwargs):
        nonlocal failed
        result = patch(value, changes, **kwargs)
        if value["kind"] == "VirtualMachine" and not failed:
            failed = True
            raise RuntimeError("synthetic response loss")
        return result

    monkeypatch.setattr(lifecycle, "patch", lose_response)
    with pytest.raises(RuntimeError):
        reconcile(lifecycle)
    originals = root(lifecycle)["status"]["managedPower"]["targets"]
    assert not root(lifecycle)["status"]["managedPower"]["producerDrained"]
    assert reconcile(lifecycle)["producerDrained"]
    assert root(lifecycle)["status"]["managedPower"]["targets"] == originals


@pytest.mark.parametrize(
    "entry",
    [
        "project_create",
        "project_status_check",
        "project_update",
        "project_managed_power_resume",
        "vm_create",
        "vm_update",
        "network_create",
        "network_update",
    ],
)
def test_all_handler_boundaries_gate_delayed_events(lifecycle, monkeypatch, entry):
    from handlers import network, project, vm

    module = (
        project
        if entry.startswith("project")
        else vm
        if entry.startswith("vm")
        else network
    )
    import kopf

    decorators = [
        kopf.on.create.return_value,
        kopf.on.update.return_value,
        kopf.on.resume.return_value,
        kopf.timer.return_value,
    ]
    handlers = [c.args[0] for d in decorators for c in d.call_args_list if c.args]
    handler = next(h for h in handlers if getattr(h, "__name__", None) == entry)
    body = (
        root(lifecycle)
        if module == project
        else lifecycle.get(
            "TroshkaVM" if module == vm else "TroshkaNetwork",
            "ns",
            "vm" if module == vm else "network",
        )
    )
    monkeypatch.setattr(power, "PowerClient", lambda: lifecycle)
    monkeypatch.setattr(power, "reconcile_stop", lambda *args: None)
    kwargs = {
        "body": body,
        "namespace": "ns",
        "name": body["metadata"]["name"],
        "meta": body["metadata"],
        "spec": body["spec"],
        "status": body["status"],
        "patch": object(),
        "old": {},
        "new": {},
        "diff": [],
    }
    # The decorated handler never reaches a builder, API, capture or heal path.
    asyncio.run(handler(**kwargs))
    assert not lifecycle.calls


def test_direct_container_wrapper_requires_guard():
    from handlers.container import create_container_pods

    with pytest.raises(power.ManagedPowerError, match="context_required"):
        create_container_pods("ns", [], {}, {"uid": "project-uid"})


def test_inflight_producer_drains_before_stop(lifecycle, monkeypatch):
    monkeypatch.setattr(power, "PowerClient", lambda: lifecycle)
    body = root(lifecycle)
    del lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ]
    power._locks.clear()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        @power.producer(project=True)
        async def produce(body, namespace):
            entered.set()
            await release.wait()

        @power.producer(project=True, reconcile=True)
        async def stop(body, namespace):
            raise AssertionError("stopped callback invoked")

        task = asyncio.create_task(produce(body=body, namespace="ns"))
        await entered.wait()
        lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
            power.ANNOTATION
        ] = body["metadata"]["annotations"][power.ANNOTATION]
        stopped = asyncio.create_task(stop(body=body, namespace="ns"))
        await asyncio.sleep(0)
        assert "managedPower" not in root(lifecycle)["status"]
        release.set()
        await task
        await stopped
        assert root(lifecycle)["status"]["managedPower"]["producerDrained"]

    asyncio.run(scenario())


def test_running_annotation_cannot_bypass_missing_admission(lifecycle):
    reconcile(lifecycle)
    request = power.intent(root(lifecycle))
    request.update(mode="running", resumeOf="stop-1", requestId="resume-1")
    lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ] = json.dumps(request)
    assert reconcile(lifecycle)["reason"] == "managed_resume_profile_invalid"
    assert (
        lifecycle.get("VirtualMachine", "ns", "vm-runtime")["spec"]["runStrategy"]
        == "Halted"
    )


def add_container(api):
    value = api.values["TroshkaProject", "project"]
    value["spec"]["topology"]["nodes"].append(
        {
            "id": "container-1",
            "type": "containerNode",
            "data": {"image": "example.invalid/image:v1", "label": "basic"},
        }
    )
    value["status"]["containersStarted"] = True
    rows, _ = power.inventory(api, value)
    manifest = next(iter(power.container_manifests(value, rows).values()))
    name = manifest["metadata"]["name"]
    manifest["metadata"].update(uid="container-uid", resourceVersion="1", generation=1)
    api.values["Pod", name] = manifest
    return name


def test_container_only_shutdown_preserves_storage_and_started_flag(lifecycle):
    del lifecycle.values["VirtualMachine", "vm-runtime"]
    del lifecycle.values["TroshkaVM", "vm"]
    name = add_container(lifecycle)
    state = reconcile(lifecycle)
    target = next(t for t in state["targets"] if t["kind"] == "Pod")
    assert target["nodeId"] == "container-1"
    assert len(target["manifestDigest"]) == 64
    assert lifecycle.get("Pod", "ns", name)["metadata"]["deletionTimestamp"]
    assert root(lifecycle)["status"]["containersStarted"] is True
    del lifecycle.values["Pod", name]  # Kubernetes completes graceful deletion.
    assert reconcile(lifecycle)["targets"] == state["targets"]
    assert len([c for c in lifecycle.calls if c[0] == "DELETE"]) == 1


def test_runtime_owned_storage_blocks_pod_delete(lifecycle):
    name = add_container(lifecycle)
    pod = lifecycle.get("Pod", "ns", name)
    pvc = resource(
        "PersistentVolumeClaim", "unsafe", pod, {"volumeName": "pv"}, {"phase": "Bound"}
    )
    lifecycle.values["PersistentVolumeClaim", "unsafe"] = pvc
    assert reconcile(lifecycle)["reason"] == "managed_storage_not_retained"
    assert not any(c[0] == "DELETE" for c in lifecycle.calls)


def test_delayed_container_replacement_blocks_repeat_stop(lifecycle):
    name = add_container(lifecycle)
    state = reconcile(lifecycle)
    lifecycle.values["Pod", name]["metadata"].pop("deletionTimestamp")
    lifecycle.values["Pod", name]["metadata"]["uid"] = "replacement"
    assert reconcile(lifecycle)["reason"] == "managed_target_changed"
    assert root(lifecycle)["status"]["managedPower"]["targets"] == state["targets"]


def test_legacy_callback_uses_validated_context(lifecycle, monkeypatch):
    del lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ]
    monkeypatch.setattr(power, "PowerClient", lambda: lifecycle)
    called = []

    @power.producer(project=True)
    async def callback(body, namespace):
        power.assert_running(namespace, body["metadata"]["uid"])
        called.append(True)

    asyncio.run(callback(body=root(lifecycle), namespace="ns"))
    assert called == [True]


def test_changed_intent_after_await_blocks_remaining_producer(lifecycle, monkeypatch):
    body = root(lifecycle)
    del lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"][
        power.ANNOTATION
    ]
    monkeypatch.setattr(power, "PowerClient", lambda: lifecycle)

    async def change_intent():
        lifecycle.values["TroshkaProject", "project"]["metadata"]["annotations"] = body[
            "metadata"
        ]["annotations"]

    @power.producer(project=True)
    async def callback(body, namespace):
        await power.checked_await(change_intent())
        raise AssertionError("producer continued after stop")

    with pytest.raises(power.ManagedPowerError, match="producer_stopped"):
        asyncio.run(callback(body=body, namespace="ns"))


def test_actual_builder_accepts_kubernetes_normalized_readback(lifecycle):
    name = add_container(lifecycle)
    spec = lifecycle.values["Pod", name]["spec"]
    container = spec["containers"][0]
    assert container.pop("env") == []
    container["resources"]["requests"]["cpu"] = "1"
    container["resources"]["requests"]["memory"] = "536870912"
    assert reconcile(lifecycle)["producerDrained"] is True


@pytest.mark.parametrize("field", ["image", "command", "cpu"])
def test_real_container_intent_changes_still_block(lifecycle, field):
    name = add_container(lifecycle)
    container = lifecycle.values["Pod", name]["spec"]["containers"][0]
    if field == "cpu":
        container["resources"]["requests"]["cpu"] = "2"
    else:
        container[field] = (
            ["changed"] if field == "command" else "different.invalid/image:v2"
        )
    assert reconcile(lifecycle)["reason"] == "managed_container_changed"


@pytest.mark.parametrize("identity", ["operator", "provider"])
def test_shipped_identity_can_read_every_inventory_kind(identity):
    from pathlib import Path

    import yaml

    repository = Path(__file__).resolve().parents[3]
    filename = repository / (
        "src/operator/deploy/clusterrole.yaml"
        if identity == "operator"
        else "infra/ocpvirt-rbac.yaml"
    )
    documents = list(yaml.safe_load_all(filename.read_text()))
    names = {"troshka-" + identity, "troshka-" + identity + "-namespaced"}
    rules = [
        rule
        for doc in documents
        if doc.get("kind") == "ClusterRole" and doc["metadata"]["name"] in names
        for rule in doc["rules"]
        if not rule.get("resourceNames")
    ]
    for kind in power.INVENTORY:
        version, resource_name = power.KINDS[kind]
        group = version.split("/")[0] if "/" in version else ""
        verbs = {
            verb
            for rule in rules
            if group in rule["apiGroups"] and resource_name in rule["resources"]
            for verb in rule["verbs"]
        }
        assert "*" in verbs or {"get", "list"} <= verbs, (identity, kind, verbs)


def test_real_pvc_mapping_change_is_not_normalized_away():
    expected = {
        "volumes": [
            {"name": "disk", "persistentVolumeClaim": {"claimName": "retained-a"}}
        ]
    }
    actual = {
        "volumes": [
            {"name": "disk", "persistentVolumeClaim": {"claimName": "retained-b"}}
        ]
    }
    assert not power.manifest_matches(expected, actual)
