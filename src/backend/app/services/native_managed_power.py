"""Managed native retained resume, durable handoff and independent power proof.

Only the retained configured-workload profile executes. First deploy remains
unavailable. Kubernetes transport runs outside database transactions/locks.
"""

import importlib
import json
from copy import deepcopy
from datetime import UTC, datetime

from app.models.host import Host
from app.models.provider import Provider
from app.models.user import User
from app.schemas.kubevirt_storage import ProjectRoot
from app.schemas.quota import Resources
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


def stop_context(db, project, rows):
    """Allow only the exact managed resume row alongside settled originals."""
    pending = [r for r in rows if r.state != "confirmed"]
    if not pending:
        return retained_context(db, project, rows)
    if len(pending) != 1:
        raise AccountingUnavailable("Native stop has unexplained unsettled work")
    target = validate_claimed_context(db, project, pending[0])
    return {**target["context"], "supersedes": target["resume_of"]}


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
        module.require(
            current is None
            or current.get("mode") == "running"
            or (
                current.get("mode") == "stopped"
                and current.get("requestId") == context.get("supersedes")
                and current.get("allocationGeneration") == context["generation"]
            ),
            "managed_other_episode_unresolved",
        )
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
        "observation_id": module.digest(
            {
                "rows": rows,
                "request": request,
                "started_at": started.isoformat(),
                "observed_at": finished.isoformat(),
            }
        ),
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


def with_client(provider, function, *args):
    from app.services.providers.kubevirt import _get_k8s_clients

    _, _, api_client = _get_k8s_clients(provider)
    try:
        return function(helpers().PowerClient(api_client), *args)
    finally:
        api_client.close()


def resume_target(api, context, *, clock=None):
    """Observe stopped originals and render exact retained workload demand."""
    clock = clock or (lambda: datetime.now(UTC))
    module = helpers()
    if isinstance(api, module.PowerClient):
        api.begin_inventory()
    root = _live_root(api, context, None)
    request = module.intent(root)
    module.require(
        request is not None and request["mode"] == "stopped", "managed_stop_required"
    )
    proof = observe_off(api, context, request["requestId"], clock(), clock)
    module.require(proof is not None, "managed_off_proof_required")
    rows, by_uid = module.inventory(api, root)
    module.profile(root, rows, by_uid, api)
    state = root["status"]["managedPower"]
    profile = configured_demand(root, rows, state)
    result = {
        "context": context,
        "resume_of": request["requestId"],
        "snapshot": deepcopy(state),
        "root_spec": {"topology": deepcopy(root["spec"].get("topology", {}))},
        "root_spec_digest": module.digest(root["spec"]),
        "profile": "configured-workloads-v1",
        "demand": profile,
        "off_proof": proof,
        "storage_inventory": sorted(
            v["metadata"]["uid"] for v in rows["PersistentVolumeClaim"]
        ),
        "configuration_digest": module.configuration_digest(rows),
    }
    result["execution_digest"] = module.digest(
        {k: v for k, v in result.items() if k != "off_proof"}
    )
    module.require(
        module.intent(_live_root(api, context, None)) == request,
        "managed_episode_changed",
    )
    return result


def configured_demand(root, rows, state):
    from decimal import ROUND_CEILING, Decimal

    from kubernetes.utils.quantity import parse_quantity

    from app.services.quota_native import (
        _helpers,
        _render_vms,
        demand_for_manifest,
        resource_id,
    )

    module = helpers()
    topology = root["spec"].get("topology", {})
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        module.require(
            not any(
                data.get(k)
                for k in (
                    "isPod",
                    "podContainers",
                    "initContainers",
                    "tpm",
                    "tpmEnabled",
                    "guestfishCommands",
                )
            ),
            "managed_configured_workload_unmapped",
        )
        module.require(
            not data.get("firmware", "bios").startswith("uefi"),
            "managed_firmware_unsupported",
        )
    rendered, _ = _render_vms(root, deepcopy(topology), _helpers())
    expected = {resource_id(v): v for v in rendered if v["kind"] == "VirtualMachine"}
    cpu, ram, evidence = Decimal(0), 0, []
    targets = {t["name"]: t for t in state["targets"] if t["kind"] == "VirtualMachine"}
    for vm in rows["VirtualMachine"]:
        planned = expected.get(resource_id(vm))
        module.require(
            planned is not None
            and demand_for_manifest(vm) == demand_for_manifest(planned),
            "managed_configured_workload_changed",
        )
        desired = targets[vm["metadata"]["name"]]["desired"]
        module.require(
            desired.get("runStrategy", "Always") in {"Always", "Halted"},
            "managed_vm_power_profile_unsupported",
        )
        if desired_running(desired):
            domain = vm["spec"]["template"]["spec"]["domain"]
            cfg = domain["cpu"]
            cpu += Decimal(
                cfg.get("cores", 1) * cfg.get("sockets", 1) * cfg.get("threads", 1)
            )
            memory = demand_for_manifest(vm).ram_bytes
            if memory is None:
                raise AccountingUnavailable("Native configured VM memory unavailable")
            ram += memory
        evidence.append(
            {
                "identity": resource_id(vm),
                "uid": vm["metadata"]["uid"],
                "specDigest": module.digest(vm["spec"]["template"]),
                "desired": targets[vm["metadata"]["name"]]["desired"],
            }
        )
    module.require(
        set(expected) == {resource_id(v) for v in rows["VirtualMachine"]},
        "managed_configured_workload_changed",
    )
    for pod in module.container_manifests(root, rows).values():
        resources = pod["spec"]["containers"][0]["resources"]["requests"]
        cpu += parse_quantity(resources["cpu"])
        ram += int(parse_quantity(resources["memory"]))
        evidence.append(
            {"identity": resource_id(pod), "manifestDigest": module.digest(pod)}
        )
    module.require(bool(evidence), "managed_configured_workload_missing")
    return {
        "cpu": int(cpu.to_integral_value(rounding=ROUND_CEILING)),
        "ram_bytes": ram,
        "configured_cpu": str(cpu),
        "sources": evidence,
    }


