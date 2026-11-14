"""Bounded managed power lifecycle for a single active, standalone operator.

Annotations coordinate trusted Troshka producers; they are not a Kubernetes
security boundary. Status is a durable intent snapshot, never shutdown proof.
"""

import asyncio
import inspect
import json
import time
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from kubernetes import client

GROUP = "troshka.redhat.com"
VERSION = "v1alpha1"
ANNOTATION = GROUP + "/managed-power"
KINDS = {
    "VirtualMachine": ("kubevirt.io/v1", "virtualmachines"),
    "VirtualMachineInstance": ("kubevirt.io/v1", "virtualmachineinstances"),
    "TroshkaProject": (GROUP + "/" + VERSION, "troshkaprojects"),
    "TroshkaVM": (GROUP + "/" + VERSION, "troshkavms"),
    "TroshkaNetwork": (GROUP + "/" + VERSION, "troshkanetworks"),
    "TroshkaCeph": (GROUP + "/" + VERSION, "troshkancephs"),
    "DataVolume": ("cdi.kubevirt.io/v1beta1", "datavolumes"),
    "Pod": ("v1", "pods"),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims"),
    "PersistentVolume": ("v1", "persistentvolumes"),
    "Namespace": ("v1", "namespaces"),
    "ServiceAccount": ("v1", "serviceaccounts"),
    "Service": ("v1", "services"),
    "Route": ("route.openshift.io/v1", "routes"),
    "Role": ("rbac.authorization.k8s.io/v1", "roles"),
    "RoleBinding": ("rbac.authorization.k8s.io/v1", "rolebindings"),
    **{
        k: ("apps/v1", p)
        for k, p in (
            ("Deployment", "deployments"),
            ("ReplicaSet", "replicasets"),
            ("StatefulSet", "statefulsets"),
            ("DaemonSet", "daemonsets"),
        )
    },
    "Job": ("batch/v1", "jobs"),
    "CronJob": ("batch/v1", "cronjobs"),
}
SUPPORT_KINDS = {"ServiceAccount", "Service", "Route", "Role", "RoleBinding"}
CLEANUP_KINDS = {
    "ConfigMap": ("v1", "configmaps"),
    "Secret": ("v1", "secrets"),
    "NetworkAttachmentDefinition": ("k8s.cni.cncf.io/v1", "network-attachment-definitions"),
    "VolumeSnapshot": ("snapshot.storage.k8s.io/v1", "volumesnapshots"),
}
CLEANUP_ANNOTATION = GROUP + "/managed-cleanup"
INVENTORY = tuple(
    k
    for k in KINDS
    if k not in {"TroshkaProject", "PersistentVolume", "Namespace"} | SUPPORT_KINDS
)
_locks = {}
_context: ContextVar[tuple[dict, str, Any] | None] = ContextVar(
    "managed_power_producer", default=None
)


class ManagedPowerError(ValueError):
    """Only fixed reason codes may cross the API boundary."""


def require(condition, reason="managed_profile_unsupported"):
    if not condition:
        raise ManagedPowerError(reason)


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def intent(root):
    raw = root.get("metadata", {}).get("annotations", {}).get(ANNOTATION)
    if raw is None:
        return None
    try:
        require(len(raw) <= 512 * 1024, "managed_intent_limit")
        value = json.loads(raw)
        require(
            value["version"] == 1 and value["mode"] in {"stopped", "running", "deploy"}
        )
        require(
            all(
                type(value[k]) is str and 0 < len(value[k]) <= 256
                for k in ("requestId", "allocationGeneration")
            )
        )
        if "cleanupCustody" in value:
            from helpers.native_custody import validate_intent

            validate_intent(root, value)
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedPowerError("managed_intent_invalid") from error


def validate_cleanup_root(root, value):
    """DELETE may advance generation once without changing the pinned spec."""
    pinned, meta = value["root"], root["metadata"]
    expected = (value.get("stop_proof") or {}).get("root_spec_digest")
    pending = value.get("pending_initialization")
    if pending is not None:
        expected = pending["request"]["payloadDigest"]
    if expected is not None:
        require(expected == digest(root["spec"]), "cleanup_root_spec_changed")
    generation = pinned["deployment_generation"]
    deleting_original = (
        bool(meta.get("deletionTimestamp"))
        and expected is not None
        and meta["generation"] == generation + 1
    )
    require(
        meta["uid"] == pinned["project_cr_uid"]
        and (meta["generation"] == generation or deleting_original),
        "cleanup_root_changed",
    )


def cleanup_intent(root):
    raw = root.get("metadata", {}).get("annotations", {}).get(CLEANUP_ANNOTATION)
    if raw is None:
        return None
    try:
        require(len(raw) <= 512 * 1024, "cleanup_intent_limit")
        value = json.loads(raw)
        require(value["version"] == "native-cleanup-v1" and value["namespace_mode"] == "managed", "cleanup_intent_invalid")
        validate_cleanup_root(root, value)
        require(len(value["objects"]) <= 1000 and value["inventory_digest"] == digest(sorted(value["objects"], key=lambda v: (v["kind"], v["name"]))), "cleanup_inventory_changed")
        stopped = intent(root)
        require(stopped is not None and stopped["mode"] == "stopped" and stopped["requestId"] == value["stop_proof"]["request_id"] and stopped["allocationGeneration"] == value["generation"], "cleanup_off_proof_changed")
        if value.get("pending_initialization") is not None:
            validate_pending_cleanup_marker(root, value, stopped)
        return value
    except ManagedPowerError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ManagedPowerError("cleanup_intent_invalid") from error


def validate_pending_cleanup_marker(root, value, stopped):
    pending = value["pending_initialization"]
    deployment = root.get("status", {}).get("managedDeployment", {})
    journal = {"creates": deployment.get("creates", {}), "generatedClaims": deployment.get("generatedClaims", {})}
    state = root.get("status", {}).get("managedPower", {})
    require(stopped.get("pendingInitialization") == pending["request"] and journal == pending["journal"], "pending_cleanup_journal_changed")
    require(state.get("state") == "off" and state.get("producerDrained") is True and state.get("pendingCleanup") == digest({"request": stopped, "journal": journal}), "pending_cleanup_ack_changed")


