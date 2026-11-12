"""Synthetic admission requests: no credentials or cluster discovery."""

import ast
import json
import uuid
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from admission import models
from admission.models import ValidationError, load_enrollments, parse_enrollments
from admission.quarantine import review
from helpers.kubevirt import build_kubevirt_vm, build_recert_job


def uid(number=1):
    return str(uuid.UUID(int=number))


def bundle():
    return {
        "schema_version": 1,
        "enrollments": [
            {
                "mode": "quarantine",
                "cluster_key": uid(1),
                "enrollment_id": uid(2),
                "namespace_name": "isolated-project",
                "namespace_uid": uid(3),
                "project_id": uid(4),
                "project_cr_name": "project-one",
                "project_cr_uid": uid(5),
            }
        ],
    }


def obj(group="apps", version="v1", kind="Deployment"):
    return {
        "apiVersion": f"{group}/{version}" if group else version,
        "kind": kind,
        "metadata": {
            "name": "workload",
            "namespace": "isolated-project",
            "uid": uid(6),
            "resourceVersion": "10",
            "generation": 10,
            "finalizers": ["example.com/cleanup"],
        },
        "spec": {
            "replicas": 2,
            "template": {"spec": {"containers": [{"image": "safe:v1"}]}},
        },
    }


def envelope(
    old=None,
    new=None,
    operation="UPDATE",
    group="apps",
    resource="deployments",
    subresource="",
    kind=None,
):
    old = deepcopy(old or obj())
    new = deepcopy(new if new is not None else old)
    kind = kind or {"group": group, "version": "v1", "kind": old["kind"]}
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": uid(7),
            "operation": operation,
            "namespace": "isolated-project",
            "name": old["metadata"]["name"],
            "resource": {"group": group, "version": "v1", "resource": resource},
            "kind": kind,
            "subResource": subresource,
            "object": None if operation == "DELETE" else new,
            "oldObject": old if operation in {"UPDATE", "DELETE"} else None,
            "userInfo": {"username": "synthetic"},
        },
    }


def allowed(document):
    return review(document, parse_enrollments(bundle()))["response"]["allowed"]


def test_enrollment_is_immutable_and_loadable(tmp_path):
    path = tmp_path / "enrollment.json"
    path.write_text(json.dumps(bundle()))
    entries = load_enrollments(path)
    assert entries == parse_enrollments(bundle())
    with pytest.raises(FrozenInstanceError):
        entries[0].mode = "growth"


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.update(extra=True),
        lambda b: b.update(schema_version=True),
        lambda b: b.update(enrollments=[]),
        lambda b: b.update(enrollments=b["enrollments"] * 33),
        lambda b: b["enrollments"][0].update(mode="grant"),
        lambda b: b["enrollments"][0].update(namespace_name="UPPER"),
        lambda b: b["enrollments"][0].update(project_id="00000000"),
        lambda b: b["enrollments"][0].pop("namespace_uid"),
        lambda b: b["enrollments"][0].update(extra="ignored?"),
        lambda b: b["enrollments"].append(
            dict(b["enrollments"][0], enrollment_id=uid(9))
        ),
    ],
)
def test_enrollment_rejects_invalid_and_conflicting_records(change):
    value = bundle()
    change(value)
    with pytest.raises(ValidationError):
        parse_enrollments(value)


@pytest.mark.parametrize("operation", ["CREATE", "CONNECT"])
def test_growth_denied_for_any_identity(operation):
    document = envelope(operation=operation)
    document["request"]["userInfo"] = {
        "username": "admin",
        "groups": ["system:masters"],
    }
    assert not allowed(document)


