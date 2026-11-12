"""Opt-in direct workload inventory. No application, database or ledger imports."""

import json
from datetime import UTC, datetime
from urllib.parse import quote

from urllib3 import Timeout

from app.schemas.native_workload_observations import (
    WorkloadCollection,
    WorkloadObservation,
    WorkloadResource,
)
from app.services.kubevirt_storage_inventory import KubernetesReader, _proof_root

KINDS = (
    "VirtualMachine",
    "VirtualMachineInstance",
    "Pod",
    "Deployment",
    "ReplicaSet",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "PersistentVolumeClaim",
    "DataVolume",
)
_PHASES = {"Pending", "Running", "Succeeded", "Failed", "Unknown", "Bound", "Lost"}


def _identity(reader, root):
    if root.deployment_generation is None:
        return "project_generation_required"
    try:
        if reader.cluster_identity() != root.cluster_key:
            return "cluster_identity_mismatch"
        return _proof_root(reader, root, [])
    except Exception:
        # API exceptions can contain bearer tokens, response bodies and URLs.
        return "identity_unavailable"


def _compute_state(kind, phase, paused, deleting):
    if kind not in {"Pod", "VirtualMachineInstance"}:
        return "not_runtime"
    if deleting or paused or phase in {"Running", "Pending"}:
        return "potentially_active"
    if phase in {"Succeeded", "Failed"}:
        return "terminal_observed"
    return "unknown"


def _resource(body, kind, namespace):
    meta = body["metadata"]
    if meta["namespace"] != namespace:
        raise ValueError("namespace mismatch")
    status, spec = body.get("status", {}), body.get("spec", {})
    phase = status.get("phase", "Unset")
    if phase not in _PHASES | {"Unset"}:
        phase = "Other"
    paused = any(
        c.get("type") == "Paused" and c.get("status") == "True"
        for c in status.get("conditions", [])
    )
    running = spec.get("running") if kind == "VirtualMachine" else None
    if kind == "VirtualMachine" and spec.get("runStrategy") == "Halted":
        running = False
    replicas = (
        spec.get("replicas")
        if kind in {"Deployment", "ReplicaSet", "StatefulSet"}
        else None
    )
    if running is not None and not isinstance(running, bool):
        raise ValueError("invalid running state")
    if replicas is not None and (type(replicas) is not int or replicas < 0):
        raise ValueError("invalid replica state")
    return WorkloadResource(
        name=meta["name"],
        uid=meta["uid"],
        resource_version=meta["resourceVersion"],
        owner_uids=[o["uid"] for o in meta.get("ownerReferences", [])],
        deleting=bool(meta.get("deletionTimestamp")),
        phase=phase,
        paused=paused,
        compute_state=_compute_state(
            kind, phase, paused, bool(meta.get("deletionTimestamp"))
        ),
        desired_running=running,
        desired_replicas=replicas,
    )


def _resources(payload, kind, namespace, max_objects):
    items, metadata = payload["items"], payload["metadata"]
    if (
        not isinstance(items, list)
        or len(items) > max_objects
        or metadata.get("continue")
    ):
        raise ValueError("incomplete list")
    resources = [_resource(body, kind, namespace) for body in items]
    if len({r.uid for r in resources}) != len(resources) or len(
        {r.name for r in resources}
    ) != len(resources):
        raise ValueError("duplicate resource identity")
    return metadata["resourceVersion"], resources


