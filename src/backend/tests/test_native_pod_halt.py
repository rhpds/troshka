"""Explicit bare Pod deletion retains assets and never proves physical stop."""

import json
from copy import deepcopy

import pytest

from app.schemas.native_vm_halt import HaltManifest
from app.services.native_vm_halt import HaltJournal, halt_vms
from tests.test_native_vm_halt import Cluster, manifest, obj


class Pods(Cluster):
    def __init__(self):
        super().__init__()
        project = self.objects["TroshkaProject", "root"]
        self.objects["Pod", "showroom"] = obj("Pod", "showroom", project, volumes=[])
        self.followup = "absent"
        self.reply = None

    def delete_pod(self, namespace, name, options, *, dry_run):
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        assert events[-1]["event"] == "attempt"
        assert options == {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": "showroom-uid", "resourceVersion": "7"},
        }
        self.requests.append((namespace, name, options, dry_run))
        if self.error:
            raise self.error
        result = deepcopy(self.objects["Pod", name])
        result["metadata"]["deletionTimestamp"] = "2026-11-12T12:00:00Z"
        if not dry_run:
            self.objects["Pod", name] = result
        return self.reply if self.reply is not None else result

    def read_pod_after_delete(self, namespace, name):
        if self.followup == "absent":
            return None
        if self.followup == "error":
            raise TimeoutError("private")
        value = deepcopy(self.objects["Pod", name])
        if self.followup == "replacement":
            value["metadata"]["uid"] = "replacement"
        return value


def execute(cluster, tmp_path, *, apply=False, targets=None):
    data = manifest()
    data["targets"] = targets or [
        {
            "kind": "Pod",
            "name": "showroom",
            "uid": "showroom-uid",
            "resource_version": "7",
        }
    ]
    cluster.journal = tmp_path / "pods.jsonl"
    with HaltJournal(cluster.journal) as journal:
        return halt_vms(
            cluster, HaltManifest.model_validate(data), journal, apply=apply
        )


@pytest.mark.parametrize("apply", [False, True])
def test_direct_project_pod_graceful_delete_only(tmp_path, apply):
    cluster = Pods()
    before = deepcopy(cluster.objects)
    report = execute(cluster, tmp_path, apply=apply)
    assert report["status"] == "complete"
    assert report["results"][0]["status"] == (
        "absence_observed" if apply else "dry_run_accepted"
    )
    assert len(cluster.requests) == 1 and cluster.requests[0][-1] is not apply
    for key in before:
        if key != ("Pod", "showroom") or not apply:
            assert cluster.objects[key] == before[key]
    for flag in (
        "settlement_verified",
        "physical_stop_verified",
        "capacity_release_allowed",
        "storage_absence_verified",
    ):
        assert report[flag] is False


@pytest.mark.parametrize(
    "hazard",
    [
        "controller",
        "launcher",
        "ephemeral",
        "hostPath",
        "unknown",
        "pod_pvc",
        "indirect_dv",
        "ownerless",
        "rv",
        "deleting",
        "pv",
    ],
)
def test_unsafe_pod_profiles_block_before_delete(tmp_path, hazard):
    cluster = Pods()
    pod = cluster.objects["Pod", "showroom"]
    pvc = cluster.objects["PersistentVolumeClaim", "disk"]
    if hazard in {"controller", "launcher"}:
        pod["metadata"]["ownerReferences"][0]["kind"] = (
            "Job" if hazard == "controller" else "VirtualMachineInstance"
        )
    elif hazard in {"ephemeral", "hostPath", "unknown"}:
        pod["spec"]["volumes"] = [{"name": "unsafe", hazard: {}}]
    elif hazard == "pod_pvc":
        pvc["metadata"]["ownerReferences"] = obj("X", "x", pod)["metadata"][
            "ownerReferences"
        ]
    elif hazard == "indirect_dv":
        dv = obj("DataVolume", "dv", pod)
        cluster.objects["DataVolume", "dv"] = dv
        pvc["metadata"]["ownerReferences"] = obj("X", "x", dv)["metadata"][
            "ownerReferences"
        ]
    elif hazard == "ownerless":
        pod["metadata"]["ownerReferences"] = []
    elif hazard == "rv":
        pod["metadata"]["resourceVersion"] = "changed"
    elif hazard == "deleting":
        pod["metadata"]["deletionTimestamp"] = "now"
    else:
        pvc["metadata"]["ownerReferences"] = obj(
            "X", "x", cluster.objects["TroshkaProject", "root"]
        )["metadata"]["ownerReferences"]
        pod["spec"]["volumes"] = [
            {"name": "disk", "persistentVolumeClaim": {"claimName": "disk"}}
        ]
        cluster.objects["PersistentVolume", "pv"]["spec"]["claimRef"]["uid"] = "wrong"
    report = execute(cluster, tmp_path)
    assert report["status"] == "blocked" and not cluster.requests


