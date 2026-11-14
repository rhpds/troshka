"""Bootstrap Troshka into an existing Argo CD / OpenShift GitOps installation.

Prepare a private, reviewable bundle, then apply it explicitly. Existing installs
are adopted without pruning, secret rotation, migration hooks, or pod restarts.
Requires python3 with PyYAML, Helm, Git, and oc. Never installs cluster operators.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import yaml

REPO = "https://github.com/rhpds/troshka.git"
ROOT = Path(__file__).resolve().parents[1]
SECRET_PATHS = (
    ("secrets",), ("postgres", "password"), ("auth", "basicAuth", "password"),
    ("auth", "oauth2Proxy", "clientSecret"), ("auth", "oauth2Proxy", "cookieSecret"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(args, *, payload=None, timeout=90):
    result = subprocess.run(args, input=payload, text=True, capture_output=True,
                            timeout=timeout, cwd=ROOT, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed (output suppressed to protect credentials)")
    return result.stdout


def oc(kc, *args, payload=None):
    return command(["oc", f"--kubeconfig={kc}", "--request-timeout=30s", *args], payload=payload)


def no_credentials(values):
    for path in SECRET_PATHS:
        value = values
        for key in path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        require(not (any(value.values()) if isinstance(value, dict) else value),
                "credential values must stay in Kubernetes Secrets, not an Application")


def dev_images(values):
    for component in ("backend", "frontend"):
        values.setdefault(component, {})["image"] = {
            "repository": f"quay.io/redhat-gpte/troshka-{component}", "tag": "latest"}


def image_updater(name, namespace):
    return {"apiVersion": "argocd-image-updater.argoproj.io/v1alpha1", "kind": "ImageUpdater",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"writeBackConfig": {"method": "argocd"}, "applicationRefs": [{
                "namePattern": name, "images": [{
                    "alias": component, "imageName": f"quay.io/redhat-gpte/troshka-{component}:latest",
                    "commonUpdateSettings": {"updateStrategy": "digest", "forceUpdate": True},
                    "manifestTargets": {"helm": {"name": f"{component}.image.repository",
                                                 "tag": f"{component}.image.tag"}}}
                    for component in ("backend", "frontend")]}]}}


def enable_dev_updater(kc, namespace, name):
    """Use OpenShift GitOps' installed controller, with an exact Application target."""
    current = json.loads(oc(kc, "get", "argocds.argoproj.io", "openshift-gitops", "-n", namespace, "-o", "json"))
    if not current.get("spec", {}).get("imageUpdater", {}).get("enabled"):
        settings = dict(current.get("spec", {}).get("imageUpdater") or {})
        settings["enabled"] = True
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                 {"op": "add", "path": "/spec/imageUpdater", "value": settings}]
        oc(kc, "patch", "argocds.argoproj.io", "openshift-gitops", "-n", namespace,
           "--type=json", "-p", json.dumps(patch))
    oc(kc, "create", "-f", "-", payload=json.dumps(image_updater(name, namespace)))


def application(name, namespace, revision, values, adopted, *, argo_namespace="openshift-gitops"):
    require(revision == "main" or bool(re.fullmatch(r"v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", revision)),
            "main or a named release tag starting with v is required")
    no_credentials(values)
    rules = [{"group": "", "kind": "Secret", "name": secret, "namespace": namespace,
              "jsonPointers": ["/data", "/stringData"]}
             for secret in ("troshka-secrets", "troshka-oauth-proxy", "troshka-oauth2-proxy", "troshka-basic-auth")]
    rules += [{"group": "oauth.openshift.io", "kind": "OAuthClient", "name": "troshka", "jsonPointers": ["/secret"]},
              {"group": "", "kind": "Namespace", "name": namespace, "jsonPointers": ["/spec/finalizers"]}]
    for resource in adopted:
        kind = resource["kind"]
        pointers = []
        if kind in {"Deployment", "StatefulSet"}:
            pointers = ["/spec/template", "/spec/replicas"]
        elif kind == "ConfigMap":
            pointers = ["/data", "/binaryData"]
        if pointers:
            rules.append({"group": resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else "",
                          "kind": kind, "name": resource["metadata"]["name"], "namespace": namespace,
                          "jsonPointers": pointers})
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": name, "namespace": argo_namespace},
            "spec": {"project": name,
                     "source": {"repoURL": REPO, "targetRevision": revision, "path": "deploy/helm",
                                "helm": {"releaseName": name, "valuesObject": values}},
                     "destination": {"server": "https://kubernetes.default.svc", "namespace": namespace},
                     "ignoreDifferences": rules,
                     "syncPolicy": {"syncOptions": ["RespectIgnoreDifferences=true", "FailOnSharedResource=true"]}}}


