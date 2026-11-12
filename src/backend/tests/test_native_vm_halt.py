"""Maintenance halt must never grow, delete, retry, or mistake intent for stop."""

import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "operator"))

PROJECT_ID = "00000000-0000-4000-8000-000000000001"
VM_UID = "00000000-0000-4000-8000-000000000002"


def obj(kind, name, owner=None, **spec):
    versions = {
        "VirtualMachine": "kubevirt.io/v1",
        "VirtualMachineInstance": "kubevirt.io/v1",
        "TroshkaVM": "troshka.redhat.com/v1alpha1",
        "TroshkaProject": "troshka.redhat.com/v1alpha1",
        "DataVolume": "cdi.kubevirt.io/v1beta1",
    }
    value = {
        "apiVersion": versions.get(kind, "v1"),
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "project",
            "uid": name + "-uid",
            "resourceVersion": "7",
            "generation": 2,
        },
        "spec": spec,
    }
    if kind == "VirtualMachine":
        value["metadata"]["uid"] = VM_UID
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


class Cluster:
    max_objects = 1000

    def __init__(self):
        project = obj("TroshkaProject", "root", projectId=PROJECT_ID)
        owner = obj("TroshkaVM", "troshka-vm", project)
        pvc = obj("PersistentVolumeClaim", "disk", owner, volumeName="pv")
        pvc["status"] = {"phase": "Bound"}
        pv = obj(
            "PersistentVolume",
            "pv",
            claimRef={"name": "disk", "namespace": "project", "uid": "disk-uid"},
        )
        pv["metadata"].pop("namespace")
        vm = obj(
            "VirtualMachine",
            "vm",
            owner,
            runStrategy="Always",
            template={
                "spec": {
                    "volumes": [
                        {
                            "name": "disk",
                            "persistentVolumeClaim": {"claimName": "disk"},
                        },
                        {
                            "name": "cloudinit",
                            "cloudInitNoCloud": {"secretRef": {"name": "private"}},
                        },
                    ]
                }
            },
        )
        self.objects = {
            (v["kind"], v["metadata"]["name"]): v for v in (project, owner, pvc, pv, vm)
        }
        self.requests = []
        self.error = None
        self.journal = None

    def cluster_identity(self):
        return "cluster-uid"

    def namespace(self, name):
        return {"metadata": {"uid": "namespace-uid"}}

    def projects(self, namespace):
        return [deepcopy(self.objects["TroshkaProject", "root"])]

    def get(self, kind, namespace, name):
        return deepcopy(self.objects[kind, name])

    def workloads(self, kind, namespace):
        return {
            "metadata": {"resourceVersion": "8"},
            "items": [deepcopy(v) for (k, n), v in self.objects.items() if k == kind],
        }

    def patch_vm(self, namespace, name, patch, *, dry_run):
        # The output is a durable recovery artifact before the external effect.
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        assert events[-1]["event"] == "attempt"
        assert events[-1]["target"]["uid"] == VM_UID
        self.requests.append((namespace, name, patch, dry_run))
        if self.error:
            raise self.error
        before = self.objects["VirtualMachine", name]
        assert patch[:2] == [
            {"op": "test", "path": "/metadata/uid", "value": VM_UID},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
        ]
        assert patch[2] == {
            "op": "replace",
            "path": "/spec/runStrategy",
            "value": "Halted",
        }
        result = deepcopy(before)
        result["spec"]["runStrategy"] = "Halted"
        result["metadata"].update(generation=3, resourceVersion="8")
        if not dry_run:
            self.objects["VirtualMachine", name] = result
        return result


def manifest():
    return {
        "collector_id": "test",
        "root": {
            "project_id": PROJECT_ID,
            "cluster_key": "cluster-uid",
            "namespace": "project",
            "namespace_uid": "namespace-uid",
            "project_cr_uid": "root-uid",
            "deployment_generation": 2,
        },
        "targets": [{"name": "vm", "uid": VM_UID, "resource_version": "7"}],
    }


def execute(cluster, tmp_path, apply=False):
    module = importlib.import_module("app.services.native_vm_halt")
    schema = importlib.import_module("app.schemas.native_vm_halt")
    cluster.journal = tmp_path / "journal.jsonl"
    with module.HaltJournal(cluster.journal) as journal:
        return module.halt_vms(
            cluster,
            schema.HaltManifest.model_validate(manifest()),
            journal,
            apply=apply,
        )


def test_default_dryrun_and_apply_use_exact_cleanup_and_keep_release_false(tmp_path):
    cluster = Cluster()
    report = execute(cluster, tmp_path)
    assert cluster.requests[0][3] is True
    assert cluster.objects["VirtualMachine", "vm"]["spec"]["runStrategy"] == "Always"
    assert report["results"][0]["status"] == "dry_run_accepted"
    assert report["capacity_release_allowed"] is False
    assert report["settlement_verified"] is False
    assert (tmp_path / "journal.jsonl").stat().st_mode & 0o777 == 0o600