@pytest.mark.parametrize(
    "followup,status",
    [
        ("deleting", "delete_request_accepted"),
        ("error", "delete_request_accepted"),
        ("replacement", "ambiguous"),
    ],
)
def test_followup_never_confuses_replacement_or_deleting_with_stop(
    tmp_path, followup, status
):
    cluster = Pods()
    cluster.followup = followup
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == status
    assert report["status"] == "incomplete"


def test_root_changed_before_absence_read_is_not_absence(tmp_path):
    cluster = Pods()
    original = cluster.delete_pod

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        cluster.namespace = lambda name: {"metadata": {"uid": "replacement"}}
        return result

    cluster.delete_pod = changed
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == "delete_request_accepted"


def test_root_changes_during_absence_read_does_not_prove_absence(tmp_path):
    cluster = Pods()

    def changed(namespace, name):
        cluster.namespace = lambda name: {"metadata": {"uid": "replacement"}}
        return None

    cluster.read_pod_after_delete = changed
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == "delete_request_accepted"


def test_mixed_target_invalid_pod_blocks_vm_request(tmp_path):
    cluster = Pods()
    targets = manifest()["targets"] + [
        {"kind": "Pod", "name": "showroom", "uid": "wrong", "resource_version": "7"}
    ]
    assert execute(cluster, tmp_path, targets=targets)["status"] == "blocked"
    assert cluster.requests == []


def test_failed_attempt_fsync_prevents_delete(tmp_path, monkeypatch):
    import os

    cluster = Pods()
    original = os.fsync

    def fsync(fd):
        if (
            cluster.journal
            and cluster.journal.exists()
            and '"event": "attempt"' in cluster.journal.read_text()
        ):
            raise OSError("fsync failed")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="fsync failed"):
        execute(cluster, tmp_path)
    assert cluster.requests == []


def test_lost_delete_response_never_retries_or_dispatches_next_target(tmp_path):
    cluster = Pods()
    cluster.error = TimeoutError("private response")
    targets = [
        {
            "kind": "Pod",
            "name": "showroom",
            "uid": "showroom-uid",
            "resource_version": "7",
        }
    ] + manifest()["targets"]
    report = execute(cluster, tmp_path, apply=True, targets=targets)
    assert report["results"][0]["status"] == "ambiguous"
    assert len(report["results"]) == len(cluster.requests) == 1
    assert "private response" not in cluster.journal.read_text()


def test_project_owned_bound_pvc_is_retained_with_volatile_volumes(tmp_path):
    cluster = Pods()
    project = cluster.objects["TroshkaProject", "root"]
    pvc = cluster.objects["PersistentVolumeClaim", "disk"]
    pvc["metadata"]["ownerReferences"] = obj("X", "x", project)["metadata"][
        "ownerReferences"
    ]
    cluster.objects["Pod", "showroom"]["spec"]["volumes"] = [
        {"name": "disk", "persistentVolumeClaim": {"claimName": "disk"}},
        {"name": "volatile", "emptyDir": {}},
        {"name": "config", "configMap": {"name": "settings"}},
    ]
    before = deepcopy(pvc)
    assert execute(cluster, tmp_path, apply=True)["status"] == "complete"
    assert cluster.objects["PersistentVolumeClaim", "disk"] == before


