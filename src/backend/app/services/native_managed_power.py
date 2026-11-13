"""Quota-managed native stop handoff and independent bounded off observation.

No database transaction spans these Kubernetes calls. Start remains unavailable
until native admission/claim and retained demand are connected.
"""

import importlib
import json
from datetime import UTC, datetime

from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_storage import ProjectRoot
from app.services import quota_operations as operations
from app.services.quota_accounting import AccountingUnavailable, utc


def helpers():
    from app.services.quota_native import _helpers

    _helpers()
    return importlib.import_module("helpers.managed_power")


def retained_context(db, project, rows):
    """Pure retained identity validation under the existing ledger locks."""
    from app.services.quota_storage import measurement_row, storage_adapter

    if not rows or any(storage_adapter(r) != "kubevirt" for r in rows):
        raise AccountingUnavailable("Native managed allocation identity unavailable")
    row = measurement_row(rows)
    identity = row.dispatch_metadata.get("native_storage_identity")
    owner = db.get(User, project.owner_id)
    host = db.get(Host, project.host_id)
    provider = db.get(
        Provider, project.provider_id or getattr(host, "provider_id", None)
    )
    if not identity or owner is None or host is None or provider is None:
        raise AccountingUnavailable("Native managed allocation identity unavailable")
    if (
        host.host_type != "kubevirt-cluster"
        or provider.type != "kubevirt"
        or (host.billing_mode or "shared") != "shared"
    ):
        raise AccountingUnavailable("Native managed provider changed")
    _validate_rows(project, rows, identity, owner)
    root = ProjectRoot.model_validate(identity["root"])
    if (
        str(root.project_id) != project.id
        or root.deployment_generation is None
        or identity["host_id"] != host.id
        or identity["provider_id"] != provider.id
    ):
        raise AccountingUnavailable("Native managed root changed")
    return {
        "root": root.model_dump(mode="json"),
        "provider_id": provider.id,
        "generation": operations._generation(row),
        "identity": identity,
    }


def _validate_rows(project, rows, identity, owner):
    for row in rows:
        if (
            row.project_id != project.id
            or row.owner_id != owner.id
            or row.organization_id != owner.organization_id
            or row.dispatch_metadata.get("native_storage_identity") != identity
            or row.state != "confirmed"
            or not operations._settled(row)
        ):
            raise AccountingUnavailable(
                "Native managed allocation unsettled or changed"
            )


def _live_root(api, context, request_id):
    module = helpers()
    root = ProjectRoot.model_validate(context["root"])
    # Namespace UID is independently reread before AND after the bounded lists.
    value = api.get("Namespace", None, root.namespace)
    module.require(
        api.get("Namespace", None, "kube-system")["metadata"]["uid"]
        == root.cluster_key,
        "managed_cluster_changed",
    )
    module.require(
        value["metadata"]["uid"] == root.namespace_uid
        and not value["metadata"].get("deletionTimestamp"),
        "managed_namespace_changed",
    )
    projects = api.rows("TroshkaProject", root.namespace)
    module.require(len(projects) == 1, "managed_root_changed")
    project = projects[0]
    meta = project["metadata"]
    module.require(
        meta["uid"] == root.project_cr_uid
        and meta["generation"] == root.deployment_generation
        and project["spec"]["projectId"] == str(root.project_id)
        and not meta.get("deletionTimestamp"),
        "managed_root_changed",
    )
    return project


def stop_and_observe(api, context, request_id, *, clock=None):
    """Annotation publication is not proof. Return fresh complete off proof only."""
    clock = clock or (lambda: datetime.now(UTC))
    module = helpers()
    started = clock()
    if isinstance(api, module.PowerClient):
        api.begin_inventory()
    root = _live_root(api, context, request_id)
    wanted = {
        "version": 1,
        "mode": "stopped",
        "requestId": request_id,
        "allocationGeneration": context["generation"],
    }
    current = module.intent(root)
    if current != wanted:
        module.require(current is None, "managed_other_episode_unresolved")
        annotations = {
            **root["metadata"].get("annotations", {}),
            module.ANNOTATION: json.dumps(
                wanted, sort_keys=True, separators=(",", ":")
            ),
        }
        root = api.patch(
            root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}]
        )
    return observe_off(api, context, request_id, started, clock)


def observe_off(api, context, request_id, started, clock):
    module = helpers()
    root = _live_root(api, context, request_id)
    request = module.intent(root)
    module.require(
        request is not None
        and request["mode"] == "stopped"
        and request["requestId"] == request_id
        and request["allocationGeneration"] == context["generation"],
        "managed_episode_changed",
    )
    state = root.get("status", {}).get("managedPower", {})
    if not state.get("producerDrained") or state.get("state") not in {
        "stopping",
        "off",
    }:
        return None
    module.validate_snapshot(root, state, request)
    rows, by_uid = module.inventory(api, root)
    module.profile(root, rows, by_uid, api)
    module.require(
        state.get("retainedDigest") == module.retained_digest(rows),
        "managed_retained_identity_changed",
    )
    verify_off(rows, state, module)
    final_root = _live_root(api, context, request_id)
    module.require(
        module.intent(final_root) == request
        and final_root.get("status", {}).get("managedPower") == state,
        "managed_episode_changed",
    )
    finished = clock()
    module.require(
        0 <= (utc(finished) - utc(started)).total_seconds() <= 60,
        "managed_observation_stale",
    )
    return {
        "request_id": request_id,
        "generation": context["generation"],
        "started_at": started.isoformat(),
        "observed_at": finished.isoformat(),
        "inventory_digest": state["inventoryDigest"],
    }


def verify_off(rows, state, module):
    # Even terminal runtime objects must be absent in this bounded first profile.
    module.require(
        not rows["Pod"] and not rows["VirtualMachineInstance"],
        "managed_runtime_present",
    )
    for kind in ("VirtualMachine", "Deployment"):
        expected = {t["name"]: t["uid"] for t in state["targets"] if t["kind"] == kind}
        actual = {v["metadata"]["name"]: v["metadata"]["uid"] for v in rows[kind]}
        module.require(actual == expected, "managed_inventory_changed")
        for value in rows[kind]:
            module.require(
                not value["metadata"].get("deletionTimestamp"),
                "managed_target_deleting",
            )
            spec, status = value["spec"], value.get("status", {})
            if kind == "VirtualMachine":
                module.require(
                    spec.get("runStrategy") == "Halted" and "running" not in spec,
                    "managed_vm_not_halted",
                )
            else:
                module.require(
                    spec.get("replicas") == 0
                    and status.get("observedGeneration")
                    == value["metadata"]["generation"],
                    "managed_controller_unsettled",
                )
    module.require(
        all(v["spec"].get("replicas") == 0 for v in rows["ReplicaSet"]),
        "managed_replicaset_active",
    )


def execute(provider, context, request_id):
    from app.services.providers.kubevirt import _get_k8s_clients

    _, _, api_client = _get_k8s_clients(provider)
    try:
        return stop_and_observe(helpers().PowerClient(api_client), context, request_id)
    finally:
        api_client.close()