def sync_operation(revision, resources=None):
    sync = {"revision": revision, "prune": False,
            "syncOptions": ["RespectIgnoreDifferences=true", "FailOnSharedResource=true"]}
    if resources is not None:
        sync["resources"] = [
            {"group": item["apiVersion"].split("/")[0] if "/" in item["apiVersion"] else "",
             "kind": item["kind"], "name": item["metadata"]["name"],
             "namespace": item["metadata"].get("namespace", "")}
            for item in resources if not item["metadata"].get("annotations", {}).get("argocd.argoproj.io/hook")
            and not item["metadata"].get("annotations", {}).get("helm.sh/hook")
        ]
        require(bool(sync["resources"]), "no resources to adopt")
    return {"initiatedBy": {"username": "troshka-bootstrap"}, "sync": sync}


def live_resource(kc, item):
    kind = {"Application": "applications.argoproj.io", "AppProject": "appprojects.argoproj.io"}.get(item["kind"], item["kind"])
    args = ["get", kind, item["metadata"]["name"], "--ignore-not-found", "-o", "json"]
    if item["metadata"].get("namespace"):
        args += ["-n", item["metadata"]["namespace"]]
    raw = oc(kc, *args)
    return json.loads(raw) if raw.strip() else None


def rendered_chart(revision, name, namespace, values, output):
    ref = "refs/heads/main" if revision == "main" else f"refs/tags/{revision}"
    remote = command(["git", "ls-remote", REPO, ref, f"{ref}^{{}}"])
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    local = refs.get(f"{ref}^{{}}", refs.get(ref))
    require(bool(local), "published revision not found")
    command(["git", "cat-file", "-e", f"{local}^{{commit}}"])
    result = subprocess.run(["git", "archive", local, "deploy/helm"], cwd=ROOT,
                            capture_output=True, check=True, timeout=30)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(output / "source", filter="data")
    path = output / "values.json"
    path.write_text(json.dumps(values, indent=2))
    rendered = command(["helm", "template", name, str(output / "source/deploy/helm"), "-n", namespace,
                        "-f", str(path)])
    (output / "rendered.yaml").write_text(rendered)
    return [item for item in yaml.safe_load_all(rendered) if item], local


def hydrate_native(values, kc, namespace):
    descriptor = {"kind": "Deployment", "metadata": {"name": "troshka-collection-worker", "namespace": namespace}}
    deployment = live_resource(kc, descriptor)
    if deployment is None:
        return
    volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
    cm = live_resource(kc, {"kind": "ConfigMap", "metadata": {"name": volumes["config"]["configMap"]["name"], "namespace": namespace}})
    runtime = yaml.safe_load(cm["data"]["config.yaml"])["native_collection"]
    native = values.setdefault("nativeCollection", {})
    for key, value in runtime.items():
        pieces = key.split("_")
        native[pieces[0] + "".join(p.title() for p in pieces[1:])] = value
    worker = native.setdefault("worker", {})
    worker.update(enabled=True, replicas=deployment["spec"]["replicas"],
                  configMapName=volumes["collection-config"]["configMap"]["name"],
                  secretName=volumes["collection-transport"]["secret"]["secretName"])
    for init in deployment["spec"]["template"]["spec"].get("initContainers", []):
        if init["name"] == "prepare-collection-cli":
            worker["cliImage"] = init["image"]
    descriptor["metadata"]["name"] = "troshka-readiness-worker"
    readiness = live_resource(kc, descriptor)
    if readiness:
        native.setdefault("readinessWorker", {}).update(enabled=True, replicas=readiness["spec"]["replicas"])
    hydrate_scheduler(native, kc, namespace)


def hydrate_scheduler(native, kc, namespace):
    """The backend schedules collection; a worker-only config can disable it."""
    backend = live_resource(kc, {"kind": "Deployment", "metadata": {"name": "troshka-backend", "namespace": namespace}})
    if backend is None:
        return
    config_volume = next((v for v in backend["spec"]["template"]["spec"].get("volumes", []) if v["name"] == "config"), None)
    require(config_volume is not None, "backend config volume missing")
    cm = live_resource(kc, {"kind": "ConfigMap", "metadata": {"name": config_volume["configMap"]["name"], "namespace": namespace}})
    scheduler = yaml.safe_load(cm["data"]["config.yaml"]).get("native_collection", {})
    for key in ("enabled", "interval_seconds", "domain_interval_seconds", "readiness_interval_seconds",
                "readiness_timeout_seconds", "job_timeout_seconds", "provider_slots"):
        if key in scheduler:
            pieces = key.split("_")
            native[pieces[0] + "".join(p.title() for p in pieces[1:])] = scheduler[key]


