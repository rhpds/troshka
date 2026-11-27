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


def publisher_application():
    return {"metadata": {"name": "troshka-dev", "namespace": "openshift-gitops"},
            "spec": {"destination": {"namespace": "troshka-dev"}, "source": {"helm": {"valuesObject": {
                "collectorMaintenance": {"enabled": True, "application": "troshka-dev"}}}}}}


def test_publisher_grant_is_outside_chart_and_names_one_application(release, monkeypatch):
    created = []
    monkeypatch.setattr(release, "oc", lambda kc, verb, kind, *a, **kw: '{"items": []}' if kind == "rolebindings" else "")
    monkeypatch.setattr(release, "run", lambda args, **kw: created.append(json.loads(kw["stdin"])))
    release.ensure_publisher_argo_access("kc", publisher_application())
    assert created[0]["metadata"]["namespace"] == "openshift-gitops"
    assert created[0]["rules"] == [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                                    "resourceNames": ["troshka-dev"], "verbs": ["get", "patch"]}]
    assert created[1]["subjects"] == [{"kind": "ServiceAccount", "name": "troshka-collector-maintenance", "namespace": "troshka-dev"}]
    monkeypatch.setattr(release, "oc", lambda kc, verb, kind, *a, **kw: json.dumps(
        {"items": [created[1]]} if kind == "rolebindings" else next(d for d in created if d["kind"].lower() == kind)))
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("must not rewrite matching grants"))
    release.ensure_publisher_argo_access("kc", publisher_application())


def test_publisher_grant_refuses_drift_and_wrong_application(release, monkeypatch):
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("must not mutate unapproved scope"))
    monkeypatch.setattr(release, "oc", lambda *a, **kw: json.dumps({"rules": [{"verbs": ["*"]}]}))
    with pytest.raises(RuntimeError, match="explicit reconciliation"):
        release.ensure_publisher_argo_access("kc", publisher_application())
    app = publisher_application()
    app["spec"]["source"]["helm"]["valuesObject"]["collectorMaintenance"]["application"] = "other"
    with pytest.raises(RuntimeError, match="scope"):
        release.ensure_publisher_argo_access("kc", app)


def test_publisher_never_activates_a_stale_binding(release, monkeypatch):
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("must inspect all bindings before granting access"))
    monkeypatch.setattr(release, "oc", lambda kc, verb, kind, *a, **kw: "" if kind == "role" else json.dumps({"subjects": []}))
    with pytest.raises(RuntimeError, match="explicit reconciliation"):
        release.ensure_publisher_argo_access("kc", publisher_application())
    unexpected = {"metadata": {"name": "unexpected"}, "roleRef": {"kind": "Role", "name": "troshka-dev-collector-maintenance"}}
    monkeypatch.setattr(release, "oc", lambda kc, verb, kind, *a, **kw: json.dumps({"items": [unexpected]}) if kind == "rolebindings" else "")
    with pytest.raises(RuntimeError, match="Additional"):
        release.ensure_publisher_argo_access("kc", publisher_application())


def test_publisher_honors_parameter_precedence(release, monkeypatch):
    app = publisher_application()
    helm = app["spec"]["source"]["helm"]
    helm["parameters"] = [{"name": "collectorMaintenance.enabled", "value": "false"}]
    monkeypatch.setattr(release, "oc", lambda *a, **kw: pytest.fail("disabled publisher requires no grants"))
    release.ensure_publisher_argo_access("kc", app)
    helm["parameters"] = [{"name": "collectorMaintenance.enabled", "value": "true"},
                          {"name": "collectorMaintenance.application", "value": "other"}]
    with pytest.raises(RuntimeError, match="scope"):
        release.ensure_publisher_argo_access("kc", app)
    helm["parameters"][0]["forceString"] = True
    with pytest.raises(RuntimeError, match="boolean"):
        release.ensure_publisher_argo_access("kc", app)


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


