"""Deployment checks must reject partial and mixed-image rollouts."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "deploy_release.py"


@pytest.fixture
def release():
    assert PATH.exists(), "release verification helper is missing"
    spec = importlib.util.spec_from_file_location("deploy_release", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployment():
    return {
        "metadata": {"name": "workers", "namespace": "troshka", "generation": 2},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "worker"}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "worker",
                            "image": "quay.io/redhat-gpte/troshka-backend:production",
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 2,
            "updatedReplicas": 2,
            "availableReplicas": 2,
        },
    }


def pod(name, digest="sha256:new", ready=True):
    return {
        "metadata": {"name": name, "namespace": "troshka", "labels": {"app": "worker"}},
        "status": {
            "containerStatuses": [
                {
                    "name": "worker",
                    "ready": ready,
                    "imageID": "quay.io/redhat-gpte/troshka-backend@" + digest,
                }
            ]
        },
    }


def test_checks_every_replica_not_just_first(release):
    with pytest.raises(RuntimeError, match="digest"):
        release.verify_deployment(
            deployment(),
            [pod("one"), pod("two", "sha256:old")],
            {"backend": "sha256:new"},
        )


def test_missing_replica_is_not_success(release):
    with pytest.raises(RuntimeError, match="replica"):
        release.verify_deployment(deployment(), [pod("one")], {"backend": "sha256:new"})


def test_unready_container_is_not_success(release):
    with pytest.raises(RuntimeError, match="ready"):
        release.verify_deployment(
            deployment(),
            [pod("one"), pod("two", ready=False)],
            {"backend": "sha256:new"},
        )


def test_terminating_old_pods_do_not_fail_completed_rollout(release):
    old = pod("old", "sha256:old")
    old["metadata"]["deletionTimestamp"] = "2026-10-27T00:00:00Z"
    release.verify_deployment(
        deployment(), [pod("one"), pod("two"), old], {"backend": "sha256:new"}
    )


def test_unobserved_generation_is_not_success(release):
    dep = deployment()
    dep["status"]["observedGeneration"] = 1
    with pytest.raises(RuntimeError, match="generation"):
        release.verify_deployment(
            dep, [pod("one"), pod("two")], {"backend": "sha256:new"}
        )


def test_image_roles_do_not_confuse_vnc_with_other_repositories(release):
    assert (
        release.image_role(
            "quay.io/redhat-gpte/troshka-vnc-proxy:production@sha256:abc"
        )
        == "vnc-proxy"
    )
    assert release.image_role("quay.io/other/troshka-backend:production") is None


def test_exec_keeps_kubernetes_flags_out_of_remote_command(release, monkeypatch):
    commands = []
    monkeypatch.setattr(release, "run", lambda args, timeout: commands.append(args))
    release.oc(
        "/tmp/cluster", "exec", "deployment/backend", "--", "python3", "-c", "print(1)"
    )
    command = commands[0]
    assert command[command.index("--") + 1 :] == ["python3", "-c", "print(1)"]


def test_promote_preflights_all_images_before_first_copy(release, monkeypatch):
    copies = []

    def run(args, **kwargs):
        if args[:2] == ["skopeo", "copy"]:
            copies.append(args)
        if "troshka-terminal:vtest" in " ".join(args):
            raise RuntimeError("missing terminal release image")
        return "sha256:abc"

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(RuntimeError, match="missing terminal"):
        release.promote("vtest")
    assert copies == []


def test_promote_includes_terminal_and_ops_pod_and_verifies_destination(
    release, monkeypatch
):
    copied = []

    def run(args, **kwargs):
        if args[:2] == ["skopeo", "copy"]:
            copied.append(args[-1])
        return "sha256:abc"

    monkeypatch.setattr(release, "run", run)
    release.promote("vtest")
    assert "docker://quay.io/redhat-gpte/troshka-terminal:production" in copied
    assert "docker://quay.io/redhat-gpte/troshka-ops-pod:production" in copied
    assert len(copied) == 10


def test_promotion_digest_mismatch_fails(release, monkeypatch):
    def run(args, **kwargs):
        return "sha256:bad" if args[-1].endswith(":production") else "sha256:good"

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(RuntimeError, match="digest"):
        release.promote("vtest")


@pytest.fixture
def shell_commands(tmp_path):
    """Replace external CLIs; execute the real shell orchestration."""
    log = tmp_path / "commands.jsonl"
    fake = tmp_path / "fake"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\\n")
if name == "git" and args[0] == "rev-parse":
    print("candidate")
if name == "gh" and args[:2] == ["run", "list"]:
    print("42")
if name == "gh" and args[:2] == ["run", "view"]:
    print(os.environ.get("CI_CONCLUSION", "success"))
if name == "python3" and "cluster" in args and os.environ.get("FAIL_CLUSTER"):
    sys.exit(1)
"""
    )
    fake.chmod(0o755)
    for name in ("git", "gh", "oc", "python3"):
        (tmp_path / name).symlink_to(fake)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
    }
    return env, log


def test_full_deploy_pushes_main_and_release_then_checks_tagged_build(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [
        "git",
        "push",
        "--atomic",
        "origin",
        "HEAD:main",
        "refs/tags/vtest",
    ] in calls
    query = next(c for c in calls if c[:3] == ["gh", "run", "list"])
    assert query[query.index("--branch") + 1] == "vtest"
    assert any(c[0] == "python3" and "promote" in c and "vtest" in c for c in calls)
    assert len([c for c in calls if c[0] == "python3" and "cluster" in c]) == 7


def test_failed_ci_never_promotes(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "CI_CONCLUSION": "failure"},
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("promote" in c for c in calls)


def test_failed_cluster_is_not_reported_as_success(shell_commands):
    env, _ = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "FAIL_CLUSTER": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cluster rollouts failed" in result.stderr