def hydrate_images(values, kc, namespace):
    for component, kind, name in [("backend", "Deployment", "troshka-backend"),
                                  ("frontend", "Deployment", "troshka-frontend"),
                                  ("postgres", "StatefulSet", "troshka-postgres"),
                                  ("redis", "Deployment", "troshka-redis"), ("s4", "Deployment", "troshka-s4")]:
        resource = live_resource(kc, {"kind": kind, "metadata": {"name": name, "namespace": namespace}})
        if resource is None:
            continue
        image = resource["spec"]["template"]["spec"]["containers"][0]["image"]
        if component in {"backend", "frontend"}:
            repository, tag = image.rsplit(":", 1)
            require("@" not in image, "adoption requires tagged images")
            values.setdefault(component, {})["image"] = {"repository": repository, "tag": tag}
        else:
            values.setdefault(component, {})["image"] = image


def hydrate_quota_override(values, kc, namespace):
    overrides = set()
    for name in ("backend", "worker", "tunnel", "collection-worker", "readiness-worker"):
        resource = live_resource(kc, {"kind": "Deployment", "metadata": {"name": f"troshka-{name}", "namespace": namespace}})
        if resource is None:
            continue
        for container in resource["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env", []):
                if env["name"] == "TROSHKA_QUOTAS__ENABLED":
                    require(env.get("value") in {"true", "false"}, "quota override needs explicit boolean review")
                    overrides.add(env["value"] == "true")
    require(len(overrides) <= 1, "conflicting live quota overrides require review")
    if overrides:
        values.setdefault("quotas", {})["enabled"] = overrides.pop()


