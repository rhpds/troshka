"""Offline bundle contracts; live CEL/API validation is a separate gate."""

import importlib.util
import json
import re
import socket
import ssl
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from admission.bundle import render_bundle
from admission.bundle_policy import control_policy
from admission.models import ValidationError, parse_enrollments

from tests.test_quota_quarantine import bundle, uid

ROLES = (
    "deployment",
    "replicaset",
    "scheduler",
    "pod_status",
    "endpoints",
    "endpointslice",
    "garbage_collection",
    "disruption",
)
TLS_NAME = "quarantine-tls"

# Complete persisted Kubernetes v1.30 ObjectMeta except the two bookkeeping fields.
# This is a structural/reference-model contract, not a CEL interpreter/API test.
STATUS_METADATA = {
    "name": "troshka-quarantine-a-b",
    "generateName": "troshka-quarantine-a-",
    "namespace": "quarantine-control",
    "uid": uid(60),
    "selfLink": "/legacy",
    "generation": 10,
    "creationTimestamp": "2026-11-12T00:00:00Z",
    "deletionTimestamp": "2026-11-12T01:00:00Z",
    "deletionGracePeriodSeconds": 30,
    "labels": {"app.kubernetes.io/name": "troshka-quarantine"},
    "annotations": {"example.com/note": "retained"},
    "ownerReferences": [
        {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "name": "troshka-quarantine-a",
            "uid": uid(61),
            "controller": True,
        }
    ],
    "finalizers": ["example.com/cleanup"],
}


def status_contract():
    spec = control_policy(
        "quarantine-control",
        TLS_NAME,
        "config",
        "example/operator:v1",
        ["fixture-admin"],
        {role: ["fixture-" + role] for role in ROLES},
        ["fixture-node"],
    )[0]["spec"]
    variables = {item["name"]: item["expression"] for item in spec["variables"]}
    expression = variables["statusMetadataUnchanged"]
    # Recognize only the generated presence+equality grammar, then model it on
    # JSON fixtures. Never eval CEL as Python or claim actual CEL execution.
    pattern = (
        r"\(has\(object\.metadata\.(\w+)\) == has\(oldObject\.metadata\.\1\) && "
        r"\(!has\(object\.metadata\.\1\) \|\| "
        r"object\.metadata\.\1 == oldObject\.metadata\.\1\)\)"
    )
    matches = list(re.finditer(pattern, expression))
    assert " && ".join(match.group(0) for match in matches) == expression
    fields = [match.group(1) for match in matches]
    assert len(fields) == len(STATUS_METADATA) and set(fields) == set(STATUS_METADATA)
    validation = spec["validations"][0]["expression"]
    assert (
        validation.count(
            "variables.statusMetadataUnchanged && object.spec == oldObject.spec"
        )
        == 4
    )
    assert "object.metadata == oldObject.metadata" not in validation
    return fields


def status_reference_allows(fields, old, new):
    return (
        "spec" in old
        and "spec" in new
        and old["spec"] == new["spec"]
        and all(
            (field in old["metadata"]) == (field in new["metadata"])
            and old["metadata"].get(field) == new["metadata"].get(field)
            for field in fields
        )
    )


def status_pair(existing_fields):
    old = {
        "metadata": deepcopy(STATUS_METADATA),
        "spec": {"replicas": 2},
        "status": {"readyReplicas": 0},
    }
    manager = {
        "manager": "fixture-controller",
        "operation": "Update",
        "apiVersion": "v1",
        "subresource": "status",
        "fieldsType": "FieldsV1",
        "fieldsV1": {"f:status": {"f:conditions": {}}},
        "time": "2026-11-12T00:00:00Z",
    }
    if existing_fields:
        old["metadata"]["managedFields"] = [deepcopy(manager)]
    new = deepcopy(old)
    new["status"] = {"readyReplicas": 2}
    manager["time"] = "2026-11-12T00:01:00Z"
    manager["fieldsV1"]["f:status"]["f:readyReplicas"] = {}
    new["metadata"].update(resourceVersion="11", managedFields=[manager])
    return old, new