@pytest.mark.parametrize("enabled", [False, True])
def test_application_verifies_enabled_collector_publisher(release, monkeypatch, enabled):
    checked = []
    schema = (PATH.parents[1] / "src/backend/schemas/troshka-template.schema.json").read_text()

    def oc(_kc, *args):
        if args[0] == "get":
            return "deployment/troshka-collector-maintenance" if enabled and args[2] == "troshka-collector-maintenance" else ""
        if "load_template_schema" in args[-1]:
            return schema
        return "200"

    monkeypatch.setattr(release, "oc", oc)
    monkeypatch.setattr(release, "run", lambda *a, **kw: None)
    monkeypatch.setattr(release, "wait_rollout", lambda *a: None)
    monkeypatch.setattr(release, "verify_live", lambda kc, ns, name, expected: checked.append(name))
    monkeypatch.setattr(release, "ensure_argo_ready", lambda *a: None)
    release.application("kc", {"backend": "sha256:expected"}, roles=["backend"])
    assert ("troshka-collector-maintenance" in checked) is enabled


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
        # Production starts stale; after copy, verification must see the new digest.
        if args[:2] == ["skopeo", "inspect"] and str(args[-1]).endswith(":production"):
            return "sha256:abc" if str(args[-1]) in copied else "sha256:old"
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
    aligned = set()

    def run(args, **kwargs):
        calls.append(args)
        # Per-role: latest starts stale, then matches after that role is copied.
        if args[:2] == ["skopeo", "copy"]:
            dest = str(args[-1])
            for role in ("backend", "frontend"):
                if dest.endswith(f"{role}:latest"):
                    aligned.add(role)
            return "sha256:abc"
        if args[:2] == ["skopeo", "inspect"] and str(args[-1]).endswith(":latest"):
            role = next(
                (r for r in ("backend", "frontend") if f"{r}:latest" in str(args[-1])),
                None,
            )
            return "sha256:abc" if role in aligned else "sha256:old"
        return "sha256:abc"

    monkeypatch.setattr(release, "run", run)
    release.stage_dev_images("vtest")
    copies = [c for c in calls if c[:2] == ["skopeo", "copy"]]
    assert len(copies) == 2
    assert all(c[-2].endswith("@sha256:abc") for c in copies)
    assert {c[-1] for c in copies} == {
        f"docker://{release.REGISTRY}{role}:latest" for role in ("backend", "frontend")
    }
    assert len([c for c in calls if c[:2] == ["skopeo", "inspect"] and c[-1].endswith(":latest")]) >= 2


def test_stage_dev_rejects_destination_digest_mismatch(release, monkeypatch):
    monkeypatch.setattr(release, "run", lambda args, **kw: "sha256:bad" if args[-1].endswith(":latest") else "sha256:good")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        release.stage_dev_images("vtest")


def test_pin_application_preserves_settings_and_overrides_existing_tag_parameters(release, monkeypatch):
    app = {"metadata": {"resourceVersion": "123"}, "spec": {
        "destination": {"namespace": "troshka"},
        "source": {"repoURL": "https://github.com/rhpds/troshka.git", "path": "deploy/helm", "targetRevision": "vtest",
                   "helm": {"valuesObject": {"secret": "SENTINEL_VALUES", "backend": {"image": {"tag": "latest"}}}, "parameters": [  # pragma: allowlist secret
                       {"name": "secret.override", "value": "SENTINEL_PARAMETER"},
                       {"name": "backend.image.tag", "value": "vold", "forceString": True},
                       {"name": "frontend.image.tag", "value": "vold"}]}},
    }}
    calls = []

    def oc(kc, *args, **kwargs):
        calls.append(args)
        return json.dumps(app)

    monkeypatch.setattr(release, "oc", oc)
    resolved = []
    monkeypatch.setattr(release, "digest", lambda role, tag: resolved.append((role, tag)) or "sha256:new")
    release.pin_application_release("kc", "troshka", "vtest", ["backend"])
    patch_call = calls[-1]
    patch = json.loads(patch_call[patch_call.index("-p") + 1])
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "123"}
    assert patch[1:] == [{"op": "add", "path": "/spec/source/helm/parameters/1/value", "value": "vtest@sha256:new"}]
    assert "SENTINEL" not in json.dumps(patch)
    assert resolved == [("backend", "vtest")]


def test_updater_annotations_pin_only_selected_image(release):
    key = "argocd-image-updater.argoproj.io/image-list"
    app = {"metadata": {"annotations": {key: f"backend={release.REGISTRY}backend:vold, frontend={release.REGISTRY}frontend:vold"}}}
    patch = release.updater_annotation_patch(app, "vnew", ["backend"])
    assert patch[0]["path"].endswith("argoproj.io~1image-list")
    assert patch[0]["value"] == f"backend={release.REGISTRY}backend:vnew, frontend={release.REGISTRY}frontend:vold"


