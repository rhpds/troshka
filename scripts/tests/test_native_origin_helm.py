"""Native origin capture uses the shared Helm config without enabling quotas."""

from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _render(*overrides):
    command = [
        "helm", "template", "capture-test", str(ROOT / "deploy/helm"),
        "--set", "nativeCollection.worker.enabled=true",
        "--set", "nativeCollection.readinessWorker.enabled=true",
        "--set", "nativeCollection.worker.configMapName=protected-config",
        "--set", "nativeCollection.worker.secretName=protected-transport",
    ]
    for override in overrides:
        command.extend(["--set", override])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(result.stdout) if document
    }


def _config(rendered):
    return yaml.safe_load(rendered[("ConfigMap", "troshka-config")]["data"]["config.yaml"])


@pytest.fixture(scope="module")
def rendered_charts():
    charts = {"default": _render()}
    for capture, quotas in [(False, False), (True, False), (False, True), (True, True)]:
        charts[capture, quotas] = _render(
            f"nativeOrigin.captureEnabled={str(capture).lower()}",
            f"quotas.enabled={str(quotas).lower()}",
        )
    return charts


def test_native_origin_capture_defaults_to_boolean_false(rendered_charts):
    config = _config(rendered_charts["default"])
    assert config["native_origin"]["capture_enabled"] is False
    assert config["quotas"]["enabled"] is False


@pytest.mark.parametrize("capture,quotas", [(False, False), (True, False), (False, True), (True, True)])
def test_native_origin_capture_and_quotas_are_independent_booleans(rendered_charts, capture, quotas):
    config = _config(rendered_charts[capture, quotas])
    assert config["native_origin"]["capture_enabled"] is capture
    assert config["quotas"]["enabled"] is quotas
    assert config["native_collection"]["profiles"] == []
    assert "origin_admission" not in config["native_origin"]


def test_capture_change_reaches_shared_config_consumers_and_changes_rollout_checksum(rendered_charts):
    for name in ["backend", "worker", "tunnel", "collection-worker", "readiness-worker"]:
        checksums = []
        for chart in [rendered_charts["default"], rendered_charts[True, False]]:
            template = chart["Deployment", f"troshka-{name}"]["spec"]["template"]
            checksums.append(template["metadata"]["annotations"]["checksum/config"])
            spec = template["spec"]
            config_volume = next(v for v in spec["volumes"] if v["name"] == "config")
            assert config_volume["configMap"]["name"] == "troshka-config"
            for container in spec["containers"]:
                mount = next(m for m in container["volumeMounts"] if m["name"] == "config")
                assert mount["mountPath"] == "/opt/app-root/src/config/config.yaml"
                assert mount["subPath"] == "config.yaml"
                assert not any(
                    env["name"] == "TROSHKA_NATIVE_ORIGIN__CAPTURE_ENABLED"
                    for env in container.get("env", [])
                )
        assert all(checksums)
        assert checksums[0] != checksums[1]