def desired_running(desired):
    return desired.get("running", desired.get("runStrategy") != "Halted")


def running_request(target, operation_id):
    return {
        "version": 1,
        "mode": "running",
        "requestId": operation_id,
        "allocationGeneration": target["context"]["generation"],
        "resumeOf": target["resume_of"],
        "executionDigest": target["execution_digest"],
        "profile": target["profile"],
        "targetDigest": target["snapshot"]["inventoryDigest"],
        "configurationDigest": target["configuration_digest"],
    }


def publish_resume(api, target, operation_id):
    module = helpers()
    root = _live_root(api, target["context"], operation_id)
    wanted = running_request(target, operation_id)
    current = module.intent(root)
    if current == wanted:
        return
    module.require(
        current
        == {
            "version": 1,
            "mode": "stopped",
            "requestId": target["resume_of"],
            "allocationGeneration": target["context"]["generation"],
        },
        "managed_episode_changed",
    )
    module.require(
        root.get("status", {}).get("managedPower") == target["snapshot"],
        "managed_snapshot_changed",
    )
    module.validate_restore(root, target["snapshot"], wanted)
    annotations = {
        **root["metadata"].get("annotations", {}),
        module.ANNOTATION: json.dumps(wanted, sort_keys=True, separators=(",", ":")),
    }
    api.patch(
        root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}]
    )


def observe_running(
    api, target, operation_id, *, clock=None, expected_confirmation=None
):
    clock = clock or (lambda: datetime.now(UTC))
    module, started = helpers(), clock()
    if isinstance(api, module.PowerClient):
        api.begin_inventory()
    root = _live_root(api, target["context"], operation_id)
    request = running_request(target, operation_id)
    module.require(module.intent(root) == request, "managed_episode_changed")
    state = root.get("status", {}).get("managedPower", {})
    module.validate_restore(root, state, request)
    module.require(
        state.get("resumeRequestId") == operation_id
        and (
            state.get("state") == "restoring"
            or (
                state.get("state") == "running"
                and expected_confirmation is not None
                and state.get("confirmedOperationId") == operation_id
                and state.get("confirmedObservationId") == expected_confirmation
            )
        ),
        "managed_restore_pending",
    )
    rows, by_uid = module.inventory(api, root)
    module.profile(root, rows, by_uid, api)
    module.validate_restore_inventory(rows, state)
    module.require(
        module.configuration_digest(rows) == target["configuration_digest"],
        "managed_configuration_changed",
    )
    module.require(
        module.retained_digest(rows) == state["retainedDigest"],
        "managed_retained_identity_changed",
    )
    module.require(
        configured_demand(root, rows, state) == target["demand"],
        "managed_configured_workload_changed",
    )
    verify_running(rows, by_uid, state, module)
    final = _live_root(api, target["context"], operation_id)
    module.require(
        module.intent(final) == request
        and final.get("status", {}).get("managedPower") == state,
        "managed_episode_changed",
    )
    finished = clock()
    module.require(
        0 <= (utc(finished) - utc(started)).total_seconds() <= 60,
        "managed_observation_stale",
    )
    module.require(
        module.digest(root["spec"]) == target["root_spec_digest"],
        "managed_topology_changed",
    )
    return {
        "request_id": operation_id,
        "resume_of": target["resume_of"],
        "generation": target["context"]["generation"],
        "execution_digest": target["execution_digest"],
        "observed_at": finished.isoformat(),
        "started_at": started.isoformat(),
        "observation_id": module.digest(
            {
                "rows": rows,
                "request": request,
                "started_at": started.isoformat(),
                "observed_at": finished.isoformat(),
            }
        ),
    }


def ready(value):
    status = value.get("status", {})
    conditions = status.get("conditions", [])
    return (
        not value["metadata"].get("deletionTimestamp")
        and status.get("phase") == "Running"
        and any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        )
        and not any(
            c.get("type") == "Paused" and c.get("status") == "True" for c in conditions
        )
    )


def verify_running(rows, by_uid, state, module):
    expected_vms, expected_pods = set(), set()
    for target in state["targets"]:
        kind, name = target["kind"], target["name"]
        value = next((v for v in rows[kind] if v["metadata"]["name"] == name), None)
        if value is None:
            raise module.ManagedPowerError("managed_target_missing")
        if kind == "Pod":
            module.require(
                value["metadata"]["uid"] == state.get("restoredPods", {}).get(name)
                and ready(value),
                "managed_pod_pending",
            )
            expected_pods.add(value["metadata"]["uid"])
        else:
            module.require(
                all(value["spec"].get(k) == v for k, v in target["desired"].items()),
                "managed_target_pending",
            )
            if kind == "VirtualMachine" and desired_running(target["desired"]):
                expected_vms.add(target["uid"])
            if kind == "Deployment":
                verify_helper_running(
                    value, rows, target["desired"]["replicas"], module
                )
    verify_vmis(rows, by_uid, expected_vms, module)
    verify_runtime_pods(rows, by_uid, expected_pods, module)