def test_apply_reports_desired_halt_but_paused_vmi_remains_active(tmp_path):
    cluster = Cluster()
    vmi = obj(
        "VirtualMachineInstance",
        "vm",
        cluster.objects["VirtualMachine", "vm"],
        volumes=[],
    )
    vmi["status"] = {
        "phase": "Running",
        "conditions": [{"type": "Paused", "status": "True"}],
    }
    cluster.objects["VirtualMachineInstance", "vm"] = vmi
    report = execute(cluster, tmp_path, apply=True)
    assert cluster.requests[0][3] is False
    assert report["results"][0]["status"] == "desired_halt_observed"
    observed = report["observation"]["collections"]
    assert (
        next(c for c in observed if c["kind"] == "VirtualMachineInstance")["resources"][
            0
        ]["compute_state"]
        == "potentially_active"
    )
    assert report["physical_stop_verified"] is False


@pytest.mark.parametrize(
    "hazard",
    [
        "vm_uid",
        "vm_rv",
        "root_uid",
        "root_deleting",
        "wrong_owner",
        "pvc_pod_owner",
        "pv_claim",
        "pv_deleting",
        "pvc_deleting",
        "ephemeral",
        "dv_template",
        "hotplug",
        "pod_ephemeral",
    ],
)
def test_unsafe_or_unverified_profile_never_dispatches(tmp_path, hazard):
    cluster = Cluster()
    vm, pvc, pv = (
        cluster.objects[key]
        for key in [
            ("VirtualMachine", "vm"),
            ("PersistentVolumeClaim", "disk"),
            ("PersistentVolume", "pv"),
        ]
    )
    if hazard == "vm_uid":
        vm["metadata"]["uid"] = "replacement"
    elif hazard == "vm_rv":
        vm["metadata"]["resourceVersion"] = "changed"
    elif hazard == "root_uid":
        cluster.objects["TroshkaProject", "root"]["metadata"]["uid"] = "other"
    elif hazard == "root_deleting":
        cluster.objects["TroshkaProject", "root"]["metadata"][
            "deletionTimestamp"
        ] = "now"
    elif hazard == "wrong_owner":
        vm["metadata"]["ownerReferences"][0]["uid"] = "other"
    elif hazard == "pvc_pod_owner":
        pvc["metadata"]["ownerReferences"] = [
            obj("Pod", "writer")["metadata"]
            | {"kind": "Pod", "apiVersion": "v1", "controller": True}
        ]
    elif hazard == "pv_claim":
        pv["spec"]["claimRef"]["uid"] = "replacement"
    elif hazard in {"pv_deleting", "pvc_deleting"}:
        (pv if hazard == "pv_deleting" else pvc)["metadata"][
            "deletionTimestamp"
        ] = "now"
    elif hazard == "ephemeral":
        vm["spec"]["template"]["spec"]["volumes"].append(
            {
                "name": "scratch",
                "ephemeral": {"persistentVolumeClaim": {"claimName": "disk"}},
            }
        )
    elif hazard == "dv_template":
        vm["spec"]["dataVolumeTemplates"] = [{}]
    elif hazard == "hotplug":
        vm["status"] = {"volumeRequests": [{}]}
    else:
        vmi = obj("VirtualMachineInstance", "vm", vm, volumes=[])
        cluster.objects["VirtualMachineInstance", "vm"] = vmi
        cluster.objects["Pod", "launcher"] = obj(
            "Pod", "launcher", vmi, volumes=[{"name": "temp", "ephemeral": {}}]
        )
    report = execute(cluster, tmp_path, apply=True)
    assert not cluster.requests
    assert report["status"] == "blocked"


def test_transport_failure_is_ambiguous_and_not_retried(tmp_path):
    cluster = Cluster()
    cluster.error = TimeoutError("SECRET response token")
    report = execute(cluster, tmp_path, apply=True)
    assert len(cluster.requests) == 1
    assert report["results"][0]["status"] == "ambiguous"
    assert "SECRET" not in (tmp_path / "journal.jsonl").read_text()


def test_retained_cdi_chain_is_supported(tmp_path):
    cluster = Cluster()
    dv = obj("DataVolume", "disk", cluster.objects["TroshkaVM", "troshka-vm"])
    cluster.objects["DataVolume", "disk"] = dv
    cluster.objects["PersistentVolumeClaim", "disk"]["metadata"]["ownerReferences"] = [
        {
            "apiVersion": dv["apiVersion"],
            "kind": "DataVolume",
            "name": "disk",
            "uid": "disk-uid",
            "controller": True,
        }
    ]
    assert execute(cluster, tmp_path)["status"] == "complete"