def test_project_owned_datavolume_mount_retained(tmp_path):
    cluster = Pods()
    project = cluster.objects["TroshkaProject", "root"]
    dv = obj("DataVolume", "dv", project)
    cluster.objects["DataVolume", "dv"] = dv
    cluster.objects["PersistentVolumeClaim", "disk"]["metadata"][
        "ownerReferences"
    ] = obj("X", "x", dv)["metadata"]["ownerReferences"]
    cluster.objects["Pod", "showroom"]["spec"]["volumes"] = [
        {"name": "disk", "persistentVolumeClaim": {"claimName": "disk"}}
    ]
    assert execute(cluster, tmp_path)["status"] == "complete"


@pytest.mark.parametrize("hazard", ["continuation", "unknown_owner", "cycle"])
def test_whole_namespace_storage_inventory_is_required(tmp_path, hazard):
    cluster = Pods()
    if hazard == "continuation":
        original = cluster.workloads

        def workloads(kind, namespace):
            result = original(kind, namespace)
            if kind == "DataVolume":
                result["metadata"]["continue"] = "more"
            return result

        cluster.workloads = workloads
    elif hazard == "unknown_owner":
        cluster.objects["PersistentVolumeClaim", "disk"]["metadata"]["ownerReferences"][
            0
        ]["kind"] = "Unknown"
    else:
        dv = obj("DataVolume", "cycle")
        dv["metadata"]["ownerReferences"] = obj("X", "x", dv)["metadata"][
            "ownerReferences"
        ]
        cluster.objects["DataVolume", "cycle"] = dv
    assert execute(cluster, tmp_path)["status"] == "blocked"
    assert not cluster.requests


@pytest.mark.parametrize("builder", ["build_single_container_pod", "build_pod_group"])
def test_actual_showroom_container_builders_supported(tmp_path, builder):
    from helpers import containers

    cluster = Pods()
    original = cluster.objects["Pod", "showroom"]
    ctr = {
        "id": "showroom",
        "image": "example.invalid/showroom:v1",
        "podContainers": [{"name": "main", "image": "example.invalid/showroom:v1"}],
    }
    generated = getattr(containers, builder)(
        "project", ctr, {}, original["metadata"]["ownerReferences"][0], {}
    )
    generated["metadata"].update(original["metadata"])
    cluster.objects["Pod", "showroom"] = generated
    assert execute(cluster, tmp_path)["status"] == "complete"


def api_status(name="showroom", uid="showroom-uid"):
    return {
        "apiVersion": "v1",
        "kind": "Status",
        "status": "Success",
        "code": 200,
        "details": {"name": name, "kind": "pods", "uid": uid},
    }


@pytest.mark.parametrize(
    "reply", ["status", "wrong_uid", "wrong_name", "failure", "unknown", "missing_rv"]
)
def test_delete_response_exact_identity_and_known_success_required(tmp_path, reply):
    cluster = Pods()
    cluster.reply = api_status()
    if reply == "wrong_uid":
        cluster.reply["details"]["uid"] = "other"
    elif reply == "wrong_name":
        cluster.reply["details"]["name"] = "other"
    elif reply == "failure":
        cluster.reply["status"] = "Failure"
    elif reply == "unknown":
        cluster.reply = {}
    elif reply == "missing_rv":
        cluster.reply = deepcopy(cluster.objects["Pod", "showroom"])
        del cluster.reply["metadata"]["resourceVersion"]
    report = execute(cluster, tmp_path)
    assert report["results"][0]["status"] == (
        "dry_run_accepted" if reply == "status" else "ambiguous"
    )