def cleanup_bootstrap(value, by_uid=None):
    """Exact built-in OpenShift defaults and existing Troshka base grants only."""
    from helpers.native_custody import support

    attested = support(value, by_uid or {})
    if attested is not None:
        return attested
    meta = value.get("metadata", {})
    name, namespace = meta.get("name"), meta.get("namespace")
    if value.get("kind") == "Secret":
        grant = cleanup_pull_secret(value, by_uid or {})
        from helpers.native_custody import evidence, identity
        roots = [v for v in (by_uid or {}).values() if v.get("kind") == "TroshkaProject"]
        if grant is not None and len(roots) == 1 and evidence(roots[0]) is not None:
            return {"custody": identity(value)}
        return grant
    if meta.get("ownerReferences"):
        return None
    if value.get("kind") == "ConfigMap":
        return cleanup_ca_configmap(value)
    if value.get("kind") == "ServiceAccount" and name in {"default", "builder", "deployer"}:
        return {"kind": "ServiceAccount", "name": name}
    if value.get("kind") != "RoleBinding":
        return None
    expected = cleanup_grant_body(name, namespace)
    return expected if expected and all(value.get(k) == v for k, v in expected.items()) else None


def cleanup_ca_configmap(value):
    meta = value["metadata"]
    name = meta["name"]
    if name == "odh-kserve-custom-ca-bundle":
        return cleanup_odh_configmap(value)
    shapes = {
        "kube-root-ca.crt": ("kubernetes.io/description", "Contains a CA bundle that can be used to verify the kube-apiserver when using internal endpoints such as the internal service IP or kubernetes.default.svc. No other usage is guaranteed across distributions of Kubernetes clusters.", "ca.crt"),
        "openshift-service-ca.crt": ("service.beta.openshift.io/inject-cabundle", "true", "service-ca.crt"),
    }
    if name not in shapes:
        return None
    annotation, expected, data_key = shapes[name]
    if meta.get("annotations", {}).get(annotation) != expected or set(value.get("data", {})) != {data_key} or value.get("binaryData"):
        return None
    return {"kind": "ConfigMap", "name": name, "publisher": {annotation: expected}, "data_keys": [data_key]}


def cleanup_odh_configmap(value):
    """Recognize the exact namespace CA publisher shape; retain until namespace GC."""
    meta = value["metadata"]
    labels = {"opendatahub.io/managed": "true"}
    if (
        meta.get("labels") != labels
        or meta.get("annotations")
        or set(value.get("data", {})) != {"cabundle.crt"}
        or value.get("binaryData")
        or value.get("immutable")
    ):
        return None
    return {"kind": "ConfigMap", "name": meta["name"], "publisher_labels": labels, "data_keys": ["cabundle.crt"]}


def cleanup_pull_account(sa, by_uid):
    if sa.get("kind") != "ServiceAccount" or sa.get("apiVersion") != "v1":
        return False
    if cleanup_bootstrap(sa, by_uid) is not None:
        return True
    roots = [v for v in by_uid.values() if v.get("kind") == "TroshkaProject"
             and v.get("metadata", {}).get("namespace") == sa.get("metadata", {}).get("namespace")]
    if len(roots) != 1:
        return False
    try:
        verify_chain(sa, roots[0], by_uid)
    except (ManagedPowerError, KeyError, TypeError):
        return False
    return True


def cleanup_pull_secret(value, by_uid):
    from helpers.native_custody import evidence, matches

    meta = value["metadata"]
    refs = meta.get("ownerReferences", [])
    if len(refs) != 1:
        return None
    ref = refs[0]
    sa = by_uid.get(ref.get("uid"), {})
    sa_meta = sa.get("metadata", {})
    name = sa_meta.get("name")
    expected_ref = {"apiVersion": "v1", "kind": "ServiceAccount", "name": name, "uid": sa_meta.get("uid")}
    annotations = meta.get("annotations", {})
    valid = (
        ref == expected_ref
        and sa_meta.get("namespace") == meta.get("namespace")
        and cleanup_pull_account(sa, by_uid)
        and sa_meta.get("annotations", {}).get("openshift.io/internal-registry-pull-secret-ref") == meta["name"]
        and value.get("type") == "kubernetes.io/dockercfg"
        and annotations.get("openshift.io/internal-registry-auth-token.service-account") == name
        and annotations.get("openshift.io/internal-registry-auth-token.binding") == "bound"
    )
    roots = [v for v in by_uid.values() if v.get("kind") == "TroshkaProject"]
    if len(roots) == 1 and evidence(roots[0]) is not None:
        valid = valid and matches(value, roots[0])
    return {"kind": "Secret", "service_account_uid": sa_meta["uid"], "service_account": name, "type": value["type"]} if valid else None