def test_updater_resource_preserves_other_apps_and_rejects_shared_targets(release):
    image = {"alias": "backend", "imageName": release.REGISTRY + "backend:vold",
             "manifestTargets": {"helm": {"name": "backend.image.repository", "tag": "backend.image.tag"}}}
    updater = {"spec": {"applicationRefs": [{"namePattern": "other", "images": [image]},
                                           {"namePattern": "troshka-dev", "images": [image]}]}}
    patch = release.updater_resource_patch(updater, "troshka-dev", "vnew", ["backend"])
    assert patch == [{"op": "replace", "path": "/spec/applicationRefs/1/images/0/imageName", "value": release.REGISTRY + "backend:vnew"}]
    updater["spec"]["applicationRefs"][1]["namePattern"] = "troshka*"
    with pytest.raises(RuntimeError, match="Shared"):
        release.updater_resource_patch(updater, "troshka-dev", "vnew", ["backend"])
    updater["spec"]["applicationRefs"][1]["useAnnotations"] = True
    assert release.updater_resource_patch(updater, "troshka-dev", "vnew", ["backend"]) == []


def test_updater_rejects_unexpected_image_repository(release):
    updater = {"spec": {"applicationRefs": [{"namePattern": "troshka", "images": [
        {"alias": "backend", "imageName": "other/backend:vold"}]}]}}
    with pytest.raises(RuntimeError, match="Unexpected"):
        release.updater_resource_patch(updater, "troshka", "vnew", ["backend"])


@pytest.mark.parametrize("mutation", ["uid", "repoURL", "path", "namespace"])
def test_pin_revalidates_target_after_updater_changes(release, monkeypatch, mutation):
    import copy
    app = {"metadata": {"uid": "original", "resourceVersion": "1"}, "spec": {
        "destination": {"namespace": "troshka"}, "source": {
            "repoURL": "https://github.com/rhpds/troshka.git", "path": "deploy/helm"}}}
    changed = copy.deepcopy(app)
    if mutation == "uid":
        changed["metadata"][mutation] = "replacement"
    elif mutation == "namespace":
        changed["spec"]["destination"][mutation] = "other"
    else:
        changed["spec"]["source"][mutation] = "other"
    reads = iter([app, changed])
    def oc(kc, *args):
        assert args[0] == "get", "must not patch changed target"
        return json.dumps(next(reads))
    monkeypatch.setattr(release, "oc", oc)
    with pytest.raises(RuntimeError, match="target|replaced"):
        release.pin_application_release("kc", "troshka", "vnew", [])


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


@pytest.mark.parametrize("roles", [None, []])
def test_pin_creates_missing_helm_parameters_and_honors_empty_roles(release, monkeypatch, roles):
    app = {"metadata": {"resourceVersion": "9"}, "spec": {"destination": {"namespace": "troshka-dev"},
        "source": {"repoURL": "https://github.com/rhpds/troshka.git", "path": "deploy/helm", "targetRevision": "main"}}}
    calls = []
    monkeypatch.setattr(release, "oc", lambda kc, *args: calls.append(args) or json.dumps(app))
    resolved = []
    monkeypatch.setattr(release, "digest", lambda role, tag: resolved.append(role) or "sha256:new")
    release.pin_application_release("dev", "troshka-dev", "vtest", roles)
    patch = json.loads(calls[-1][-1])
    assert patch[1] == {"op": "add", "path": "/spec/source/targetRevision", "value": "vtest"}
    assert resolved == (["backend", "frontend"] if roles is None else [])
    assert any(p["path"] == "/spec/source/helm" for p in patch) is (roles is None)


def test_pin_rejects_wrong_repository_before_resolving_images(release, monkeypatch):
    monkeypatch.setattr(release, "oc", lambda *a: json.dumps({"spec": {
        "destination": {"namespace": "troshka"}, "source": {"path": "deploy/helm", "repoURL": "https://other"}}}))
    monkeypatch.setattr(release, "digest", lambda *a: pytest.fail("resolved unapproved target"))
    with pytest.raises(RuntimeError, match="target"):
        release.pin_application_release("kc", "troshka", "vtest")


def test_stage_dev_with_cluster_pins_application_without_latest_copy(release, monkeypatch):
    from types import SimpleNamespace
    args = SimpleNamespace(tag="vtest", roles="backend", action="stage-dev", kubeconfig="dev", namespace="troshka-dev")
    pinned = []
    monkeypatch.setattr(release, "pin_application_release", lambda *a: pinned.append(a))
    monkeypatch.setattr(release, "stage_dev_images", lambda *a: pytest.fail("latest mutation"))
    release.application_release_action(args, None)
    assert pinned == [("dev", "troshka-dev", "vtest", ["backend"])]