@pytest.mark.parametrize("operation", ["delete", "get"])
@pytest.mark.parametrize(
    "response_kind",
    [
        "pod",
        "notfound",
        "namespace_missing",
        "wrong_name",
        "http500",
        "timeout",
        "oversized",
        "invalid",
    ],
)
def test_real_api_client_bounded_single_shot_transport(operation, response_kind):
    import io
    from types import SimpleNamespace

    from kubernetes.client import ApiClient, Configuration
    from urllib3.response import HTTPResponse

    from app.services.native_vm_halt_transport import HaltClient

    config = Configuration()
    config.host = "https://example.invalid"
    api = ApiClient(config)
    body, status = obj("Pod", "showroom"), 200
    if response_kind in {"notfound", "namespace_missing", "wrong_name"}:
        status = 404
        body = {
            "apiVersion": "v1",
            "kind": "Status",
            "status": "Failure",
            "reason": "NotFound",
            "code": 404,
            "details": {"name": "showroom", "kind": "pods"},
        }
        if response_kind == "namespace_missing":
            body["details"].update(name="project", kind="namespaces")
        elif response_kind == "wrong_name":
            body["details"]["name"] = "other"
    elif response_kind == "http500":
        status = 500
    payload = json.dumps(body).encode()
    if response_kind == "oversized":
        payload = b"x" * 2049
    elif response_kind == "invalid":
        payload = b"[]"
    response = HTTPResponse(
        body=io.BytesIO(payload), status=status, preload_content=False
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if response_kind == "timeout":
            raise TimeoutError("private")
        return response

    api.rest_client.pool_manager = SimpleNamespace(request=request)
    client = object.__new__(HaltClient)
    client.api, client.scan_deadline, client.max_response_bytes = api, None, 2048
    options = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {"uid": "showroom-uid", "resourceVersion": "7"},
    }

    def run():
        return (
            client.delete_pod("project", "showroom", options, dry_run=True)
            if operation == "delete"
            else client.read_pod_after_delete("project", "showroom")
        )

    try:
        if response_kind == "pod":
            assert run() == body
        elif response_kind == "notfound" and operation == "get":
            assert run() is None
        else:
            with pytest.raises((ValueError, TimeoutError)):
                run()
        assert len(calls) == 1
        method, url, kwargs = calls[0]
        assert method == ("DELETE" if operation == "delete" else "GET")
        assert (
            url
            == "https://example.invalid/api/v1/namespaces/project/pods/showroom"
            + ("?dryRun=All" if operation == "delete" else "")
        )
        assert kwargs["redirect"] is False and kwargs["retries"] is False
        assert kwargs["timeout"].total <= 15
        if operation == "delete":
            assert json.loads(kwargs["body"]) == options
            assert kwargs["headers"]["Content-Type"] == "application/json"
        if response_kind != "timeout":
            assert response.closed
    finally:
        api.close()


def test_delete_transport_exhausted_deadline_never_sends_request():
    from types import SimpleNamespace

    from kubernetes.client import ApiClient, Configuration

    from app.services.native_vm_halt_transport import HaltClient

    api = ApiClient(Configuration())
    calls = []
    api.rest_client.pool_manager = SimpleNamespace(
        request=lambda *a, **kw: calls.append(a)
    )
    client = object.__new__(HaltClient)
    client.api, client.scan_deadline, client.max_response_bytes = api, 1, 1024
    try:
        with pytest.raises(TimeoutError):
            client.delete_pod("project", "showroom", {}, dry_run=False)
        assert calls == []
    finally:
        api.close()


def test_pod_rv_revalidated_before_dispatch(tmp_path):
    cluster = Pods()
    original = cluster.get
    reads = 0

    def get(kind, namespace, name):
        nonlocal reads
        value = original(kind, namespace, name)
        if kind == "Pod":
            reads += 1
            if reads > 1:
                value["metadata"]["resourceVersion"] = "changed"
        return value

    cluster.get = get
    assert execute(cluster, tmp_path)["status"] == "blocked"
    assert not cluster.requests