def verify_vmis(rows, by_uid, expected_vms, module):
    vmis = rows["VirtualMachineInstance"]
    module.require(
        {v["metadata"]["ownerReferences"][0]["uid"] for v in vmis} == expected_vms
        and len(vmis) == len(expected_vms)
        and all(ready(v) for v in vmis),
        "managed_vm_pending",
    )
    for vmi in vmis:
        vm = by_uid[vmi["metadata"]["ownerReferences"][0]["uid"]]
        module.require(
            module.manifest_matches(vm["spec"]["template"]["spec"], vmi["spec"]),
            "managed_vm_configuration_changed",
        )
        from app.services.quota_native import demand_for_manifest

        actual = {**vm, "spec": {"template": {"spec": vmi["spec"]}}}
        module.require(
            demand_for_manifest(actual) == demand_for_manifest(vm),
            "managed_vm_configuration_changed",
        )


def verify_helper_running(value, rows, replicas, module):
    status = value.get("status", {})
    module.require(
        status.get("observedGeneration") == value["metadata"]["generation"]
        and all(
            status.get(k, 0) == replicas
            for k in (
                "replicas",
                "readyReplicas",
                "availableReplicas",
                "updatedReplicas",
            )
        ),
        "managed_helper_pending",
    )
    sets = [
        v
        for v in rows["ReplicaSet"]
        if v["metadata"]["ownerReferences"][0]["uid"] == value["metadata"]["uid"]
    ]
    module.require(
        sum(v["spec"].get("replicas", 0) for v in sets) == replicas,
        "managed_replicaset_pending",
    )
    for rs in sets:
        expected = value["spec"]["template"]["spec"]
        module.require(
            module.manifest_matches(expected, rs["spec"]["template"]["spec"]),
            "managed_helper_configuration_changed",
        )
        pods = [
            v
            for v in rows["Pod"]
            if v["metadata"]["ownerReferences"][0]["uid"] == rs["metadata"]["uid"]
        ]
        module.require(
            len(pods) == rs["spec"].get("replicas", 0) and all(ready(v) for v in pods),
            "managed_helper_pending",
        )
        module.require(
            all(module.manifest_matches(expected, p["spec"]) for p in pods),
            "managed_helper_configuration_changed",
        )


def verify_runtime_pods(rows, by_uid, bare, module):
    for pod in rows["Pod"]:
        parent = by_uid[pod["metadata"]["ownerReferences"][0]["uid"]]
        module.require(ready(pod), "managed_pod_pending")
        if parent["kind"] == "TroshkaProject":
            module.require(pod["metadata"]["uid"] in bare, "managed_inventory_changed")
    for vmi in rows["VirtualMachineInstance"]:
        pods = [
            p
            for p in rows["Pod"]
            if p["metadata"]["ownerReferences"][0]["uid"] == vmi["metadata"]["uid"]
        ]
        module.require(len(pods) == 1, "managed_launcher_pending")


def supported(project, host):
    if (
        not host
        or host.host_type != "kubevirt-cluster"
        or project.host_assignments
        or project.mesh_subnet_id
    ):
        raise AccountingUnavailable(
            "Managed native resume requires its retained single native host"
        )
    if project.topology != operations.retained_topology(project):
        raise AccountingUnavailable("Native retained topology changed")


def preflight(project_id):
    from app.core.database import SessionLocal

    with SessionLocal() as reader:
        from app.models.project import Project

        project = reader.get(Project, project_id)
        rows = operations.current_generation(operations._rows(reader, project_id))
        context = retained_context(reader, project, rows)
        provider = reader.get(Provider, context["provider_id"])
        reader.expunge(provider)
    return with_client(provider, resume_target, context)


def originals(rows):
    return sorted(
        (
            {
                "id": r.operation_id,
                "observed": {
                    k: v for k, v in r.observed.items() if k != "storage_bytes"
                },
                "identity": r.dispatch_metadata.get("native_storage_identity"),
                "generation": operations._generation(r),
                "observation_id": r.observation_id,
            }
            for r in rows
        ),
        key=lambda v: str(v["id"]),
    )


def prepare(db, project, kind, *, host_id=None):
    from uuid import uuid4

    from app.models.quota import QuotaReservation
    from app.services import quota_admission as admission
    from app.services import quota_storage
    from app.services.quota_locks import lock_project

    if kind != "start":
        raise AccountingUnavailable("Native first-deploy admission remains unavailable")
    if host_id not in (None, project.host_id):
        raise AccountingUnavailable("Native resume cannot move its retained host")
    supported(project, db.get(Host, project.host_id))
    if db.new or db.dirty or db.deleted:
        raise AccountingUnavailable(
            "Persist retained configuration before native resume"
        )
    project_id = project.id
    db.rollback()  # End the read-only request transaction before provider I/O.
    try:
        target = preflight(project_id)
    except Exception as error:
        raise AccountingUnavailable("Native stopped inventory unavailable") from error
    operation_id = str(uuid4())
    previous = operations.current_generation(operations._rows(db, project_id))
    admission.lock_operations(db, [operation_id, *[r.operation_id for r in previous]])
    project, _, _ = lock_project(db, project_id)
    rows = operations.current_generation(operations._rows(db, project.id))
    context = retained_context(db, project, rows)
    if context != target["context"] or target["root_spec"].get(
        "topology", {}
    ) != operations.retained_topology(project):
        raise AccountingUnavailable("Native retained resume identity changed")
    measurement = quota_storage._native_measurement(db, project, rows, operations.now())
    validate_storage_inventory(db, project, target, measurement)
    root = quota_storage.measurement_row(rows)
    if root.dispatch_metadata.get("native_storage_evidence") != measurement.evidence():
        raise AccountingUnavailable(
            "Native current physical measurement requires reconciliation"
        )
    current = admission.usage(db, "project", project.id)[0]
    if current.cpu or current.ram_bytes:
        raise AccountingUnavailable("Native managed stop has not released compute")
    increment = Resources(
        cpu=target["demand"]["cpu"], ram_bytes=target["demand"]["ram_bytes"]
    )
    validate_stopped_collection(db, project, rows, target)
    confirm_current_power(db, project, rows, target["off_proof"], "stopped")
    saved = originals(rows)
    admission.reserve(db, project.id, operation_id, increment, operations.now())
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    row.dispatch_metadata = {
        "adapter": "kubevirt",
        "kind": "start",
        "generation_id": context["generation"],
        "native_storage_identity": context["identity"],
        "retained_measurement_at_reservation": measurement.evidence(),
        "native_resume": target,
        "native_originals": saved,
        "desired_digest": helpers().digest(project.topology),
        "calls": {},
        "worker_finished": False,
    }
    db.flush()
    return operation_id