def test_pin_concurrency_conflict_is_not_retried(release, monkeypatch):
    app = {"metadata": {"resourceVersion": "9"}, "spec": {"destination": {"namespace": "troshka"},
        "source": {"repoURL": "https://github.com/rhpds/troshka.git", "path": "deploy/helm", "targetRevision": "vold"}}}
    calls = []
    def oc(kc, *args):
        calls.append(args)
        if args[0] == "patch":
            patch = json.loads(args[-1])
            assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "9"}
            raise subprocess.CalledProcessError(1, "oc patch")
        return json.dumps(app)
    monkeypatch.setattr(release, "oc", oc)
    with pytest.raises(subprocess.CalledProcessError):
        release.pin_application_release("kc", "troshka", "vtest", [])
    assert sum(args[0] == "patch" for args in calls) == 1


def test_operator_digest_change_forces_rbac_even_after_undeployed_tag(release, monkeypatch):
    monkeypatch.setattr(release, "previous_release_tag", lambda *a: "vundeployed")
    monkeypatch.setattr(release, "roles_changed_since", lambda *a: ({}, ["operator"]))
    monkeypatch.setattr(release, "git_paths_changed", lambda *a: False)
    assert release.release_plan("vtest")["apply_crds_rbac"] is True


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
    if "main" in args:
        print("43")
    else:
        # Simulate Actions lag after tag push: N empty lists, then the run id.
        delay = int(os.environ.get("TAG_RUN_LIST_EMPTY", "0"))
        path = os.environ.get("TAG_RUN_LIST_STATE")
        seen = 0
        if path:
            state = pathlib.Path(path)
            seen = int(state.read_text()) if state.exists() else 0
            state.write_text(str(seen + 1))
        print("" if seen < delay else "42")
if name == "gh" and args[:2] == ["run", "view"]:
    key = "MAIN_CI_CONCLUSION" if args[2] == "43" else "CI_CONCLUSION"
    print(os.environ.get(key, "success"))
if name == "python3" and args and args[0] == "-c":
    code = args[1]
    sys.argv = ["python3", *args[2:]]
    exec(compile(code, "<deploy-full-test>", "exec"), {})
    raise SystemExit(0)
if name == "python3" and any(str(a).endswith("deploy_hosts.py") for a in args):
    # Mirror the historical hardcoded fleet so existing orchestration asserts stay stable.
    print("KC='/tmp/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig'")
    print("DEV_KC='/tmp/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig'")
    print("OPERATOR_KUBECONFIGS=(")
    for host in (
        "ocpv01.dfw3", "ocpv02.dfw3", "ocpv06.dal10", "ocpv07.wdc06",
        "ocpv08.dal10", "ocpv09.dal13", "ocpvdev01.dal13",
    ):
        print(f"  '/tmp/{host}.infra.demo.redhat.com.kubeconfig'")
    print(")")
    raise SystemExit(0)
if name == "python3" and "scripts/deploy_release.py" in args:
    if "images" in args and "--state" in args:
        state = pathlib.Path(args[args.index("--state") + 1])
        roles = [
            "backend", "frontend", "operator", "dnsmasq", "gateway",
            "tools", "bmc", "vnc-proxy", "ops-pod", "terminal",
        ]
        state.write_text(json.dumps({role: "sha256:test" for role in roles}))
    if "plan" in args and "--state" in args:
        state = pathlib.Path(args[args.index("--state") + 1])
        state.write_text(json.dumps({
            "tag": "vtest",
            "previous_tag": "",
            "baseline": "production",
            "changed_roles": [
                "backend", "frontend", "operator", "dnsmasq", "gateway",
                "tools", "bmc", "vnc-proxy", "ops-pod", "terminal",
            ],
            "unchanged_roles": [],
            "operator_roles_changed": [
                "operator", "dnsmasq", "gateway", "tools", "bmc",
                "vnc-proxy", "ops-pod", "terminal",
            ],
            "app_roles_changed": ["backend", "frontend"],
            "cluster_roles_changed": [
                "operator", "dnsmasq", "gateway", "bmc", "vnc-proxy",
            ],
            "apply_crds_rbac": True,
        }))
if name == "python3" and "cluster" in args and os.environ.get("FAIL_CLUSTER"):
    sys.exit(1)
