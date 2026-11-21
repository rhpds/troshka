"""Release promotion and rollout verification used by deploy-full.sh."""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REGISTRY = "quay.io/redhat-gpte/troshka-"
IMAGES = (
    "backend",
    "frontend",
    "operator",
    "dnsmasq",
    "gateway",
    "tools",
    "bmc",
    "vnc-proxy",
    "ops-pod",
    "terminal",
)
APP_ROLES = ("backend", "frontend")
OPERATOR_ROLES = tuple(role for role in IMAGES if role not in APP_ROLES)
CLUSTER_ROLLOUT_ROLES = ("operator", "dnsmasq", "gateway", "bmc", "vnc-proxy")
CRD_RBAC_PATHS = (
    "src/operator/crds/",
    "src/operator/deploy/clusterrole.yaml",
    "infra/ocpvirt-rbac.yaml",
    "infra/storage-collector-base-rbac.yaml",
    "infra/storage-collector-project-rbac.yaml",
)


def run(args, timeout=180, stdin=None):
    result = subprocess.run(
        args, input=stdin, text=True, capture_output=True, timeout=timeout, check=True
    )
    return result.stdout.strip()


def oc(kubeconfig, *args, timeout=180):
    return run(
        ["oc", f"--kubeconfig={kubeconfig}", "--request-timeout=30s", *args], timeout
    )


def image_role(image):
    repository = image.split("@", 1)[0].split(":", 1)[0]
    return next((role for role in IMAGES if repository == REGISTRY + role), None)


def digest(role, tag):
    value = run(
        [
            "skopeo",
            "inspect",
            "--format",
            "{{.Digest}}",
            f"docker://{REGISTRY}{role}:{tag}",
        ]
    )
    if not value.startswith("sha256:"):
        raise RuntimeError(f"Missing digest for {role}:{tag}")
    return value


def digest_or_none(role, tag):
    try:
        return digest(role, tag)
    except (RuntimeError, subprocess.SubprocessError):
        return None


def release_digests(tag):
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(IMAGES, pool.map(lambda role: digest(role, tag), IMAGES)))


def parse_roles(value):
    if value is None or value == "":
        return list(IMAGES)
    roles = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [role for role in roles if role not in IMAGES]
    if unknown:
        raise RuntimeError(f"Unknown image roles: {', '.join(unknown)}")
    return roles


def roles_changed_since(tag, baseline="production", roles=None):
    """Return roles whose release digest differs from baseline (or is new)."""
    check = list(IMAGES if roles is None else roles)
    expected = {role: digest(role, tag) for role in check}
    changed = [
        role
        for role in check
        if digest_or_none(role, baseline) != expected[role]
    ]
    return expected, changed


def git_paths_changed(previous_ref, head_ref="HEAD", paths=CRD_RBAC_PATHS):
    """True when any listed path changed between previous_ref and head_ref."""
    if not previous_ref:
        return True
    try:
        diff = run(
            ["git", "diff", "--name-only", f"{previous_ref}...{head_ref}", "--", *paths],
            timeout=60,
        )
    except subprocess.SubprocessError:
        return True
    return bool(diff.strip())


def previous_release_tag(current_tag):
    tags = run(
        ["git", "tag", "-l", "v*", "--sort=-creatordate"], timeout=60
    ).splitlines()
    for tag in tags:
        if tag and tag != current_tag:
            return tag
    return ""


def promote(tag, roles=None):
    # Resolve every required source before mutating destinations. Unchanged roles
    # already match production and are skipped.
    expected = release_digests(tag)
    selected = list(IMAGES if roles is None else roles)
    for role in selected:
        current = digest_or_none(role, "production")
        if current == expected[role]:
            print(f"  {role}: production already at {tag} digest", flush=True)
            continue
        source = f"docker://{REGISTRY}{role}:{tag}"
        target = f"docker://{REGISTRY}{role}:production"
        run(["skopeo", "copy", "--all", source, target], timeout=600)
        if digest(role, "production") != expected[role]:
            raise RuntimeError(f"Production digest mismatch for {role}")
        print(f"  {role}: {tag} promoted and verified", flush=True)
    return expected


