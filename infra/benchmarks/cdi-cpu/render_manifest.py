"""Render benchmark resources as JSON; never contact a cluster or S3."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def metadata(name, namespace, labels):
    return {"name": name, "namespace": namespace, "labels": labels}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "namespace",
        "name",
        "importer-image",
        "controller-image",
        "endpoint",
        "bucket",
        "secret",
        "storage-class",
        "scratch-storage-class",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--node", help="Pin every pod to this node; otherwise use the controller's node"
    )
    parser.add_argument("--cpu-limits", default="750m,2,4,10,10,4,2,750m")
    parser.add_argument("--access-key-field", default="accessKeyId")
    parser.add_argument("--secret-key-field", default="secretKey")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,43}[a-z0-9])?", args.name):
        parser.error("--name must be a DNS label of at most 45 characters")
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        parser.error("--endpoint must be an HTTP(S) S3 service URL")
    if (
        endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/"}
    ):
        parser.error(
            "--endpoint must not contain credentials, paths, queries or fragments"
        )
    args.cpu_limits = args.cpu_limits.split(",")
    if not args.cpu_limits or any(
        not re.fullmatch(r"(?:[1-9][0-9]*m?|0\.[0-9]+)", value)
        for value in args.cpu_limits
    ):
        parser.error(
            "--cpu-limits must contain positive CPU quantities separated by commas"
        )
    return args


def render(args):
    name, namespace = args.name, args.namespace
    labels = {"troshka.io/benchmark": name}
    meta = lambda suffix="": metadata(name + suffix, namespace, labels)
    secret_env = [
        {
            "name": variable,
            "valueFrom": {"secretKeyRef": {"name": args.secret, "key": key}},
        }
        for variable, key in (
            ("IMPORTER_ACCESS_KEY_ID", args.access_key_field),
            ("IMPORTER_SECRET_KEY", args.secret_key_field),
        )
    ]
    settings = {
        "image": args.importer_image,
        "endpoint": args.endpoint.rstrip("/"),
        "bucket": args.bucket,
        "key": f"benchmarks/{name}/fully-populated-2g.qcow2",
        "data_class": args.storage_class,
        "scratch_class": args.scratch_storage_class,
        "cpu_limits": args.cpu_limits,
        "env": [
            {"name": "IMPORTER_SOURCE", "value": "s3"},
            {"name": "IMPORTER_CONTENTTYPE", "value": "kubevirt"},
            {"name": "FILESYSTEM_OVERHEAD", "value": "0.06"},
            {"name": "INSECURE_TLS", "value": "false"},
            {"name": "PREALLOCATION", "value": "false"},
        ]
        + secret_env,
    }
    source = Path(__file__).parent
    scripts = {
        filename: (source / filename).read_text()
        for filename in ("controller.py", "import_wrapper.py", "generate_disk.sh")
    }
    scripts["settings.json"] = json.dumps(settings)
    pod_spec = {
        "serviceAccountName": name,
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "controller",
                "image": args.controller_image,
                "command": ["python3", "-u", "/scripts/controller.py"],
                "env": [
                    {"name": "NAMESPACE", "value": namespace},
                    {"name": "BENCHMARK_NAME", "value": name},
                    {
                        "name": "NODE_NAME",
                        "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
                    },
                ]
                + secret_env,
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                },
                "volumeMounts": [
                    {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                    {"name": "source", "mountPath": "/benchmark"},
                ],
            }
        ],
        "volumes": [
            {"name": "scripts", "configMap": {"name": name + "-scripts"}},
            {
                "name": "source",
                "persistentVolumeClaim": {"claimName": name + "-source"},
            },
        ],
    }
    if args.node:
        pod_spec["nodeName"] = args.node
    resources = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta()},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": meta(),
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "persistentvolumeclaims", "configmaps"],
                    "verbs": ["get", "list", "watch", "create", "patch", "delete"],
                },
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": meta(),
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": name, "namespace": namespace}
            ],
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": meta("-source"),
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "storageClassName": args.storage_class,
                "resources": {"requests": {"storage": "4Gi"}},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta("-scripts"),
            "data": scripts,
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": meta(),
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 7200,
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        },
    ]
    return {"apiVersion": "v1", "kind": "List", "items": resources}


if __name__ == "__main__":
    options = parse_args()
    options.output.write_text(json.dumps(render(options), indent=2) + "\n")
