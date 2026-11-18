"""Protected native runtime files reach every app process that reads custody."""

from itertools import product
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
APP_PODS = ("backend", "worker", "tunnel")
NATIVE_PODS = ("collection-worker", "readiness-worker")
CLI_IMAGE = "registry.example/cli:v4.20.8"


def _helm(*overrides, check=True):
    command = ["helm", "template", "runtime-test", str(ROOT / "deploy/helm")]
    for override in overrides:
        command.extend(["--set", override])
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=20)


def _objects(result):
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout) if doc
    }


def _pod(objects, name):
    return objects["Deployment", f"troshka-{name}"]["spec"]["template"]["spec"]


def _assert_runtime(pod, *, config="protected-config", transport="protected-transport", cli=CLI_IMAGE):
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["collection-config"]["configMap"]["name"] == config
    assert volumes["collection-transport"]["secret"]["secretName"] == transport
    assert volumes["protected-files"]["emptyDir"] == {"medium": "Memory"}
    container = pod["containers"][0]
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts["/opt/app-root/src/config/config.yaml"]["readOnly"] is True
    for path, subpath in [
        ("/etc/troshka/collection", "config"),
        ("/etc/troshka/collection-transport", "transport"),
    ]:
        assert mounts[path] == {
            "name": "protected-files", "mountPath": path,
            "subPath": subpath, "readOnly": True,
        }
    assert not {"collection-config", "collection-transport"} & {
        mount["name"] for mount in container["volumeMounts"]
    }
    init = {item["name"]: item for item in pod["initContainers"]}
    copier = init["prepare-collection-config"]
    assert copier["command"][:2] == ["python3", "-c"]
    source_mounts = {mount["mountPath"]: mount for mount in copier["volumeMounts"]}
    assert source_mounts["/config-source"]["name"] == "collection-config"
    assert source_mounts["/transport-source"]["name"] == "collection-transport"
    assert source_mounts["/config-source"]["readOnly"] is True
    assert source_mounts["/transport-source"]["readOnly"] is True
    assert source_mounts["/protected"]["name"] == "protected-files"
    env = {item["name"]: item.get("value") for item in container["env"]}
    if cli:
        assert init["prepare-collection-cli"]["image"] == cli
        assert init["prepare-collection-cli"]["volumeMounts"] == [
            {"name": "collection-bin", "mountPath": "/collection-bin"},
        ]
        assert mounts["/opt/collection-bin"]["readOnly"] is True
        assert env["PATH"].split(":")[0] == "/opt/collection-bin"
    else:
        assert "prepare-collection-cli" not in init
        assert "collection-bin" not in volumes
        assert "PATH" not in env


@pytest.mark.parametrize("enabled,capture,quotas", list(product((False, True), repeat=3)))
def test_runtime_wiring_follows_collection_enablement_independent_of_capture_and_quotas(enabled, capture, quotas):
    result = _helm(
        f"nativeCollection.enabled={str(enabled).lower()}",
        f"nativeOrigin.captureEnabled={str(capture).lower()}",
        f"quotas.enabled={str(quotas).lower()}",
        "nativeCollection.worker.configMapName=protected-config",
        "nativeCollection.worker.secretName=protected-transport",
        f"nativeCollection.worker.cliImage={CLI_IMAGE}",
    )
    objects = _objects(result)
    for name in APP_PODS:
        pod = _pod(objects, name)
        assert pod["serviceAccountName"] == "troshka-backend"
        assert "automountServiceAccountToken" not in pod
        if enabled:
            _assert_runtime(pod)
        else:
            assert "initContainers" not in pod
            assert [v["name"] for v in pod["volumes"]] == ["config"]
    assert ("Deployment", "troshka-collection-worker") not in objects
    assert ("Deployment", "troshka-readiness-worker") not in objects
    assert objects["Deployment", "troshka-worker"]["spec"]["replicas"] == 100
    assert "TROSHKA_WORKER_PROFILE" not in {
        env["name"] for env in _pod(objects, "worker")["containers"][0]["env"]
    }
    assert {name for kind, name in objects if kind == "Secret"} == {"troshka-secrets"}
    assert ("ConfigMap", "protected-config") not in objects