def stage_dev_images(tag, roles=None):
    # Main and release builds can both publish latest. Resolve release digests
    # first and copy by digest so dev tests exactly the promotion candidate.
    selected = [role for role in (roles or APP_ROLES) if role in APP_ROLES]
    if not selected:
        print("  stage-dev: no app image roles to align", flush=True)
        return
    expected = {role: digest(role, tag) for role in selected}
    for role, image_digest in expected.items():
        if digest_or_none(role, "latest") == image_digest:
            print(f"  {role}: dev latest already aligned with {tag}", flush=True)
            continue
        run([
            "skopeo", "copy", "--all",
            f"docker://{REGISTRY}{role}@{image_digest}",
            f"docker://{REGISTRY}{role}:latest",
        ], timeout=600)
        if digest(role, "latest") != image_digest:
            raise RuntimeError(f"Dev digest mismatch for {role}")
        print(f"  {role}: dev latest aligned with {tag}", flush=True)


def pin_application_release(kc, namespace, tag):
    if not re.fullmatch(r"v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", tag):
        raise RuntimeError("A named release tag starting with v is required")
    app = json.loads(oc(kc, "get", "applications.argoproj.io", namespace,
                        "-n", "openshift-gitops", "-o", "json"))
    spec = app.get("spec", {})
    source = spec.get("source", {})
    if (spec.get("destination", {}).get("namespace") != namespace
            or source.get("path") != "deploy/helm"):
        raise RuntimeError("Unexpected Argo application target")
    if source.get("targetRevision") == tag:
        return
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": app["metadata"]["resourceVersion"]},
        {"op": "replace", "path": "/spec/source/targetRevision", "value": tag},
    ]
    oc(kc, "patch", "applications.argoproj.io", namespace, "-n", "openshift-gitops",
       "--type=json", "-p", json.dumps(patch))
    print(f"Argo {namespace}: Git revision pinned to {tag}", flush=True)


def matching_pods(dep, pods):
    labels = dep["spec"]["selector"]["matchLabels"]
    return [
        pod
        for pod in pods
        if pod["metadata"]["namespace"] == dep["metadata"]["namespace"]
        and not pod["metadata"].get("deletionTimestamp")
        and all(
            pod["metadata"].get("labels", {}).get(k) == v for k, v in labels.items()
        )
    ]


def verify_container(pod, name, expected):
    status = next(
        (
            c
            for c in pod.get("status", {}).get("containerStatuses", [])
            if c["name"] == name
        ),
        {},
    )
    if not status.get("ready"):
        raise RuntimeError(f"{pod['metadata']['name']}/{name}: not ready")
    if status.get("imageID", "").split("@")[-1] != expected:
        raise RuntimeError(f"{pod['metadata']['name']}/{name}: release digest mismatch")


def verify_deployment(dep, pods, expected):
    name = dep["metadata"]["name"]
    replicas = dep["spec"].get("replicas", 1)
    status = dep.get("status", {})
    if status.get("observedGeneration", 0) < dep["metadata"]["generation"]:
        raise RuntimeError(f"{name}: latest generation not observed")
    if any(
        status.get(key, 0) != replicas
        for key in ("updatedReplicas", "availableReplicas")
    ):
        raise RuntimeError(f"{name}: incomplete replica rollout")
    current = matching_pods(dep, pods)
    if len(current) != replicas:
        raise RuntimeError(
            f"{name}: expected {replicas} current replicas, found {len(current)}"
        )
    for container in dep["spec"]["template"]["spec"]["containers"]:
        role = image_role(container["image"])
        if role:
            for pod in current:
                verify_container(pod, container["name"], expected[role])


def read_deployment(kc, namespace, name):
    return json.loads(oc(kc, "get", "deployment", name, "-n", namespace, "-o", "json"))


def _text_attachment(value):
    parts = [v.strip() for v in value.strip().split("@")]
    if len(parts) > 2 or (len(parts) == 2 and not parts[1]):
        raise ValueError("invalid interface selection")
    path = [v.strip() for v in parts[0].split("/")]
    if len(path) == 1:
        return {"name": path[0]}
    if len(path) == 2:
        return {"namespace": path[0], "name": path[1]}
    raise ValueError("invalid network selection")


def _attachment_identity(item, namespace):
    if not isinstance(item, dict):
        raise TypeError("invalid network selection")
    name, namespace = item.get("name"), item.get("namespace") or namespace
    for value in (namespace, name):
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value
        ):
            raise ValueError("invalid network identity")
    return namespace, name


def network_attachments(dep):
    """Read Multus text/JSON selections without logging annotation contents."""
    namespace, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    raw = dep["spec"]["template"].get("metadata", {}).get("annotations", {}).get(
        "k8s.v1.cni.cncf.io/networks", ""
    )
    try:
        raw = raw.strip()
        if not raw:
            return set()
        if raw.startswith(("[", "{")):
            items = json.loads(raw)
            if not isinstance(items, list):
                raise TypeError("expected network selection array")
        else:
            items = [_text_attachment(value) for value in raw.split(",")]
        return {_attachment_identity(item, namespace) for item in items}
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError(f"{namespace}/{name}: Invalid Multus network selection") from None