@pytest.mark.parametrize("existing_fields", [False, True])
def test_status_metadata_bookkeeping_contract(existing_fields):
    old, new = status_pair(existing_fields)
    fields = status_contract()
    assert status_reference_allows(fields, old, new)
    # Missing optional fields on both sides remain equal.
    old["metadata"] = {"uid": uid(60)}
    new["metadata"] = {"uid": uid(60), "managedFields": []}
    assert status_reference_allows(fields, old, new)


@pytest.mark.parametrize("field", list(STATUS_METADATA) + ["spec"])
@pytest.mark.parametrize("mutation", ["change", "remove", "add"])
def test_status_metadata_and_spec_hitchhiking_contract(field, mutation):
    old, new = status_pair(True)
    before, after = (
        (old, new) if field == "spec" else (old["metadata"], new["metadata"])
    )
    if mutation == "remove":
        after.pop(field)
    elif mutation == "add":
        before.pop(field)
    else:
        after[field] = "changed"
    fields = status_contract()
    assert not status_reference_allows(fields, old, new)


@pytest.fixture
def options(tmp_path):
    cert = tmp_path / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=synthetic-ca",
            "-keyout",
            str(tmp_path / "key.pem"),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return {
        "control_namespace": "quarantine-control",
        "control_namespace_uid": uid(30),
        "image": "quay.io/example/operator:v2026.11.12-1",
        "tls_secret": TLS_NAME,
        "ca_pem": cert.read_text(),
        "bootstrap_users": ["fixture-admin"],
        "scheduler_nodes": ["fixture-node"],
        "controller_users": {role: [f"fixture-{role}"] for role in ROLES},
    }


def render(options):
    return render_bundle(parse_enrollments(bundle()), **options)


def policies(result):
    return {
        item["metadata"]["name"]: item
        for item in result["00-protections"]
        if item["kind"] == "ValidatingAdmissionPolicy"
    }


def test_webhook_never_selects_by_mutable_label(options):
    hook = render(options)["20-activation"][0]["webhooks"][0]
    assert "namespaceSelector" not in hook and "objectSelector" not in hook
    assert hook["failurePolicy"] == "Fail"
    assert hook["rules"] == [
        {
            "operations": ["CREATE", "UPDATE", "CONNECT"],
            "apiGroups": ["*"],
            "apiVersions": ["*"],
            "resources": ["*/*"],
            "scope": "Namespaced",
        }
    ]
    assert hook["matchConditions"] == [
        {
            "name": "exact-enrollment",
            "expression": 'request.namespace in ["isolated-project"]',
        }
    ]
    assert hook["timeoutSeconds"] == 3 and hook["sideEffects"] == "None"


def test_namespace_lifetime_and_control_policy_structure(options):
    rules = policies(render(options))
    lifetime = rules["troshka-quarantine-lifetimes"]["spec"]
    text = json.dumps(lifetime)
    assert uid(3) in text and uid(30) in text
    assert 'request.operation != \\"CREATE\\"' in text
    assert "oldObject.metadata.uid" in text and "object.metadata.uid" in text
    assert "namespaces/*" in text
    assert 'request.operation != \\"DELETE\\"' in text
    control = rules["troshka-quarantine-control"]["spec"]
    assert control["failurePolicy"] == "Fail"
    assert control["matchConstraints"]["resourceRules"][0]["operations"] == ["*"]
    assert (
        control["matchConditions"][0]["expression"]
        == 'request.namespace == "quarantine-control"'
    )
    assert "namespaceSelector" not in json.dumps(rules)


