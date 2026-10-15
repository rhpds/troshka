"""Durable controller for isolated 2GiB CDI CPU comparisons.

Requires boto3 and kubernetes in the controller image. Credentials arrive only
through Kubernetes Secret references rendered by render_manifest.py.
"""

import datetime
import json
import os
import time
import traceback
from pathlib import Path

import boto3
from botocore.config import Config
from kubernetes import client, config

config.load_incluster_config()
api = client.CoreV1Api()
ns = os.environ["NAMESPACE"]
prefix = os.environ["BENCHMARK_NAME"]
node = os.environ["NODE_NAME"]
settings = json.loads(Path("/scripts/settings.json").read_text())
label = {"troshka.io/benchmark": prefix}
results = {
    "benchmark": prefix,
    "node": node,
    "virtual_bytes": 2147483648,
    "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "image": settings["image"],
    "runs": [],
    "status": "running",
}
logs = {}
owned_pods = []
owned_pvcs = []
s3 = boto3.client(
    "s3",
    endpoint_url=settings["endpoint"],
    aws_access_key_id=os.environ["IMPORTER_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["IMPORTER_SECRET_KEY"],
    region_name="us-east-1",
    config=Config(signature_version="s3v4"),
)


def persist():
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": prefix + "-results", "namespace": ns, "labels": label},
        "data": {"results.json": json.dumps(results, indent=2), **logs},
    }
    try:
        api.create_namespaced_config_map(ns, body)
    except client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise
        api.patch_namespaced_config_map(prefix + "-results", ns, body)
    print(json.dumps(results), flush=True)


def pvc(name, size, storage):
    api.create_namespaced_persistent_volume_claim(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "labels": label},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Filesystem",
                "storageClassName": storage,
                "resources": {"requests": {"storage": size}},
            },
        },
    )
    owned_pvcs.append(name)


def wait_pod(name, timeout=600):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        pod = api.read_namespaced_pod(name, ns)
        if pod.status.phase in ("Succeeded", "Failed"):
            output = api.read_namespaced_pod_log(name, ns, timestamps=True)
            logs[name + ".log"] = output
            logs[name + ".status.json"] = json.dumps(
                api.api_client.sanitize_for_serialization(pod.status)
            )
            persist()
            if pod.status.phase != "Succeeded":
                raise RuntimeError("Benchmark pod failed: " + name)
            return output
        time.sleep(2)
    raise TimeoutError("Benchmark pod timed out: " + name)


def pod(name, container, volumes):
    api.create_namespaced_pod(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": label},
            "spec": {
                "restartPolicy": "Never",
                "nodeName": node,
                "activeDeadlineSeconds": 650,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container],
                "volumes": volumes,
            },
        },
    )
    owned_pods.append(name)


def remove(name, kind):
    try:
        if kind == "pod":
            api.delete_namespaced_pod(name, ns)
        else:
            api.delete_namespaced_persistent_volume_claim(name, ns)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


try:
    persist()
    generator = prefix + "-generate"
    pod(
        generator,
        {
            "name": "generator",
            "image": settings["image"],
            "command": ["sh", "/scripts/generate_disk.sh"],
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "2", "memory": "512Mi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "runAsNonRoot": True,
            },
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
            ],
        },
        [
            {
                "name": "data",
                "persistentVolumeClaim": {"claimName": prefix + "-source"},
            },
            {"name": "scripts", "configMap": {"name": prefix + "-scripts"}},
        ],
    )
    wait_pod(generator)
    path = Path("/benchmark/benchmark.qcow2")
    results["object_bytes"] = path.stat().st_size
    results["object_key"] = settings["key"]
    s3.upload_file(str(path), settings["bucket"], settings["key"])
    results["uploaded_bytes"] = s3.head_object(
        Bucket=settings["bucket"], Key=settings["key"]
    )["ContentLength"]
    persist()
    remove(generator, "pod")
    for index, cpu in enumerate(
        settings.get("cpu_limits", ["750m", "2", "2", "750m"]), 1
    ):
        name = prefix + "-run" + str(index)
        pvc(name + "-data", "3Gi", settings["data_class"])
        pvc(name + "-scratch", "4Gi", settings["scratch_class"])
        env = [
            e
            for e in settings["env"]
            if e["name"]
            not in ("IMPORTER_ENDPOINT", "IMPORTER_IMAGE_SIZE", "OWNER_UID")
        ]
        env += [
            {
                "name": "IMPORTER_ENDPOINT",
                "value": settings["endpoint"]
                + "/"
                + settings["bucket"]
                + "/"
                + settings["key"],
            },
            {"name": "IMPORTER_IMAGE_SIZE", "value": "3Gi"},
            {"name": "OWNER_UID", "value": prefix + "-" + str(index)},
            {"name": "BENCHMARK_SOURCE_BYTES", "value": str(results["object_bytes"])},
        ]
        started = time.monotonic()
        pod(
            name,
            {
                "name": "importer",
                "image": settings["image"],
                "command": ["python3", "/scripts/import_wrapper.py"],
                "env": env,
                "resources": {
                    "requests": {"cpu": "100m", "memory": "60M"},
                    "limits": {"cpu": cpu, "memory": "600M"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": "/data"},
                    {"name": "scratch", "mountPath": "/scratch"},
                    {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                ],
            },
            [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": name + "-data"},
                },
                {
                    "name": "scratch",
                    "persistentVolumeClaim": {"claimName": name + "-scratch"},
                },
                {"name": "scripts", "configMap": {"name": prefix + "-scripts"}},
            ],
        )
        output = wait_pod(name)
        metrics = [
            json.loads(line.split("BENCHMARK_METRICS ", 1)[1])
            for line in output.splitlines()
            if "BENCHMARK_METRICS " in line
        ][-1]
        metrics.update(
            {
                "run": index,
                "cpu_limit": cpu,
                "pod": name,
                "elapsed_including_scheduling_seconds": time.monotonic() - started,
            }
        )
        results["runs"].append(metrics)
        persist()
        remove(name, "pod")
        remove(name + "-data", "pvc")
        remove(name + "-scratch", "pvc")
    results["status"] = "complete"
except Exception as exc:  # noqa: BLE001 - persist failures before cleanup and nonzero exit
    results["status"] = "failed"
    results["error"] = str(exc)
    traceback.print_exc()
finally:
    for name in owned_pods:
        remove(name, "pod")
    for name in owned_pvcs:
        remove(name, "pvc")
    s3.delete_object(Bucket=settings["bucket"], Key=settings["key"])
    Path("/benchmark/benchmark.qcow2").unlink(missing_ok=True)
    remove(prefix + "-source", "pvc")
    results["cleanup"] = (
        "benchmark importer/generator pods, PVCs, and test S3 object deleted; results ConfigMap retained"
    )
    results["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    persist()
if results["status"] != "complete":
    raise SystemExit(1)
