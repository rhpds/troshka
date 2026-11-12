"""Controller maintenance changes intent only and retains the runtime graph."""

import json
from copy import deepcopy

import pytest

from app.schemas.native_vm_halt import HaltManifest
from app.services.native_vm_halt import HaltJournal, halt_vms
from tests.test_native_vm_halt import Cluster, manifest, obj


class Helpers(Cluster):
    def __init__(self, kind="Deployment"):
        super().__init__()
        project = self.objects["TroshkaProject", "root"]
        network = obj("TroshkaNetwork", "network", project)
        network["apiVersion"] = "troshka.redhat.com/v1alpha1"
        owner = network if kind == "Deployment" else project
        controller = obj(kind, "helper", owner, template={"spec": {"volumes": []}})
        controller["apiVersion"] = "batch/v1" if kind == "Job" else "apps/v1"
        controller["metadata"]["uid"] = "00000000-0000-4000-8000-000000000003"
        if kind != "Job":
            controller["spec"]["replicas"] = 1
        self.objects[network["kind"], "network"] = network
        self.objects[kind, "helper"] = controller
        self.kind = kind

    def patch_controller(self, kind, namespace, name, patch, *, dry_run):
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        assert events[-1]["event"] == "attempt"
        before = self.objects[kind, name]
        assert patch[:2] == [
            {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
        ]
        self.requests.append((kind, namespace, name, patch, dry_run))
        if self.error:
            raise self.error
        result = deepcopy(before)
        field = patch[-1]["path"].split("/")[-1]
        result["spec"][field] = patch[-1]["value"]
        result["metadata"].update(generation=3, resourceVersion="8")
        if not dry_run:
            self.objects[kind, name] = result
        return result


def execute(cluster, tmp_path, *, apply=False, targets=None):
    data = manifest()
    controller = cluster.objects[cluster.kind, "helper"]
    data["targets"] = targets or [
        {
            "kind": cluster.kind,
            "name": "helper",
            "uid": controller["metadata"]["uid"],
            "resource_version": "7",
        }
    ]
    cluster.journal = tmp_path / "controller.jsonl"
    with HaltJournal(cluster.journal) as journal:
        return halt_vms(
            cluster, HaltManifest.model_validate(data), journal, apply=apply
        )


@pytest.mark.parametrize("kind", ["Deployment", "ReplicaSet", "StatefulSet", "Job"])
@pytest.mark.parametrize("apply", [False, True])
def test_supported_controller_intent_only(tmp_path, kind, apply):
    cluster = Helpers(kind)
    before = deepcopy(cluster.objects)
    report = execute(cluster, tmp_path, apply=apply)
    assert report["status"] == "complete"
    assert report["results"][0]["status"] == (
        "desired_stop_observed" if apply else "dry_run_accepted"
    )
    patch = cluster.requests[0][3][-1]
    assert patch == {
        "op": "add" if kind == "Job" else "replace",
        "path": "/spec/suspend" if kind == "Job" else "/spec/replicas",
        "value": True if kind == "Job" else 0,
    }
    assert cluster.requests[0][-1] is not apply
    for flag in (
        "settlement_verified",
        "physical_stop_verified",
        "capacity_release_allowed",
        "storage_absence_verified",
    ):
        assert report[flag] is False
    for key in before:
        if key != (kind, "helper") or not apply:
            assert cluster.objects[key] == before[key]


@pytest.mark.parametrize(
    "hazard",
    [
        "foreign_root",
        "owner_api",
        "templates",
        "delete_scaled",
        "ephemeral",
        "host_path",
        "runtime_pvc",
        "pod_pvc",
        "wrong_pod_owner",
        "deleting_pod",
        "deleting_rs",
        "runtime_unmounted_pvc",
        "pvc_unbound",
        "pv_claim",
    ],
)
def test_unsafe_runtime_graph_blocks_entire_preflight(tmp_path, hazard):
    cluster = Helpers(
        "StatefulSet" if hazard in {"templates", "delete_scaled"} else "Deployment"
    )
    controller = cluster.objects[cluster.kind, "helper"]
    rs = obj("ReplicaSet", "runtime", controller, template={"spec": {"volumes": []}})
    rs["apiVersion"] = "apps/v1"
    pod = obj("Pod", "runtime", rs, volumes=[])
    cluster.objects["ReplicaSet", "runtime"] = rs
    cluster.objects["Pod", "runtime"] = pod
    pvc = cluster.objects["PersistentVolumeClaim", "disk"]
    if hazard == "foreign_root":
        controller["metadata"]["ownerReferences"][0]["kind"] = "TroshkaCeph"
    elif hazard == "owner_api":
        controller["metadata"]["ownerReferences"][0]["apiVersion"] = "evil/v1"
    elif hazard == "templates":
        controller["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"}}]
    elif hazard == "delete_scaled":
        controller["spec"]["persistentVolumeClaimRetentionPolicy"] = {
            "whenScaled": "Delete"
        }
    elif hazard in {"ephemeral", "host_path"}:
        controller["spec"]["template"]["spec"]["volumes"] = [
            {"name": "unsafe", "ephemeral" if hazard == "ephemeral" else "hostPath": {}}
        ]
    elif hazard in {"runtime_pvc", "runtime_unmounted_pvc"}:
        pvc["metadata"]["ownerReferences"] = obj("X", "x", rs)["metadata"][
            "ownerReferences"
        ]
    elif hazard == "pod_pvc":
        pvc["metadata"]["ownerReferences"] = obj("X", "x", pod)["metadata"][
            "ownerReferences"
        ]
    elif hazard == "wrong_pod_owner":
        pod["metadata"]["ownerReferences"][0]["apiVersion"] = "evil/v1"
    elif hazard in {"deleting_pod", "deleting_rs"}:
        (pod if hazard == "deleting_pod" else rs)["metadata"][
            "deletionTimestamp"
        ] = "now"
    elif hazard == "pvc_unbound":
        pvc["status"]["phase"] = "Pending"
    else:
        cluster.objects["PersistentVolume", "pv"]["spec"]["claimRef"]["uid"] = "wrong"
    if hazard in {"pvc_unbound", "pv_claim", "runtime_pvc", "pod_pvc"}:
        controller["spec"]["template"]["spec"]["volumes"] = [
            {"name": "disk", "persistentVolumeClaim": {"claimName": "disk"}}
        ]
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_job_with_retained_vm_owned_pvc_and_runtime_pod(tmp_path):
    cluster = Helpers("Job")
    job = cluster.objects["Job", "helper"]
    vm = cluster.objects["TroshkaVM", "troshka-vm"]
    job["metadata"]["ownerReferences"] = obj("X", "x", vm)["metadata"][
        "ownerReferences"
    ]
    job["spec"]["template"]["spec"]["volumes"] = [
        {"name": "disk", "persistentVolumeClaim": {"claimName": "disk"}}
    ]
    pod = obj(
        "Pod",
        "job-runtime",
        job,
        volumes=deepcopy(job["spec"]["template"]["spec"]["volumes"]),
    )
    pod["status"] = {"phase": "Running"}
    cluster.objects["Pod", "job-runtime"] = pod
    report = execute(cluster, tmp_path, apply=True)
    assert report["status"] == "complete"
    assert cluster.objects["Pod", "job-runtime"] == pod
    assert report["physical_stop_verified"] is False