if name == "python3" and "preflight" in args and os.environ.get("FAIL_NETWORK"):
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
    dev_image = next(i for i, c in enumerate(calls) if "application" in c and "troshka-dev" in c)
    assert dev_image < promote
    hosts = [i for i, c in enumerate(calls) if "cluster" in c and "--image-tag" in c and "vtest" in c]
    assert len(hosts) == 7
    assert dev_image < min(hosts) <= max(hosts) < promote
    assert not any("scripts/collector_release_check.py" in c for c in calls)
    fleet_checks = [i for i, c in enumerate(calls) if "scripts/collector_fleet_preflight.py" in c]
    assert any(dev_image < i < promote for i in fleet_checks)
    assert any(i > promote for i in fleet_checks)
    assert all("--skip-project-pods" in calls[i] for i in hosts)


def test_full_deploy_polls_until_tag_build_registers(shell_commands, tmp_path):
    env, log = shell_commands
    state = tmp_path / "tag-run-list-seen"
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
        env={
            **env,
            "TAG_RUN_LIST_EMPTY": "2",
            "TAG_RUN_LIST_STATE": str(state),
            "IMAGE_RUN_POLL_SECONDS": "0",
            "IMAGE_RUN_WAIT_SECONDS": "30",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    tag_lists = [
        c
        for c in calls
        if c[:3] == ["gh", "run", "list"] and "--branch" in c and c[c.index("--branch") + 1] == "vtest"
    ]
    assert len(tag_lists) >= 3
    assert "Waiting for GitHub to register tag build vtest" in result.stderr + result.stdout


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
    dev = next(i for i, c in enumerate(calls) if "application" in c and "troshka-dev" in c)
    promote = next(i for i, c in enumerate(calls) if "promote" in c)
    pin = next(i for i, c in enumerate(calls) if "pin-application" in c)
    prod = next(i for i, c in enumerate(calls) if "application" in c and "troshka-dev" not in c)
    assert main_build < stage < dev < promote < pin < prod
    assert calls[pin][calls[pin].index("--tag") + 1] == "vtest"
    assert "--kubeconfig" in calls[stage] and "troshka-dev" in calls[stage]
    for index in (dev, prod):
        assert calls[index][calls[index].index("--image-tag") + 1] == "vtest"
    assert not any("latest" in c for c in calls)
    assert any(c[:3] == ["gh", "run", "watch"] for c in calls[main_build:stage])


@pytest.mark.parametrize("flags", [[], ["--verify-storage"]])
def test_failed_fleet_check_allows_host_upgrade_but_never_promotes(shell_commands, flags):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest", *flags],
        env={**env, "FAIL_FLEET": "1"}, capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([c for c in calls if "cluster" in c]) == 7
    assert not any("promote" in c for c in calls)


def test_failed_dev_accounting_never_promotes(shell_commands):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest", "--verify-storage"],
        env={**env, "FAIL_DEV_ACCOUNTING": "1"}, capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("scripts/collector_release_check.py" in c for c in calls)
    assert not any("promote" in c for c in calls)


@pytest.mark.parametrize("flags,mode", [(["--verify-storage"], "health"), (["--fresh-accounting"], "fresh"),
    (["--accounting-project", "project-one"], "fresh")])
def test_accounting_scans_only_after_promotion_when_requested(shell_commands, flags, mode):
    env, log = shell_commands
    result = subprocess.run(
        ["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest", *flags],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    promote = next(i for i, c in enumerate(calls) if "promote" in c)
    checks = [(i, c) for i, c in enumerate(calls) if "scripts/collector_release_check.py" in c]
    assert len(checks) == 4
    for i, call in checks:
        assert call[call.index("--mode") + 1] == ("health" if i < promote else mode)
        assert ("--project" in call) == (bool(flags) and flags[0] == "--accounting-project"
                                        and i > promote and "troshka" in call)


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
    # deploy-full merges stderr into its tee-backed stdout release log.
    assert "cluster rollouts failed" in result.stdout + result.stderr
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
    monkeypatch.setattr(release, "read_deployment", lambda *args: dep)
    release.update_deployment("config", dep, {"operator": "digest"}, "vtest")
    change = next(c for c in calls if "set" in c)
    assert change[-1] == "operator=" + release.REGISTRY + "operator:vtest"


def test_operator_identity_sync_preserves_unrelated_configuration(release, monkeypatch):
    dep = deployment()
    dep["spec"]["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}}
    dep["spec"]["template"]["spec"]["containers"][0] = {"name": "operator", "image": release.REGISTRY + "operator:production",
        "env": [{"name": "EXISTING", "value": "keep"}], "resources": {"limits": {"cpu": "2"}}}
    dep["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "image": "example.test/sidecar:latest"})
    calls = []
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args))
    assert release.sync_operator_identity("kc", dep)
    patch = json.loads(calls[0][-1])
    assert patch["spec"]["replicas"] == 1
    assert patch["spec"]["strategy"] == {"type": "Recreate", "rollingUpdate": None}
    assert set(patch["spec"]) == {"replicas", "strategy", "template"}
    assert len(patch["spec"]["template"]["spec"]["containers"]) == 1
    container = patch["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "operator" and "resources" not in container
    assert {e["name"]: e["valueFrom"]["fieldRef"]["fieldPath"] for e in container["env"]} == {
        "POD_NAME": "metadata.name", "POD_NAMESPACE": "metadata.namespace", "POD_UID": "metadata.uid"}
    dep["spec"]["template"]["spec"]["containers"][0]["env"] += [
        {k: v for k, v in entry.items() if k != "value"} for entry in container["env"]]
    dep["spec"].update(replicas=1, strategy={"type": "Recreate"})
    calls.clear()
    assert not release.sync_operator_identity("kc", dep)
    assert not calls


def test_api_defaulted_identity_does_not_restart_current_operator(release, monkeypatch):
    dep = deployment()
    dep["spec"].update(replicas=1, strategy={"type": "Recreate"})
    container = dep["spec"]["template"]["spec"]["containers"][0]
    container["image"] = release.REGISTRY + "operator:production"
    container["env"] = [{"name": key, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": path}}}
                        for key, path in (("POD_NAME", "metadata.name"), ("POD_NAMESPACE", "metadata.namespace"),
                                          ("POD_UID", "metadata.uid"))]
    monkeypatch.setattr(release, "read_deployment", lambda *args: dep)
    monkeypatch.setattr(release, "preflight_networks", lambda *args: None)
    monkeypatch.setattr(release, "verify_live", lambda *args: None)
    monkeypatch.setattr(release, "wait_rollout", lambda *args: pytest.fail("unexpected rollout"))
    calls = []
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args))
    release.update_deployment("kc", dep, {})
    assert not calls


@pytest.mark.parametrize("change", ["replicas", "strategy", "rollingUpdate"])
def test_operator_rollout_contract_is_repaired_even_when_identity_and_image_are_current(release, monkeypatch, change):
    dep = deployment()
    dep["spec"].update(replicas=1, strategy={"type": "Recreate"})
    container = dep["spec"]["template"]["spec"]["containers"][0]
    container["image"] = release.REGISTRY + "operator:production"
    container["env"] = [{"name": key, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": path}}}
                        for key, path in (("POD_NAME", "metadata.name"), ("POD_NAMESPACE", "metadata.namespace"),
                                          ("POD_UID", "metadata.uid"))]
    if change == "replicas":
        dep["spec"]["replicas"] = 2
    elif change == "strategy":
        dep["spec"].pop("strategy")  # API default is RollingUpdate.
    else:
        dep["spec"]["strategy"]["rollingUpdate"] = {"maxSurge": 1}
    calls, waited = [], []
    monkeypatch.setattr(release, "read_deployment", lambda *args: dep)
    monkeypatch.setattr(release, "preflight_networks", lambda *args: None)
    monkeypatch.setattr(release, "verify_live", lambda *args: None)
    monkeypatch.setattr(release, "wait_rollout", lambda *args: waited.append(args))
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args))
    release.update_deployment("kc", dep, {"operator": "digest"})
    patch = json.loads(next(call for call in calls if call[1] == "patch")[-1])
    expected = {"replicas": 1} if change == "replicas" else {"strategy": {"type": "Recreate", "rollingUpdate": None}}
    assert patch == {"spec": expected}
    assert waited == [("kc", "troshka", "workers")]


def test_non_operator_rollout_configuration_is_untouched(release, monkeypatch):
    dep = deployment()
    dep["spec"]["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}}
    monkeypatch.setattr(release, "oc", lambda *args: pytest.fail("unexpected patch"))
    assert not release.sync_operator_identity("kc", dep)


def test_operator_contract_patch_failure_aborts_before_image_update(release, monkeypatch):
    dep = deployment()
    dep["spec"]["template"]["spec"]["containers"][0]["image"] = release.REGISTRY + "operator:production"
    monkeypatch.setattr(release, "read_deployment", lambda *args: dep)
    monkeypatch.setattr(release, "preflight_networks", lambda *args: None)
    calls = []

    def denied(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, "oc patch", stderr="Forbidden")

    monkeypatch.setattr(release, "oc", denied)
    monkeypatch.setattr(release, "verify_live", lambda *args: pytest.fail("patch failure was swallowed"))
    with pytest.raises(subprocess.CalledProcessError):
        release.update_deployment("kc", dep, {})
    assert len(calls) == 1 and calls[0][1] == "patch"


def helper_deployment(annotation=None, name="bmc"):
    dep = deployment()
    dep["metadata"].update(namespace="troshka-project", name=name)
    dep["spec"]["template"]["spec"]["containers"][0]["image"] = "quay.io/redhat-gpte/troshka-bmc:production"
    if annotation is not None:
        dep["spec"]["template"]["metadata"] = {"annotations": {
            "k8s.v1.cni.cncf.io/networks": annotation,
        }}
    return dep


def test_missing_network_blocks_update_before_any_mutation(release, monkeypatch):
    dep = helper_deployment("net-bmc-nad")
    calls = []
    monkeypatch.setattr(release, "read_deployment", lambda *args: dep)
    monkeypatch.setattr(release, "verify_live", lambda *args: None)
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args) or "")
    with pytest.raises(RuntimeError, match="troshka-project/net-bmc-nad"):
        release.update_deployment("kc", dep, {})
    assert calls and all(c[1] == "get" for c in calls)