def cleanup_grant_body(name, namespace):
    accounts = {
        "troshka-provider-namespaced": ("troshka", "troshka", "troshka-provider-namespaced"),
        "troshka-operator-namespaced": ("troshka-operator", "troshka-operator", "troshka-operator-namespaced"),
        "system:deployers": ("deployer", namespace, "system:deployer"),
        "system:image-builders": ("builder", namespace, "system:image-builder"),
    }
    if name == "system:image-pullers":
        subjects = [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "system:serviceaccounts:" + namespace}]
        role = "system:image-puller"
    elif name in accounts:
        sa, sa_ns, role = accounts[name]
        subjects = [{"kind": "ServiceAccount", "name": sa, "namespace": sa_ns}]
    else:
        return None
    return {"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role}, "subjects": subjects}


def cleanup_delete(api, root):
    """Scoped finalizer: never call legacy capture, finalizer stripping or sweeps."""
    value = cleanup_intent(root)
    require(value is not None, "cleanup_intent_required")
    ns = value["root"]["namespace"]
    require(api.get("Namespace", None, ns)["metadata"]["uid"] == value["root"]["namespace_uid"], "cleanup_namespace_changed")
    kinds = (set(KINDS) | set(CLEANUP_KINDS)) - {"Namespace", "PersistentVolume", "TroshkaProject"}
    rows = {kind: api.rows(kind, ns) for kind in sorted(kinds)}
    expected = {(v["kind"], v["name"]): v for v in value["objects"] if v.get("namespace")}
    by_uid = {obj["metadata"]["uid"]: obj for values in rows.values() for obj in values}
    by_uid[root["metadata"]["uid"]] = root
    found = []
    for kind, objects in rows.items():
        for obj in objects:
            meta = obj["metadata"]
            original = expected.get((kind, meta["name"]), {})
            if cleanup_original(obj, original, by_uid):
                continue  # Preserve bootstrap access until namespace deletion.
            cleanup_runtime(obj)
            found.append(obj)
    if not found:
        return True
    # Preserve the established workload -> network -> Services -> storage order.
    first = min(cleanup_stage(obj) for obj in found)
    for obj in found:
        if cleanup_stage(obj) != first or obj["metadata"].get("deletionTimestamp"):
            continue
        meta = obj["metadata"]
        if obj["kind"] in {"TroshkaVM", "TroshkaNetwork", "TroshkaCeph"}:
            annotations = {**meta.get("annotations", {}), CLEANUP_ANNOTATION: json.dumps(value, sort_keys=True)}
            obj = api.patch(obj, [{"op": "add", "path": "/metadata/annotations", "value": annotations}])
            meta = obj["metadata"]
        result = api.request("DELETE", obj["kind"], ns, meta["name"], {
            "apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
            "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]},
        })
        require(isinstance(result, dict) and (result.get("kind") == "Status" or result.get("metadata", {}).get("uid") == meta["uid"]), "cleanup_delete_response_unverified")
    return False


def cleanup_runtime(obj):
    kind = obj["kind"]
    require(kind not in {"Pod", "VirtualMachineInstance", "Job", "CronJob", "VolumeSnapshot"}, "cleanup_runtime_present")
    require(kind != "VirtualMachine" or obj["spec"].get("runStrategy") == "Halted", "cleanup_vm_running")
    require(kind not in {"Deployment", "ReplicaSet"} or obj["spec"].get("replicas") == 0, "cleanup_controller_running")


def cleanup_original(obj, original, by_uid):
    require(original.get("uid") == obj["metadata"]["uid"], "cleanup_original_inventory_changed")
    bootstrap = original.get("namespace_bootstrap")
    if bootstrap:
        require(cleanup_bootstrap_matches(obj, bootstrap, by_uid), "cleanup_namespace_bootstrap_changed")
    return bool(bootstrap) and not ("custody" in bootstrap and obj["kind"] in {"Service", "Route"})


def cleanup_bootstrap_matches(obj, sealed, by_uid):
    """After sealing, exact custody survives deliberate dependency deletion.

    Only cleanup callers with an original UID/inventory authorization use this;
    initial admission still requires cleanup_bootstrap's full source validation.
    """
    if set(sealed) == {"custody"}:
        from helpers.native_custody import identity
        return identity(obj) == sealed["custody"]
    return cleanup_bootstrap(obj, by_uid) == sealed


def cleanup_stage(obj):
    kind = obj["kind"]
    if kind in {"TroshkaVM", "TroshkaCeph", "VirtualMachine", "Deployment", "ReplicaSet", "DataVolume"}:
        return 0
    if kind in {"TroshkaNetwork", "NetworkAttachmentDefinition", "Route"}:
        return 1
    return 2 if kind == "Service" else 3


def cleanup_child(api, meta, namespace):
    """Original managed child finalizers must not execute name-only legacy deletes."""
    marker = meta.get("annotations", {}).get(CLEANUP_ANNOTATION)
    if marker is None:
        return False
    owners = meta.get("ownerReferences", [])
    require(len(owners) == 1 and owners[0].get("kind") == "TroshkaProject", "cleanup_parent_changed")
    root = api.get("TroshkaProject", namespace, owners[0]["name"])
    require(root["metadata"]["uid"] == owners[0]["uid"], "cleanup_root_changed")
    value = cleanup_intent(root)
    require(value is not None and json.loads(marker) == value, "cleanup_child_intent_changed")
    require(value["root"]["namespace"] == namespace and api.get("Namespace", None, namespace)["metadata"]["uid"] == value["root"]["namespace_uid"], "cleanup_namespace_changed")
    require(any(v["uid"] == meta["uid"] and v["name"] == meta["name"] and v.get("namespace") == namespace for v in value["objects"]), "cleanup_child_changed")
    return True


class PowerClient:
    """Small, bounded JSON transport using the operator/provider's existing client."""

    def __init__(self, api=None):
        self.api = api or client.ApiClient()
        self.deadline = None

    def begin_inventory(self):
        self.deadline = time.monotonic() + 55

    def timeout(self):
        remaining = 5 if self.deadline is None else self.deadline - time.monotonic()
        require(remaining > 0, "managed_observation_timeout")
        return min(5, remaining)

    def request(self, method, kind, namespace, name=None, body=None, status=False, allow_absent=False):
        version, plural = (KINDS | CLEANUP_KINDS)[kind]
        prefix = "/api/" if version == "v1" else "/apis/"
        path = prefix + version
        if namespace is not None:
            path += "/namespaces/" + quote(namespace, safe="")
        path += "/" + plural
        if name:
            path += "/" + quote(name, safe="")
        if status:
            path += "/status"
        # No retry of ambiguous mutations. Kubernetes identifiers are validated
        # by the API; provider/root identities originate from retained objects.
        from urllib3 import Timeout

        headers = {**self.api.default_headers, "Accept": "application/json"}
        headers["Content-Type"] = (
            "application/json-patch+json" if method == "PATCH" else "application/json"
        )
        query = [("limit", "1001")] if method == "GET" and name is None else []
        self.api.update_params_for_auth(headers, query, ["BearerToken"])
        response = self.api.rest_client.pool_manager.request(
            method,
            self.api.configuration.host + path,
            fields=query if body is None else None,
            headers=headers,
            body=json.dumps(body).encode() if body is not None else None,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=self.timeout()),
        )
        try:
            if allow_absent and response.status == 404:
                return None
            require(200 <= response.status < 300, "managed_api_unavailable")
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, "managed_response_limit")
            value = json.loads(raw)
            require(isinstance(value, dict), "managed_response_invalid")
            return value
        finally:
            response.close()

    def get(self, kind, namespace, name):
        return self.request("GET", kind, namespace, name)

    def find(self, kind, namespace, name):
        return self.request("GET", kind, namespace, name, allow_absent=True)

    def rows(self, kind, namespace):
        cluster_scoped = kind in {"Namespace", "PersistentVolume"}
        require(
            namespace is None if cluster_scoped else isinstance(namespace, str) and bool(namespace),
            "managed_inventory_scope_invalid",
        )
        value = self.request("GET", kind, namespace)
        rows = value["items"]
        require(
            isinstance(rows, list)
            and len(rows) <= 1000
            and not value["metadata"].get("continue"),
            "managed_inventory_incomplete",
        )
        require(len({r["metadata"]["uid"] for r in rows}) == len(rows))
        for row in rows:
            require(row["metadata"].get("namespace") == namespace, "managed_inventory_namespace_changed")
            row.setdefault("kind", kind)
            version = (KINDS | CLEANUP_KINDS)[kind][0]
            row.setdefault("apiVersion", version)
            require(row["kind"] == kind and row["apiVersion"] == version)
        return rows

    def patch(self, value, changes, *, status=False):
        meta = value["metadata"]
        tests = [
            {"op": "test", "path": "/metadata/" + k, "value": meta[k]}
            for k in ("uid", "resourceVersion")
        ]
        return self.request(
            "PATCH",
            value["kind"],
            meta["namespace"],
            meta["name"],
            tests + changes,
            status=status,
        )