def test_workload_is_tokenless_and_config_immutable(options):
    docs = render(options)["10-workload"]
    by_kind = {item["kind"]: item for item in docs}
    assert by_kind["ConfigMap"]["immutable"] is True
    assert by_kind["ServiceAccount"]["automountServiceAccountToken"] is False
    pod = by_kind["Deployment"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["command"] == ["python3", "-m", "admission.server"]
    assert container["image"] == options["image"]
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["limits"]
    assert not {
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
        "Secret",
        "Namespace",
    } & set(by_kind)
    config = json.loads(by_kind["ConfigMap"]["data"]["enrollments.json"])
    assert config == bundle()


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_namespace", "x' || true"),
        ("control_namespace", "isolated-project"),
        ("control_namespace_uid", "prefix"),
        ("tls_secret", "*"),
        ("image", ""),
        ("image", "operator:latest"),
        ("image", "operator:main"),
        ("image", "operator:production"),
        ("image", "operator@sha256:" + "a" * 64),
        ("image", "operator:*"),
        ("image", "operator"),
        ("image", "operator:stable"),
        ("image", "https://repo/image:v1"),
        ("image", "repo//image:v1"),
        ("image", "repo:abc/image:v1"),
        ("ca_pem", ""),
        ("ca_pem", "-----BEGIN CERTIFICATE-----\naA==\n-----END CERTIFICATE-----"),
        ("bootstrap_users", []),
        ("bootstrap_users", ["*"]),
        ("bootstrap_users", ["x' || true"]),
        ("bootstrap_users", ["system:anonymous"]),
        ("controller_users", {}),
        ("scheduler_nodes", []),
        ("scheduler_nodes", ["*"]),
        ("scheduler_nodes", ["fixture-node", "fixture-node"]),
    ],
)
def test_invalid_security_inputs_rejected(options, field, value):
    options[field] = value
    with pytest.raises(ValidationError):
        render(options)


def test_controller_identity_conflicts_and_unknown_roles(options):
    options["controller_users"]["deployment"] = options["bootstrap_users"]
    with pytest.raises(ValidationError):
        render(options)
    options["controller_users"]["deployment"] = ["fixture-controller"]
    options["controller_users"]["unknown"] = ["fixture-controller"]
    with pytest.raises(ValidationError):
        render(options)


def test_offline_deterministic_no_mutation(options, monkeypatch):
    source, original = bundle(), deepcopy(options)

    def forbidden(*args, **kwargs):
        pytest.fail("renderer attempted external action")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = render_bundle(parse_enrollments(source), **options)
    assert result == render(options)
    assert source == bundle() and options == original
    assert "quota_enabled" not in json.dumps(result)
    assert ssl.PEM_cert_to_DER_cert(options["ca_pem"])
    assert (
        list(yaml.safe_load_all(yaml.safe_dump_all(result["00-protections"])))
        == result["00-protections"]
    )


def cli_module():
    path = Path(__file__).resolve().parents[3] / "scripts/render_native_quarantine.py"
    spec = importlib.util.spec_from_file_location("render_native_quarantine", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_output_private_exclusive_and_content_manifest(options, tmp_path):
    import hashlib

    output = tmp_path / "output"
    cli_module().write_bundle(output, render(options))
    assert output.stat().st_mode & 0o777 == 0o700
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["files"]) == {
        "00-protections.yaml",
        "10-workload.yaml",
        "20-activation.yaml",
    }
    for name, digest in manifest["files"].items():
        path = output / name
        assert path.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert list(yaml.safe_load_all(path.read_text()))
    original = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(FileExistsError):
        cli_module().write_bundle(output, render(options))
    assert original == {path.name: path.read_bytes() for path in output.iterdir()}


def test_output_refuses_symlinks(options, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValidationError):
        cli_module().write_bundle(link / "output", render(options))
    assert list(real.iterdir()) == []


def test_cli_explicit_inputs_and_offline_render(options, tmp_path, monkeypatch):
    module = cli_module()
    with pytest.raises(SystemExit) as missing:
        module.main([])
    assert missing.value.code == 2
    inputs = {
        "enrollments": bundle(),
        "bootstrap-users-file": options["bootstrap_users"],
        "controller-users-file": options["controller_users"],
        "scheduler-nodes-file": options["scheduler_nodes"],
    }
    args = []
    for flag, content in inputs.items():
        path = tmp_path / (flag + ".json")
        path.write_text(json.dumps(content))
        args += ["--" + flag, str(path)]
    ca = tmp_path / "ca-input.pem"
    ca.write_text(options["ca_pem"])
    args += ["--ca-file", str(ca), "--output", str(tmp_path / "rendered")]
    for key in ("control_namespace", "control_namespace_uid", "image", "tls_secret"):
        args += ["--" + key.replace("_", "-"), options[key]]
    original = {path: path.read_bytes() for path in tmp_path.glob("*.json")}

    def forbidden(*args, **kwargs):
        pytest.fail("CLI attempted external action")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert module.main(args) == 0
    assert original == {path: path.read_bytes() for path in tmp_path.glob("*.json")}
    with pytest.raises(SystemExit) as duplicate:
        module.main(args)
    assert duplicate.value.code == 2


