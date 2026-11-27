"""Scheduled collector renewal using separately supplied maintenance credentials.

Default is a read-only plan. --apply publishes immutable configuration through
Argo. An unfinished attempt blocks subsequent runs until explicitly reconciled.
No collector receives maintenance credentials, new scope, or wildcard exec RBAC.
"""

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from collector_fleet_preflight import evaluate, inspect
from collector_provision import config_key, native_settings, oc, require, settings_patch
from collector_transport_health import RENEW_MARGIN, selected_credentials, token_expiry


def renewal_due(token, *, now=None, margin=RENEW_MARGIN):
    return token_expiry(token) <= (time.time() if now is None else now) + margin


def controller(value, kind, name, uid):
    refs = [r for r in value["metadata"].get("ownerReferences", []) if r.get("controller")]
    return len(refs) == 1 and all(refs[0].get(k) == v for k, v in
                                  (("kind", kind), ("name", name), ("uid", uid)))


def ready_toolbox(pod, container):
    status = pod.get("status", {})
    return (not pod["metadata"].get("deletionTimestamp") and status.get("phase") == "Running"
            and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            and any(c.get("name") == container and c.get("ready") for c in status.get("containerStatuses", [])))


def select_toolbox(deployment, replicas, pods, expected_uid, container):
    meta = deployment["metadata"]
    require(meta["uid"] == expected_uid and not meta.get("deletionTimestamp"), "toolbox Deployment changed")
    owned = [r for r in replicas if controller(r, "Deployment", meta["name"], meta["uid"])]
    candidates = [p for p in pods if ready_toolbox(p, container) and any(
        controller(p, "ReplicaSet", r["metadata"]["name"], r["metadata"]["uid"]) for r in owned)]
    require(len(candidates) == 1, "toolbox rollout ambiguous or unavailable")
    return candidates[0]


def toolbox_role_patch(role, old_name, new_name):
    expected = [{"apiGroups": [""], "resources": [resource], "resourceNames": [old_name], "verbs": [verb]}
                for resource, verb in (("pods", "get"), ("pods/exec", "create"))]
    require(role["rules"] == expected, "toolbox access differs from exact approved scope")
    rules = copy.deepcopy(expected)
    for rule in rules:
        rule["resourceNames"] = [new_name]
    return [{"op": "test", "path": "/metadata/resourceVersion", "value": role["metadata"]["resourceVersion"]},
            {"op": "replace", "path": "/rules", "value": rules}]


def verify_toolbox_binding(binding):
    require(binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                    "name": "troshka-storage-collector-toolbox"}, "toolbox role reference changed")
    subjects = binding.get("subjects", [])
    require(len(subjects) == 1 and subjects[0].get("kind") == "ServiceAccount"
            and subjects[0].get("name") == "troshka-storage-collector"
            and subjects[0].get("namespace") == "troshka"
            and subjects[0].get("apiGroup", "") == "", "toolbox role subjects changed")


def require_no_pending(state):
    require(not (state / "pending.json").exists(), "unfinished maintenance attempt; reconcile private journal before retry")


def read(kc, resource, name=None, namespace=None):
    args = ["get", resource] + ([name] if name else [])
    if namespace:
        args += ["-n", namespace]
    return json.loads(oc(kc, *args, "-o", "json"))


def inspect_toolbox(source, backend, cluster_key):
    kc, trusted = source["kubeconfig"], source["toolbox"]
    toolbox = backend["toolbox"]
    require(toolbox["cluster_key"] == cluster_key, "toolbox cluster differs from protected pins")
    require(trusted["namespace"] == toolbox["namespace"] and trusted["container"] == toolbox["container"],
            "maintenance scope differs from configured toolbox")
    require(read(kc, "namespace", "kube-system")["metadata"]["uid"] == cluster_key, "storage cluster replaced")
    ns = trusted["namespace"]
    dep = read(kc, "deployment", trusted["deployment"], ns)
    pod = select_toolbox(dep, read(kc, "replicasets", namespace=ns)["items"],
                         read(kc, "pods", namespace=ns)["items"], trusted["deployment_uid"], trusted["container"])
    role = read(kc, "role", "troshka-storage-collector-toolbox", ns)
    verify_toolbox_binding(read(kc, "rolebinding", "troshka-storage-collector-toolbox", ns))
    patch = toolbox_role_patch(role, toolbox["pod_name"], pod["metadata"]["name"])
    return pod, role, patch