def root_for(body, namespace, api):
    if body.get("kind") == "TroshkaProject":
        meta = body["metadata"]
    else:
        refs = body.get("metadata", {}).get("ownerReferences", [])
        require(
            len(refs) == 1 and refs[0].get("kind") == "TroshkaProject",
            "managed_parent_unverified",
        )
        meta = refs[0]
    root = api.get("TroshkaProject", namespace, meta["name"])
    require(root["metadata"]["uid"] == meta["uid"], "managed_root_changed")
    require(not root["metadata"].get("deletionTimestamp"), "managed_root_deleting")
    return root


def assert_running(namespace, owner_uid):
    context = _context.get()
    if context is None:
        raise ManagedPowerError("managed_producer_context_required")
    require(
        context[1] == namespace and context[0]["metadata"]["uid"] == owner_uid,
        "managed_producer_context_required",
    )
    root, _, api = context
    current = root_for(root, namespace, api)
    require(intent(current) is None, "managed_producer_stopped")


async def checked_await(awaitable):
    """Recheck external annotation changes after yielding during provisioning."""
    result = await awaitable
    context = _context.get()
    if context is None:
        raise ManagedPowerError("managed_producer_context_required")
    root, namespace, _ = context
    assert_running(namespace, root["metadata"]["uid"])
    return result


def producer(*, project=False, reconcile=False):
    """Serialize entry points, reread intent, drain before acknowledging a stop."""

    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs).arguments
            extra = bound.get("_", {})
            namespace = bound.get("namespace", extra.get("namespace"))
            body = bound.get("body", extra.get("body"))
            if body is None:
                body = {
                    "kind": "TroshkaProject" if project else "",
                    "metadata": bound["meta"],
                }
            api = PowerClient()
            root = root_for(body, namespace, api)
            key = root["metadata"]["uid"]
            async with _locks.setdefault(key, asyncio.Lock()):
                root = root_for(body, namespace, api)
                token = _context.set((root, namespace, api))
                try:
                    if (
                        root.get("metadata", {}).get("annotations", {}).get(ANNOTATION)
                        is not None
                    ):
                        reconcile_intent(api, root, reconcile)
                        return None
                    return await function(*args, **kwargs)
                finally:
                    _context.reset(token)

        return wrapped

    return decorate


def reconcile_intent(api, root, reconcile):
    request = intent(root)
    if request is None:
        raise ManagedPowerError("managed_intent_required")
    if request["mode"] == "deploy":
        from helpers.managed_deploy import reconcile as deploy

        deploy(api, root)
    elif reconcile:
        reconcile_stop(api, root)


def inventory(api, root, *, deployment_support=True):
    namespace = root["metadata"]["namespace"]
    rows = {kind: api.rows(kind, namespace) for kind in INVENTORY}
    all_rows = [root] + [value for values in rows.values() for value in values]
    require(len(all_rows) <= 1000, "managed_inventory_limit")
    by_uid = {value["metadata"]["uid"]: value for value in all_rows}
    require(len(by_uid) == len(all_rows), "managed_identity_duplicate")
    from helpers.native_custody import evidence
    if evidence(root) is not None:
        for secret in api.rows("Secret", namespace):
            require(secret["metadata"]["uid"] not in by_uid, "managed_identity_duplicate")
            by_uid[secret["metadata"]["uid"]] = secret
    for value in all_rows[1:]:
        verify_chain(value, root, by_uid)
    if deployment_support and root.get("status", {}).get("managedDeployment", {}).get(
        "confirmedObservationId"
    ):
        from helpers import managed_deploy

        managed_deploy.retained_support(api, root, rows, by_uid)
    return rows, by_uid


def verify_chain(value, root, by_uid):
    from helpers.native_custody import evidence, matches, stable

    if evidence(root) is not None and stable(value):
        require(matches(value, root), "custody_object_changed")
    seen = set()
    while value["metadata"]["uid"] != root["metadata"]["uid"]:
        from helpers.native_custody import (
            bmc_shape,
            matches,
            support_shape,
            writer_secret,
            writer_shape,
        )

        if matches(value, root) and (support_shape(value, root) or bmc_shape(value, root, by_uid) or writer_shape(value, root, by_uid) or writer_secret(value, root, by_uid)):
            return
        uid = value["metadata"]["uid"]
        require(uid not in seen, "managed_owner_cycle")
        seen.add(uid)
        refs = value["metadata"].get("ownerReferences", [])
        require(
            len(refs) == 1 and refs[0].get("controller") is True,
            "managed_ownership_unverified",
        )
        ref = refs[0]
        parent = by_uid.get(ref["uid"])
        require(parent is not None, "managed_owner_missing")
        require(
            ref["name"] == parent["metadata"]["name"]
            and ref["kind"] == parent["kind"]
            and ref["apiVersion"] == parent["apiVersion"],
            "managed_owner_changed",
        )
        value = parent


def profile(root, rows, by_uid, api):
    from helpers.native_custody import evidence, verify_ceph

    custody = evidence(root) is not None
    status = root.get("status", {})
    require(
        status.get("phase") == "Running"
        or (custody and status.get("phase") == "CaptureComplete" and status.get("captureProgress") == "Done"),
        "managed_project_not_settled",
    )
    require(
        not any(status.get(k) for k in ("recertConfig", "capture", "filePull")),
        "managed_active_work_unsupported",
    )
    annotations = root["metadata"].get("annotations", {})
    require(
        not any("capture" in k or "file-pull" in k for k in annotations),
        "managed_active_work_unsupported",
    )
    topology = root["spec"].get("topology", {})
    require(not topology.get("projectCephCapture"), "managed_ceph_unsupported")
    validate_profile_nodes(topology, custody)
    for kind in (("StatefulSet", "DaemonSet", "CronJob") if custody else ("TroshkaCeph", "StatefulSet", "DaemonSet", "Job", "CronJob")):
        require(not rows[kind], "managed_controller_unsupported")
    for vm in rows["TroshkaVM"]:
        require(
            vm.get("status", {}).get("observedGeneration")
            == vm["metadata"]["generation"],
            "managed_child_not_settled",
        )
        require(
            (custody or not vm["spec"].get("bmcEnabled")) and not vm["spec"].get("recertEnabled")
        )
    if custody:
        verify_ceph(root, rows, by_uid)
    verify_storage(rows, by_uid, api, custody=custody)
    verify_workloads(root, rows, by_uid)