@pytest.mark.parametrize(
    "resource,kind",
    [
        ("deployments", "Deployment"),
        ("statefulsets", "StatefulSet"),
        ("replicasets", "ReplicaSet"),
    ],
)
@pytest.mark.parametrize("scale", [False, True])
def test_replica_cleanup_only(resource, kind, scale):
    old = obj(kind=kind)
    subresource = ""
    request_kind = None
    if scale:
        old = obj("autoscaling", kind="Scale")
        old["spec"] = {"replicas": 2}
        old["status"] = {"replicas": 2, "selector": "app=workload"}
        subresource = "scale"
        request_kind = {"group": "autoscaling", "version": "v1", "kind": "Scale"}
    new = deepcopy(old)
    new["spec"]["replicas"] = 0
    new["metadata"]["resourceVersion"] = "11"
    new["metadata"]["managedFields"] = []
    if not scale:
        new["metadata"]["generation"] = 11
    assert allowed(
        envelope(
            old, new, resource=resource, subresource=subresource, kind=request_kind
        )
    )
    new["spec"]["hiddenGrowth"] = True
    assert not allowed(
        envelope(
            old, new, resource=resource, subresource=subresource, kind=request_kind
        )
    )


def test_actual_operator_vm_and_job_cleanup():
    vm = build_kubevirt_vm(
        {
            "metadata": {"name": "vm", "namespace": "isolated-project"},
            "spec": {"cpus": 2, "memory": 1024},
        },
        {},
        {},
        None,
    )
    vm["metadata"]["uid"] = uid(8)
    vm["metadata"]["generation"] = 10
    halted = deepcopy(vm)
    halted["metadata"]["generation"] = 11
    halted["spec"]["runStrategy"] = "Halted"
    assert allowed(
        envelope(vm, halted, group="kubevirt.io", resource="virtualmachines")
    )
    halted["spec"]["template"]["spec"]["volumes"].append({"name": "growth"})
    assert not allowed(
        envelope(vm, halted, group="kubevirt.io", resource="virtualmachines")
    )
    job = build_recert_job("recert", "isolated-project", "disk")
    job["metadata"]["uid"] = uid(9)
    job["metadata"]["generation"] = 10
    suspended = deepcopy(job)
    suspended["metadata"]["generation"] = 11
    suspended["spec"]["suspend"] = True
    assert allowed(envelope(job, suspended, group="batch", resource="jobs"))


@pytest.mark.parametrize("strategy", ["running", "runStrategy"])
def test_vm_power_mode_exclusive(strategy):
    old = obj("kubevirt.io", kind="VirtualMachine")
    old["spec"] = {strategy: True if strategy == "running" else "Always"}
    new = deepcopy(old)
    new["spec"][strategy] = False if strategy == "running" else "Halted"
    new["metadata"]["generation"] = 11
    assert allowed(envelope(old, new, group="kubevirt.io", resource="virtualmachines"))
    new["spec"]["runStrategy" if strategy == "running" else "running"] = (
        "Halted" if strategy == "running" else False
    )
    assert not allowed(
        envelope(old, new, group="kubevirt.io", resource="virtualmachines")
    )


ROOT_CLEANUPS = [
    ("apps", "deployments", "Deployment", "replicas", 2, 0),
    ("apps", "statefulsets", "StatefulSet", "replicas", 2, 0),
    ("apps", "replicasets", "ReplicaSet", "replicas", 2, 0),
    ("batch", "jobs", "Job", "suspend", False, True),
    ("kubevirt.io", "virtualmachines", "VirtualMachine", "running", True, False),
    (
        "kubevirt.io",
        "virtualmachines",
        "VirtualMachine",
        "runStrategy",
        "Always",
        "Halted",
    ),
]


def root_cleanup(case):
    group, resource, kind, field, before, after = case
    old = obj(group, kind=kind)
    old["spec"] = {field: before, "template": {"image": "safe:v1"}}
    new = deepcopy(old)
    new["spec"][field] = after
    new["metadata"].update(generation=11, resourceVersion="11")
    return envelope(old, new, group=group, resource=resource)


@pytest.mark.parametrize("case", ROOT_CLEANUPS)
def test_root_cleanup_generation_increment_and_noop(case):
    document = root_cleanup(case)
    assert allowed(document)
    document["request"]["oldObject"] = deepcopy(document["request"]["object"])
    assert allowed(document)  # Already stopped: no generation increment.
    document["request"]["object"]["metadata"]["generation"] += 1
    assert not allowed(document)