@pytest.mark.parametrize("annotation,expected", [
    (None, set()), ("", set()),
    ("net-one, other/net-two@iface", {("troshka-project", "net-one"), ("other", "net-two")}),
    ('[{"name":"net-one","interface":"net1"},{"name":"net-two","namespace":"other","ips":["192.0.2.1"]}]', {("troshka-project", "net-one"), ("other", "net-two")}),
    (" other / net-two @ iface ", {("other", "net-two")}),
    ("[]", set()),
])
def test_multus_attachment_forms(release, annotation, expected):
    assert release.network_attachments(helper_deployment(annotation)) == expected


@pytest.mark.parametrize("annotation", ["[bad", '["net-one"]', '[{}]', '{"name":1}',
    "one,,two", "other/one/two", "one@", '{"name":"net","namespace":1}',
    '{"name":"secret-value!"}', '{"name":"net-one","namespace":"other"}'])
def test_invalid_multus_annotations_fail_closed_without_echo(release, annotation):
    with pytest.raises(RuntimeError, match="Invalid Multus") as error:
        release.network_attachments(helper_deployment(annotation))
    assert annotation not in str(error.value)


def test_operator_without_multus_never_queries_network_api(release, monkeypatch):
    monkeypatch.setattr(release, "oc", lambda *args: pytest.fail("unexpected API call"))
    release.preflight_networks("kc", helper_deployment())