def validate_profile_nodes(topology, custody):
    kinds = {"vmNode", "containerNode", "networkNode", "storageNode"}
    fields = {"bmcEnabled", "recertEnabled", "isShowroom"}
    if custody:
        kinds |= {"clusterNode", "cephClusterNode"}
        fields.remove("bmcEnabled")
        fields.remove("isShowroom")  # Still requires the exact existing container manifest.
    for node in topology.get("nodes", []):
        require(node.get("type") in kinds)
        require(not any(node.get("data", {}).get(k) for k in fields))


def verify_storage(rows, by_uid, api, *, custody=False):
    runtime_kinds = {"Pod", "VirtualMachineInstance", "ReplicaSet"}
    for kind in ("PersistentVolumeClaim", "DataVolume"):
        for value in rows[kind]:
            require(
                not value["metadata"].get("deletionTimestamp"),
                "managed_storage_deleting",
            )
            parent = by_uid[value["metadata"]["ownerReferences"][0]["uid"]]
            from helpers.native_custody import persistent_state
            require(
                parent["kind"] not in runtime_kinds | {"VirtualMachine", "Deployment"}
                or (custody and persistent_state(value, parent)),
                "managed_storage_not_retained",
            )
            if kind == "DataVolume":
                require(
                    value.get("status", {}).get("phase") == "Succeeded",
                    "managed_import_active",
                )
            else:
                verify_pvc(value, api)
    verify_mounts(rows)


def verify_mounts(rows):
    claims = {v["metadata"]["name"] for v in rows["PersistentVolumeClaim"]}
    for kind in (
        "VirtualMachine",
        "VirtualMachineInstance",
        "Deployment",
        "ReplicaSet",
        "Pod",
    ):
        for value in rows[kind]:
            spec = value.get("spec", {})
            if kind in {"VirtualMachine", "Deployment", "ReplicaSet"}:
                spec = spec.get("template", {}).get("spec", {})
            verify_volumes(spec, claims)


def verify_volumes(spec, claims):
    allowed = {
        "emptyDir",
        "persistentVolumeClaim",
        "secret",
        "configMap",
        "projected",
        "downwardAPI",
        "cloudInitNoCloud",
    }
    volumes = spec.get("volumes", [])
    require(isinstance(volumes, list) and len(volumes) <= 128)
    for volume in volumes:
        sources = set(volume) - {"name"}
        require(len(sources) == 1 and sources <= allowed, "managed_volume_unsupported")
        if "persistentVolumeClaim" in volume:
            require(
                volume["persistentVolumeClaim"].get("claimName") in claims,
                "managed_claim_missing",
            )


def verify_pvc(pvc, api):
    require(pvc.get("status", {}).get("phase") == "Bound", "managed_pvc_not_bound")
    pv = api.get("PersistentVolume", None, pvc["spec"]["volumeName"])
    require(
        not pv["metadata"].get("deletionTimestamp")
        and not pv["metadata"].get("ownerReferences"),
        "managed_pv_not_retained",
    )
    claim = pv["spec"]["claimRef"]
    require(
        all(claim[k] == pvc["metadata"][k] for k in ("name", "namespace", "uid")),
        "managed_pv_changed",
    )


def parent_kind(value, by_uid):
    from helpers.native_custody import bmc_shape, matches, writer_shape
    if not value["metadata"].get("ownerReferences"):
        roots = [v for v in by_uid.values() if v.get("kind") == "TroshkaProject"]
        if len(roots) == 1 and matches(value, roots[0]) and writer_shape(value, roots[0], by_uid):
            return "CustodyWriter"
        if len(roots) == 1 and matches(value, roots[0]) and bmc_shape(value, roots[0], by_uid):
            return "TroshkaProject"  # Explicit custody, never a fabricated ownerRef.
    return by_uid[value["metadata"]["ownerReferences"][0]["uid"]]["kind"]


def verify_workloads(root, rows, by_uid):
    verify_virtual_machines(rows, by_uid)
    verify_helpers(rows, by_uid)
    verify_pods(root, rows, by_uid)


def verify_virtual_machines(rows, by_uid):
    require(len(rows["VirtualMachine"]) == len(rows["TroshkaVM"]), "managed_vm_missing")
    for value in rows["VirtualMachine"]:
        require(parent_kind(value, by_uid) == "TroshkaVM")
        require(
            not value["spec"].get("dataVolumeTemplates")
            and not value.get("status", {}).get("volumeRequests")
        )
        spec = value["spec"]
        require(
            len({"running", "runStrategy"} & set(spec)) == 1, "managed_vm_mode_invalid"
        )
        require(
            spec.get("runStrategy", "Halted")
            in {"Always", "Halted", "RerunOnFailure", "Manual", "Once"}
        )


def verify_helpers(rows, by_uid):
    from helpers.native_custody import evidence
    custody = any(v.get("kind") == "TroshkaProject" and evidence(v) is not None for v in by_uid.values())
    for value in rows["Deployment"]:
        ceph = custody and parent_kind(value, by_uid) == "TroshkaCeph"
        require(ceph or parent_kind(value, by_uid) in {"TroshkaProject", "TroshkaNetwork"})
        name = value["metadata"]["name"]
        require(
            ceph or (custody and name.startswith("bmc-")) or name.startswith(("dnsmasq-", "gateway-", "exec-", "vnc-")),
            "managed_helper_unsupported",
        )
        require(
            type(value["spec"].get("replicas")) is int
            and value["spec"]["replicas"] >= 0
        )
    for value in rows["ReplicaSet"]:
        require(parent_kind(value, by_uid) == "Deployment")