def validate_claimed_context(db, project, row, *, native_proof=None):
    from app.services import quota_storage

    metadata = row.dispatch_metadata
    if metadata.get("adapter") != "kubevirt" or metadata.get("kind") != "start":
        raise AccountingUnavailable("Native resume operation identity required")
    supported(project, db.get(Host, project.host_id))
    rows = operations.current_generation(operations._rows(db, project.id))
    previous = [r for r in rows if r.operation_id != row.operation_id]
    context = retained_context(db, project, previous)
    target = metadata["native_resume"]
    expected = Resources(
        cpu=target["demand"]["cpu"], ram_bytes=target["demand"]["ram_bytes"]
    )
    if (
        row.requested != expected.model_dump()
        or operations._generation(row) != context["generation"]
        or metadata.get("native_storage_identity") != context["identity"]
    ):
        raise AccountingUnavailable("Native reserved resume vector or identity changed")
    if (
        context != target["context"]
        or originals(previous) != metadata["native_originals"]
    ):
        raise AccountingUnavailable("Native original retained allocations changed")
    if helpers().digest(project.topology) != metadata["desired_digest"] or target[
        "root_spec"
    ].get("topology", {}) != operations.retained_topology(project):
        raise AccountingUnavailable("Native resume topology changed")
    if (
        native_proof is not None
        and native_proof["execution_digest"] != target["execution_digest"]
    ):
        raise AccountingUnavailable("Native off proof or execution target changed")
    measurement = quota_storage._native_measurement(db, project, rows, operations.now())
    validate_storage_inventory(db, project, target, measurement)
    root = quota_storage.measurement_row(rows)
    if root.dispatch_metadata.get("native_storage_evidence") != measurement.evidence():
        raise AccountingUnavailable("Native retained physical measurement changed")
    return target


def validate_storage_inventory(db, project, target, measurement):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    report = db.get(NativeStorageSnapshot, measurement.report_id)
    # Persisted reports were strictly validated at import. Match live retained
    # PVC identities to that same complete report rather than trusting names.
    payload = report.payload
    bindings = payload["volume_bindings"]
    expected = sorted(
        v["pvc_uid"] for v in bindings if v["root"]["project_id"] == project.id
    )
    if expected != target["storage_inventory"]:
        raise AccountingUnavailable("Native measured/live PVC inventory changed")


def claim_preflight(operation_id):
    from app.core.database import SessionLocal
    from app.models.quota import QuotaReservation

    with SessionLocal() as reader:
        row = (
            reader.query(QuotaReservation)
            .filter_by(operation_id=operation_id)
            .one_or_none()
        )
        if row is None or row.dispatch_metadata.get("adapter") != "kubevirt":
            return None
        if row.state != "pending" or row.dispatch_metadata.get("claim"):
            raise AccountingUnavailable("Claimed native work must be observed")
        target = deepcopy(row.dispatch_metadata["native_resume"])
        provider = reader.get(Provider, target["context"]["provider_id"])
        reader.expunge(provider)
    current = with_client(provider, resume_target, target["context"])
    if current["execution_digest"] != target["execution_digest"]:
        raise AccountingUnavailable("Native target changed before worker claim")
    return current


def apply_claim_preflight(db, project_id, operation_id, target):
    """Renew confirmed off evidence for the exact never-claimed queued start."""
    from app.models.quota import QuotaReservation
    from app.services import quota_admission as admission
    from app.services.quota_locks import lock_project

    rows = operations.current_generation(operations._rows(db, project_id))
    admission.lock_operations(db, [r.operation_id for r in rows])
    project, _, _ = lock_project(db, project_id)
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    if (
        row.state != "pending"
        or row.dispatch_metadata.get("claim")
        or row.dispatch_metadata.get("cancelled")
    ):
        raise AccountingUnavailable("Native queued start no longer accepts a claim")
    validate_claimed_context(db, project, row, native_proof=target)
    originals = [
        r
        for r in operations.current_generation(operations._rows(db, project_id))
        if r.operation_id != operation_id
    ]
    validate_stopped_collection(db, project, originals, target)
    confirm_current_power(db, project, originals, target["off_proof"], "stopped")