def _collect(reader, kind, root, clock, max_objects):
    started = clock()
    try:
        payload = reader.workloads(kind, root.namespace)
    except TimeoutError:
        status = "timeout"
    except Exception:
        status = "unavailable"
    else:
        try:
            rv, resources = _resources(payload, kind, root.namespace, max_objects)
            return WorkloadCollection(
                kind=kind,
                started_at=started,
                finished_at=clock(),
                status="complete",
                list_resource_version=rv,
                resources=resources,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            status = "invalid_output"
    return WorkloadCollection(
        kind=kind, started_at=started, finished_at=clock(), status=status
    )


def observe_workloads(reader, root, *, collector_id, clock=None, max_objects=10000):
    """Inspect named kinds across the whole namespace, never prove quiescence.

    A list is a separate API observation, not an atomic cross-kind snapshot.
    Unowned objects are included. Ownership UID chains are not proven here.
    """
    if not 1 <= max_objects <= 10000:
        raise ValueError("object bound must be 1..10000")
    clock = clock or (lambda: datetime.now(UTC))
    started, collections, blockers = clock(), [], []
    reason = _identity(reader, root)
    if reason:
        blockers.append(reason)
    else:
        collections = [
            _collect(reader, kind, root, clock, max_objects) for kind in KINDS
        ]
        reason = _identity(reader, root)
        if reason:
            blockers.append(reason)
    complete = (
        not blockers
        and len(collections) == len(KINDS)
        and all(c.status == "complete" for c in collections)
    )
    return WorkloadObservation(
        collector_id=collector_id,
        root=root,
        started_at=started,
        finished_at=clock(),
        inventory_complete=complete,
        collections=collections,
        blockers=blockers,
    )


_ENDPOINTS = {
    "Pod": ("/api/v1", "pods"),
    "PersistentVolumeClaim": ("/api/v1", "persistentvolumeclaims"),
    "VirtualMachine": ("/apis/kubevirt.io/v1", "virtualmachines"),
    "VirtualMachineInstance": ("/apis/kubevirt.io/v1", "virtualmachineinstances"),
    "DataVolume": ("/apis/cdi.kubevirt.io/v1beta1", "datavolumes"),
    **{
        kind: ("/apis/apps/v1", plural)
        for kind, plural in (
            ("Deployment", "deployments"),
            ("ReplicaSet", "replicasets"),
            ("StatefulSet", "statefulsets"),
            ("DaemonSet", "daemonsets"),
        )
    },
    "Job": ("/apis/batch/v1", "jobs"),
    "CronJob": ("/apis/batch/v1", "cronjobs"),
}


class WorkloadReader(KubernetesReader):
    """Static kubeconfig transport, bounded GET only; never invokes exec/auth plugins.

    Truncated lists fail closed rather than following unbounded continuation pages.
    API versions are explicit; missing APIs are unavailable, never empty evidence.
    """

    max_response_bytes = 4 * 1024 * 1024

    def _get(self, path, *, listing=False):
        # ApiClient.call_api raises on HTTP errors by reading response.data in
        # full. Reuse its configured TLS/proxy pool and auth, but inspect every
        # status ourselves before any body is materialized. No redirects/retries
        # may drain an intermediate body or send credentials to another target.
        query = [("limit", str(self.max_objects + 1))] if listing else []
        headers = {**self.api.default_headers, "Accept": "application/json"}
        self.api.update_params_for_auth(headers, query, ["BearerToken"])
        response = self.api.rest_client.pool_manager.request(
            "GET",
            self.api.configuration.host + path,
            fields=query,
            headers=headers,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=Timeout(total=self._timeout()),
        )
        try:
            if not 200 <= response.status <= 299:
                raise ValueError("HTTP response unavailable")
            data = response.read(self.max_response_bytes + 1)
            if len(data) > self.max_response_bytes:
                raise ValueError("response limit")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            return payload
        finally:
            response.close()

    def namespace(self, name):
        return self._get("/api/v1/namespaces/" + quote(name, safe=""))

    def projects(self, namespace):
        payload = self._get(
            "/apis/troshka.redhat.com/v1alpha1/namespaces/"
            + quote(namespace, safe="")
            + "/troshkaprojects",
            listing=True,
        )
        return self._items(payload)

    def workloads(self, kind, namespace):
        prefix, resource = _ENDPOINTS[kind]
        return self._get(
            f"{prefix}/namespaces/{quote(namespace, safe='')}/{resource}", listing=True
        )