def verify_pods(root, rows, by_uid):
    from helpers.native_custody import evidence
    for value in rows["VirtualMachineInstance"]:
        require(parent_kind(value, by_uid) == "VirtualMachine")
    for value in rows["Pod"]:
        require(
            parent_kind(value, by_uid)
            in ({"TroshkaProject", "ReplicaSet", "VirtualMachineInstance"}
                | ({"Job", "CustodyWriter"} if evidence(root) is not None else set()))
        )
        if parent_kind(value, by_uid) == "TroshkaProject":
            manifest = container_manifests(root, rows).get(value["metadata"]["name"])
            if manifest is None:
                raise ManagedPowerError("managed_bare_pod_unsupported")
            from helpers.native_custody import container_spec_matches
            require(
                container_spec_matches(manifest["spec"], value["spec"]) if evidence(root) is not None else manifest_matches(manifest["spec"], value["spec"]),
                "managed_container_changed",
            )


def manifest_matches(expected, actual, path=()):
    if isinstance(expected, dict):
        return manifest_dict_matches(expected, actual, path)
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                manifest_matches(a, b, path + ("[]",)) for a, b in zip(expected, actual)
            )
        )
    if len(path) >= 2 and path[-2] in {"limits", "requests"}:
        from kubernetes.utils.quantity import parse_quantity

        try:
            return parse_quantity(expected) == parse_quantity(actual)
        except (ValueError, TypeError):
            return False
    return expected == actual


def manifest_dict_matches(expected, actual, path):
    if not isinstance(actual, dict) or extra_intent_fields(expected, actual, path):
        return False
    for key, value in expected.items():
        if key not in actual:
            if value == [] or value == {}:
                continue  # Kubernetes omitempty preserves these empty defaults.
            return False
        if not manifest_matches(value, actual[key], path + (key,)):
            return False
    return True


def extra_intent_fields(expected, actual, path):
    # Ignore API-owned scheduling/default fields, but never silently accept new
    # commands, mounts, sidecars or resource demand absent from retained topology.
    fields = set()
    if not path:
        fields = {
            "containers",
            "initContainers",
            "volumes",
            "hostNetwork",
            "hostPID",
            "hostIPC",
        }
    elif path[-1] in {"resources", "limits", "requests"}:
        fields = set(actual)
    elif len(path) >= 2 and path[-2] in {"containers", "initContainers"}:
        fields = {
            "command",
            "args",
            "env",
            "envFrom",
            "volumeMounts",
            "volumeDevices",
            "resources",
            "ports",
            "workingDir",
            "lifecycle",
            "livenessProbe",
            "readinessProbe",
            "startupProbe",
            "restartPolicy",
        }
    return any(
        actual[k] not in ([], {}, None, False, "")
        for k in (set(actual) - set(expected)) & fields
    )


def container_manifests(root, rows):
    from helpers.containers import build_pod_group, build_single_container_pod
    from helpers.k8s import owner_ref
    from helpers.topology import (
        enrich_container_nics,
        enrich_showroom_infra_networks,
        extract_containers,
    )

    topology = deepcopy(root["spec"].get("topology", {}))
    containers = extract_containers(topology)
    enrich_container_nics(topology, containers)
    enrich_showroom_infra_networks(topology, containers)
    refs = {
        v["metadata"]["name"]: v["status"]["nadName"] for v in rows["TroshkaNetwork"]
    }
    # Matches _provision_container_pvcs without provisioning or resizing anything.
    from helpers.topology import collect_container_disk_mounts, container_disk_pvc_name

    pvcs = {
        disk: container_disk_pvc_name(ctr, disk)
        for ctr, disk, _ in collect_container_disk_mounts(topology)
    }
    result = {}
    for ctr in containers:
        builder = build_pod_group if ctr.get("isPod") else build_single_container_pod
        value = builder(root["metadata"]["namespace"], ctr, refs, owner_ref(root), pvcs)
        require(
            value["metadata"]["name"] not in result,
            "managed_container_identity_collision",
        )
        result[value["metadata"]["name"]] = value
    return result


def snapshot(root, rows, by_uid, request):
    manifests = container_manifests(root, rows)
    bare_names = {
        p["metadata"]["name"]
        for p in rows["Pod"]
        if parent_kind(p, by_uid) == "TroshkaProject"
    }
    require(set(manifests) == bare_names, "managed_container_not_settled")
    targets = []
    for kind in ("VirtualMachine", "Deployment", "Pod", "Job"):
        for value in rows[kind]:
            if kind == "Pod" and parent_kind(value, by_uid) not in {"TroshkaProject", "CustodyWriter"}:
                continue
            meta = value["metadata"]
            target = {"kind": kind, "name": meta["name"], "uid": meta["uid"]}
            if kind == "Pod":
                if parent_kind(value, by_uid) == "CustodyWriter":
                    target["custodyWriter"] = True
                else:
                    target["manifestDigest"] = digest(manifests[meta["name"]])
                    target["nodeId"] = container_node_id(root, value)
            else:
                target["desired"] = {
                    k: value["spec"][k]
                    for k in ("replicas", "running", "runStrategy")
                    if k in value["spec"]
                }
            targets.append(target)
    retained = retained_digest(rows)
    return {
        "requestId": request["requestId"],
        "allocationGeneration": request["allocationGeneration"],
        "mode": "stopped",
        "state": "stopping",
        "producerDrained": False,
        "topologyDigest": digest(root["spec"]),
        "targets": targets,
        "retainedDigest": retained,
        "inventoryDigest": digest({"targets": targets, "retainedDigest": retained}),
    }


def retained_digest(rows):
    result = []
    kinds = ["TroshkaVM", "TroshkaNetwork", "PersistentVolumeClaim", "DataVolume", "TroshkaCeph"]
    kinds.extend(sorted(SUPPORT_KINDS.intersection(rows)))
    for kind in kinds:
        for value in rows[kind]:
            meta = value["metadata"]
            result.append(
                {
                    "kind": kind,
                    "name": meta["name"],
                    "uid": meta["uid"],
                    "owners": meta.get("ownerReferences"),
                    "spec": {
                        k: v
                        for k, v in value.items()
                        if k not in {"apiVersion", "kind", "metadata", "status"}
                    }
                    if kind in SUPPORT_KINDS
                    else value["spec"],
                }
            )
    return digest(sorted(result, key=lambda v: (v["kind"], v["name"])))


def container_node_id(root, pod):
    from helpers.topology import extract_containers

    prefix = pod["metadata"].get("labels", {}).get("troshka-container")
    matches = [
        c["id"]
        for c in extract_containers(root["spec"].get("topology", {}))
        if c["id"][:8] == prefix
    ]
    require(len(matches) == 1, "managed_container_identity_changed")
    return matches[0]