def start_claimed(db, project):
    from app.models.quota import QuotaReservation
    from app.services import quota_admission as admission

    operation_id = operations.current_operation.get()
    if not operation_id:
        raise AccountingUnavailable("Native resume requires original worker claim")
    admission.lock_operations(db, (operation_id,))
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    if (
        row.project_id != project.id
        or row.state != "dispatched"
        or not row.dispatch_metadata.get("claim")
    ):
        raise AccountingUnavailable("Native resume requires original worker claim")
    if row.dispatch_metadata.get("cancelled"):
        raise AccountingUnavailable("Native resume superseded by a new stop")
    target = validate_claimed_context(db, project, row)
    if row.dispatch_metadata.get("native_handoff"):
        raise AccountingUnavailable(
            "Native handoff already issued; observe before recovery"
        )
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "native_handoff": {
            "state": "intent",
            "at": operations.now().isoformat(),
            "request": running_request(target, operation_id),
        },
    }
    provider = db.get(Provider, target["context"]["provider_id"])
    db.expunge(provider)
    db.commit()  # Durable intent precedes provider I/O, including ambiguous errors.
    with_client(provider, publish_resume, target, operation_id)


def reconcile(project_id):
    """Read running evidence outside locks; confirm only the original increment."""
    from app.core.database import SessionLocal
    from app.models.quota import QuotaReservation
    from app.services import quota_admission as admission
    from app.services import quota_storage

    with SessionLocal() as reader:
        rows = operations.current_generation(operations._rows(reader, project_id))
        if not rows or not any(
            r.dispatch_metadata.get("adapter") == "kubevirt" for r in rows
        ):
            return False
        candidates = [
            r
            for r in rows
            if r.state in admission.PENDING
            and r.dispatch_metadata.get("native_handoff")
            and operations._settled(r)
        ]
        work = [
            (
                r.operation_id,
                deepcopy(r.dispatch_metadata["native_resume"]),
                reader.get(
                    Provider,
                    r.dispatch_metadata["native_resume"]["context"]["provider_id"],
                ),
            )
            for r in candidates
        ]
        for _, _, provider in work:
            reader.expunge(provider)
    for operation_id, target, provider in work:
        try:
            proof = with_client(provider, observe_running, target, operation_id)
            with SessionLocal() as db:
                admission.lock_operations(
                    db,
                    [
                        operation_id,
                        *[v["id"] for v in _originals_for(target, project_id)],
                    ],
                )
                from app.services.quota_locks import lock_project

                project, _, _ = lock_project(db, project_id)
                row = (
                    db.query(QuotaReservation)
                    .filter_by(operation_id=operation_id)
                    .one()
                )
                if row.dispatch_metadata.get("cancelled"):
                    continue
                validate_claimed_context(db, project, row)
                if row.state not in admission.PENDING or not row.dispatch_metadata.get(
                    "native_handoff"
                ):
                    continue
                observed = datetime.fromisoformat(proof["observed_at"])
                if not operations._observation_follows_worker(row, observed):
                    continue
                row.dispatch_metadata = {
                    **row.dispatch_metadata,
                    "native_running_proof": proof,
                }
                admission.confirm(
                    db,
                    operation_id,
                    Resources(**row.requested),
                    "native-running:" + proof["observation_id"],
                    observed,
                )
                mark_active(project, observed)
                from app.services import metering_service as metering

                snap = billing_snapshot(db, project, rows=None, proof=proof)
                metering.reconcile_project(db, project, now=observed, snapshot=snap)
                db.commit()
        except (
            AccountingUnavailable,
            helpers().ManagedPowerError,
            ValueError,
            KeyError,
            TypeError,
        ):
            continue  # Ambiguous/partial work retains its full reservation.
    quota_storage.reconcile_project(project_id)
    reconcile_receipts(project_id)
    refresh_settled(project_id)
    return True


def publish_confirmation(api, target, operation_id, proof):
    module = helpers()
    root = _live_root(api, target["context"], operation_id)
    request = running_request(target, operation_id)
    module.require(module.intent(root) == request, "managed_episode_changed")
    state = deepcopy(root.get("status", {}).get("managedPower", {}))
    module.validate_restore(root, state, request)
    if state.get("state") == "running":
        module.require(
            state.get("confirmedOperationId") == operation_id
            and state.get("confirmedObservationId") == proof["observation_id"],
            "managed_confirmation_changed",
        )
        return
    module.require(
        state.get("state") == "restoring"
        and state.get("resumeRequestId") == operation_id,
        "managed_restore_pending",
    )
    state.update(
        state="running",
        confirmedOperationId=operation_id,
        confirmedObservationId=proof["observation_id"],
    )
    module.write_status(api, root, state)


def mark_active(project, observed):
    """Apply existing restart UI/timer semantics only after complete proof."""
    from datetime import timedelta

    project.state, project.deploy_error, project.auto_stopped = "active", None, False
    if project.auto_stop_minutes:
        project.auto_stop_started_at = observed
        project.auto_stop_expires_at = observed + timedelta(
            minutes=project.auto_stop_minutes
        )
        project.auto_stop_warned = False


def reconcile_receipts(project_id):
    """Retry only the completion receipt, never a claimed restore dispatch."""
    from app.core.database import SessionLocal
    from app.models.quota import QuotaReservation
    from app.services import quota_admission as admission

    with SessionLocal() as reader:
        rows = operations.current_generation(operations._rows(reader, project_id))
        work = [
            (
                r.operation_id,
                deepcopy(r.dispatch_metadata["native_resume"]),
                deepcopy(r.dispatch_metadata["native_running_proof"]),
            )
            for r in rows
            if r.state == "confirmed"
            and r.dispatch_metadata.get("native_running_proof")
            and not r.dispatch_metadata.get("native_confirmation_delivered")
        ]
        providers = {}
        for _, target, _ in work:
            provider = reader.get(Provider, target["context"]["provider_id"])
            if provider is None:
                raise AccountingUnavailable("Native receipt provider unavailable")
            providers[provider.id] = provider
        for provider in providers.values():
            reader.expunge(provider)
    for operation_id, target, proof in work:
        try:
            with_client(
                providers[target["context"]["provider_id"]],
                publish_confirmation,
                target,
                operation_id,
                proof,
            )
            with SessionLocal() as db:
                admission.lock_operations(db, (operation_id,))
                row = (
                    db.query(QuotaReservation)
                    .filter_by(operation_id=operation_id)
                    .one()
                )
                if (
                    row.state == "confirmed"
                    and row.dispatch_metadata.get("native_running_proof") == proof
                ):
                    row.dispatch_metadata = {
                        **row.dispatch_metadata,
                        "native_confirmation_delivered": True,
                    }
                    db.commit()
        except Exception:
            continue  # Receipt loss is retried; allocation/meter proof remains durable.