def test_lost_controller_response_stops_without_retry(tmp_path):
    cluster = Helpers()
    cluster.error = TimeoutError("secret body must not be recorded")
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == "ambiguous"
    assert len(cluster.requests) == 1
    assert "secret body" not in cluster.journal.read_text()


def test_returned_controller_spec_tampering_is_ambiguous(tmp_path):
    cluster = Helpers()
    original = cluster.patch_controller

    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        result["spec"]["unrelated"] = "tampered"
        return result

    cluster.patch_controller = altered
    report = execute(cluster, tmp_path)
    assert report["results"][0]["status"] == "ambiguous"


def test_controller_manifest_default_preserves_vm_and_kind_identity():
    data = manifest()
    assert HaltManifest.model_validate(data).targets[0].kind == "VirtualMachine"
    data["targets"][0]["kind"] = "Pod"
    with pytest.raises(ValueError):
        HaltManifest.model_validate(data)


def test_runtime_owned_datavolume_blocks_even_unmounted_pvc(tmp_path):
    cluster = Helpers("Job")
    job = cluster.objects["Job", "helper"]
    pod = obj("Pod", "job-runtime", job, volumes=[])
    dv = obj("DataVolume", "runtime-dv", pod)
    cluster.objects["Pod", "job-runtime"] = pod
    cluster.objects["DataVolume", "runtime-dv"] = dv
    cluster.objects["PersistentVolumeClaim", "disk"]["metadata"][
        "ownerReferences"
    ] = obj("X", "x", dv)["metadata"]["ownerReferences"]
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_unknown_unmounted_storage_ancestry_blocks(tmp_path):
    cluster = Helpers()
    pvc = cluster.objects["PersistentVolumeClaim", "disk"]
    pvc["metadata"]["ownerReferences"][0]["kind"] = "Secret"
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_mixed_targets_preflight_before_first_dispatch(tmp_path):
    cluster = Helpers()
    targets = deepcopy(manifest()["targets"])
    targets.append(
        {
            "kind": "Deployment",
            "name": "helper",
            "uid": "wrong",
            "resource_version": "7",
        }
    )
    report = execute(cluster, tmp_path, targets=targets)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_failed_fresh_controller_read_is_request_accepted_only(tmp_path):
    cluster = Helpers()
    original = cluster.patch_controller

    def patch(*args, **kwargs):
        returned = original(*args, **kwargs)
        del cluster.objects["Deployment", "helper"]
        return returned

    cluster.patch_controller = patch
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == "stop_request_accepted"
    assert report["status"] == "incomplete"