@pytest.mark.parametrize("case", ROOT_CLEANUPS)
@pytest.mark.parametrize(
    "before,after",
    [
        (10, 10),
        (10, 9),
        (10, 12),
        (10, None),
        (None, 11),
        (None, None),
        (0, 1),
        (-1, 0),
        (True, 2),
        (1, True),
        (10, "11"),
        ("10", 11),
        (2**63 - 1, 2**63),
    ],
)
def test_root_cleanup_invalid_generation_denied(case, before, after):
    document = root_cleanup(case)
    for key, generation in (("oldObject", before), ("object", after)):
        metadata = document["request"][key]["metadata"]
        if generation is None:
            metadata.pop("generation")
        else:
            metadata["generation"] = generation
    assert not allowed(document)


@pytest.mark.parametrize("case", ROOT_CLEANUPS)
@pytest.mark.parametrize(
    "mutation",
    [
        lambda n: n["metadata"].update(labels={"bypass": "yes"}),
        lambda n: n["metadata"].update(annotations={"bypass": "yes"}),
        lambda n: n["metadata"].update(ownerReferences=[]),
        lambda n: n["metadata"].update(unknown="value"),
        lambda n: n["metadata"].update(finalizers=[]),
        lambda n: n["spec"]["template"].update(image="growth:v2"),
        lambda n: n["spec"].update(unknown=True),
        lambda n: n.update(unknown=True),
        lambda n: n.update(status={"ready": True}),
    ],
)
def test_each_root_cleanup_rejects_mixed_mutations(case, mutation):
    document = root_cleanup(case)
    mutation(document["request"]["object"])
    assert not allowed(document)


@pytest.mark.parametrize("cleanup", ["status", "scale", "finalizers"])
@pytest.mark.parametrize(
    "mutation",
    [
        lambda n: n["metadata"].update(generation=11),
        lambda n: n["metadata"].update(labels={"bypass": "yes"}),
        lambda n: n["metadata"].update(annotations={"bypass": "yes"}),
        lambda n: n["metadata"].update(ownerReferences=[]),
        lambda n: n["metadata"].update(unknown="value"),
        lambda n: n.update(unknown=True),
        lambda n: n["spec"].update(unknown=True),
    ],
)
def test_nonroot_cleanup_keeps_generation_and_other_fields(cleanup, mutation):
    old = obj("autoscaling", kind="Scale") if cleanup == "scale" else obj()
    if cleanup == "finalizers":
        old["metadata"]["deletionTimestamp"] = "2026-11-12T00:00:00Z"
    new = deepcopy(old)
    if cleanup == "finalizers":
        new["metadata"]["finalizers"] = []
    elif cleanup == "status":
        new["status"] = {"replicas": 0}
    else:
        new["spec"]["replicas"] = 0
    document = envelope(
        old,
        new,
        subresource="" if cleanup == "finalizers" else cleanup,
        kind=(
            {"group": "autoscaling", "version": "v1", "kind": "Scale"}
            if cleanup == "scale"
            else None
        ),
    )
    assert allowed(document)
    mutation(document["request"]["object"])
    assert not allowed(document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda n: n["metadata"].update(labels={"bypass": "yes"}),
        lambda n: n["metadata"].update(annotations={"token": "secret"}),
        lambda n: n["metadata"].update(ownerReferences=[]),
        lambda n: n["metadata"].update(uid=uid(90)),
        lambda n: n["metadata"].update(name="spoofed"),
        lambda n: n["metadata"].update(namespace="other"),
        lambda n: n.update(status={"replicas": 0}),
        lambda n: n["spec"]["template"]["spec"]["containers"][0].update(image="bad:v2"),
        lambda n: n.update(unknown=True),
    ],
)
def test_cleanup_rejects_mixed_mutations(mutation):
    old = obj()
    new = deepcopy(old)
    new["spec"]["replicas"] = 0
    new["metadata"]["generation"] = 11
    mutation(new)
    assert not allowed(envelope(old, new))