def _originals_for(target, project_id):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        return [
            {"id": r.operation_id}
            for r in operations.current_generation(operations._rows(db, project_id))
        ]


def current_profile(db, project, rows):
    """Validate current ledger authority; pending resume never implies running."""
    from app.models.quota import QuotaAllocation

    demand, desired, root = profile_sources(db, project, rows)
    if root["spec"].get("topology", {}) != operations.retained_topology(project):
        raise AccountingUnavailable("Native billing topology changed")
    retained = [r for r in rows if any(r.observed.values())]
    current = admission_vector(rows)
    if current.projects != 1 or (current.cpu, current.ram_bytes) not in {
        (0, 0),
        (demand["cpu"], demand["ram_bytes"]),
    }:
        raise AccountingUnavailable("Native compute ledger profile incomplete")
    validate_profile_rows(db, project, rows)
    times = []
    for row in retained:
        allocation = (
            db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
        )
        if allocation.observed != row.observed or allocation.observed_at is None:
            raise AccountingUnavailable("Native billing allocation proof unavailable")
        times.append(utc(allocation.observed_at))
    active = bool(current.cpu or current.ram_bytes)
    if active and not any(
        r.dispatch_metadata.get("native_running_proof") for r in retained
    ):
        raise AccountingUnavailable("Native running observation required")
    observed = max(times)
    current = current_power_record(rows)
    if current is not None:
        observed = utc(datetime.fromisoformat(current["proof"]["observed_at"]))
    from app.services.quota_storage import measurement_row

    failed = measurement_row(rows).dispatch_metadata.get("native_power_failure")
    if (
        failed
        and failed.get("basis") == power_basis(rows)
        and utc(datetime.fromisoformat(failed["started_at"])) >= observed
    ):
        raise AccountingUnavailable("Latest native power inventory is unavailable")
    return demand, desired, active, observed


def power_basis(rows):
    return originals([r for r in rows if r.state == "confirmed"])


def current_power_record(rows):
    from app.services.quota_storage import measurement_row

    root = measurement_row(rows)
    record = root.dispatch_metadata.get("native_current_power")
    if (
        record
        and record.get("generation") == operations._generation(root)
        and record.get("basis") == power_basis(rows)
    ):
        return record
    return None


def confirm_current_power(db, project, rows, proof, mode):
    """Persist a new collection boundary, preserving original operation proofs."""
    from app.services import metering_service as metering
    from app.services.quota_storage import measurement_row

    observed = utc(datetime.fromisoformat(proof["observed_at"]))
    started = utc(datetime.fromisoformat(proof["started_at"]))
    current_time = utc(operations.now())
    if not (
        0
        <= (current_time - observed).total_seconds()
        <= max(30, metering.poll_seconds())
        and 0 <= (observed - started).total_seconds() <= 60
    ):
        raise AccountingUnavailable("Native current power collection is stale")
    root = measurement_row(rows)
    if proof.get("generation") != operations._generation(root):
        raise AccountingUnavailable("Native current power generation changed")
    previous = root.dispatch_metadata.get("native_current_power")
    if (
        previous
        and utc(datetime.fromisoformat(previous["proof"]["observed_at"])) > observed
    ):
        return
    failed = root.dispatch_metadata.get("native_power_failure")
    if failed and utc(datetime.fromisoformat(failed["started_at"])) > observed:
        return
    metadata = {
        k: v for k, v in root.dispatch_metadata.items() if k != "native_power_failure"
    }
    root.dispatch_metadata = {
        **metadata,
        "native_current_power": {
            "mode": mode,
            "generation": operations._generation(root),
            "basis": power_basis(rows),
            "proof": deepcopy(proof),
        },
    }
    db.flush()


def expected_stop(db, project, rows):
    record = current_power_record(rows)
    if record is not None and record["mode"] == "stopped":
        proof = record["proof"]
        return proof["request_id"], proof["inventory_digest"]
    from app.models.quota import QuotaAction
    from app.services.quota_storage import measurement_row

    root = measurement_row(rows)
    if (root.observation_id or "").startswith("managed-off:"):
        request_id = root.observation_id.removeprefix("managed-off:")
        run = (
            db.query(QuotaAction)
            .filter_by(project_id=project.id, kind="stop_run")
            .one_or_none()
        )
        proof = run.details.get("managed_off_proof", {}) if run else {}
        if proof.get("request_id") != request_id or proof.get(
            "generation"
        ) != operations._generation(root):
            raise AccountingUnavailable("Native settled stop episode unavailable")
        return request_id, proof["inventory_digest"]
    compute = reviewed_compute(db, project, rows)
    if root.observation_id != compute["observation_id"]:
        raise AccountingUnavailable("Native settled stop observation changed")
    state = compute["managed_inventory"]["TroshkaProject"][0]["status"]["managedPower"]
    return compute["request_id"], state["inventoryDigest"]