def prepare(args):
    require(not args.dev or args.revision == "main", "--dev requires --revision main")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    oc(args.kubeconfig, "get", "crd", "applications.argoproj.io", "-o", "name")
    descriptor = {"kind": "Application", "metadata": {"name": args.name, "namespace": args.argo_namespace}}
    require(live_resource(args.kubeconfig, descriptor) is None, "Application already exists; manage it instead of bootstrapping again")
    if args.adopt:
        values = json.loads(command(["helm", "get", "values", args.name, "-n", args.namespace,
                                     f"--kubeconfig={args.kubeconfig}", "--all", "-o", "json"]))
    else:
        values = {}
    if args.values:
        # Values file is a complete override, not a recursive implicit merge.
        values.update(yaml.safe_load(args.values.read_text()) or {})
    values.setdefault("namespace", {}).update(name=args.namespace, create=False)
    if args.adopt:
        hydrate_native(values, args.kubeconfig, args.namespace)
        hydrate_images(values, args.kubeconfig, args.namespace)
        hydrate_quota_override(values, args.kubeconfig, args.namespace)
    else:
        for component in ("backend", "frontend"):
            values.setdefault(component, {}).setdefault("image", {}).setdefault("tag", "latest" if args.revision == "main" else args.revision)
    if args.dev:
        dev_images(values)
    no_credentials(values)
    rendered, revision = rendered_chart(args.revision, args.name, args.namespace, values, args.output)
    live = [value for item in rendered if (value := live_resource(args.kubeconfig, item))]
    require(args.adopt or not live, "existing resources require --adopt")
    app = application(args.name, args.namespace, args.revision, values, live if args.adopt else [],
                      argo_namespace=args.argo_namespace)
    if args.dev and not args.adopt:
        app["spec"]["syncPolicy"]["automated"] = {"prune": False, "selfHeal": True}
    project = {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
               "metadata": {"name": args.name, "namespace": args.argo_namespace},
               "spec": {"sourceRepos": [REPO], "destinations": [app["spec"]["destination"]],
                        "namespaceResourceWhitelist": [{"group": "*", "kind": "*"}],
                        "clusterResourceWhitelist": [{"group": g, "kind": k} for g, k in
                            [("", "Namespace"), ("rbac.authorization.k8s.io", "ClusterRole"),
                             ("rbac.authorization.k8s.io", "ClusterRoleBinding"), ("oauth.openshift.io", "OAuthClient")]]}}
    project["spec"]["destinations"][0] = {"server": "https://kubernetes.default.svc", "namespace": args.namespace}
    for filename, value in [("application.json", app), ("project.json", project), ("live-before.json", live),
                            ("plan.json", {"kubeconfig": args.kubeconfig, "adopt": args.adopt,
                                           "revision": revision, "resources": rendered, "dev": args.dev})]:
        (args.output / filename).write_text(json.dumps(value, indent=2))
    report = {"application": args.name, "namespace": args.namespace, "revision": args.revision,
              "adopt": args.adopt, "existing_resources": len(live), "rendered_resources": len(rendered),
              "automatic_sync": "automated" in app["spec"]["syncPolicy"], "pruning": False,
              "adoption_guards": [r for r in app["spec"]["ignoreDifferences"] if r["kind"] in {"Deployment", "StatefulSet", "ConfigMap"}]}
    (args.output / "review.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def revalidate(kc, saved):
    for item in saved:
        current = live_resource(kc, item)
        require(current is not None and current["metadata"]["uid"] == item["metadata"]["uid"], "resource identity changed since preparation")
        for field in ("spec", "data", "secret"):
            require(current.get(field) == item.get(field), "resource contents changed since preparation")


def apply(output, sync):
    app = json.loads((output / "application.json").read_text())
    plan = json.loads((output / "plan.json").read_text())
    kc = plan["kubeconfig"]
    saved = json.loads((output / "live-before.json").read_text())
    revalidate(kc, saved)
    marker = output / "apply-started"
    with marker.open("x") as stream:
        stream.write("Inspect live state before retrying an interrupted apply.\n")
    namespace = app["spec"]["destination"]["namespace"]
    if live_resource(kc, {"kind": "Namespace", "metadata": {"name": namespace}}) is None:
        oc(kc, "create", "namespace", namespace)
    # Pre-provision generated credentials once; Argo respects these existing fields.
    for item in plan["resources"]:
        if item["kind"] in {"Secret", "OAuthClient"} and live_resource(kc, item) is None:
            oc(kc, "create", "-f", "-", payload=json.dumps(item))
    for filename in ("project.json", "application.json"):
        oc(kc, "create", "-f", str(output / filename))
    if sync and "automated" not in app["spec"]["syncPolicy"]:
        resume_sync(output)
    if plan.get("dev"):
        enable_dev_updater(kc, app["metadata"]["namespace"], app["metadata"]["name"])
    print("Argo bootstrap submitted; verify application status and retained adoption guards.")


def resume_sync(output):
    """Explicitly resume after inspecting an interrupted bootstrap; never recreate it."""
    app = json.loads((output / "application.json").read_text())
    plan = json.loads((output / "plan.json").read_text())
    current = live_resource(plan["kubeconfig"], app)
    require(current is not None, "Argo Application does not exist")
    require(current["spec"] == app["spec"], "Application differs from prepared bundle")
    require(not current.get("operation"), "an Argo operation is already pending")
    revalidate(plan["kubeconfig"], json.loads((output / "live-before.json").read_text()))
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
             {"op": "add", "path": "/operation", "value": sync_operation(plan["revision"],
                       plan["resources"] if plan["adopt"] else None)}]
    oc(plan["kubeconfig"], "patch", "applications.argoproj.io", app["metadata"]["name"],
       "-n", app["metadata"]["namespace"], "--type=json", "-p", json.dumps(patch))


def verify(output):
    app = json.loads((output / "application.json").read_text())
    plan = json.loads((output / "plan.json").read_text())
    deadline = time.monotonic() + 600
    waiter = threading.Event()
    while time.monotonic() < deadline:
        current = live_resource(plan["kubeconfig"], app)
        require(current is not None, "Argo Application does not exist")
        status = current.get("status", {})
        phase = status.get("operationState", {}).get("phase")
        report = {"sync": status.get("sync", {}).get("status"), "health": status.get("health", {}).get("status"),
                  "operation": phase, "conditions": [c["type"] for c in status.get("conditions", [])]}
        (output / "status.json").write_text(json.dumps(report, indent=2))
        if phase in {"Failed", "Error"}:
            raise RuntimeError("Argo sync failed; inspect application conditions")
        if not current.get("operation") and report["sync"] == "Synced" and report["health"] == "Healthy" and phase == "Succeeded":
            print(json.dumps(report))
            return
        waiter.wait(3)
    raise TimeoutError("Argo has not reached Synced/Healthy/Succeeded; inspect retained status")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--name", default="troshka")
    parser.add_argument("--namespace", default="troshka")
    parser.add_argument("--argo-namespace", default="openshift-gitops")
    parser.add_argument("--revision")
    parser.add_argument("--values", type=Path)
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Use main/latest and enable OpenShift GitOps image updater")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--resume-sync", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.verify:
            verify(args.output)
        elif args.resume_sync:
            resume_sync(args.output)
        elif args.apply:
            apply(args.output, args.sync)
        else:
            require(args.kubeconfig and args.revision, "--kubeconfig and --revision required")
            prepare(args)
        return 0
    except (ValueError, RuntimeError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