def write_status(api, root, state):
    change = {"op": "add", "path": "/status/managedPower", "value": state}
    if "status" not in root:
        change = {"op": "add", "path": "/status", "value": {"managedPower": state}}
    return api.patch(root, [change], status=True)


def validate_snapshot(root, state, request):
    require(
        state.get("requestId") == request["requestId"]
        and state.get("allocationGeneration") == request["allocationGeneration"],
        "managed_episode_changed",
    )
    require(
        state.get("topologyDigest") == digest(root["spec"]), "managed_topology_changed"
    )
    require(
        state.get("inventoryDigest")
        == digest(
            {
                "targets": state.get("targets"),
                "retainedDigest": state.get("retainedDigest"),
            }
        ),
        "managed_snapshot_invalid",
    )


def reconcile_stop(api, root):
    try:
        begin = getattr(api, "begin_inventory", None)
        if begin is not None:
            begin()
        request = intent(root)
        if request is None:
            raise ManagedPowerError("managed_intent_missing")
        if request["mode"] == "running":
            reconcile_restore(api, root, request)
            return
        if request.get("pendingInitialization") is not None:
            from helpers.managed_deploy import reconcile_pending_cleanup

            reconcile_pending_cleanup(api, root)
            return
        rows, by_uid = inventory(api, root)
        profile(root, rows, by_uid, api)
        state = deepcopy(root.get("status", {}).get("managedPower"))
        if state is None or "inventoryDigest" not in state:
            state = snapshot(root, rows, by_uid, request)
            root = write_status(api, root, state)  # Persist originals BEFORE mutation.
        elif state.get("requestId") != request["requestId"]:
            state = superseding_stop(root, rows, state, request)
            root = write_status(api, root, state)
        validate_snapshot(root, state, request)
        require(
            state.get("retainedDigest") == retained_digest(rows),
            "managed_retained_identity_changed",
        )
        stop_targets(api, root, rows, state)
        current = root_for(root, root["metadata"]["namespace"], api)
        require(intent(current) == request, "managed_episode_changed")
        state.update(
            state="off" if off_inventory(rows) else "stopping", producerDrained=True
        )
        write_status(api, current, state)
    except ManagedPowerError as error:
        current = root_for(root, root["metadata"]["namespace"], api)
        state = deepcopy(current.get("status", {}).get("managedPower", {}))
        state.update(state="blocked", producerDrained=False, reason=str(error))
        write_status(api, current, state)


def superseding_stop(root, rows, state, request):
    """Carry desired originals through partial restore; bind recreated Pod UIDs."""
    validate_snapshot(root, state, {**request, "requestId": state["requestId"]})
    validate_restore_inventory(rows, state)
    targets = deepcopy(state["targets"])
    pods = {p["metadata"]["name"]: p for p in rows["Pod"]}
    manifests = container_manifests(root, rows)
    for target in targets:
        if target["kind"] != "Pod" or target["name"] not in pods:
            continue
        pod = pods[target["name"]]
        manifest = manifests[target["name"]]
        require(
            digest(manifest) == target["manifestDigest"]
            and manifest_matches(manifest["spec"], pod["spec"]),
            "managed_container_changed",
        )
        uid = pod["metadata"]["uid"]
        if uid != target["uid"]:
            require(
                bool(state.get("resumeRequestId"))
                and state.get("restoredPods", {}).get(target["name"], uid) == uid,
                "managed_target_changed",
            )
            target["uid"] = uid
    result = {k: deepcopy(state[k]) for k in ("topologyDigest", "retainedDigest")}
    result.update(
        requestId=request["requestId"],
        allocationGeneration=request["allocationGeneration"],
        mode="stopped",
        state="stopping",
        producerDrained=False,
        targets=targets,
    )
    result["inventoryDigest"] = digest(
        {"targets": targets, "retainedDigest": result["retainedDigest"]}
    )
    return result


def validate_restore(root, state, request):
    require(
        request.get("profile") == "configured-workloads-v1",
        "managed_resume_profile_invalid",
    )
    require(
        isinstance(request.get("executionDigest"), str)
        and bool(request["executionDigest"])
        and request.get("resumeOf") == state.get("requestId")
        and request.get("targetDigest") == state.get("inventoryDigest"),
        "managed_resume_target_changed",
    )
    previous = {**request, "mode": "stopped", "requestId": request["resumeOf"]}
    validate_snapshot(root, state, previous)
    require(state.get("producerDrained") is True, "managed_producer_not_drained")
    require(
        state.get("resumeRequestId") in (None, request["requestId"]),
        "managed_episode_changed",
    )


def reconcile_restore(api, root, request):
    state = deepcopy(root.get("status", {}).get("managedPower", {}))
    validate_restore(root, state, request)
    rows, by_uid = inventory(api, root)
    profile(root, rows, by_uid, api)
    require(
        state["retainedDigest"] == retained_digest(rows),
        "managed_retained_identity_changed",
    )
    validate_restore_inventory(rows, state)
    require(
        configuration_digest(rows) == request.get("configurationDigest"),
        "managed_configuration_changed",
    )
    if state.get("state") == "running":
        require(
            state.get("confirmedOperationId") == request["requestId"]
            and bool(state.get("confirmedObservationId")),
            "managed_confirmation_invalid",
        )
        return  # Completed managed plans never reopen ordinary producer paths.
    state.update(state="restoring", resumeRequestId=request["requestId"])
    root = write_status(api, root, state)
    manifests = container_manifests(root, rows)
    for target in state["targets"]:
        restore_target(api, root, request, target, state, manifests)
    current = root_for(root, root["metadata"]["namespace"], api)
    require(intent(current) == request, "managed_episode_changed")
    write_status(api, current, state)


def validate_restore_inventory(rows, state):
    for kind in ("VirtualMachine", "Deployment"):
        expected = {t["name"]: t["uid"] for t in state["targets"] if t["kind"] == kind}
        actual = {v["metadata"]["name"]: v["metadata"]["uid"] for v in rows[kind]}
        require(actual == expected, "managed_inventory_changed")


def configuration_digest(rows):
    """Power transitions may change desired state, never workload declarations."""
    values = []
    for kind in ("VirtualMachine", "Deployment"):
        for value in rows[kind]:
            values.append(
                {
                    "kind": kind,
                    "name": value["metadata"]["name"],
                    "uid": value["metadata"]["uid"],
                    "spec": {
                        k: v
                        for k, v in value["spec"].items()
                        if k not in {"running", "runStrategy", "replicas"}
                    },
                }
            )
    return digest(sorted(values, key=lambda v: (v["kind"], v["name"])))