def test_named_roots_and_controller_bounds(options):
    spec = policies(render(options))["troshka-quarantine-control"]["spec"]
    text = spec["validations"][0]["expression"]
    assert 'request.subResource == ""' in text
    assert 'request.subResource == "binding"' in text
    assert 'request.subResource == "status"' in text
    assert 'target.target.name in ["fixture-node"]' in text
    assert "request.userInfo.groups" not in text
    assert '"serviceaccounts/token"' not in text and '"roles"' not in text
    assert '"rolebindings"' not in text and '"CONNECT"' not in text
    assert "object.spec == oldObject.spec" in text
    variables = {var["name"]: var["expression"] for var in spec["variables"]}
    assert variables["target"] == 'request.operation == "DELETE" ? oldObject : object'
    assert (
        "ownerReferences" in variables["pod"]
        and "automountServiceAccountToken" in variables["pod"]
    )
    assert "ownerReferences" in variables["replicaSet"]
    assert "ownerReferences" in variables["slice"]
    assert 'o.uid != ""' in variables["pod"]
    assert "has(variables.target.metadata.labels)" in variables["pod"]
    assert "hostPath" not in variables["pod"]  # Only named config/TLS volumes.
    assert "size(variables.target.spec.volumes) == 2" in variables["pod"]


def test_same_verified_controller_username_can_have_narrow_role_union(options):
    options["controller_users"]["replicaset"] = options["controller_users"][
        "deployment"
    ]
    assert render(options)


def test_ca_rejects_any_noncertificate_payload(options):
    options["ca_pem"] += "unrelated-input"
    with pytest.raises(ValidationError):
        render(options)


def test_renderer_orders_inventory_without_changing_source(options):
    source = bundle()
    second = deepcopy(source["enrollments"][0])
    second.update(
        enrollment_id=uid(41),
        namespace_name="aaa-project",
        namespace_uid=uid(42),
        project_id=uid(43),
        project_cr_name="aaa-root",
        project_cr_uid=uid(44),
    )
    source["enrollments"].append(second)
    first = render_bundle(parse_enrollments(source), **options)
    source["enrollments"].reverse()
    assert first == render_bundle(parse_enrollments(source), **options)


def test_binding_nodes_and_namespace_uid_are_explicit(options):
    result = render(options)
    deployment = next(
        item for item in result["10-workload"] if item["kind"] == "Deployment"
    )
    affinity = deployment["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
    fields = affinity["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchFields"]
    assert fields == [
        {"key": "metadata.name", "operator": "In", "values": ["fixture-node"]}
    ]
    options.pop("control_namespace_uid")
    with pytest.raises(TypeError):
        render(options)


def test_no_policy_self_protection_claim(options):
    resources = [
        resource
        for policy in policies(render(options)).values()
        for rule in policy["spec"]["matchConstraints"]["resourceRules"]
        for resource in rule["resources"]
    ]
    assert "validatingadmissionpolicies" not in resources
    assert "validatingadmissionpolicybindings" not in resources


def test_renderer_revalidates_enrollments(options):
    with pytest.raises(ValidationError):
        render_bundle([], **options)
    enrollment = parse_enrollments(bundle())[0]
    for field, value in (
        ("namespace_name", "*"),
        ("namespace_name", "x' || true"),
        ("mode", "allow"),
        ("namespace_uid", "prefix"),
    ):
        with pytest.raises(ValidationError):
            render_bundle([replace(enrollment, **{field: value})], **options)