def test_journal_refuses_existing_output(tmp_path):
    module = importlib.import_module("app.services.native_vm_halt")
    path = tmp_path / "existing"
    path.write_text("preserve")
    with pytest.raises(FileExistsError):
        with module.HaltJournal(path):
            pytest.fail("existing journal replaced")
    assert path.read_text() == "preserve"


def test_all_targets_preflight_before_first_request(tmp_path):
    from app.schemas.native_vm_halt import HaltManifest
    from app.services.native_vm_halt import HaltJournal, halt_vms

    cluster = Cluster()
    data = manifest()
    data["targets"].append({"name": "missing", "uid": "other", "resource_version": "7"})
    with HaltJournal(tmp_path / "journal") as journal:
        report = halt_vms(
            cluster, HaltManifest.model_validate(data), journal, apply=True
        )
    assert report["status"] == "blocked" and not cluster.requests


@pytest.mark.parametrize("at", ["attempt", "result"])
def test_journal_failure_aborts_dispatch_or_leaves_attempt_unresolved(tmp_path, at):
    from app.schemas.native_vm_halt import HaltManifest
    from app.services.native_vm_halt import HaltJournal, halt_vms

    cluster = Cluster()
    cluster.journal = tmp_path / "journal"
    with HaltJournal(cluster.journal) as journal:
        append = journal.append

        def fail(event, **fields):
            if event == at:
                raise OSError("disk full")
            append(event, **fields)

        journal.append = fail
        with pytest.raises(OSError):
            halt_vms(
                cluster, HaltManifest.model_validate(manifest()), journal, apply=True
            )
    assert len(cluster.requests) == (1 if at == "result" else 0)
    events = [json.loads(line) for line in cluster.journal.read_text().splitlines()]
    assert not any(e["event"] == "result" for e in events)


@pytest.mark.parametrize("bad", ["uid", "desired", "namespace", "name", "hitchhiking"])
def test_success_http_with_wrong_returned_resource_is_ambiguous(tmp_path, bad):
    cluster = Cluster()
    original = cluster.patch_vm

    def patch(*args, **kwargs):
        result = original(*args, **kwargs)
        if bad == "desired":
            result["spec"]["runStrategy"] = "Always"
        elif bad == "hitchhiking":
            result["spec"]["template"]["spec"]["domain"] = {"cpu": {"cores": 999}}
        else:
            result["metadata"][bad] = "other"
        return result

    cluster.patch_vm = patch
    report = execute(cluster, tmp_path, apply=True)
    assert report["results"][0]["status"] == "ambiguous"


@pytest.mark.parametrize("status", [200, 302, 409, 503])
def test_patch_transport_bounds_body_disables_redirect_retry_and_carries_dryrun(status):
    import io
    from types import SimpleNamespace

    from kubernetes.client import ApiClient, Configuration
    from urllib3.response import HTTPResponse

    module = importlib.import_module("app.services.native_vm_halt_transport")
    config = Configuration()
    config.host = "https://example.invalid"
    config.api_key["authorization"] = "Bearer PRIVATE"
    api = ApiClient(config)
    calls = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 1025
            assert status == 200, "error body must not be read"
            return super().read(size)

    response = HTTPResponse(body=Body(b"{}"), status=status, preload_content=False)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    api.rest_client.pool_manager = SimpleNamespace(request=request)
    client = object.__new__(module.HaltClient)
    client.api, client.scan_deadline, client.max_response_bytes = api, None, 1024
    try:
        if status == 200:
            assert client.patch_vm("project", "vm", [], dry_run=True) == {}
        else:
            with pytest.raises(ValueError):
                client.patch_vm("project", "vm", [], dry_run=True)
        assert len(calls) == 1
        method, url, kwargs = calls[0]
        assert method == "PATCH"
        assert (
            url
            == "https://example.invalid/apis/kubevirt.io/v1/namespaces/project/virtualmachines/vm?dryRun=All"
        )
        assert kwargs["headers"]["Content-Type"] == "application/json-patch+json"
        assert kwargs["headers"]["authorization"] == "Bearer PRIVATE"
        assert kwargs["body"] == b"[]"
        assert (
            not kwargs["redirect"]
            and not kwargs["retries"]
            and not kwargs["preload_content"]
        )
        assert response.closed
    finally:
        api.close()


def test_cli_default_dryrun_and_create_new_private_output(tmp_path):
    import runpy
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[3] / "scripts" / "halt_kubevirt_vms.py"
    module = runpy.run_path(str(script))
    source, output = tmp_path / "targets.json", tmp_path / "journal.jsonl"
    source.write_text(json.dumps(manifest()))
    cluster = Cluster()
    cluster.journal = output
    cluster.api = SimpleNamespace(close=lambda: None)
    module["main"].__globals__["HaltClient"] = lambda *a, **kw: cluster
    argv = [
        "--manifest",
        str(source),
        "--kubeconfig",
        "/explicit",
        "--output",
        str(output),
    ]
    assert module["main"](argv) == 0
    assert cluster.requests[0][-1] is True
    before = output.read_bytes()
    assert module["main"](argv) == 1
    assert output.read_bytes() == before


