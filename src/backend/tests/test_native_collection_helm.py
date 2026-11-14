"""Render the production worker contract without contacting a cluster."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def test_collection_profile_mounts_and_capacity(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[3] / "deploy" / "helm"
    result = subprocess.run(
        [
            "helm",
            "template",
            "troshka",
            str(chart),
            "--set",
            "deploy=true",
            "--set",
            "postgres.deploy=true",
            "--set",
            "nativeCollection.worker.enabled=true",
            "--set",
            "nativeCollection.worker.configMapName=collector",
            "--set",
            "nativeCollection.worker.secretName=transport",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    objects = {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }
    rules = objects["Role", "troshka-self-update"]["rules"]
    native_names = {"troshka-collection-worker", "troshka-readiness-worker"}
    native_rules = [r for r in rules if native_names & set(r.get("resourceNames", []))]
    assert len(native_rules) == 1
    assert native_rules[0] == {
        "apiGroups": ["apps"],
        "resources": ["deployments"],
        "resourceNames": ["troshka-collection-worker", "troshka-readiness-worker"],
        "verbs": ["get"],
    }
    patch_names = {
        name
        for rule in rules
        if "patch" in rule["verbs"]
        for name in rule.get("resourceNames", [])
    }
    assert patch_names == {
        "troshka-backend",
        "troshka-frontend",
        "troshka-worker",
        "troshka-tunnel",
    }
    lifecycle = objects["Deployment", "troshka-worker"]
    collection = objects["Deployment", "troshka-collection-worker"]
    assert lifecycle["spec"]["replicas"] == 100
    assert collection["spec"]["replicas"] == 1
    pod = collection["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["collection-transport"]["secret"]["secretName"] == "transport"
    assert pod["initContainers"][0]["command"][:2] == ["python3", "-c"]
    assert volumes["protected-files"]["emptyDir"]["medium"] == "Memory"
    script = pod["initContainers"][0]["command"][2]
    for name in ("config-source", "transport-source", "protected"):
        directory = tmp_path / name
        directory.mkdir()
        script = script.replace("/" + name, str(directory))
    (tmp_path / "config-source" / "pins.json").write_text("{}")
    (tmp_path / "transport-source" / "kubeconfig").write_text("fake test credentials")
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
    output = tmp_path / "protected" / "transport" / "kubeconfig"
    assert output.stat().st_mode & 0o777 == 0o400
    # A retry must replace already-created output and clean interrupted temps.
    abandoned = output.parent / ".copy-abandoned"
    abandoned.write_text("partial")
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
    assert not abandoned.exists()
    assert output.read_text() == "fake test credentials"
    assert all(
        v["name"] != "collection-transport"
        for v in lifecycle["spec"]["template"]["spec"]["volumes"]
    )
    env = {v["name"]: v.get("value") for v in pod["containers"][0]["env"]}
    assert env["TROSHKA_WORKER_PROFILE"] == "collection"
    encryption = next(
        v
        for v in pod["containers"][0]["env"]
        if v["name"] == "TROSHKA_AUTH__ENCRYPTION_KEY"
    )
    assert encryption["valueFrom"]["secretKeyRef"]["key"] == "encryption-key"
    postgres = objects["StatefulSet", "troshka-postgres"]
    pg_env = {
        v["name"]: v.get("value")
        for v in postgres["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert int(pg_env["POSTGRESQL_MAX_CONNECTIONS"]) >= 222


@pytest.mark.parametrize(
    "image,valid",
    [
        ("registry.example/cli:v4.20.8", True),
        ("registry.example/cli:latest", False),
        ("registry.example/cli@sha256:abcd", False),
        ("registry.example/cli", False),
    ],
)
def test_optional_collection_cli_image(image, valid):
    if shutil.which("helm") is None:
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[3] / "deploy" / "helm"
    command = [
        "helm",
        "template",
        "troshka",
        str(chart),
        "--set",
        "deploy=true",
        "--set",
        "nativeCollection.worker.enabled=true",
        "--set",
        "nativeCollection.worker.configMapName=collector",
        "--set",
        "nativeCollection.worker.secretName=transport",
        "--set",
        "nativeCollection.worker.cliImage=" + image,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if not valid:
        assert result.returncode != 0
        assert "explicit version tag" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    objects = [d for d in yaml.safe_load_all(result.stdout) if d]
    deployment = next(
        d for d in objects if d["metadata"]["name"] == "troshka-collection-worker"
    )
    spec = deployment["spec"]["template"]["spec"]
    init = spec["initContainers"][0]
    assert init["image"] == image
    assert [v["name"] for v in init["volumeMounts"]] == ["collection-bin"]
    assert any(
        v["name"] == "collection-bin" and v["readOnly"]
        for v in spec["containers"][0]["volumeMounts"]
    )


def test_readiness_reserves_one_worker_and_preserves_two_lifecycle_workers():
    if shutil.which("helm") is None:
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[3] / "deploy" / "helm"
    command = [
        "helm",
        "template",
        "troshka",
        str(chart),
        "-f",
        str(chart / "values-ocpvdev01.yaml"),
        "--set",
        "deploy=true",
        "--set",
        "nativeCollection.worker.enabled=true",
        "--set",
        "nativeCollection.readinessWorker.enabled=true",
        "--set",
        "nativeCollection.worker.configMapName=collector",
        "--set",
        "nativeCollection.worker.secretName=transport",
        "--set",
        "nativeCollection.worker.cliImage=registry.example/cli:v4.20.8",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=20
    )
    objects = {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }
    assert objects["Deployment", "troshka-worker"]["spec"]["replicas"] == 2
    for name, profile in [
        ("troshka-collection-worker", "collection"),
        ("troshka-readiness-worker", "readiness"),
    ]:
        deployment = objects["Deployment", name]
        assert deployment["spec"]["replicas"] == 1
        pod = deployment["spec"]["template"]["spec"]
        env = {v["name"]: v.get("value") for v in pod["containers"][0]["env"]}
        assert env["TROSHKA_WORKER_PROFILE"] == profile
        assert pod["initContainers"][0]["image"] == "registry.example/cli:v4.20.8"
        assert any(
            v.get("secret", {}).get("secretName") == "transport" for v in pod["volumes"]
        )
    config = yaml.safe_load(
        objects["ConfigMap", "troshka-config"]["data"]["config.yaml"]
    )
    assert config["native_collection"]["enabled"] is False
    assert config["native_collection"]["readiness_interval_seconds"] == 20
    postgres = objects["StatefulSet", "troshka-postgres"]
    env = {
        v["name"]: v.get("value")
        for v in postgres["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    with_readiness = int(env["POSTGRESQL_MAX_CONNECTIONS"])
    baseline = subprocess.run(
        command + ["--set", "nativeCollection.readinessWorker.enabled=false"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    baseline_postgres = next(
        d
        for d in yaml.safe_load_all(baseline.stdout)
        if d
        and d["kind"] == "StatefulSet"
        and d["metadata"]["name"] == "troshka-postgres"
    )
    baseline_env = {
        v["name"]: v.get("value")
        for v in baseline_postgres["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert with_readiness == int(baseline_env["POSTGRESQL_MAX_CONNECTIONS"]) + 2
