"""Direct observations must never collapse live helpers into desired VM state."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.kubevirt_storage import ProjectRoot

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = ProjectRoot(
    project_id="00000000-0000-4000-8000-000000000001",
    cluster_key="cluster",
    namespace="project",
    namespace_uid="ns",
    project_cr_uid="cr",
    deployment_generation=2,
)


class Reader:
    def __init__(self):
        self.rows = {}
        self.calls = 0

    def cluster_identity(self):
        return "cluster"

    def namespace(self, name):
        self.calls += 1
        return {"metadata": {"uid": "ns"}}

    def projects(self, namespace):
        return [
            {
                "metadata": {"uid": "cr", "generation": 2},
                "spec": {"projectId": str(ROOT.project_id)},
            }
        ]

    def workloads(self, kind, namespace):
        assert namespace == "project"
        value = self.rows.get(kind, [])
        if isinstance(value, Exception):
            raise value
        return {"metadata": {"resourceVersion": "42"}, "items": deepcopy(value)}


def row(name, **fields):
    return {
        "metadata": {
            "name": name,
            "namespace": "project",
            "uid": name,
            "resourceVersion": "5",
        },
        **fields,
    }


def observe(reader):
    from app.services import native_workload_observations as service

    return service.observe_workloads(
        reader, ROOT, collector_id="test", clock=lambda: NOW
    )


def test_halted_vm_does_not_hide_paused_vmi_and_live_unowned_helper():
    reader = Reader()
    reader.rows = {
        "VirtualMachine": [row("vm", spec={"running": False})],
        "VirtualMachineInstance": [
            row(
                "vmi",
                status={
                    "phase": "Running",
                    "conditions": [{"type": "Paused", "status": "True"}],
                },
            )
        ],
        "Pod": [
            row(
                "installer",
                spec={"containers": [{"env": [{"value": "SECRET"}]}]},
                status={"phase": "Running"},
            )
        ],
    }
    report = observe(reader)
    assert report.inventory_complete
    resources = {r.name: r for item in report.collections for r in item.resources}
    assert resources["vmi"].paused is True
    assert resources["vmi"].compute_state == "potentially_active"
    assert resources["installer"].compute_state == "potentially_active"
    assert resources["vm"].compute_state == "not_runtime"
    assert not report.capacity_release_allowed and not report.settlement_verified
    assert "SECRET" not in report.model_dump_json()


@pytest.mark.parametrize(
    "kind", ["Pod", "VirtualMachineInstance", "PersistentVolumeClaim", "DataVolume"]
)
def test_read_failure_is_incomplete_and_does_not_export_exception(kind):
    reader = Reader()
    reader.rows[kind] = RuntimeError("credential SECRET")
    report = observe(reader)
    assert not report.inventory_complete
    assert "SECRET" not in report.model_dump_json()
    assert next(c for c in report.collections if c.kind == kind).status == "unavailable"


def test_namespace_replacement_invalidates_whole_observation():
    reader = Reader()
    reader.namespace = lambda name: {"metadata": {"uid": "replaced"}}
    report = observe(reader)
    assert not report.inventory_complete
    assert report.blockers == ["namespace_identity_mismatch"]
    assert not report.collections


def test_replacement_after_inventory_retains_evidence_but_invalidates_identity():
    reader = Reader()
    reader.namespace = lambda name: {
        "metadata": {"uid": "ns" if not reader.calls else "new"}
    }
    original = reader.workloads

    def listing(kind, namespace):
        reader.calls += 1
        return original(kind, namespace)

    reader.workloads = listing
    report = observe(reader)
    assert report.collections
    assert not report.inventory_complete
    assert "namespace_identity_mismatch" in report.blockers


@pytest.mark.parametrize(
    "change", ["duplicate", "missing_uid", "namespace", "continuation", "missing_items"]
)
def test_untrustworthy_list_cannot_claim_complete(change):
    reader = Reader()
    payload = {"metadata": {"resourceVersion": "42"}, "items": [row("x")]}
    if change == "duplicate":
        payload["items"].append(row("x"))
    elif change == "missing_uid":
        del payload["items"][0]["metadata"]["uid"]
    elif change == "namespace":
        payload["items"][0]["metadata"]["namespace"] = "other"
    elif change == "continuation":
        payload["metadata"]["continue"] = "next"
    else:
        del payload["items"]
    reader.workloads = lambda *args: payload
    report = observe(reader)
    assert not report.inventory_complete
    assert all(c.status == "invalid_output" for c in report.collections)


def test_terminating_runtime_and_retained_storage_are_preserved():
    reader = Reader()
    pod, pvc = row("pod", status={"phase": "Running"}), row(
        "disk", status={"phase": "Bound"}
    )
    for item in (pod, pvc):
        item["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    reader.rows = {"Pod": [pod], "PersistentVolumeClaim": [pvc]}
    report = observe(reader)
    resources = {r.name: r for c in report.collections for r in c.resources}
    assert resources["pod"].deleting and resources["disk"].deleting
    assert resources["pod"].compute_state == "potentially_active"
    assert resources["disk"].compute_state == "not_runtime"
    assert not report.capacity_release_allowed


def test_generation_required_and_changed_generation_rejected():
    from app.services.native_workload_observations import observe_workloads

    report = observe_workloads(
        Reader(),
        ROOT.model_copy(update={"deployment_generation": None}),
        collector_id="test",
    )
    assert not report.inventory_complete
    reader = Reader()
    reader.projects = lambda ns: [
        {
            "metadata": {"uid": "cr", "generation": 3},
            "spec": {"projectId": str(ROOT.project_id)},
        }
    ]
    assert observe(reader).blockers == ["project_generation_mismatch"]


def test_object_bound_discards_overflow_without_partial_complete_rows():
    from app.services.native_workload_observations import observe_workloads

    reader = Reader()
    reader.rows["Pod"] = [row("one"), row("two")]
    report = observe_workloads(reader, ROOT, collector_id="test", max_objects=1)
    pods = next(c for c in report.collections if c.kind == "Pod")
    assert pods.status == "invalid_output" and not pods.resources


def test_bounded_reader_uses_only_get_without_selectors_and_closes_response():
    import io
    import json
    from types import SimpleNamespace

    from app.services.native_workload_observations import WorkloadReader

    reader = object.__new__(WorkloadReader)
    reader.scan_deadline = None
    reader.max_objects = 7
    reader.max_response_bytes = 1024
    response = io.BytesIO(
        json.dumps({"metadata": {"resourceVersion": "42"}, "items": []}).encode()
    )

    def request(path, method, **kwargs):
        assert path == "/api/v1/namespaces/project/pods"
        assert method == "GET"
        assert kwargs["query_params"] == [("limit", 8)]
        assert kwargs["_preload_content"] is False
        return response

    reader.api = SimpleNamespace(call_api=request)
    assert reader.workloads("Pod", "project")["items"] == []
    assert response.closed


def test_cli_writes_private_report_and_sanitizes_credential_failure(
    tmp_path, monkeypatch, capsys
):
    import json
    import runpy

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "observe_kubevirt_workloads.py"
    )
    module = runpy.run_path(str(script))
    manifest = tmp_path / "root.json"
    output = tmp_path / "report.json"
    manifest.write_text(
        json.dumps({"collector_id": "test", "root": ROOT.model_dump(mode="json")})
    )

    def unavailable(*args, **kwargs):
        raise ValueError("SECRET credential invalid")

    module["main"].__globals__["WorkloadReader"] = unavailable
    status = module["main"](
        ["--root", str(manifest), "--kubeconfig", "/missing", "--output", str(output)]
    )
    assert status == 2
    report = json.loads(output.read_text())
    assert report["blockers"] == ["reader_unavailable"]
    assert not report["inventory_complete"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert "SECRET" not in output.read_text() + capsys.readouterr().err


@pytest.mark.parametrize("payload", [b"x" * 1025, b"{bad json", b"[]"])
def test_reader_rejects_oversize_or_invalid_response_and_closes_it(payload):
    import io
    from types import SimpleNamespace

    from app.services.native_workload_observations import WorkloadReader

    reader = object.__new__(WorkloadReader)
    reader.scan_deadline, reader.max_objects, reader.max_response_bytes = None, 10, 1024
    response = io.BytesIO(payload)
    reader.api = SimpleNamespace(call_api=lambda *args, **kwargs: response)
    with pytest.raises(ValueError):
        reader.workloads("Pod", "project")
    assert response.closed


def test_expired_scan_never_dispatches_another_http_request():
    from types import SimpleNamespace

    from app.services.native_workload_observations import WorkloadReader

    reader = object.__new__(WorkloadReader)
    reader.scan_deadline, reader.max_objects = -1, 10

    def unexpected(*args, **kwargs):
        pytest.fail("expired scan made request")

    reader.api = SimpleNamespace(call_api=unexpected)
    with pytest.raises(TimeoutError):
        reader.workloads("Pod", "project")


def test_exec_plugin_credentials_are_rejected_without_execution(tmp_path):
    from app.services.native_workload_observations import WorkloadReader

    config = tmp_path / "config"
    config.write_text(
        "users:\n- name: user\n  user:\n    exec:\n      command: never-run\n"
    )
    config.chmod(0o600)
    with pytest.raises(ValueError, match="static operator kubeconfig"):
        WorkloadReader(str(config), None, collector_id="test", cache_roots=[])


@pytest.mark.parametrize("kind", ["VirtualMachineInstance", "Pod"])
def test_deleting_terminal_resource_still_cannot_be_treated_as_inactive(kind):
    reader = Reader()
    item = row("terminating", status={"phase": "Succeeded"})
    item["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    reader.rows[kind] = [item]
    report = observe(reader)
    resource = next(c for c in report.collections if c.kind == kind).resources[0]
    assert resource.compute_state == "potentially_active"


def test_empty_inventory_is_never_a_release_or_absence_proof():
    report = observe(Reader())
    assert report.inventory_complete
    assert not report.atomic_snapshot
    assert not report.storage_absence_verified
    assert not report.admitted_plan_verified
    assert not report.capacity_release_allowed
    assert report.started_at == NOW and report.finished_at == NOW
    assert all(c.list_resource_version == "42" for c in report.collections)


@pytest.mark.parametrize(
    "case", ["no_root", "duplicate_root", "wrong_project", "wrong_cluster"]
)
def test_root_failure_never_turns_into_empty_complete_inventory(case):
    reader = Reader()
    roots = reader.projects("project")
    if case == "no_root":
        roots = []
    elif case == "duplicate_root":
        roots *= 2
    elif case == "wrong_project":
        roots[0]["spec"]["projectId"] = "00000000-ffff-4000-8000-000000000001"
    else:
        reader.cluster_identity = lambda: "replacement"
    reader.projects = lambda ns: roots
    report = observe(reader)
    assert not report.inventory_complete and not report.collections


def test_cli_success_uses_same_collector_and_preserves_input_file(tmp_path):
    import json
    import runpy
    from types import SimpleNamespace

    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "observe_kubevirt_workloads.py"
        )
    )
    manifest, output = tmp_path / "root.json", tmp_path / "report.json"
    manifest.write_text(
        json.dumps({"collector_id": "test", "root": ROOT.model_dump(mode="json")})
    )
    original = manifest.read_bytes()
    reader = Reader()
    reader.api = SimpleNamespace(close=lambda: None)
    module["main"].__globals__["WorkloadReader"] = lambda *a, **k: reader
    assert (
        module["main"](
            [
                "--root",
                str(manifest),
                "--kubeconfig",
                "/explicit",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["inventory_complete"] is True
    assert manifest.read_bytes() == original


def test_cli_cannot_overwrite_input_or_kubeconfig(tmp_path):
    import json
    import runpy

    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "observe_kubevirt_workloads.py"
        )
    )
    manifest, config = tmp_path / "root.json", tmp_path / "config"
    manifest.write_text(
        json.dumps({"collector_id": "test", "root": ROOT.model_dump(mode="json")})
    )
    config.write_text("PRIVATE-CREDENTIAL")
    original = manifest.read_bytes()
    for output in (manifest, config):
        assert (
            module["main"](
                [
                    "--root",
                    str(manifest),
                    "--kubeconfig",
                    str(config),
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
    assert (
        manifest.read_bytes() == original and config.read_text() == "PRIVATE-CREDENTIAL"
    )
