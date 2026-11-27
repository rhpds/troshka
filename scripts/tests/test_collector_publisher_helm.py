"""Central publisher chart boundary: opt-in, persistent and narrowly scoped."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
NAME = "troshka-collector-maintenance"


def _render(*overrides):
    command = ["helm", "template", "publisher-test", str(ROOT / "deploy/helm")]
    for override in overrides:
        command.extend(["--set", override])
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    return [obj for obj in yaml.safe_load_all(result.stdout) if obj]


def _publisher(objects):
    return [obj for obj in objects if "collector-maintenance" in obj["metadata"]["name"]]


@pytest.mark.parametrize("overrides", [(), ("deploy=false", "collectorMaintenance.enabled=true")])
def test_disabled_publisher_has_no_resources(overrides):
    assert _publisher(_render(*overrides)) == []


def test_enabled_publisher_uses_backend_image_and_persistent_state_without_runtime_mounts():
    objects = _publisher(_render(
        "collectorMaintenance.enabled=true",
        "backend.image.tag=v2026.11.27",
        "nativeCollection.runtimeEnabled=true",
        "nativeCollection.worker.configMapName=protected-config",
        "nativeCollection.worker.secretName=protected-secret",
    ))
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == NAME
    assert pod["automountServiceAccountToken"] is True
    assert "initContainers" not in pod
    assert "runAsUser" not in pod["securityContext"]
    assert "fsGroup" not in pod["securityContext"]
    container, = pod["containers"]
    assert container["image"] == "quay.io/redhat-gpte/troshka-backend:v2026.11.27"
    assert container["command"] == ["python3", "scripts/collector_controller.py"]
    assert container["args"] == [
        "--plan", "/etc/troshka-maintenance/plan.json", "--state-dir",
        "/var/lib/troshka-collector-maintenance", "--interval", "300",
    ]
    assert "ports" not in container
    assert "readinessProbe" not in container
    env = {item["name"]: item for item in container["env"]}
    for name, key in [("TROSHKA_DATABASE__URL", "database-url"),
                      ("TROSHKA_AUTH__ENCRYPTION_KEY", "encryption-key")]:
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "troshka-secrets", "key": key}
    assert env["COLLECTOR_MAINTENANCE_STATUS_CONFIGMAP"]["value"] == NAME + "-status"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert set(volumes) == {"plan", "config", "state"}
    assert volumes["plan"]["configMap"] == {
        "name": NAME, "items": [{"key": "plan.json", "path": "plan.json"}],
    }
    assert volumes["config"]["configMap"]["name"] == "troshka-config"
    assert volumes["state"]["persistentVolumeClaim"]["claimName"] == NAME + "-state"
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["config"]["subPath"] == "config.yaml"
    assert mounts["config"]["readOnly"] is True
    assert mounts["plan"]["readOnly"] is True
    assert not mounts["state"].get("readOnly", False)
    pvc = next(obj for obj in objects if obj["kind"] == "PersistentVolumeClaim")
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert "storageClassName" not in pvc["spec"]
    status, = [obj for obj in objects if obj["kind"] == "ConfigMap"]
    assert status["metadata"]["name"] == NAME + "-status"
    assert "data" not in status


def test_permissions_are_namespaced_and_cannot_issue_tokens_or_list_secrets():
    rendered = _render(
        "collectorMaintenance.enabled=true", "namespace.name=troshka-test",
        "collectorMaintenance.application=troshka-test-app",
    )
    assert all(obj["metadata"].get("namespace") != "openshift-gitops" for obj in rendered)
    objects = _publisher(rendered)
    assert all(obj["metadata"]["namespace"] == "troshka-test" for obj in objects)
    assert not {"ClusterRole", "ClusterRoleBinding"} & {obj["kind"] for obj in objects}
    roles = {obj["metadata"]["namespace"]: obj for obj in objects if obj["kind"] == "Role"}
    assert set(roles) == {"troshka-test"}
    assert roles["troshka-test"]["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps", "secrets"], "verbs": ["get", "create"]},
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [NAME + "-status"],
         "verbs": ["patch", "update"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
    ]
    for binding in [obj for obj in objects if obj["kind"] == "RoleBinding"]:
        assert binding["subjects"] == [{
            "kind": "ServiceAccount", "name": NAME, "namespace": "troshka-test",
        }]


@pytest.mark.parametrize("storage_class,expected", [("fast", "fast"), ("-", "")])
def test_storage_class_override(storage_class, expected):
    objects = _publisher(_render(
        "collectorMaintenance.enabled=true",
        f"collectorMaintenance.state.storageClass={storage_class}",
    ))
    pvc = next(obj for obj in objects if obj["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == expected