def test_cli_refuses_input_alias_before_opening_client(tmp_path):
    import runpy

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "scripts" / "halt_kubevirt_vms.py")
    )
    source, alias = tmp_path / "targets.json", tmp_path / "alias"
    source.write_text(json.dumps(manifest()))
    alias.symlink_to(source)
    module["main"].__globals__["HaltClient"] = lambda *a, **kw: pytest.fail(
        "opened client"
    )
    assert (
        module["main"](
            [
                "--manifest",
                str(source),
                "--kubeconfig",
                "/explicit",
                "--output",
                str(alias),
            ]
        )
        == 1
    )


@pytest.mark.parametrize("kind", ["StatefulSet", "Pod", "mixed"])
def test_runtime_and_mixed_pvc_owners_rejected(tmp_path, kind):
    cluster = Cluster()
    refs = cluster.objects["PersistentVolumeClaim", "disk"]["metadata"][
        "ownerReferences"
    ]
    if kind == "mixed":
        refs.append(deepcopy(refs[0]))
    else:
        refs[0]["kind"] = kind
    assert execute(cluster, tmp_path, apply=True)["status"] == "blocked"
    assert not cluster.requests


def test_ambiguous_first_target_never_dispatches_second(tmp_path):
    from app.schemas.native_vm_halt import HaltManifest
    from app.services.native_vm_halt import HaltJournal, halt_vms

    cluster = Cluster()
    second = deepcopy(cluster.objects["VirtualMachine", "vm"])
    second["metadata"].update(name="vm2", uid="00000000-0000-4000-8000-000000000003")
    cluster.objects["VirtualMachine", "vm2"] = second
    data = manifest()
    data["targets"].append(
        {"name": "vm2", "uid": second["metadata"]["uid"], "resource_version": "7"}
    )
    cluster.error = TimeoutError("secret")
    cluster.journal = tmp_path / "journal"
    with HaltJournal(cluster.journal) as journal:
        report = halt_vms(
            cluster, HaltManifest.model_validate(data), journal, apply=True
        )
    assert len(cluster.requests) == 1
    assert report["status"] == "incomplete"
    assert cluster.objects["VirtualMachine", "vm2"]["spec"]["runStrategy"] == "Always"


def test_root_generation_changed_during_preflight_rejects_before_dispatch(tmp_path):
    cluster = Cluster()
    original = cluster.get

    def get(kind, namespace, name):
        if kind == "PersistentVolume":
            cluster.objects["TroshkaProject", "root"]["metadata"]["generation"] = 3
        return original(kind, namespace, name)

    cluster.get = get
    report = execute(cluster, tmp_path, apply=True)
    assert not cluster.requests
    assert report["status"] == "blocked"


@pytest.mark.parametrize("mode", ["running", "both"])
def test_deprecated_running_exact_transition_or_mixed_mode_denial(tmp_path, mode):
    cluster = Cluster()
    vm = cluster.objects["VirtualMachine", "vm"]
    vm["spec"]["running"] = True
    if mode == "running":
        del vm["spec"]["runStrategy"]

    def patch(namespace, name, changes, *, dry_run):
        assert changes[-1] == {"op": "replace", "path": "/spec/running", "value": False}
        cluster.requests.append(changes)
        returned = deepcopy(vm)
        returned["spec"]["running"] = False
        returned["metadata"]["generation"] = 3
        return returned

    cluster.patch_vm = patch
    report = execute(cluster, tmp_path)
    assert bool(cluster.requests) is (mode == "running")
    assert report["status"] == ("complete" if mode == "running" else "blocked")


@pytest.mark.parametrize("payload", [b"x" * 1025, b"[]", b"not-json"])
def test_patch_rejects_unbounded_or_malformed_success_body(payload):
    import io
    from types import SimpleNamespace

    from kubernetes.client import ApiClient, Configuration
    from urllib3.response import HTTPResponse

    from app.services.native_vm_halt_transport import HaltClient

    api = ApiClient(Configuration())
    response = HTTPResponse(body=io.BytesIO(payload), status=200, preload_content=False)
    api.rest_client.pool_manager = SimpleNamespace(request=lambda *a, **kw: response)
    client = object.__new__(HaltClient)
    client.api, client.scan_deadline, client.max_response_bytes = api, None, 1024
    try:
        with pytest.raises(ValueError):
            client.patch_vm("project", "vm", [], dry_run=False)
        assert response.closed
    finally:
        api.close()