@pytest.mark.parametrize(
    "builder",
    ["build_dnsmasq_deployment", "build_gateway_deployment", "build_exec_deployment"],
)
def test_actual_generated_helper_builders_are_supported(tmp_path, builder):
    import helpers.k8s as source

    cluster = Helpers()
    project = cluster.objects["TroshkaProject", "root"]
    network = cluster.objects["TroshkaNetwork", "network"]
    network["spec"] = {"cidr": "10.0.0.0/24"}
    if builder == "build_dnsmasq_deployment":
        generated = source.build_dnsmasq_deployment(network)
    elif builder == "build_gateway_deployment":
        generated = source.build_gateway_deployment(project, ["nad"])
    else:
        generated = source.build_exec_deployment(project, "nad")
    owners = generated["metadata"]["ownerReferences"]
    generated["metadata"].update(cluster.objects["Deployment", "helper"]["metadata"])
    generated["metadata"]["ownerReferences"] = owners
    cluster.objects["Deployment", "helper"] = generated
    report = execute(cluster, tmp_path)
    assert report["status"] == "complete"


def test_storage_ancestry_cycle_is_bounded_and_blocked(tmp_path):
    cluster = Helpers()
    dv = obj("DataVolume", "cycle")
    dv["metadata"]["ownerReferences"] = obj("X", "x", dv)["metadata"]["ownerReferences"]
    cluster.objects["DataVolume", "cycle"] = dv
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_runtime_pod_missing_uid_is_not_retention_evidence(tmp_path):
    cluster = Helpers("Job")
    pod = obj("Pod", "job-runtime", cluster.objects["Job", "helper"], volumes=[])
    cluster.objects["Pod", "job-runtime"] = pod
    original = cluster.workloads
    reads = 0

    def workloads(kind, namespace):
        nonlocal reads
        result = original(kind, namespace)
        if kind == "Pod":
            reads += 1
            if reads > 1:
                result["items"][0]["metadata"]["uid"] = ""
        return result

    cluster.workloads = workloads
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


def test_controller_resource_version_rechecked_before_dispatch(tmp_path):
    cluster = Helpers()
    original = cluster.get
    reads = 0

    def get(kind, namespace, name):
        nonlocal reads
        result = original(kind, namespace, name)
        if kind == "Deployment":
            reads += 1
            if reads > 1:
                result["metadata"]["resourceVersion"] = "changed"
        return result

    cluster.get = get
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked"
    assert cluster.requests == []


@pytest.mark.parametrize(
    "kind,resource",
    [
        ("Deployment", "deployments"),
        ("ReplicaSet", "replicasets"),
        ("StatefulSet", "statefulsets"),
        ("Job", "jobs"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_real_controller_transport_uses_exact_one_shot_endpoint(
    kind, resource, dry_run
):
    import io
    from types import SimpleNamespace

    from kubernetes.client import ApiClient, Configuration
    from urllib3.response import HTTPResponse

    from app.services.native_vm_halt_transport import HaltClient

    config = Configuration()
    config.host = "https://example.invalid"
    api = ApiClient(config)
    calls = []
    response = HTTPResponse(body=io.BytesIO(b"{}"), status=200, preload_content=False)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    api.rest_client.pool_manager = SimpleNamespace(request=request)
    client = object.__new__(HaltClient)
    client.api, client.scan_deadline, client.max_response_bytes = api, None, 1024
    try:
        assert (
            client.patch_controller(kind, "project", "helper", [], dry_run=dry_run)
            == {}
        )
        assert len(calls) == 1
        method, url, kwargs = calls[0]
        group = "batch" if kind == "Job" else "apps"
        assert method == "PATCH"
        assert (
            url
            == f"https://example.invalid/apis/{group}/v1/namespaces/project/{resource}/helper"
            + ("?dryRun=All" if dry_run else "")
        )
        assert kwargs["redirect"] is False and kwargs["retries"] is False
        assert response.closed
    finally:
        api.close()