def prepare_application(entry):
    import yaml

    kc, ns = entry["kubeconfig"], entry["namespace"]
    app = read(kc, "applications.argoproj.io", entry["application"], "openshift-gitops")
    require(app["spec"]["destination"]["namespace"] == ns, "application namespace mismatch")
    native = native_settings(app)
    cm = read(kc, "configmap", native["worker"]["configMapName"], ns)
    secret = read(kc, "secret", native["worker"]["secretName"], ns)
    deployments = read(kc, "deployments", namespace=ns)["items"]
    consumers = [d["metadata"]["name"] for d in deployments if any(v["name"] == "collection-transport"
                 for v in d["spec"]["template"]["spec"].get("volumes", []))]
    require(bool(consumers), "no collector configuration consumers")
    result = {"entry": {**entry, "expected_consumers": consumers}, "app": app, "native": native,
              "cm": cm, "secret": secret, "changes": []}
    snapshot = inspect(kc, ns)
    require(not snapshot["blockers"] and all(p["accepted"] and p["enrolled"] for p in snapshot["profiles"]),
            "existing collector enrollment requires review")
    result["fleet_ready"] = evaluate(snapshot)["ready"]
    keys = [config_key(p["kubeconfig"], "/etc/troshka/collection-transport") for p in native["profiles"]]
    require(len(keys) == len(set(keys)) and set(keys) == set(entry["transports"]), "exact fleet transport coverage required")
    for profile, key in zip(native["profiles"], keys, strict=True):
        backend_key = config_key(profile["backend_config"], "/etc/troshka/collection")
        backends = json.loads(cm["data"][backend_key])
        require(len(backends) == 1 and backends[0]["mode"] == "toolbox", "one toolbox backend required")
        pins = json.loads(cm["data"][config_key(profile["pins"], "/etc/troshka/collection")])
        require(str(pins["domain_id"]) == profile["domain_id"] and len(pins["provider_clusters"]) == 1,
                "profile pins mismatch")
        config = yaml.safe_load(base64.b64decode(secret["data"][key]))
        user, cluster = selected_credentials(config)
        source = entry["transports"][key]
        if entry.get("mode") == "operator":
            from collector_operator_source import prepare

            change, changed = prepare(source, backends, pins, config, key, backend_key)
            if changed:
                result["changes"].append(change)
            continue
        pod, role, patch = inspect_toolbox(source, backends[0], pins["provider_clusters"][0]["cluster_key"])
        view = json.loads(oc(source["kubeconfig"], "config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
        trusted_cluster = view["clusters"][0]["cluster"]
        require(all(trusted_cluster.get(k) == cluster.get(k) for k in
                    ("server", "certificate-authority-data", "tls-server-name")),
                "maintenance TLS identity differs from collector")
        changed = pod["metadata"]["uid"] != backends[0]["toolbox"]["pod_uid"]
        change = {"key": key, "backend_key": backend_key, "backends": backends,
                  "config": config, "source": source, "pod": pod,
                  "role": role, "patch": patch, "toolbox_changed": changed}
        if renewal_due(user["token"]) or changed:
            result["changes"].append(change)
        else:
            verify_current_transport(change)
    return result


def verify_current_transport(change):
    with tempfile.TemporaryDirectory(prefix="collector-transport-") as directory:
        path = Path(directory) / "collector.kubeconfig"
        path.write_text(json.dumps(change["config"]))
        path.chmod(0o600)
        toolbox = change["backends"][0]["toolbox"]
        if change.get("mode") == "operator":
            require(read(str(path), "namespace", "kube-system")["metadata"]["uid"] == toolbox["cluster_key"],
                    "collector storage cluster replaced")
        pod = read(str(path), "pod", toolbox["pod_name"], toolbox["namespace"])
        require(pod["metadata"]["uid"] == toolbox["pod_uid"], "collector toolbox changed during inspection")
        if change.get("mode") == "operator":
            require(ready_toolbox(pod, toolbox["container"]), "collector toolbox is not ready")
        verify_storage(str(path), change)


def verify_storage(kc, change):
    """Prove replacement identity before changing RBAC or publishing configuration."""
    toolbox = change["backends"][0]["toolbox"]
    prefix = ("exec", "-n", toolbox["namespace"], change["pod"]["metadata"]["name"],
              "-c", toolbox["container"], "--")
    fsid = oc(kc, *prefix, "ceph", "--connect-timeout", "5", "fsid").strip()
    require(fsid == toolbox["ceph_fsid"] == change["backends"][0]["ceph_fsid"], "Ceph identity changed")
    pools = json.loads(oc(kc, *prefix, "ceph", "--connect-timeout", "5", "osd", "lspools", "--format", "json"))
    found = {(p["poolnum"], p["poolname"]) for p in pools}
    require(all((p["pool_id"], p["name"]) in found for p in change["backends"][0]["pools"]), "Ceph pool identity changed")


def renew(change, output):
    if change.get("mode") == "operator":
        from collector_operator_source import renew as renew_operator

        return renew_operator(change)
    kc = change["source"]["kubeconfig"]
    toolbox = change["backends"][0]["toolbox"]
    # Repeat ownership/identity validation immediately before access mutation.
    pod, role, patch = inspect_toolbox(change["source"], change["backends"][0], toolbox["cluster_key"])
    require(pod["metadata"]["uid"] == change["pod"]["metadata"]["uid"], "toolbox changed during preparation")
    verify_storage(kc, change)
    if change["toolbox_changed"]:
        oc(kc, "patch", "role", role["metadata"]["name"], "-n", toolbox["namespace"],
           "--type=json", "-p", json.dumps(patch))
    token = oc(kc, "create", "token", "troshka-storage-collector", "-n", "troshka", "--duration=168h").strip()
    require(not renewal_due(token, margin=RENEW_MARGIN * 2), "issued token lifetime too short for renewal window")
    config = copy.deepcopy(change["config"])
    selected_credentials(config)[0]["token"] = token
    path = output / change["key"]
    path.write_text(json.dumps(config))
    try:
        live = read(str(path), "pod", pod["metadata"]["name"], toolbox["namespace"])
        require(live["metadata"]["uid"] == pod["metadata"]["uid"], "collector observed different toolbox")
        verify_storage(str(path), change)
    finally:
        path.unlink(missing_ok=True)
    backend = copy.deepcopy(change["backends"])
    backend[0]["toolbox"].update(pod_name=pod["metadata"]["name"], pod_uid=pod["metadata"]["uid"])
    return base64.b64encode(json.dumps(config).encode()).decode(), json.dumps(backend, indent=2)


def publish(prepared, output):
    entry, native = prepared["entry"], prepared["native"]
    kc, ns = entry["kubeconfig"], entry["namespace"]
    current = read(kc, "applications.argoproj.io", entry["application"], "openshift-gitops")
    require(native_settings(current) == native, "application settings changed before maintenance")
    data, secrets = dict(prepared["cm"]["data"]), dict(prepared["secret"]["data"])
    for change in prepared["changes"]:
        secrets[change["key"]], data[change["backend_key"]] = renew(change, output)
    for change in prepared["changes"]:
        if change.get("mode") == "operator":
            from collector_operator_source import recheck

            recheck(change)
    name = "troshka-collector-maint-" + str(time.time_ns())
    for kind, content in (("ConfigMap", data), ("Secret", secrets)):
        doc = {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": ns}, "immutable": True, "data": content}
        if kind == "Secret":
            doc["type"] = "Opaque"
        write_evidence(output / (kind + ".json"), doc)
        oc(kc, "create", "-f", "-", payload=json.dumps(doc))
    current = read(kc, "applications.argoproj.io", entry["application"], "openshift-gitops")
    require(native_settings(current) == native, "application changed during maintenance")
    oc(kc, "patch", "applications.argoproj.io", entry["application"], "-n", "openshift-gitops",
       "--type=json", "-p", json.dumps(settings_patch(current, native["profiles"], name, name)))
    return name


def wait_consumers(entry, name):
    """Verify every consumer, allowing warm-shutdown workers to finish old jobs."""
    from collector_provision import wait_mounts

    wait_mounts(entry, name, allow_terminating=True)


def verify_mounted_inputs(entry, name, config_name=None):
    """Check protected copies, not just the Secret references on a Deployment."""
    from collector_provision import consumer_ready

    kc, ns = entry["kubeconfig"], entry["namespace"]
    config_name = config_name or name
    cm = read(kc, "configmap", config_name, ns)["data"]
    secret = read(kc, "secret", name, ns)["data"]
    expected = {"/etc/troshka/collection/" + k: hashlib.sha256(v.encode()).hexdigest() for k, v in cm.items()}
    expected.update({"/etc/troshka/collection-transport/" + k: hashlib.sha256(base64.b64decode(v)).hexdigest()
                     for k, v in secret.items()})
    code = ("import hashlib,json; from pathlib import Path; expected=" + repr(expected)
            + "; print(json.dumps({'matched':all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in expected.items())}))")
    deployments = read(kc, "deployments", namespace=ns)["items"]
    pods = read(kc, "pods", namespace=ns)["items"]
    seen = set()
    for deployment in deployments:
        spec = deployment["spec"]
        if not any(v["name"] == "collection-transport" for v in spec["template"]["spec"].get("volumes", [])):
            continue
        seen.add(deployment["metadata"]["name"])
        require(consumer_ready(deployment, name, True, config_name), "consumer rollout is not current")
        if not spec.get("replicas", 0):
            continue
        labels = spec["selector"]["matchLabels"]
        candidates = [p for p in pods if mounted_candidate(p, labels, name, config_name)]
        require(len(candidates) >= spec["replicas"], "not all replacement consumers are ready")
        container = spec["template"]["spec"]["containers"][0]["name"]
        for pod in candidates:
            result = json.loads(oc(kc, "exec", "-i", pod["metadata"]["name"], "-n", ns,
                                  "-c", container, "--", "python3", "-", payload=code))
            require(result.get("matched") is True, "consumer protected files differ from published inputs")
    require(bool(seen) and set(entry.get("expected_consumers", seen)) <= seen, "consumer deployment missing")


def mounted_candidate(pod, labels, name, config_name=None):
    meta = pod["metadata"]
    if meta.get("deletionTimestamp") or any(meta.get("labels", {}).get(k) != v for k, v in labels.items()):
        return False
    volumes = {v["name"]: v for v in pod["spec"].get("volumes", [])}
    return (volumes.get("collection-transport", {}).get("secret", {}).get("secretName") == name
            and volumes.get("collection-config", {}).get("configMap", {}).get("name") == (config_name or name)
            and any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])))