def restore_target(api, root, request, target, state, manifests):
    namespace = root["metadata"]["namespace"]
    require(
        intent(root_for(root, namespace, api)) == request, "managed_episode_changed"
    )
    if target["kind"] == "Pod":
        restore_pod(api, root, target, state, manifests)
        return
    value = api.get(target["kind"], namespace, target["name"])
    require(
        value["metadata"]["uid"] == target["uid"]
        and not value["metadata"].get("deletionTimestamp"),
        "managed_target_changed",
    )
    desired = target["desired"]
    changes = [
        {"op": "remove", "path": "/spec/" + k}
        for k in ("running", "runStrategy")
        if k in value["spec"] and k not in desired
    ]
    changes.extend(
        {"op": "add", "path": "/spec/" + k, "value": v}
        for k, v in desired.items()
        if value["spec"].get(k) != v
    )
    if changes:
        api.patch(value, changes)


def restore_pod(api, root, target, state, manifests):
    manifest = manifests.get(target["name"])
    require(
        manifest is not None and digest(manifest) == target.get("manifestDigest"),
        "managed_container_changed",
    )
    require(
        container_node_id(root, manifest) == target.get("nodeId"),
        "managed_container_identity_changed",
    )
    namespace = root["metadata"]["namespace"]
    values = {p["metadata"]["name"]: p for p in api.rows("Pod", namespace)}
    pod = values.get(target["name"])
    if pod is None:
        # An ambiguous create is never retried: next reconciliation reads it by
        # its exact saved name, owner and manifest before proceeding.
        pod = api.request("POST", "Pod", namespace, None, manifest)
    require(not pod["metadata"].get("deletionTimestamp"), "managed_target_deleting")
    require(
        manifest_matches(manifest["spec"], pod["spec"]), "managed_container_changed"
    )
    require(
        pod["metadata"].get("ownerReferences")
        == manifest["metadata"]["ownerReferences"],
        "managed_owner_changed",
    )
    restored = state.setdefault("restoredPods", {})
    uid = pod["metadata"]["uid"]
    require(
        restored.get(target["name"], uid) == uid and uid != target["uid"],
        "managed_target_changed",
    )
    restored[target["name"]] = uid


def off_inventory(rows):
    if rows["Pod"] or rows["VirtualMachineInstance"] or rows.get("Job"):
        return False
    if any(
        v["metadata"].get("deletionTimestamp")
        for kind in ("VirtualMachine", "Deployment")
        for v in rows[kind]
    ):
        return False
    vms_off = all(
        v["spec"].get("runStrategy") == "Halted" and "running" not in v["spec"]
        for v in rows["VirtualMachine"]
    )
    helpers_off = all(
        v["spec"].get("replicas") == 0
        and v.get("status", {}).get("observedGeneration") == v["metadata"]["generation"]
        for v in rows["Deployment"]
    )
    replicas_off = all(v["spec"].get("replicas") == 0 for v in rows["ReplicaSet"])
    return vms_off and helpers_off and replicas_off


def stop_targets(api, root, rows, state):
    namespace = root["metadata"]["namespace"]
    indexed = {
        (v["kind"], v["metadata"]["name"]): v
        for values in rows.values()
        for v in values
    }
    expected = {(t["kind"], t["name"]): t for t in state["targets"]}
    if not drain_writers(api, root, indexed, state):
        return
    if not drain_bmc(api, root, rows):
        return
    for kind in ("VirtualMachine", "Deployment"):
        require(
            {v["metadata"]["name"] for v in rows[kind]}
            == {t["name"] for t in state["targets"] if t["kind"] == kind},
            "managed_inventory_changed",
        )
    for key, target in expected.items():
        value = indexed.get(key)
        if value is None:
            require(target["kind"] in {"Pod", "Job"}, "managed_target_missing")
            continue
        require(value["metadata"]["uid"] == target["uid"], "managed_target_changed")
        require(
            intent(root_for(root, namespace, api)) == intent(root),
            "managed_episode_changed",
        )
        stop_target(api, value)


def drain_writers(api, root, indexed, state):
    """Delete the pinned backend writers and observe absence before VM stop."""
    from helpers.native_custody import matches
    present = []
    for target in state["targets"]:
        if not target.get("custodyWriter"):
            continue
        value = indexed.get(("Pod", target["name"]))
        if value is not None:
            require(value["metadata"]["uid"] == target["uid"] and matches(value, root), "custody_writer_changed")
            require(intent(root_for(root, root["metadata"]["namespace"], api)) == intent(root), "managed_episode_changed")
            present.append(value)
    for value in present:
        stop_target(api, value)
    return not present


def drain_bmc(api, root, rows):
    """BMC can mutate power/disks: prove its pods gone before touching VMs."""
    from helpers.native_custody import evidence
    if evidence(root) is None:
        return True
    deployments = [v for v in rows["Deployment"] if v["metadata"]["name"].startswith("bmc-")]
    for value in deployments:
        stop_target(api, value)
    uids = {v["metadata"]["uid"] for v in deployments}
    replicas = [v for v in rows["ReplicaSet"] if any(r.get("uid") in uids for r in v["metadata"].get("ownerReferences", []))]
    uids.update(v["metadata"]["uid"] for v in replicas)
    pods = [v for v in rows["Pod"] if any(r.get("uid") in uids for r in v["metadata"].get("ownerReferences", []))]
    return not pods and all(v["spec"].get("replicas") == 0 and v.get("status", {}).get("observedGeneration") == v["metadata"]["generation"] for v in deployments) and all(v["spec"].get("replicas") == 0 for v in replicas)


def stop_target(api, value):
    kind, spec = value["kind"], value.get("spec", {})
    if kind in {"Pod", "Job"}:
        if not value["metadata"].get("deletionTimestamp"):
            api.request(
                "DELETE",
                kind,
                value["metadata"]["namespace"],
                value["metadata"]["name"],
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {
                        k: value["metadata"][k] for k in ("uid", "resourceVersion")
                    },
                },
            )
        return
    field, desired = (
        ("replicas", 0) if kind == "Deployment" else ("runStrategy", "Halted")
    )
    if spec.get(field) == desired:
        return
    changes = []
    if kind == "VirtualMachine" and "running" in spec:
        changes.append({"op": "remove", "path": "/spec/running"})
    changes.append({"op": "add", "path": "/spec/" + field, "value": desired})
    api.patch(value, changes)