def preflight_networks(kc, dep):
    for namespace, name in sorted(network_attachments(dep)):
        value = oc(kc, "get", "network-attachment-definitions.k8s.cni.cncf.io", name,
                   "-n", namespace, "--ignore-not-found", "-o", "json")
        state = "missing" if not value else ""
        if value and json.loads(value)["metadata"].get("deletionTimestamp"):
            state = "terminating"
        if state:
            target = f"{dep['metadata']['namespace']}/{dep['metadata']['name']}"
            raise RuntimeError(
                f"{target}: {state} network attachment {namespace}/{name}; "
                "restore its intended network before releasing; release will not recreate it"
            )


def verify_live(kc, namespace, name, expected):
    dep = read_deployment(kc, namespace, name)
    pods = json.loads(oc(kc, "get", "pods", "-n", namespace, "-o", "json"))["items"]
    verify_deployment(dep, pods, expected)


def wait_rollout(kc, namespace, name):
    # oc's watch handles the wait; no first-pod sampling or sleep polling.
    run(
        [
            "oc",
            "rollout",
            "status",
            f"deployment/{name}",
            "-n",
            namespace,
            f"--kubeconfig={kc}",
            "--timeout=300s",
        ],
        timeout=330,
    )


def sync_operator_identity(kc, dep):
    """Sync the operator's identity and singleton Recreate rollout contract."""
    operators = [container for container in dep["spec"]["template"]["spec"]["containers"]
                 if image_role(container["image"]) == "operator"]
    if not operators:
        return False
    wanted = [{"name": key, "valueFrom": {"fieldRef": {"fieldPath": path}}} for key, path in (
        ("POD_NAME", "metadata.name"), ("POD_NAMESPACE", "metadata.namespace"), ("POD_UID", "metadata.uid"))]
    containers = []
    for container in operators:
        current = {entry["name"]: entry for entry in container.get("env", [])}
        if any(not _identity_env_matches(current.get(entry["name"]), entry) for entry in wanted):
            containers.append({"name": container["name"], "env": [{**entry, "value": None} for entry in wanted]})
    spec = {}
    if dep["spec"].get("replicas", 1) != 1:
        spec["replicas"] = 1
    strategy = dep["spec"].get("strategy", {})
    if strategy.get("type", "RollingUpdate") != "Recreate" or "rollingUpdate" in strategy:
        spec["strategy"] = {"type": "Recreate", "rollingUpdate": None}
    if containers:
        spec["template"] = {"spec": {"containers": containers}}
    if not spec:
        return False
    patch = {"spec": spec}
    oc(kc, "patch", "deployment/" + dep["metadata"]["name"], "-n", dep["metadata"]["namespace"],
       "--type=strategic", "-p", json.dumps(patch))
    return True


def _identity_env_matches(current, wanted):
    if not current or current.get("value"):
        return False
    expected = wanted["valueFrom"]["fieldRef"]["fieldPath"]
    reference = current.get("valueFrom", {}).get("fieldRef", {})
    return reference.get("fieldPath") == expected and reference.get("apiVersion", "v1") == "v1"


def update_deployment(kc, dep, expected, image_tag="production"):
    namespace, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    dep = read_deployment(kc, namespace, name)
    preflight_networks(kc, dep)
    identity_changed = sync_operator_identity(kc, dep)
    try:
        verify_live(kc, namespace, name, expected)
        if not identity_changed:
            return
    except RuntimeError:
        pass  # Read/API failures still propagate; only a stale rollout is repaired.
    replacements = [
        f"{c['name']}={REGISTRY}{image_role(c['image'])}:{image_tag}"
        for c in dep["spec"]["template"]["spec"]["containers"]
        if image_role(c["image"])
    ]
    oc(kc, "set", "image", f"deployment/{name}", "-n", namespace, *replacements)
    oc(kc, "rollout", "restart", f"deployment/{name}", "-n", namespace)
    wait_rollout(kc, namespace, name)
    verify_live(kc, namespace, name, expected)