def validate_stopped_collection(db, project, rows, target):
    from app.services import quota_storage

    context = retained_context(db, project, rows)
    request_id, snapshot = expected_stop(db, project, rows)
    demand, desired, _ = profile_sources(db, project, rows)
    if (
        target["context"] != context
        or target["resume_of"] != request_id
        or target["snapshot"]["inventoryDigest"] != snapshot
        or target["demand"] != demand
        or lifecycle_intent(target["snapshot"]["targets"]) != lifecycle_intent(desired)
    ):
        raise AccountingUnavailable("Native current stopped target changed")
    measurement = quota_storage._native_measurement(db, project, rows, operations.now())
    validate_storage_inventory(db, project, target, measurement)
    quota_storage._native_scope_evidence(
        db, quota_storage.measurement_row(rows), operations.now()
    )


def lifecycle_intent(targets):
    # Recreated bare Pod UIDs are pinned by the newer stop proof's inventory
    # digest; their saved manifest/node identity and all desired states persist.
    return sorted(
        (
            {k: v for k, v in t.items() if k != "uid" or t["kind"] != "Pod"}
            for t in targets
        ),
        key=lambda t: (str(t["kind"]), str(t["name"])),
    )


def settled_work(db, project, rows):
    context = retained_context(db, project, rows)
    record = current_power_record(rows)
    accepted = [
        r
        for r in rows
        if r.dispatch_metadata.get("native_running_proof")
        and r.dispatch_metadata.get("native_resume")
    ]
    running = (
        record["mode"] == "running"
        if record
        else any(
            (r.observation_id or "").startswith("native-running:") for r in accepted
        )
    )
    work = {
        "context": context,
        "basis": power_basis(rows),
        "mode": "running" if running else "stopped",
    }
    if running:
        row = max(
            accepted,
            key=lambda r: r.dispatch_metadata["native_running_proof"]["observed_at"],
        )
        work.update(
            operation_id=row.operation_id,
            target=deepcopy(row.dispatch_metadata["native_resume"]),
            confirmation=row.dispatch_metadata["native_running_proof"][
                "observation_id"
            ],
        )
    else:
        work["request_id"], work["inventory_digest"] = expected_stop(db, project, rows)
    return work


def collect_settled(api, work):
    """All provider reads run before database locks; this never dispatches work."""
    if work["mode"] == "running":
        proof = observe_running(
            api,
            work["target"],
            work["operation_id"],
            expected_confirmation=work["confirmation"],
        )
        return work["target"], proof
    target = resume_target(api, work["context"])
    if (
        target["resume_of"] != work["request_id"]
        or target["snapshot"]["inventoryDigest"] != work["inventory_digest"]
    ):
        raise AccountingUnavailable("Native stopped episode changed during collection")
    return target, target["off_proof"]


def refresh_settled(project_id):
    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.services import quota_accounting as accounting
    from app.services import quota_admission as admission
    from app.services import quota_storage

    if not accounting.quotas_enabled():
        return
    work, started = None, operations.now()
    try:
        with SessionLocal() as reader:
            project = reader.get(Project, project_id)
            rows = operations.current_generation(operations._rows(reader, project_id))
            work = settled_work(reader, project, rows)
            provider = reader.get(Provider, work["context"]["provider_id"])
            reader.expunge(provider)
        target, proof = with_client(provider, collect_settled, work)
        with SessionLocal() as db:
            admission.lock_operations(db, [r["id"] for r in work["basis"]])
            from app.services.quota_locks import lock_project

            project, _, _ = lock_project(db, project_id)
            rows = operations.current_generation(operations._rows(db, project_id))
            if settled_work(db, project, rows) != work:
                raise AccountingUnavailable(
                    "Native settled power identity changed during collection"
                )
            if work["mode"] == "stopped":
                validate_stopped_collection(db, project, rows, target)
            else:
                measurement = quota_storage._native_measurement(
                    db, project, rows, operations.now()
                )
                validate_storage_inventory(db, project, target, measurement)
                quota_storage._native_scope_evidence(
                    db, quota_storage.measurement_row(rows), operations.now()
                )
            confirm_current_power(db, project, rows, proof, work["mode"])
            db.commit()
    except Exception:
        if work is not None:
            record_power_failure(project_id, work, started)
        return  # No successful independent proof means no renewed timestamp.


def record_power_failure(project_id, work, started):
    from app.core.database import SessionLocal
    from app.services import quota_admission as admission
    from app.services.quota_locks import lock_project
    from app.services.quota_storage import measurement_row

    try:
        with SessionLocal() as db:
            admission.lock_operations(db, [r["id"] for r in work["basis"]])
            project, _, _ = lock_project(db, project_id)
            rows = operations.current_generation(operations._rows(db, project_id))
            if settled_work(db, project, rows) != work:
                return
            root = measurement_row(rows)
            current = current_power_record(rows)
            if current and utc(
                datetime.fromisoformat(current["proof"]["observed_at"])
            ) > utc(started):
                return
            root.dispatch_metadata = {
                **root.dispatch_metadata,
                "native_power_failure": {
                    "basis": power_basis(rows),
                    "started_at": started.isoformat(),
                },
            }
            db.commit()
    except Exception:
        return