def test_capture_alone_and_default_install_do_not_require_native_secret_references():
    for overrides in [(), ("nativeOrigin.captureEnabled=true",)]:
        objects = _objects(_helm(*overrides))
        for name in APP_PODS:
            assert "initContainers" not in _pod(objects, name)


@pytest.mark.parametrize("missing", ["configMapName", "secretName"])
def test_enabled_runtime_requires_existing_protected_references(missing):
    options = {
        "configMapName": "protected-config", "secretName": "protected-transport",  # pragma: allowlist secret -- fixture resource name
    }
    options[missing] = ""
    result = _helm(
        "nativeCollection.enabled=true",
        *(f"nativeCollection.worker.{key}={value}" for key, value in options.items()),
        check=False,
    )
    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize("image", ["registry.example/cli:latest", "registry.example/cli", "registry.example/cli@sha256:abcd"])
def test_app_runtime_rejects_unversioned_cli_without_dedicated_workers(image):
    result = _helm(
        "nativeCollection.enabled=true",
        "nativeCollection.worker.configMapName=protected-config",
        "nativeCollection.worker.secretName=protected-transport",
        f"nativeCollection.worker.cliImage={image}", check=False,
    )
    assert result.returncode != 0
    assert "explicit version tag" in result.stderr


def test_dedicated_workers_preserve_inherited_and_overridden_runtime_settings():
    objects = _objects(_helm(
        "nativeCollection.enabled=true",
        "nativeCollection.worker.enabled=true",
        "nativeCollection.readinessWorker.enabled=true",
        "nativeCollection.worker.configMapName=protected-config",
        "nativeCollection.worker.secretName=protected-transport",
        f"nativeCollection.worker.cliImage={CLI_IMAGE}",
        "nativeCollection.readinessWorker.configMapName=readiness-config",
        "nativeCollection.readinessWorker.secretName=readiness-transport",
        "nativeCollection.readinessWorker.cliImage=registry.example/cli:v4.19.9",
    ))
    for name in APP_PODS + ("collection-worker",):
        _assert_runtime(_pod(objects, name))
    _assert_runtime(_pod(objects, "readiness-worker"), config="readiness-config",
                    transport="readiness-transport", cli="registry.example/cli:v4.19.9")
    for name, profile in [("collection-worker", "collection"), ("readiness-worker", "readiness")]:
        pod = _pod(objects, name)
        assert pod["automountServiceAccountToken"] is False
        assert "serviceAccountName" not in pod
        env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
        assert env["TROSHKA_WORKER_PROFILE"] == profile


def test_runtime_copy_produces_private_regular_files_from_projected_symlinks(tmp_path):
    objects = _objects(_helm(
        "nativeCollection.enabled=true",
        "nativeCollection.worker.enabled=true",
        "nativeCollection.readinessWorker.enabled=true",
        "nativeCollection.worker.configMapName=protected-config",
        "nativeCollection.worker.secretName=protected-transport",
    ))
    pod = _pod(objects, "backend")
    for name in APP_PODS + NATIVE_PODS:
        _assert_runtime(_pod(objects, name), cli="")
    script = pod["initContainers"][0]["command"][2]
    for directory in ("config-source", "transport-source", "protected"):
        target = tmp_path / directory
        target.mkdir()
        script = script.replace("/" + directory, str(target))
    for source, name, value in [
        ("config-source", "native-pins.json", '{"fixture":true}'),
        ("transport-source", "kubeconfig", "fake-private-test-token"),
    ]:
        directory = tmp_path / source
        (directory / "..data").mkdir()
        (directory / "..data" / name).write_text(value)
        (directory / name).symlink_to("..data/" + name)
    for attempt in range(2):
        if attempt:
            (tmp_path / "protected/transport/.copy-abandoned").write_text("partial")
        subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
        for subpath, name in [("config", "native-pins.json"), ("transport", "kubeconfig")]:
            directory = tmp_path / "protected" / subpath
            path = directory / name
            assert directory.stat().st_mode & 0o777 == 0o700
            assert path.stat().st_mode & 0o777 == 0o400
            assert path.is_file() and not path.is_symlink()
        assert (tmp_path / "protected/transport/kubeconfig").read_text() == "fake-private-test-token"
        assert not list((tmp_path / "protected/transport").glob(".copy-*"))