def cluster_targets(kc, skip_project_pods=False, roles=None):
    wanted = set(roles or CLUSTER_ROLLOUT_ROLES) & set(CLUSTER_ROLLOUT_ROLES)
    deployments = json.loads(oc(kc, "get", "deployments", "-A", "-o", "json"))["items"]
    targets = []
    for dep in deployments:
        namespace = dep["metadata"]["namespace"]
        if namespace != "troshka-operator" and (
            skip_project_pods or not namespace.startswith("troshka-")
        ):
            continue
        dep_roles = {
            image_role(c["image"])
            for c in dep["spec"]["template"]["spec"]["containers"]
        }
        if dep_roles & wanted:
            targets.append(dep)
    if "operator" in wanted and not any(
        d["metadata"]["name"] == "troshka-operator" for d in targets
    ):
        raise RuntimeError("Operator deployment missing")
    return targets


def preflight(kc, skip_project_pods=False, roles=None):
    targets = cluster_targets(kc, skip_project_pods, roles)
    if not targets:
        print(f"{Path(kc).name}: no cluster targets for selected roles", flush=True)
        return
    for dep in targets:
        preflight_networks(kc, dep)
    print(f"{Path(kc).name}: helper network dependencies present", flush=True)


def cluster(
    kc,
    expected,
    skip_project_pods=False,
    verify_only=False,
    image_tag="production",
    roles=None,
):
    targets = cluster_targets(kc, skip_project_pods, roles)
    if not targets:
        print(f"{Path(kc).name}: skipped (no deployments use changed roles)", flush=True)
        return
    # Fail the entire cluster before scheduling even one rollout mutation.
    for dep in targets:
        preflight_networks(kc, dep)
    failures = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(
                verify_live,
                kc,
                d["metadata"]["namespace"],
                d["metadata"]["name"],
                expected,
            )
            if verify_only
            else pool.submit(update_deployment, kc, d, expected, image_tag): d
            for d in targets
        }
        for future, dep in futures.items():
            try:
                future.result()
            except (RuntimeError, subprocess.SubprocessError) as error:
                failures.append(
                    f"{dep['metadata']['namespace']}/{dep['metadata']['name']}: {error}"
                )
    if failures:
        raise RuntimeError("\n".join(failures))
    print(f"{Path(kc).name}: verified all {len(targets)} deployments", flush=True)


def idle(kc):
    code = (
        "import json; from app.core.redis import get_redis_raw; "
        "from rq import Queue; from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry; "
        'r=get_redis_raw(); print(json.dumps([{"queue":q.name,"queued":q.count,'
        '"started":len(StartedJobRegistry(q.name,connection=r).get_job_ids(cleanup=False)),'
        '"deferred":len(DeferredJobRegistry(q.name,connection=r).get_job_ids(cleanup=False)),'
        '"scheduled":len(ScheduledJobRegistry(q.name,connection=r).get_job_ids(cleanup=False))}'
        ' for q in Queue.all(connection=r)]))'
    )
    queues = json.loads(
        oc(
            kc,
            "exec",
            "deployment/troshka-backend",
            "-n",
            "troshka",
            "--",
            "python3",
            "-c",
            code,
        )
    )
    if any(q["queued"] or q["started"] or q["deferred"] or q["scheduled"] for q in queues):
        raise RuntimeError(
            "Jobs are active, queued, deferred or scheduled; reconcile them before promotion"
        )
    print("Job queues are idle", flush=True)