def test_finalizers_removal_only():
    old = obj()
    old["metadata"]["deletionTimestamp"] = "2026-11-12T00:00:00Z"
    new = deepcopy(old)
    new["metadata"]["finalizers"] = []
    assert allowed(envelope(old, new))
    new["metadata"]["finalizers"] = ["attacker"]
    assert not allowed(envelope(old, new))
    new["metadata"]["finalizers"] = []
    new["spec"]["replicas"] = 0
    assert not allowed(envelope(old, new))  # one exact cleanup delta at a time


def test_finalizer_removal_requires_preexisting_deletion():
    old = obj()
    new = deepcopy(old)
    new["metadata"]["finalizers"] = []
    assert not allowed(envelope(old, new))


@pytest.mark.parametrize("field,value", [("replicas", True), ("replicas", -1)])
def test_invalid_old_replica_schema_cannot_authorize_cleanup(field, value):
    old = obj()
    old["spec"][field] = value
    new = deepcopy(old)
    new["spec"][field] = 0
    assert not allowed(envelope(old, new))


@pytest.mark.parametrize(
    "field,value", [("running", "yes"), ("runStrategy", "UnknownMode")]
)
def test_invalid_old_vm_schema_cannot_authorize_cleanup(field, value):
    old = obj("kubevirt.io", kind="VirtualMachine")
    old["spec"] = {field: value}
    new = deepcopy(old)
    new["spec"][field] = False if field == "running" else "Halted"
    assert not allowed(
        envelope(old, new, group="kubevirt.io", resource="virtualmachines")
    )


def test_builtin_status_only_and_unknown_status_denied():
    old = obj()
    new = deepcopy(old)
    new["status"] = {"replicas": 0}
    assert allowed(envelope(old, new, subresource="status"))
    new["metadata"]["labels"] = {"mutated": "true"}
    assert not allowed(envelope(old, new, subresource="status"))
    custom = obj("custom.example", kind="Deployment")
    assert not allowed(
        envelope(custom, custom, group="custom.example", subresource="status")
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(
            resource={"group": "apps", "version": "v2", "resource": "deployments"}
        ),
        lambda r: r.update(
            resource={
                "group": "apps",
                "version": "v1",
                "resource": "deployments",
                "extra": 1,
            }
        ),
        lambda r: r.update(
            kind={"group": "apps", "version": "v1", "kind": "StatefulSet"}
        ),
        lambda r: r.update(subResource="resize"),
        lambda r: r.update(namespace="unenrolled"),
        lambda r: r.update(operation="UPSERT"),
        lambda r: r.update(oldObject=None),
        lambda r: r["object"]["metadata"].pop("uid"),
        lambda r: r["object"]["spec"].update(replicas=False),
    ],
)
def test_malformed_request_or_identity_denied(change):
    document = envelope()
    document["request"]["object"]["spec"]["replicas"] = 0
    change(document["request"])
    assert not allowed(document)


def test_delete_defensive_allow_requires_valid_enrolled_envelope():
    assert allowed(envelope(operation="DELETE"))
    document = envelope(operation="DELETE")
    document["request"]["namespace"] = "other"
    assert not allowed(document)
    document = envelope(operation="DELETE")
    document["request"]["oldObject"]["metadata"]["name"] = "spoof"
    assert not allowed(document)


def test_invalid_envelope_with_uid_denies_without_echoing_data():
    document = envelope()
    document["apiVersion"] = "attacker-secret"
    result = review(document, parse_enrollments(bundle()))
    assert result["response"]["uid"] == uid(7)
    assert not result["response"]["allowed"]
    assert "attacker-secret" not in json.dumps(result)
    with pytest.raises(ValidationError):
        review({"request": {"uid": "bad"}}, parse_enrollments(bundle()))


@pytest.mark.parametrize("value", [None, [], "secret", 1, True])
def test_malformed_objects_and_resource_are_bounded_denials(value):
    for field in ("object", "oldObject", "resource", "kind"):
        document = envelope()
        document["request"][field] = value
        assert not allowed(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(namespaceUID=uid(3)),
        lambda r: r.update(dryRun="false"),
        lambda r: r.update(userInfo="administrator"),
        lambda r: r.update(
            requestResource={
                "group": "apps",
                "version": "v1beta1",
                "resource": "deployments",
            }
        ),
        lambda r: r.update(
            requestKind={"group": "apps", "version": "v1beta1", "kind": "Deployment"}
        ),
        lambda r: r.update(requestSubResource="scale"),
    ],
)
def test_request_schema_and_conversion_mismatch_denied(change):
    document = envelope()
    document["request"]["object"]["spec"]["replicas"] = 0
    change(document["request"])
    assert not allowed(document)