def test_existing_but_terminating_nad_blocks_rollout(release, monkeypatch):
    monkeypatch.setattr(release, "oc", lambda *args: json.dumps({"metadata": {"deletionTimestamp": "now"}}))
    with pytest.raises(RuntimeError, match="terminating"):
        release.preflight_networks("kc", helper_deployment("net-one"))


def test_network_api_errors_propagate_before_rollout(release, monkeypatch):
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, ["oc", "get"])
    monkeypatch.setattr(release, "oc", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        release.preflight_networks("kc", helper_deployment("net-one"))


def test_network_check_uses_explicit_namespace_and_deduplicates(release, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "oc", lambda *args: calls.append(args) or '{"metadata":{}}')
    release.preflight_networks("kc", helper_deployment("other/net-one@iface, other/net-one"))
    assert calls == [("kc", "get", "network-attachment-definitions.k8s.cni.cncf.io", "net-one",
                      "-n", "other", "--ignore-not-found", "-o", "json")]


def test_update_rechecks_current_template_instead_of_discovery_snapshot(release, monkeypatch):
    monkeypatch.setattr(release, "read_deployment", lambda *args: helper_deployment("new-network"))
    monkeypatch.setattr(release, "oc", lambda *args: "")
    monkeypatch.setattr(release, "verify_live", lambda *args: pytest.fail("dependency should fail first"))
    with pytest.raises(RuntimeError, match="new-network"):
        release.update_deployment("kc", helper_deployment(), {})


def test_cluster_preflights_every_target_before_starting_any_update(release, monkeypatch):
    operator = helper_deployment(name="troshka-operator")
    operator["metadata"]["namespace"] = "troshka-operator"
    helper = helper_deployment("net-missing")
    def oc(kc, *args):
        return json.dumps({"items": [operator, helper]}) if args[1] == "deployments" else ""
    monkeypatch.setattr(release, "oc", oc)
    monkeypatch.setattr(release, "update_deployment", lambda *args: pytest.fail("mutated before fleet checked"))
    with pytest.raises(RuntimeError, match="net-missing"):
        release.cluster("kc", {})


def test_full_deploy_network_preflight_precedes_mutations(shell_commands):
    env, log = shell_commands
    result = subprocess.run(["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
                            env=env, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    checks = [i for i, c in enumerate(calls) if "preflight" in c]
    assert len(checks) == 14  # Early fleet check and fresh check before promotion.
    stage = next(i for i, c in enumerate(calls) if "stage-dev" in c)
    promote = next(i for i, c in enumerate(calls) if "promote" in c)
    assert max(checks[:7]) < stage < min(checks[7:]) < max(checks[7:]) < promote


def test_failed_network_preflight_never_changes_workloads_or_images(shell_commands):
    env, log = shell_commands
    result = subprocess.run(["bash", str(PATH.with_name("deploy-full.sh")), "--tag", "vtest"],
                            env={**env, "FAIL_NETWORK": "1"}, capture_output=True, check=False)
    assert result.returncode != 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("promote" in c or "stage-dev" in c or "cluster" in c for c in calls)
    assert not any(c[:2] == ["oc", "apply"] for c in calls)


def test_preflight_scc_rejects_missing_sa_membership(release, monkeypatch):
    dep = {
        "metadata": {"name": "bmc-abc", "namespace": "troshka-abc"},
        "spec": {"template": {"spec": {"serviceAccountName": "troshka-bmc", "containers": []}}},
    }

    def oc(kc, *args, **kwargs):
        if args[:2] == ("get", "scc"):
            return json.dumps({"users": ["system:serviceaccount:other:troshka-bmc"]})
        raise AssertionError(args)

    monkeypatch.setattr(release, "oc", oc)
    with pytest.raises(RuntimeError, match="not in SCC troshka-gateway"):
        release.preflight_scc("kc", dep)


def test_preflight_scc_accepts_present_membership(release, monkeypatch):
    dep = {
        "metadata": {"name": "dnsmasq-net", "namespace": "troshka-abc"},
        "spec": {
            "template": {"spec": {"serviceAccountName": "troshka-network", "containers": []}}
        },
    }
    seen = []

    def oc(kc, *args, **kwargs):
        seen.append(args[2])
        return json.dumps(
            {"users": ["system:serviceaccount:troshka-abc:troshka-network"]}
        )

    monkeypatch.setattr(release, "oc", oc)
    release.preflight_scc("kc", dep)
    assert seen == ["troshka-network-pods", "troshka-gateway"]


def test_out_of_sync_resources_lists_drift(release):
    app = {
        "status": {
            "resources": [
                {"kind": "ClusterRole", "name": "troshka-group-reader", "status": "OutOfSync"},
                {"kind": "Deployment", "name": "troshka-backend", "namespace": "troshka", "status": "Synced",
                 "health": {"status": "Healthy"}},
            ]
        }
    }
    bad = release.out_of_sync_resources(app)
    assert bad == ["ClusterRole/troshka-group-reader status=OutOfSync health=None"]


def test_ensure_argo_ready_deletes_orphans_then_fails_with_list(release, monkeypatch):
    calls = []
    app = {
        "status": {
            "sync": {"status": "OutOfSync"},
            "resources": [
                {"kind": "Namespace", "name": "troshka", "status": "OutOfSync"},
            ],
        }
    }

    def oc(kc, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("get", "clusterrole") or args[:2] == ("get", "clusterrolebinding"):
            return f"{args[0]}/{args[1]}"
        if args[0] == "delete":
            return ""
        if args[0] == "annotate":
            return ""
        if args[0] == "wait":
            raise RuntimeError("timed out")
        if args[:3] == ("get", "applications.argoproj.io", "troshka"):
            return json.dumps(app)
        raise AssertionError(args)

    monkeypatch.setattr(release, "oc", oc)
    with pytest.raises(RuntimeError, match="OutOfSync"):
        release.ensure_argo_ready("kc", "troshka")
    assert any(c[0] == "delete" and c[1] == "clusterrole" for c in calls)
    assert any(c[0] == "delete" and c[1] == "clusterrolebinding" for c in calls)
