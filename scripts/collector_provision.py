"""Install approved collector profiles and rotate dedicated static credentials.

The plan names application and storage-cluster kubeconfigs. Existing profiles
cannot be replaced. Preparation is read-only; apply journals mutations and never
retries an ambiguous attempt. Output directories contain credentials: keep private.
"""

import argparse
import base64
import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

from collector_fleet_preflight import inspect


def require(condition, message):
    if not condition:
        raise ValueError(message)


def oc(kc, *args, payload=None, timeout=90):
    result = subprocess.run(
        ["oc", f"--kubeconfig={kc}", "--request-timeout=30s", *args],
        input=payload, text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError("cluster command failed; inspect private deployment state")
    return result.stdout


def native_settings(app):
    helm = app["spec"]["source"]["helm"]
    require(not any(p["name"].startswith("nativeCollection") for p in helm.get("parameters", [])),
            "nativeCollection Helm parameter overrides require explicit reconciliation")
    return helm["valuesObject"]["nativeCollection"]


def merge_profiles(existing, additions):
    result = copy.deepcopy(existing)
    seen = {p["domain_id"] for p in existing}
    require(len(seen) == len(existing), "duplicate existing profile")
    for profile in additions:
        require(profile["domain_id"] not in seen, "cannot replace an existing profile")
        seen.add(profile["domain_id"])
        result.append(copy.deepcopy(profile))
    return result


def collector_transport(cluster, token):
    require(cluster.get("server", "").startswith("https://")
            and not cluster.get("insecure-skip-tls-verify")
            and not cluster.get("proxy-url"), "verified direct TLS transport required")
    require(not cluster.get("certificate-authority"), "CA must be embedded")
    selected = {key: cluster[key] for key in
                ("server", "certificate-authority-data", "tls-server-name") if key in cluster}
    return {"apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "target", "cluster": selected}],
            "users": [{"name": "collector", "user": {"token": token}}],
            "contexts": [{"name": "collector", "context": {"cluster": "target", "user": "collector"}}],
            "current-context": "collector"}


def settings_patch(app, profiles, config_name, secret_name):
    native = copy.deepcopy(native_settings(app))
    require(native.get("worker", {}).get("replicas", 0) > 0, "existing collection workers required")
    native["profiles"] = profiles
    native["worker"].update(configMapName=config_name, secretName=secret_name)
    return [{"op": "test", "path": "/metadata/resourceVersion", "value": app["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/spec/source/helm/valuesObject/nativeCollection", "value": native}]


def config_key(path, prefix):
    parsed = PurePosixPath(path)
    require(str(parsed.parent) == prefix and re.fullmatch(r"[A-Za-z0-9_.-]+", parsed.name),
            "profile path must name one protected file")
    return parsed.name


def additions(entry, data):
    profiles = []
    for item in entry.get("additions", []):
        profile = item["profile"]
        for field in ("pins", "inventory", "backend_config"):
            key = config_key(profile[field], "/etc/troshka/collection")
            require(key not in data, "cannot overwrite existing protected configuration")
            content = Path(item["files"][field]).read_text()
            json.loads(content)
            data[key] = content
        profiles.append(profile)
    return profiles


def verify_coverage(snapshot, profiles, data, transports):
    require(not snapshot["blockers"], "current protected profiles are invalid")
    require(all(p["accepted"] and p["enrolled"] for p in snapshot["profiles"]),
            "existing profiles require valid enrollment anchors")
    providers, keys = [], set()
    for profile in profiles:
        pins = json.loads(data[config_key(profile["pins"], "/etc/troshka/collection")])
        require(str(pins["domain_id"]) == profile["domain_id"] and pins.get("initialization_evidence"),
                "approved matching domain pins required")
        require(len(pins["provider_clusters"]) == 1, "one cluster per protected profile required")
        providers.append(pins["provider_clusters"][0]["provider_id"])
        keys.add(config_key(profile["kubeconfig"], "/etc/troshka/collection-transport"))
    expected = {h["provider_id"] for h in snapshot["hosts"]}
    require(None not in expected and set(providers) == expected and len(providers) == len(expected),
            "approved profiles must cover every configured native host exactly once")
    require(keys == set(transports), "explicit transport required for every profile")


def prepare_entry(entry):
    kc, ns = entry["kubeconfig"], entry["namespace"]
    app = json.loads(oc(kc, "get", "applications.argoproj.io", entry["application"], "-n", "openshift-gitops", "-o", "json"))
    require(app["spec"]["destination"]["namespace"] == ns, "application namespace mismatch")
    native = native_settings(app)
    cm = json.loads(oc(kc, "get", "configmap", native["worker"]["configMapName"], "-n", ns, "-o", "json"))
    data = dict(cm.get("data", {}))
    profiles = merge_profiles(native["profiles"], additions(entry, data))
    transports = entry["transports"]
    verify_coverage(inspect(kc, ns), profiles, data, transports)
    prepared_transports = {}
    for profile in profiles:
        pins = json.loads(data[config_key(profile["pins"], "/etc/troshka/collection")])
        key = config_key(profile["kubeconfig"], "/etc/troshka/collection-transport")
        source = transports[key]
        cluster_key = pins["provider_clusters"][0]["cluster_key"]
        identity = json.loads(oc(source, "get", "namespace", "kube-system", "-o", "json"))
        require(identity["metadata"]["uid"] == cluster_key, "storage cluster identity mismatch")
        view = json.loads(oc(source, "config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
        cluster = view["clusters"][0]["cluster"]
        collector_transport(cluster, "validation-only")
        prepared_transports[key] = {"kubeconfig": source, "cluster_key": cluster_key, "cluster": cluster}
    return {"kubeconfig": kc, "namespace": ns, "application": entry["application"],
            "expected_native": native, "profiles": profiles, "data": data, "transports": prepared_transports}


def journal(path, stage):
    with path.open("a") as stream:
        stream.write(json.dumps(stage) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def rotate_transports(entry):
    result = {}
    for key, item in entry["transports"].items():
        kc = item["kubeconfig"]
        identity = json.loads(oc(kc, "get", "namespace", "kube-system", "-o", "json"))
        require(identity["metadata"]["uid"] == item["cluster_key"], "storage cluster replaced")
        token = oc(kc, "create", "token", "troshka-storage-collector", "-n", "troshka", "--duration=168h").strip()
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        require(claims["sub"] == "system:serviceaccount:troshka:troshka-storage-collector"
                and claims["exp"] > time.time() + 3600, "unexpected collector token identity or expiry")
        result[key] = json.dumps(collector_transport(item["cluster"], token))
    return result


def wait_mounts(entry, name):
    deadline = time.monotonic() + 600
    waiter = threading.Event()
    while time.monotonic() < deadline:
        complete = True
        for deployment in ("troshka-collection-worker", "troshka-readiness-worker"):
            value = json.loads(oc(entry["kubeconfig"], "get", "deployment", deployment,
                                  "-n", entry["namespace"], "-o", "json"))
            volumes = {v["name"]: v for v in value["spec"]["template"]["spec"]["volumes"]}
            status = value.get("status", {})
            replicas = value["spec"]["replicas"]
            complete = complete and (
                volumes["collection-config"]["configMap"]["name"] == name
                and volumes["collection-transport"]["secret"]["secretName"] == name
                and status.get("observedGeneration", 0) >= value["metadata"]["generation"]
                and status.get("updatedReplicas", 0) == replicas
                and status.get("availableReplicas", 0) == replicas
                and status.get("replicas", 0) == replicas
            )
        if complete:
            return
        waiter.wait(2)
    raise TimeoutError("collector configuration rollout did not complete")


def apply_entry(entry, output, name):
    kc, ns = entry["kubeconfig"], entry["namespace"]
    app = json.loads(oc(kc, "get", "applications.argoproj.io", entry["application"], "-n", "openshift-gitops", "-o", "json"))
    require(native_settings(app) == entry["expected_native"], "collector settings changed since preparation")
    documents = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": ns},
         "immutable": True, "data": entry["data"]},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns},
         "immutable": True, "type": "Opaque", "stringData": rotate_transports(entry)},
    ]
    for document in documents:
        path = output / (document["kind"] + ".json")
        path.write_text(json.dumps(document))
        oc(kc, "create", "--dry-run=server", "-f", str(path))
    for document in documents:
        oc(kc, "create", "-f", str(output / (document["kind"] + ".json")))
    patch = settings_patch(app, entry["profiles"], name, name)
    patch_path = output / "patch.json"
    patch_path.write_text(json.dumps(patch))
    oc(kc, "patch", "applications.argoproj.io", entry["application"], "-n", "openshift-gitops",
       "--type=json", "--patch-file", str(patch_path))
    oc(kc, "annotate", "applications.argoproj.io", entry["application"], "-n", "openshift-gitops",
       "argocd.argoproj.io/refresh=normal", "--overwrite")
    wait_mounts(entry, name)
    bootstrap = Path(__file__).with_name("collector_bootstrap.py").read_text()
    oc(kc, "exec", "-i", "deployment/troshka-collection-worker", "-n", ns, "-c", "worker",
       "--", "python3", "-", payload=bootstrap, timeout=3600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--application", nargs=2, action="append")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if not args.apply:
            require(args.plan is not None, "plan required")
            args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
            plan = json.loads(args.plan.read_text())
            identities = [(a["kubeconfig"], a["namespace"]) for a in plan["applications"]]
            require(len(identities) == len(set(identities)), "duplicate application")
            if args.application:
                require(set(identities) == {tuple(a) for a in args.application}, "plan must cover all release applications")
            entries = [prepare_entry(entry) for entry in plan["applications"]]
            require(bool(entries), "application plan is empty")
            (args.output / "prepared.json").write_text(json.dumps(entries))
        else:
            attempts = args.output / "journal.jsonl"
            require(not attempts.exists(), "prior application attempt requires inspection, not automatic retry")
            entries = json.loads((args.output / "prepared.json").read_text())
            name = "troshka-collector-" + str(time.time_ns())
            journal(attempts, {"stage": "started", "name": name})
            for index, entry in enumerate(entries):
                destination = args.output / str(index)
                destination.mkdir(mode=0o700)
                journal(attempts, {"stage": "applying", "index": index})
                apply_entry(entry, destination, name)
                journal(attempts, {"stage": "complete", "index": index})
        print("Collector provisioning phase completed; private evidence: " + str(args.output))
        return 0
    except Exception:  # noqa: BLE001 -- never print tokens or embedded Helm values
        print("Collector provisioning failed; inspect private state before retry", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