def test_unknown_create_connect_and_subresources_never_authorize():
    for operation in ("CREATE", "CONNECT", "UPDATE"):
        custom = obj("unknown.example", kind="Unknown")
        assert not allowed(
            envelope(custom, custom, operation, "unknown.example", "unknowns")
        )
    document = envelope()
    document["request"]["subResource"] = "start"
    document["request"]["resource"] = {
        "group": "subresources.kubevirt.io",
        "version": "v1",
        "resource": "virtualmachines",
    }
    assert not allowed(document)


def test_no_boolean_integer_equality_bypass_in_unchanged_fields():
    old = obj()
    old["spec"]["unknown"] = 0
    new = deepcopy(old)
    new["spec"].update(replicas=0, unknown=False)
    assert not allowed(envelope(old, new))


def test_enrollment_full_bundle_and_identity_conflicts():
    value = bundle()
    template = value["enrollments"][0]
    value["enrollments"] = [
        dict(
            template,
            namespace_name=f"project-{i}",
            project_cr_name=f"project-{i}",
            enrollment_id=uid(i * 10 + 10),
            namespace_uid=uid(i * 10 + 11),
            project_id=uid(i * 10 + 12),
            project_cr_uid=uid(i * 10 + 13),
        )
        for i in range(32)
    ]
    assert len(parse_enrollments(value)) == 32
    for field in (
        "enrollment_id",
        "namespace_name",
        "namespace_uid",
        "project_id",
        "project_cr_name",
        "project_cr_uid",
    ):
        bad = deepcopy(value)
        bad["enrollments"][1][field] = bad["enrollments"][0][field]
        with pytest.raises(ValidationError):
            parse_enrollments(bad)
    value["enrollments"][1]["cluster_key"] = uid(99)
    with pytest.raises(ValidationError):
        parse_enrollments(value)


def test_direct_review_limits_and_safe_reason_codes():
    for secret in ("x" * 65537, [0] * 20001, 2**63, 1.1):
        document = envelope()
        document["request"]["object"]["secret"] = secret
        response = review(document, parse_enrollments(bundle()))
        assert not response["response"]["allowed"]
        assert len(json.dumps(response)) < 300
    document = envelope()
    nested = document["request"]["object"]
    for _ in range(40):
        nested["next"] = {}
        nested = nested["next"]
    assert not allowed(document)


def test_direct_review_total_text_is_bounded_before_comparison():
    old = obj()
    old["metadata"]["annotations"] = {str(index): "x" * 65536 for index in range(17)}
    new = deepcopy(old)
    new["spec"]["replicas"] = 0
    assert not allowed(envelope(old, new))


def test_invalid_config_files_are_content_free(tmp_path):
    with pytest.raises(ValidationError, match="config_unavailable"):
        load_enrollments(tmp_path / "nonexistent-secret")
    path = tmp_path / "invalid.json"
    path.write_bytes(b'{"marker":"credential","marker":2}')
    with pytest.raises(ValidationError) as caught:
        load_enrollments(path)
    assert "credential" not in str(caught.value)


def test_admission_imports_cannot_reach_authority_or_ledger_modules():
    directory = Path(models.__file__).parent
    for path in directory.glob("*.py"):
        offline_imports = (
            {"base64", "hashlib", "bundle_policy"}
            if path.name in {"bundle.py", "bundle_policy.py"}
            else set()
        )
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert all(
                name.split(".")[0]
                in offline_imports
                | {
                    "dataclasses",
                    "json",
                    "pathlib",
                    "re",
                    "uuid",
                    "copy",
                    "argparse",
                    "asyncio",
                    "logging",
                    "ssl",
                    "aiohttp",
                    "models",
                    "quarantine",
                }
                for name in imports
            ), (path, imports)
