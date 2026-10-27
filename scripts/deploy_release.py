"""Release promotion and rollout verification used by deploy-full.sh."""

import argparse
import json
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


def release_digests(tag):
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(IMAGES, pool.map(lambda role: digest(role, tag), IMAGES)))


def promote(tag):
    # Resolve every source before mutating any destination, including transient
    # job images that do not have persistent Deployments to restart.
    expected = release_digests(tag)
    for role in IMAGES:
        source = f"docker://{REGISTRY}{role}:{tag}"
        target = f"docker://{REGISTRY}{role}:production"
        run(["skopeo", "copy", "--all", source, target], timeout=600)
        if digest(role, "production") != expected[role]:
            raise RuntimeError(f"Production digest mismatch for {role}")
        print(f"  {role}: {tag} promoted and verified", flush=True)
    return expected


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


def update_deployment(kc, dep, expected):
    namespace, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    try:
        verify_live(kc, namespace, name, expected)
        return
    except RuntimeError:
        pass  # Read/API failures still propagate; only a stale rollout is repaired.
    replacements = [
        f"{c['name']}={REGISTRY}{image_role(c['image'])}:production"
        for c in dep["spec"]["template"]["spec"]["containers"]
        if image_role(c["image"])
    ]
    oc(kc, "set", "image", f"deployment/{name}", "-n", namespace, *replacements)
    oc(kc, "rollout", "restart", f"deployment/{name}", "-n", namespace)
    wait_rollout(kc, namespace, name)
    verify_live(kc, namespace, name, expected)


def cluster(kc, expected, skip_project_pods=False, verify_only=False):
    deployments = json.loads(oc(kc, "get", "deployments", "-A", "-o", "json"))["items"]
    targets = []
    for dep in deployments:
        namespace = dep["metadata"]["namespace"]
        if namespace != "troshka-operator" and (
            skip_project_pods or not namespace.startswith("troshka-")
        ):
            continue
        roles = {
            image_role(c["image"])
            for c in dep["spec"]["template"]["spec"]["containers"]
        }
        if roles & {"operator", "dnsmasq", "gateway", "bmc", "vnc-proxy"}:
            targets.append(dep)
    if not any(d["metadata"]["name"] == "troshka-operator" for d in targets):
        raise RuntimeError("Operator deployment missing")
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
            else pool.submit(update_deployment, kc, d, expected): d
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
        "from rq import Queue; from rq.registry import StartedJobRegistry; "
        'r=get_redis_raw(); print(json.dumps([{"queue":q.name,"queued":q.count,'
        '"started":StartedJobRegistry(q.name,connection=r).count} for q in Queue.all(connection=r)]))'
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
    if any(q["queued"] or q["started"] for q in queues):
        raise RuntimeError(
            "Jobs are active or queued; wait for them to finish before promotion"
        )
    print("Job queues are idle", flush=True)


def application(kc, expected):
    for name, role in (
        ("backend", "backend"),
        ("frontend", "frontend"),
        ("worker", "backend"),
        ("tunnel", "backend"),
    ):
        deployment = f"troshka-{name}"
        image = f"{REGISTRY}{role}:production@{expected[role]}"
        run(
            [
                "oc",
                "wait",
                f"deployment/{deployment}",
                "-n",
                "troshka",
                f"--for=jsonpath={{.spec.template.spec.containers[0].image}}={image}",
                "--timeout=600s",
                f"--kubeconfig={kc}",
            ],
            timeout=630,
        )
        wait_rollout(kc, "troshka", deployment)
        verify_live(kc, "troshka", deployment, expected)
        print(f"{deployment}: all replicas ready on the release digest", flush=True)
    # Compare the actual packaged schema, not just the deployment's image field.
    schema = oc(
        kc,
        "exec",
        "deployment/troshka-backend",
        "-n",
        "troshka",
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
        "troshka",
        "--",
        "python3",
        "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8200/api/v1/health',timeout=15).status)",
    )
    if health != "200":
        raise RuntimeError("Backend health check failed")
    print("Runtime schema matches; backend health HTTP 200", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("promote", "images", "cluster", "application", "idle")
    )
    parser.add_argument("--tag")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--skip-project-pods", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.action in ("promote", "images"):
        if not args.tag or args.tag in ("latest", "production"):
            parser.error("a named release --tag is required")
        values = (
            promote(args.tag) if args.action == "promote" else release_digests(args.tag)
        )
        if args.state:
            args.state.write_text(json.dumps(values))
        return
    if args.action == "idle":
        idle(args.kubeconfig)
        return
    expected = json.loads(args.state.read_text())
    if args.action == "cluster":
        cluster(args.kubeconfig, expected, args.skip_project_pods, args.verify_only)
    else:
        application(args.kubeconfig, expected)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)