def jobs_idle(entry):
    code = '''from app.core.redis import get_redis_raw
from rq import Queue
from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry
import json
r=get_redis_raw()
busy=any(q.count or any(cls(q.name,connection=r).get_job_ids(cleanup=False) for cls in
    (StartedJobRegistry,DeferredJobRegistry,ScheduledJobRegistry)) for q in Queue.all(connection=r))
print(json.dumps({"idle":not busy}))
'''
    result = json.loads(oc(entry["kubeconfig"], "exec", "-i", "deployment/troshka-backend",
                           "-n", entry["namespace"], "-c", "backend", "--", "python3", "-", payload=code))
    return result.get("idle") is True


def write_evidence(path, value):
    """Persist the fence and rollback evidence before any remote mutation."""
    with path.open("x") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run(plan, state, apply=False):
    require_no_pending(state)
    entries = plan["applications"]
    require(bool(entries), "maintenance plan is empty")
    identities = [(e.get("kubeconfig"), e.get("namespace")) for e in entries]
    require(len(identities) == len(set(identities)), "duplicate application maintenance scope")
    prepared = [prepare_application(e) for e in entries]
    report = {"applications": [{"namespace": p["entry"]["namespace"], "fleet_ready": p.get("fleet_ready", True),
                                 "changes": [c["key"] for c in p["changes"]]} for p in prepared]}
    if not apply or not any(p["changes"] for p in prepared):
        if apply:
            for p in prepared:
                worker = p["native"]["worker"]
                verify_mounted_inputs(p["entry"], worker["secretName"], worker["configMapName"])
        status = "healthy" if all(p["fleet_ready"] for p in report["applications"]) else "fleet_incomplete"
        return {**report, "status": "changes_required" if any(p["changes"] for p in prepared) else status}
    if not all(jobs_idle(p["entry"]) for p in prepared if p["changes"]):
        return {**report, "status": "deferred_jobs_active"}
    output = Path(tempfile.mkdtemp(prefix="attempt-", dir=state))
    write_evidence(state / "pending.json", {"attempt": str(output), "phase": "applying"})
    for i, p in enumerate(prepared):
        if not p["changes"]:
            continue
        folder = output / str(i)
        folder.mkdir(mode=0o700)
        write_evidence(folder / "before.json", p)
        name = publish(p, folder)
        write_evidence(folder / "published.json", {"name": name})
        wait_consumers(p["entry"], name)
        verify_mounted_inputs(p["entry"], name)
        snapshot = inspect(p["entry"]["kubeconfig"], p["entry"]["namespace"])
        require(not snapshot["blockers"] and all(not r.get("transport", {"blockers": ["missing"]})["blockers"]
                for r in snapshot["profiles"]), "published collector transport is not healthy")
        report["applications"][i]["fleet_ready"] = evaluate(snapshot)["ready"]
    write_evidence(output / "complete.json", report)
    (state / "pending.json").unlink()
    return {**report, "status": "updated" if all(p["fleet_ready"] for p in report["applications"]) else "updated_fleet_incomplete"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (args.state_dir / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(json.loads(args.plan.read_text()), args.state_dir, args.apply)
        print(json.dumps(result))
        return 0 if result["status"] in {"healthy", "updated"} else 2
    except Exception:  # noqa: BLE001 -- exceptions may contain kubeconfigs or Helm secrets
        print(json.dumps({"status": "blocked", "reason": "collector_maintenance_requires_review"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
