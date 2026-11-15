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


@pytest.mark.parametrize("kind", ["queued", "started", "deferred", "scheduled"])
def test_idle_rejects_every_unfinished_job_registry(release, monkeypatch, kind):
    queue = {"queue": "default", "queued": 0, "started": 0, "deferred": 0, "scheduled": 0}
    queue[kind] = 1
    monkeypatch.setattr(release, "oc", lambda *args: json.dumps([queue]))
    with pytest.raises(RuntimeError, match="reconcile"):
        release.idle("/tmp/cluster")


def test_idle_reads_registries_without_cleanup(release, monkeypatch, capsys):
    from types import ModuleType, SimpleNamespace

    connection = object()
    visited = []

    class Registry:
        def __init__(self, name, connection):
            self.name = name

        @property
        def count(self):
            pytest.fail("Registry.count can cleanup expired jobs")

        def get_job_ids(self, *, cleanup):
            assert cleanup is False
            visited.append(self.name)
            return []

    modules = {name: ModuleType(name) for name in ("app", "app.core", "app.core.redis", "rq", "rq.registry")}
    modules["app.core.redis"].get_redis_raw = lambda: connection
    modules["rq"].Queue = SimpleNamespace(all=lambda **kwargs: [SimpleNamespace(name="default", count=0)])
    for name in ("StartedJobRegistry", "DeferredJobRegistry", "ScheduledJobRegistry"):
        setattr(modules["rq.registry"], name, Registry)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    def execute_remote_code(*args):
        exec(args[-1], {})  # noqa: S102 -- exercise the generated local code with fake modules
        return capsys.readouterr().out

    monkeypatch.setattr(release, "oc", execute_remote_code)
    release.idle("/tmp/cluster")
    assert visited == ["default", "default", "default"]
    assert "idle" in capsys.readouterr().out


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


def test_stage_dev_resolves_both_images_before_copy(release, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["skopeo", "inspect"] and args[-1].endswith("frontend:vtest"):
            raise RuntimeError("missing frontend")
        return "sha256:abc"

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(RuntimeError, match="missing frontend"):
        release.stage_dev_images("vtest")
    assert not any(c[:2] == ["skopeo", "copy"] for c in calls)


def test_stage_dev_copies_immutable_sources_and_checks_latest(release, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return "sha256:abc"

    monkeypatch.setattr(release, "run", run)
    release.stage_dev_images("vtest")
    copies = [c for c in calls if c[:2] == ["skopeo", "copy"]]
    assert len(copies) == 2
    assert all(c[-2].endswith("@sha256:abc") for c in copies)
    assert {c[-1] for c in copies} == {
        f"docker://{release.REGISTRY}{role}:latest" for role in ("backend", "frontend")
    }
    assert len([c for c in calls if c[:2] == ["skopeo", "inspect"] and c[-1].endswith(":latest")]) == 2


def test_stage_dev_rejects_destination_digest_mismatch(release, monkeypatch):
    monkeypatch.setattr(release, "run", lambda args, **kw: "sha256:bad" if args[-1].endswith(":latest") else "sha256:good")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        release.stage_dev_images("vtest")


def test_pin_application_changes_only_git_revision_with_concurrency_guard(release, monkeypatch):
    app = {"metadata": {"resourceVersion": "123"}, "spec": {
        "destination": {"namespace": "troshka"},
        "source": {"path": "deploy/helm", "targetRevision": "vold", "helm": {"parameters": [{"name": "worker.replicas", "value": "100"}]}},
    }}
    calls = []

    def oc(kc, *args, **kwargs):
        calls.append(args)
        return json.dumps(app)

    monkeypatch.setattr(release, "oc", oc)
    release.pin_application_release("kc", "troshka", "vtest")
    patch_call = calls[-1]
    patch = json.loads(patch_call[patch_call.index("-p") + 1])
    assert patch == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "123"},
        {"op": "replace", "path": "/spec/source/targetRevision", "value": "vtest"},
    ]