def application(kc, expected, namespace="troshka", image_tag="production", roles=None):
    selected = set(roles or APP_ROLES) & set(APP_ROLES)
    targets = [
        ("backend", "backend"),
        ("frontend", "frontend"),
        ("worker", "backend"),
        ("tunnel", "backend"),
    ]
    for name in ("collection-worker", "readiness-worker"):
        if oc(kc, "get", "deployment", f"troshka-{name}", "-n", namespace, "--ignore-not-found", "-o", "name"):
            targets.append((name, "backend"))
    targets = [(name, role) for name, role in targets if role in selected]
    if not targets:
        print(f"{namespace}: skipped app waits (no changed app roles)", flush=True)
        return
    for name, role in targets:
        deployment = f"troshka-{name}"
        image = f"{REGISTRY}{role}:{image_tag}@{expected[role]}"
        run(
            [
                "oc",
                "wait",
                f"deployment/{deployment}",
                "-n",
                namespace,
                f"--for=jsonpath={{.spec.template.spec.containers[0].image}}={image}",
                "--timeout=600s",
                f"--kubeconfig={kc}",
            ],
            timeout=630,
        )
        wait_rollout(kc, namespace, deployment)
        verify_live(kc, namespace, deployment, expected)
        print(f"{deployment}: all replicas ready on the release digest", flush=True)
    if "backend" not in selected:
        print("Runtime schema/health checks skipped (backend unchanged)", flush=True)
        return
    # Compare the actual packaged schema, not just the deployment's image field.
    schema = oc(
        kc,
        "exec",
        "deployment/troshka-backend",
        "-n",
        namespace,
        "--",
        "python3",
        "-c",
        "import json; from app.services.template_schema import load_template_schema; print(json.dumps(load_template_schema()))",
    )
    local = (
        Path(__file__).resolve().parents[1]
        / "src/backend/schemas/troshka-template.schema.json"
    )
    if json.loads(schema) != json.loads(local.read_text()):
        raise RuntimeError("Runtime template schema differs from the release checkout")
    health = oc(
        kc,
        "exec",
        "deployment/troshka-backend",
        "-n",
        namespace,
        "--",
        "python3",
        "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8200/api/v1/health',timeout=15).status)",
    )
    if health != "200":
        raise RuntimeError("Backend health check failed")
    for field, value in (("sync.status", "Synced"), ("health.status", "Healthy"), ("operationState.phase", "Succeeded")):
        oc(kc, "wait", f"applications.argoproj.io/{namespace}", "-n", "openshift-gitops",
           f"--for=jsonpath={{.status.{field}}}={value}", "--timeout=600s", timeout=630)
    print("Runtime schema matches; backend health HTTP 200", flush=True)


def application_release_action(args, parser):
    if not args.tag or not re.fullmatch(r"v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", args.tag):
        parser.error("a named release --tag starting with v is required")
    roles = parse_roles(args.roles) if args.roles is not None else None
    if args.action == "stage-dev":
        stage_dev_images(args.tag, roles)
    else:
        pin_application_release(args.kubeconfig, args.namespace, args.tag)


def release_plan(tag, baseline="production"):
    """Summarize which image roles and CRD/RBAC paths changed for this release."""
    previous = previous_release_tag(tag)
    _, changed = roles_changed_since(tag, baseline)
    return {
        "tag": tag,
        "previous_tag": previous,
        "baseline": baseline,
        "changed_roles": changed,
        "unchanged_roles": [role for role in IMAGES if role not in changed],
        "operator_roles_changed": [
            role for role in changed if role in OPERATOR_ROLES
        ],
        "app_roles_changed": [role for role in changed if role in APP_ROLES],
        "cluster_roles_changed": [
            role for role in changed if role in CLUSTER_ROLLOUT_ROLES
        ],
        "apply_crds_rbac": git_paths_changed(previous, "HEAD", CRD_RBAC_PATHS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "promote",
            "images",
            "cluster",
            "application",
            "idle",
            "stage-dev",
            "pin-application",
            "preflight",
            "plan",
        ),
    )
    parser.add_argument("--tag")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--namespace", default="troshka")
    parser.add_argument("--image-tag", default="production")
    parser.add_argument("--skip-project-pods", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--roles",
        help="Comma-separated image roles to act on (default: all relevant roles)",
    )
    parser.add_argument(
        "--baseline",
        default="production",
        help="Tag/digest baseline for plan/promote skip decisions",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", args.image_tag):
        parser.error("--image-tag must be a valid container tag")
    roles = parse_roles(args.roles) if args.roles is not None else None
    if args.action in ("stage-dev", "pin-application"):
        application_release_action(args, parser)
        return
    if args.action == "plan":
        if not args.tag or args.tag in ("latest", "production"):
            parser.error("a named release --tag is required")
        plan = release_plan(args.tag, args.baseline)
        text = json.dumps(plan, indent=2)
        if args.state:
            args.state.write_text(text)
        print(text, flush=True)
        return
    if args.action in ("promote", "images"):
        if not args.tag or args.tag in ("latest", "production"):
            parser.error("a named release --tag is required")
        values = (
            promote(args.tag, roles)
            if args.action == "promote"
            else release_digests(args.tag)
        )
        if args.state:
            args.state.write_text(json.dumps(values))
        return
    if args.action == "idle":
        idle(args.kubeconfig)
        return
    if args.action == "preflight":
        preflight(args.kubeconfig, args.skip_project_pods, roles)
        return
    expected = json.loads(args.state.read_text())
    if args.action == "cluster":
        cluster(
            args.kubeconfig,
            expected,
            args.skip_project_pods,
            args.verify_only,
            args.image_tag,
            roles,
        )
    else:
        application(
            args.kubeconfig, expected, args.namespace, args.image_tag, roles
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)