def profile_sources(db, project, rows) -> tuple[dict, list, dict]:
    targets = [
        r.dispatch_metadata["native_resume"]
        for r in rows
        if r.dispatch_metadata.get("native_resume")
    ]
    if targets:
        target = targets[-1]
        root = {"spec": target["root_spec"]}
        demand = target["demand"]
        desired = target["snapshot"]["targets"]
    else:
        source = next(
            (
                r.dispatch_metadata.get("native_baseline_profile")
                for r in rows
                if r.dispatch_metadata.get("native_baseline_profile")
            ),
            None,
        )
        if source is None:
            source = baseline_profile(reviewed_compute(db, project, rows))
        if source.get("unavailable"):
            raise AccountingUnavailable(
                "Native retained configured profile requires reviewed baseline evidence"
            )
        root, demand, desired = (
            {"spec": {"topology": source["topology"]}},
            source["demand"],
            source["targets"],
        )
    return demand, desired, root


def reviewed_compute(db, project, rows):
    """Read and validate the immutable pre-4A receipt without rewriting it."""
    from app.models.quota import QuotaReadiness
    from app.services import quota_baseline as legacy
    from app.services import quota_capacity_baseline as capacity
    from app.services.quota_storage import measurement_row

    marker = db.get(QuotaReadiness, "baseline")
    if marker is None or marker.baseline_version != capacity.VERSION:
        raise AccountingUnavailable("Reviewed native capacity baseline unavailable")
    details = marker.details
    evidence = details.get("evidence", {})
    if (
        not details.get("capacity_ready")
        or details.get("fresh_receipt") != capacity._receipt(db)
        or details.get("evidence_digest") != legacy._hash(evidence)
    ):
        raise AccountingUnavailable("Reviewed native capacity receipt changed")
    entry = evidence["projects"][project.id]
    root = measurement_row(rows)
    if entry["adapter"] != "kubevirt" or entry[
        "generation_id"
    ] != operations._generation(root):
        raise AccountingUnavailable("Reviewed native generation changed")
    compute = entry["compute_observation"]
    capacity._native_compute(
        project,
        entry,
        root.dispatch_metadata["native_storage_identity"],
        legacy._timestamp(compute["observed_at"]),
        legacy._timestamp(entry["jobs_checked_at"]),
    )
    return compute


def baseline_profile(compute) -> dict:
    """Keep resource evidence without duplicating exec keys/provider settings."""
    from app.services.quota_capacity_baseline import _OfflineInventory

    try:
        supplied = compute["managed_inventory"]
        root = supplied["TroshkaProject"][0]
        api = _OfflineInventory(supplied, root["metadata"]["namespace"])
        rows, _ = helpers().inventory(api, root)
        state = root["status"]["managedPower"]
        return {
            "profile": "configured-workloads-v1",
            "topology": deepcopy(root["spec"].get("topology", {})),
            "targets": deepcopy(state["targets"]),
            "demand": configured_demand(root, rows, state),
            "observation_id": compute["observation_id"],
            "observed_at": compute["observed_at"],
            "request_id": compute["request_id"],
            "inventory_digest": state["inventoryDigest"],
        }
    except (
        AccountingUnavailable,
        helpers().ManagedPowerError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        return {"unavailable": str(error)}


def validate_profile_rows(db, project, rows):
    for row in rows:
        if row.state == "confirmed":
            if not operations._settled(row):
                raise AccountingUnavailable("Native confirmed worker is unsettled")
        elif row.dispatch_metadata.get("native_resume"):
            validate_claimed_context(db, project, row)
        else:
            raise AccountingUnavailable(
                "Native unresolved work has no managed resume profile"
            )


def admission_vector(rows):
    from app.services.quota_admission import _vector_sum

    return _vector_sum(r.observed for r in rows)


def billing_snapshot(db, project, rows=None, proof=None):
    """Same monetary rates/provisioned disk pricing; verified configured compute."""
    from app.services import metering_service as metering

    rows = rows or operations.current_generation(operations._rows(db, project.id))
    demand, desired, active, observed = current_profile(db, project, rows)
    if proof is not None:
        observed = datetime.fromisoformat(proof["observed_at"])
    host = db.get(Host, project.host_id)
    provider = db.get(Provider, host.provider_id)
    rates = metering.load_type_rates(db)
    nodes = billing_nodes(project, desired, active)
    return {
        "provider_type": provider.type,
        "host_id": host.id,
        "billing_mode": "shared",
        "host_rates": metering.compose_host_rates(host, provider.type, rates),
        "rate_keys": list(
            metering._configured_rate_keys(
                metering.load_catalog().get("kubevirt_unit_rates"),
                metering._typed_rate_row(provider.type, rates),
                host.metering_rates,
            )
        ),
        "observed_at": observed.isoformat(),
        "project_state": "active" if active else "stopped",
        "vms": nodes,
        "disks": metering._snapshot_disks(
            project, operations.retained_topology(project)
        ),
        "eips": [],
        "ceph": [],
        "native_profile": "configured-workloads-v1",
    }


def billing_nodes(project, desired, active):
    from app.services.quota_native import _helpers

    topology = operations.retained_topology(project)
    topo = _helpers()[0]
    vm_desired = {
        t["name"]: desired_running(t["desired"])
        for t in desired
        if t["kind"] == "VirtualMachine"
    }
    result = []
    for vm in topo.extract_vms(topology):
        name = f"troshka-vm-{vm['id'][:8]}"
        running = active and vm_desired.get(name, False)
        result.append(
            {
                "id": vm["id"],
                "vcpus": vm["cpus"],
                "ram_gib": vm["memory"] / 1024,
                "running": running,
                "live_state": running,
            }
        )
    for ctr in topo.extract_containers(topology):
        result.append(
            {
                "id": ctr["id"],
                "vcpus": ctr.get("cpus", 1),
                "ram_gib": ctr.get("memory", 512) / 1024,
                "running": active,
                "live_state": active,
            }
        )
    return result
