"""Render and install explicitly scoped collector access, never discover new scope."""

import argparse
import copy
import json
from pathlib import Path

from deploy_release import oc

NAME = "troshka-storage-collector"
API = "rbac.authorization.k8s.io/v1"
TEMPLATE = Path(__file__).resolve().parents[1] / "infra/storage-collector-rbac.yaml"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(kubeconfig, *args):
    return json.loads(oc(kubeconfig, "get", *args, "-o", "json"))


def validate_domain(domain):
    """Reject replaced namespaces, project roots, clusters, or toolbox pods."""
    kc = domain["backend"]["kubeconfig"]
    pins = domain["pins"]
    identity = read(kc, "namespace", "kube-system")["metadata"]["uid"]
    require(
        identity == pins["provider_clusters"][0]["cluster_key"],
        "Cluster identity changed",
    )
    owners = {}
    for root in pins["project_roots"]:
        ns = root["namespace"]
        namespace = read(kc, "namespace", ns)
        require(
            namespace["metadata"]["uid"] == root["namespace_uid"],
            "Project namespace replaced",
        )
        projects = read(kc, "troshkaprojects", "-n", ns)["items"]
        require(len(projects) == 1, "Expected exactly one project root")
        project = projects[0]
        require(
            project["metadata"]["uid"] == root["project_cr_uid"],
            "Project root replaced",
        )
        require(
            project["spec"]["projectId"] == root["project_id"],
            "Project identity changed",
        )
        require(not project["metadata"].get("deletionTimestamp"), "Project is deleting")
        owners[ns] = {
            "apiVersion": project["apiVersion"],
            "kind": project["kind"],
            "name": project["metadata"]["name"],
            "uid": root["project_cr_uid"],
            "controller": True,
            "blockOwnerDeletion": False,
        }
    for cache in pins["cache_roots"]:
        namespace = read(kc, "namespace", cache["namespace"])
        require(
            namespace["metadata"]["uid"] == cache["namespace_uid"],
            "Cache namespace replaced",
        )
    toolbox = domain["backend"]["toolbox"]
    pod = read(kc, "pod", toolbox["pod_name"], "-n", toolbox["namespace"])
    require(
        pod["metadata"]["uid"] == toolbox["pod_uid"],
        "Toolbox pod replaced; review new access",
    )
    require(not pod["metadata"].get("deletionTimestamp"), "Toolbox is deleting")
    return owners


def binding(kind, name, namespace=None, owner=None):
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    if owner:
        metadata["ownerReferences"] = [owner]
    return {
        "apiVersion": API,
        "kind": kind + "Binding",
        "metadata": metadata,
        "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": "troshka"}],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": kind,
            "name": name,
        },
    }


def render(domain, owners):
    template = json.loads(
        TEMPLATE.read_text()
    )  # JSON is a YAML subset; no extra CLI dependency.
    caches = {r["namespace"] for r in domain["pins"]["cache_roots"]}
    namespaces = sorted(set(owners) | caches)
    toolbox = domain["backend"]["toolbox"]
    inventory_name = NAME + "-inventory"
    objects = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": NAME, "namespace": "troshka"},
            "automountServiceAccountToken": False,
        }
    ]
    rules = [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "resourceNames": sorted(
                set(namespaces) | {"kube-system", toolbox["namespace"]}
            ),
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["persistentvolumes"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["snapshot.storage.k8s.io"],
            "resources": ["volumesnapshotcontents"],
            "verbs": ["get", "list"],
        },
    ]
    objects.extend(
        [
            {
                "apiVersion": API,
                "kind": "ClusterRole",
                "metadata": {"name": inventory_name},
                "rules": rules,
            },
            binding("ClusterRole", inventory_name),
        ]
    )
    for ns in namespaces:
        role = copy.deepcopy(template)
        role["metadata"]["namespace"] = ns
        if ns not in owners:
            for rule in role["rules"]:
                rule["resources"] = [
                    resource
                    for resource in rule["resources"]
                    if resource not in {"secrets", "configmaps"}
                ]
        if ns in owners:
            role["metadata"]["ownerReferences"] = [owners[ns]]
        objects.extend([role, binding("Role", NAME, ns, owners.get(ns))])
    toolbox_name = NAME + "-toolbox"
    rules = [
        {
            "apiGroups": [""],
            "resources": [resource],
            "resourceNames": [toolbox["pod_name"]],
            "verbs": [verb],
        }
        for resource, verb in (("pods", "get"), ("pods/exec", "create"))
    ]
    objects.extend(
        [
            {
                "apiVersion": API,
                "kind": "Role",
                "metadata": {"name": toolbox_name, "namespace": toolbox["namespace"]},
                "rules": rules,
            },
            binding("Role", toolbox_name, toolbox["namespace"]),
        ]
    )
    return {"apiVersion": "v1", "kind": "List", "items": objects}


def prepare(plan, output):
    require(bool(plan["domains"]), "Collector access plan is empty")
    clusters = [
        d["pins"]["provider_clusters"][0]["cluster_key"] for d in plan["domains"]
    ]
    require(
        len(clusters) == len(set(clusters)), "Combine scopes into one entry per cluster"
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for index, domain in enumerate(plan["domains"]):
        owners = validate_domain(domain)
        path = output / f"{index}.json"
        path.write_text(json.dumps(render(domain, owners), indent=2) + "\n")
        oc(
            domain["backend"]["kubeconfig"],
            "apply",
            "--dry-run=server",
            "-f",
            str(path),
        )
    (output / "plan.json").write_text(json.dumps(plan, indent=2))


def apply(output):
    plan = json.loads((output / "plan.json").read_text())
    # Validate every target again before the first access mutation.
    for index, domain in enumerate(plan["domains"]):
        expected = render(domain, validate_domain(domain))
        require(
            expected == json.loads((output / f"{index}.json").read_text()),
            "Prepared access changed",
        )
    for index, domain in enumerate(plan["domains"]):
        print(
            oc(
                domain["backend"]["kubeconfig"],
                "apply",
                "-f",
                str(output / f"{index}.json"),
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply(args.output)
    else:
        require(args.plan is not None, "--plan is required when preparing access")
        prepare(json.loads(args.plan.read_text()), args.output)


if __name__ == "__main__":
    main()