@pytest.mark.parametrize("namespace,path", [("other", "deploy/helm"), ("troshka", "other")])
def test_pin_application_rejects_wrong_target(release, monkeypatch, namespace, path):
    calls = []

    def oc(kc, *args, **kwargs):
        calls.append(args)
        return json.dumps({"spec": {"destination": {"namespace": namespace}, "source": {"path": path}}})

    monkeypatch.setattr(release, "oc", oc)
    with pytest.raises(RuntimeError, match="target"):
        release.pin_application_release("kc", "troshka", "vtest")
    assert len(calls) == 1


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
    print("43" if "main" in args else "42")
if name == "gh" and args[:2] == ["run", "view"]:
    key = "MAIN_CI_CONCLUSION" if args[2] == "43" else "CI_CONCLUSION"
    print(os.environ.get(key, "success"))
if name == "python3" and "cluster" in args and os.environ.get("FAIL_CLUSTER"):
    sys.exit(1)
if name == "python3" and args[0] == "scripts/collector_release_check.py" and os.environ.get("FAIL_DEV_ACCOUNTING"):
    sys.exit(1)
if name == "python3" and args[0] == "scripts/collector_fleet_preflight.py" and "troshka" in args and os.environ.get("FAIL_FLEET"):
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
    assert len([c for c in calls if c[0] == "python3" and "cluster" in c]) == 14
    promote = next(i for i, c in enumerate(calls) if c[0] == "python3" and "promote" in c)
    dev_image = next(i for i, c in enumerate(calls) if "application" in c and "latest" in c)
    dev_accounting = next(i for i, c in enumerate(calls) if "scripts/collector_release_check.py" in c)
    assert dev_image < dev_accounting < promote
    hosts = [i for i, c in enumerate(calls) if "cluster" in c and "--image-tag" in c and "vtest" in c]
    fleet = next(i for i, c in enumerate(calls) if "scripts/collector_fleet_preflight.py" in c and "troshka" in c)
    assert len(hosts) == 7
    assert dev_accounting < min(hosts) <= max(hosts) < fleet < promote
    assert all("--skip-project-pods" in calls[i] for i in hosts)


def test_full_deploy_aligns_dev_after_main_build_and_pins_prod_git(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    main_build = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "run", "list"] and "main" in c)
    stage = next(i for i, c in enumerate(calls) if "stage-dev" in c)
    dev = next(i for i, c in enumerate(calls) if "application" in c and "latest" in c)
    promote = next(i for i, c in enumerate(calls) if "promote" in c)
    pin = next(i for i, c in enumerate(calls) if "pin-application" in c)
    prod = next(i for i, c in enumerate(calls) if "application" in c and "latest" not in c)
    assert main_build < stage < dev < promote < pin < prod
    assert calls[pin][calls[pin].index("--tag") + 1] == "vtest"
    assert any(c[:3] == ["gh", "run", "watch"] for c in calls[main_build:stage])


def test_failed_fleet_check_allows_host_upgrade_but_never_promotes(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "FAIL_FLEET": "1"}, capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([c for c in calls if "cluster" in c]) == 7
    assert not any("promote" in c for c in calls)


def test_failed_dev_accounting_never_promotes(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "FAIL_DEV_ACCOUNTING": "1"}, capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("scripts/collector_release_check.py" in c for c in calls)
    assert not any("promote" in c for c in calls)


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


def test_failed_main_build_never_aligns_dev_or_promotes(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "MAIN_CI_CONCLUSION": "failure"},
        capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("stage-dev" in c or "promote" in c for c in calls)


def test_failed_cluster_is_not_reported_as_success(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={**env, "FAIL_CLUSTER": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cluster rollouts failed" in result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("promote" in c for c in calls)


def test_operator_upgrade_uses_release_tag_before_promotion(release, monkeypatch):
    calls = []
    checks = iter([False, True])
    def verify(*args):
        if not next(checks):
            raise RuntimeError("stale image")
    monkeypatch.setattr(release, "verify_live", verify)
    monkeypatch.setattr(release, "wait_rollout", lambda *args: None)
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args))
    dep = {"metadata": {"namespace": "troshka-operator", "name": "troshka-operator"},
           "spec": {"template": {"spec": {"containers": [
               {"name": "operator", "image": release.REGISTRY + "operator:production"}
           ]}}}}
    release.update_deployment("config", dep, {"operator": "digest"}, "vtest")
    change = next(c for c in calls if "set" in c)
    assert change[-1] == "operator=" + release.REGISTRY + "operator:vtest"
