"""Bounded native first-deployment operation, with collector-owned enrollment.

Explicit startup allowance is selected by native_initialization. Provider I/O runs outside
ledger locks; durable intent is never redispatched after an ambiguous handoff.
"""

import importlib
import json
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from app.models.host import Host
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaReservation
from app.models.user import User
from app.schemas.kubevirt_storage import ProjectRoot
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import native_initialization as initialization
from app.services import native_managed_power as power
from app.services import quota_admission as admission
from app.services import quota_operations as operations
from app.services.kubevirt_storage_enrollment import digest, utc
from app.services.quota_locks import lock_project

_release_recheck: ContextVar[str | None] = ContextVar(
    "native_first_deploy_release_recheck", default=None
)


def helpers():
    power.helpers()
    return importlib.import_module("helpers.managed_deploy")


def selected_host(db, project, host_id):
    host = db.get(Host, host_id) if host_id else None
    provider = db.get(Provider, host.provider_id) if host else None
    if (
        host is None
        or host.host_type != "kubevirt-cluster"
        or host.state != "active"
        or host.agent_status != "connected"
        or not host.accepting_work
        or (host.billing_mode or "shared") != "shared"
        or provider is None
        or provider.type != "kubevirt"
        or provider.state != "active"
        or project.provider_id not in (None, provider.id)
        or project.host_assignments
        or project.mesh_subnet_id
    ):
        raise AccountingUnavailable(
            "An explicit available single native shared host is required"
        )
    return host, provider


def final_payload(project, provider, profile, *, topology=None, sources=None):
    from app.services.deploy_service import (
        _generate_exec_ssh_keypair,
        _regenerate_kubevirt_cloud_init,
    )
    from app.services.providers.kubevirt import _build_troshka_project_cr, _project_ns

    topology = deepcopy(topology if topology is not None else project.topology)
    private, public = _generate_exec_ssh_keypair(project.id)
    _regenerate_kubevirt_cloud_init(topology, project, public)
    # No custom NIC/network, imported S3 disk, installer or model-selection path
    # exists in this profile. Their unresolved runtime decisions cannot sneak in.
    payload = _build_troshka_project_cr(
        project.id,
        _project_ns(provider, project.id),
        topology,
        {},
        None,
        {"exec_ssh_key": private},
    )
    payload["spec"]["managedVncImage"] = initialization.implementation(
        profile
    ).vnc_image
    if sources:
        payload["spec"]["managedSources"] = deepcopy(sources)
    root = {**deepcopy(payload), "metadata": {**payload["metadata"], "uid": "pending"}}
    payload["spec"]["managedPlanDigest"] = helpers().plan_digest(
        helpers().render(root, profile.target.storage_class_name)
    )
    return payload


def prepare(db, project, *, host_id):
    if not host_id:
        from app.services.placement import (
            calculate_project_requirements,
            find_available_host,
        )

        if project.provider_id is None:
            raise AccountingUnavailable(
                "Automatic native placement requires the project's selected provider"
            )
        reqs = calculate_project_requirements(project.topology)
        selected = find_available_host(
            db,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            provider_id=project.provider_id,
            requires_kubevirt=True,
        )
        host_id = selected.id if selected else None
        if host_id is None:
            raise AccountingUnavailable(
                "No available native host has configured capacity"
            )
    operation_id = str(uuid4())
    # Resolve external source I/O before acquiring operation/project/ledger locks.
    from app.services import native_managed_images as images

    input_digest = digest(project.topology)
    host, provider = selected_host(db, project, host_id)
    _, manifest, candidate = initialization.enrollment(db, provider.id)
    owner = db.get(User, project.owner_id)
    topology, sources = images.freeze(db, project, owner, candidate, manifest)
    admission.lock_operations(db, [operation_id])
    project, owner, organization = lock_project(db, project.id)
    if organization is None or not owner.access_enabled:
        raise AccountingUnavailable(
            "Native deployment requires current organization attribution"
        )
    host, provider = selected_host(db, project, host_id)
    from app.services.quota_capacity_baseline import _empty

    if not _empty(
        db, project, operations.current_generation(operations._rows(db, project.id))
    ):
        raise AccountingUnavailable("Native deployment requires a verified empty draft")
    domain, manifest, profile, observation = initialization.current(
        db, provider.id, operations.now()
    )
    if digest(profile) != digest(candidate) or digest(project.topology) != input_digest:
        raise AccountingUnavailable("Native source/profile changed while freezing plan")
    images.validate_sources(
        db, owner, {"sources": sources, "sources_digest": digest(sources)}, profile
    )
    payload = final_payload(
        project, provider, profile, topology=topology, sources=sources
    )
    demand, claims = payload_demand(payload, profile)
    requested = Resources(
        cpu=demand["cpu"],
        ram_bytes=demand["ram_bytes"],
        projects=1,
        storage_bytes=len(claims) * profile.initial_storage_bound_bytes,
    )
    admission.reserve(db, project.id, operation_id, requested, operations.now())
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    row.dispatch_metadata = {
        "adapter": "kubevirt",
        "kind": "deploy",
        "generation_id": operation_id,
        "known_empty": True,
        "calls": {},
        "worker_finished": False,
        "native_deploy": {
            "phase": "reserved",
            "host_id": host.id,
            "provider_id": provider.id,
            "owner_id": owner.id,
            "organization_id": organization.id,
            "domain_id": domain.id,
            "enrollment_id": domain.enrollment_id,
            "profile_digest": digest(profile),
            "payload": payload,
            "payload_digest": digest(payload["spec"]),
            "input_digest": digest(project.topology),
            "sources": sources,
            "sources_digest": digest(sources),
            "demand": demand,
            "claims": claims,
            "initial_bound": requested.storage_bytes,
            "admission_observation": observation.model_dump(mode="json"),
        },
    }
    db.flush()
    return operation_id


def payload_demand(payload, profile):
    root = {**deepcopy(payload), "metadata": {**payload["metadata"], "uid": "pending"}}
    planned = helpers().render(root, profile.target.storage_class_name)
    if payload["spec"].get("managedVncImage") != initialization.implementation(
        profile
    ).vnc_image or payload["spec"].get("managedPlanDigest") != helpers().plan_digest(
        planned
    ):
        raise AccountingUnavailable(
            "Native final rendered plan or named helper image changed"
        )
    from kubernetes.utils.quantity import parse_quantity

    claims = []
    for value in planned:
        if value["kind"] not in {"PersistentVolumeClaim", "DataVolume"}:
            continue
        spec = value["spec"]["pvc"] if value["kind"] == "DataVolume" else value["spec"]
        size = int(parse_quantity(spec["resources"]["requests"]["storage"]))
        if not profile.minimum_capacity_bytes <= size <= profile.maximum_capacity_bytes:
            raise AccountingUnavailable(
                "Native PVC capacity is outside initialization applicability"
            )
        claims.append(value["metadata"]["name"])
    if not claims:
        raise AccountingUnavailable(
            "Native first-deploy profile requires actual new blank storage"
        )
    return helpers().demand(planned), sorted(claims)


def validate(db, project, row, *, placed=True, physical=True):
    target = row.dispatch_metadata["native_deploy"]
    host, provider = selected_host(db, project, target["host_id"])
    from app.services.providers.kubevirt import _project_ns

    if target["payload"]["metadata"]["namespace"] != _project_ns(provider, project.id):
        raise AccountingUnavailable(
            "Native namespace configuration changed after reservation"
        )
    owner = db.get(User, project.owner_id)
    if (
        owner is None
        or not owner.access_enabled
        or (row.owner_id, row.organization_id) != (owner.id, owner.organization_id)
        or (owner.id, owner.organization_id)
        != (target["owner_id"], target["organization_id"])
        or provider.id != target["provider_id"]
        or row.project_id != project.id
        or row.dispatch_metadata.get("cancelled")
        or operations._generation(row) != row.operation_id
        or digest(target["payload"]["spec"]) != target["payload_digest"]
    ):
        raise AccountingUnavailable("Native deployment identity or attribution changed")
    expected = target["payload"]["spec"]["topology"] if placed else project.topology
    if placed and (project.host_id != host.id or project.topology != expected):
        raise AccountingUnavailable("Native admitted placement or payload changed")
    if not placed and digest(project.topology) != target["input_digest"]:
        raise AccountingUnavailable("Native draft changed after reservation")
    domain, manifest, profile = initialization.enrollment(
        db, provider.id, domain_id=target["domain_id"]
    )
    if digest(profile) != target["profile_digest"]:
        raise AccountingUnavailable("Native pending initialization profile changed")
    from app.services.native_managed_images import validate_sources

    validate_sources(db, owner, target, profile)
    demand, claims = payload_demand(target["payload"], profile)
    vector = Resources(
        cpu=demand["cpu"],
        ram_bytes=demand["ram_bytes"],
        projects=1,
        storage_bytes=len(claims) * profile.initial_storage_bound_bytes,
    )
    if (
        row.requested != vector.model_dump()
        or demand != target["demand"]
        or claims != target["claims"]
    ):
        raise AccountingUnavailable("Native admitted resource plan changed")
    if physical:
        initialization.current(db, provider.id, operations.now(), domain_id=domain.id)
    return target, profile


def place(db, project, operation_id, host_id):
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    if row.state != "pending" or row.dispatch_metadata.get("claim"):
        raise AccountingUnavailable("Claimed native work cannot repeat placement")
    target, _ = validate(db, project, row, placed=False)
    if host_id not in (None, target["host_id"]):
        raise AccountingUnavailable("Native placement cannot move the reserved host")
    project.host_id = target["host_id"]
    project.topology = deepcopy(target["payload"]["spec"]["topology"])
    project.deployed_topology = deepcopy(project.topology)
    project.state, project.deploy_started_at = "deploying", operations.now()
    db.flush()
    from app.services.placement import calculate_project_requirements

    host = db.get(Host, project.host_id)
    return {
        "host_id": host.id,
        "host_ip": host.ip_address,
        "requirements": calculate_project_requirements(project.topology),
        "vni_map": {},
        "network_config": {},
    }


def marker(target, operation_id, phase, profile):
    value = {
        "version": 1,
        "mode": "deploy",
        "phase": phase,
        "profile": helpers().CLONE_PROFILE
        if profile.operation == "managed-cache-clone"
        else helpers().PROFILE,
        "profileDigest": target["profile_digest"],
        "storageClass": profile.target.storage_class_name,
        "requestId": operation_id,
        "allocationGeneration": operation_id,
        "payloadDigest": target["payload_digest"],
    }
    value["planDigest"] = target["payload"]["spec"]["managedPlanDigest"]
    if target.get("root") and phase != "pending":
        root = target["root"]
        value.update(
            rootUid=root["project_cr_uid"],
            rootGeneration=root["deployment_generation"],
            projectId=root["project_id"],
            namespaceUid=root["namespace_uid"],
            clusterKey=root["cluster_key"],
        )
    return value


def update(row, **fields):
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "native_deploy": {**row.dispatch_metadata["native_deploy"], **fields},
    }


def dispatch(db, project):
    operation_id = operations.current_operation.get()
    admission.lock_operations(db, [operation_id])
    project, _, _ = lock_project(db, project.id)
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    target, profile = validate(db, project, row)
    if (
        row.state != "dispatched"
        or not row.dispatch_metadata.get("claim")
        or target["phase"] != "reserved"
    ):
        raise AccountingUnavailable(
            "Native create requires its original never-handed-off claim"
        )
    update(row, phase="create-intent", create_started_at=operations.now().isoformat())
    provider = db.get(Provider, target["provider_id"])
    db.expunge(provider)
    db.commit()
    # Provider reads the durable original target; callers cannot supply a new CR.
    from app.services.providers.kubevirt import KubeVirtDriver

    KubeVirtDriver().deploy_project(provider, project.id, None, None)


def create_pending(provider, project_id):
    from app.core.database import SessionLocal
    from app.services.providers.kubevirt import (
        _get_k8s_clients,
        base_namespace_grants_mode,
    )

    operation_id = operations.current_operation.get()
    with SessionLocal() as db:
        admission.lock_operations(db, [operation_id])
        project, _, _ = lock_project(db, project_id)
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        target, profile = validate(db, project, row)
        if target["phase"] != "create-intent" or not row.dispatch_metadata.get("claim"):
            raise AccountingUnavailable(
                "Native pending CR has no original create intent"
            )
        # The handoff flag is committed before even Namespace creation.
        if target.get("create_handoff"):
            raise AccountingUnavailable("Ambiguous native creation must be read back")
        mode = base_namespace_grants_mode()
        update(row, create_handoff=True, base_grants_mode=mode)
        payload = deepcopy(target["payload"])
        payload["metadata"]["annotations"] = {
            power.helpers().ANNOTATION: json.dumps(
                marker(target, operation_id, "pending", profile), sort_keys=True
            )
        }
        db.commit()
    _, core_api, client = _get_k8s_clients(provider)
    try:
        api = power.helpers().PowerClient(client)
        if (
            api.get("Namespace", None, "kube-system")["metadata"]["uid"]
            != profile.target.cluster_key
        ):
            raise AccountingUnavailable(
                "Native provider cluster changed before creation"
            )
        namespace = payload["metadata"]["namespace"]
        establish_namespace(
            api, core_api, client, namespace, project_id, operation_id, mode
        )
        api.request("POST", "TroshkaProject", namespace, None, payload)
    finally:
        client.close()


def establish_namespace(
    api, core_api, client, namespace, project_id, operation_id, mode
):
    """Base grants belong to the original create handoff, before CR creation."""
    from kubernetes import client as k8s

    from app.core.database import SessionLocal
    from app.services.providers.base_namespace_grants import verify_existing_namespace
    from app.services.providers.kubevirt import ensure_troshka_namespaced_rbac

    created = None
    if mode == "external":
        verify_existing_namespace(core_api, namespace)
    else:
        created = api.request(
            "POST",
            "Namespace",
            None,
            None,
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "annotations": {
                        "troshka.redhat.com/operation": operation_id,
                        "troshka.redhat.com/project-id": project_id,
                    },
                },
            },
        )
    original = api.get("Namespace", None, namespace)["metadata"]
    if original.get("deletionTimestamp") or not original.get("uid"):
        raise AccountingUnavailable("Native create namespace is unavailable")
    if created is not None and (
        original["uid"] != created["metadata"]["uid"]
        or original.get("annotations", {}).get("troshka.redhat.com/operation")
        != operation_id
        or original.get("annotations", {}).get("troshka.redhat.com/project-id")
        != project_id
    ):
        raise AccountingUnavailable(
            "Native create namespace differs from original handoff"
        )
    # Record the exact original identity before any grant or CR side effect.
    with SessionLocal() as db:
        admission.lock_operations(db, [operation_id])
        lock_project(db, project_id)
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        if row.dispatch_metadata.get("cancelled"):
            raise AccountingUnavailable("Native namespace handoff was cancelled")
        update(row, create_namespace_uid=original["uid"])
        db.commit()
    ensure_troshka_namespaced_rbac(k8s.RbacAuthorizationV1Api(client), namespace)
    current = api.get("Namespace", None, namespace)["metadata"]
    if current.get("uid") != original["uid"] or current.get("deletionTimestamp"):
        raise AccountingUnavailable("Native create namespace changed during grants")


def bind(api, target, operation_id, profile):
    if isinstance(api, power.helpers().PowerClient):
        api.begin_inventory()
    namespace = target["payload"]["metadata"]["namespace"]
    ns = api.get("Namespace", None, namespace)
    root = api.get("TroshkaProject", namespace, target["payload"]["metadata"]["name"])
    if (
        ns["metadata"].get("deletionTimestamp")
        or root["metadata"].get("deletionTimestamp")
        or not namespace_matches(
            ns, target, operation_id, root["spec"].get("projectId")
        )
        or root["spec"] != target["payload"]["spec"]
        or power.helpers().intent(root)
        != marker(target, operation_id, "pending", profile)
        or api.get("Namespace", None, "kube-system")["metadata"]["uid"]
        != profile.target.cluster_key
    ):
        raise AccountingUnavailable(
            "Pending native CR readback differs from original handoff"
        )
    rows, _ = power.helpers().inventory(api, root)
    if any(rows.values()):
        raise AccountingUnavailable("Pending native CR already has producers")
    result = ProjectRoot(
        project_id=root["spec"]["projectId"],
        cluster_key=profile.target.cluster_key,
        namespace=namespace,
        namespace_uid=ns["metadata"]["uid"],
        project_cr_uid=root["metadata"]["uid"],
        deployment_generation=root["metadata"]["generation"],
    )
    return result.model_dump(mode="json")


def namespace_matches(ns, target, operation_id, project_id):
    if ns["metadata"].get("uid") != target.get("create_namespace_uid"):
        return False
    if target.get("base_grants_mode") == "external":
        return True
    annotations = ns["metadata"].get("annotations", {})
    return (
        annotations.get("troshka.redhat.com/operation") == operation_id
        and annotations.get("troshka.redhat.com/project-id") == project_id
    )


def publish(api, target, operation_id, request):
    context = {"root": target["root"], "generation": operation_id}
    root = power._live_root(api, context, operation_id)
    current = power.helpers().intent(root)
    if current == request:
        return
    expected = deepcopy(request)
    expected["phase"] = (
        "pending" if request["phase"] == "initializing" else "initializing"
    )
    for key in ("physicalReportId", "physicalObservationId", "initializationDigest"):
        expected.pop(key, None)
    if request["phase"] == "initializing":
        for key in (
            "rootUid",
            "rootGeneration",
            "projectId",
            "namespaceUid",
            "clusterKey",
        ):
            expected.pop(key, None)
    if current != expected:
        raise AccountingUnavailable(
            "Native deployment phase changed before CAS release"
        )
    annotations = {
        **root["metadata"].get("annotations", {}),
        power.helpers().ANNOTATION: json.dumps(request, sort_keys=True),
    }
    api.patch(
        root, [{"op": "add", "path": "/metadata/annotations", "value": annotations}]
    )


def observe(api, target, operation_id, *, runtime=False):
    started = operations.now()
    if isinstance(api, power.helpers().PowerClient):
        api.begin_inventory()
    context = {"root": target["root"], "generation": operation_id}
    root = power._live_root(api, context, operation_id)
    rows, by_uid, planned = helpers().observe(api, root, runtime=runtime)
    demand = helpers().demand(planned)
    if demand != target["demand"]:
        raise AccountingUnavailable("Native deployment configured demand changed")
    targets = observed_targets(root, rows, by_uid, planned)
    if runtime:
        state = {
            "targets": targets,
            "restoredPods": {
                v["name"]: v["uid"] for v in targets if v["kind"] == "Pod"
            },
        }
        power.verify_running(rows, by_uid, state, power.helpers())
        configured = power.configured_demand(root, rows, state)
    else:
        configured = None
    final = power._live_root(api, context, operation_id)
    if (
        final != root
        or not 0 <= (utc(operations.now()) - utc(started)).total_seconds() <= 60
    ):
        raise AccountingUnavailable("Native deployment observation changed or expired")
    return {
        "observed_at": operations.now().isoformat(),
        "started_at": started.isoformat(),
        "generation": operation_id,
        "request_id": operation_id,
        "root": target["root"],
        "observation_id": digest(
            {"root": root, "rows": rows, "started": started.isoformat()}
        ),
        "initialization_digest": helpers().initialization_digest(rows),
        "storage_inventory": sorted(
            v["metadata"]["uid"] for v in rows["PersistentVolumeClaim"]
        ),
        "targets": targets,
        "configured_profile": configured,
    }


def observed_targets(root, rows, by_uid, planned):
    targets = []
    for kind in ("VirtualMachine", "Deployment", "Pod"):
        for value in rows[kind]:
            if (
                kind == "Pod"
                and power.helpers().parent_kind(value, by_uid) != "TroshkaProject"
            ):
                continue
            planned_value = next(
                v
                for v in planned
                if (v["kind"], v["metadata"]["name"])
                == (kind, value["metadata"]["name"])
            )
            saved = {
                "kind": kind,
                "name": value["metadata"]["name"],
                "uid": value["metadata"]["uid"],
            }
            if kind != "Pod":
                field = "runStrategy" if kind == "VirtualMachine" else "replicas"
                saved["desired"] = {field: planned_value["spec"][field]}
            else:
                manifests = power.helpers().container_manifests(root, rows)
                saved.update(
                    manifestDigest=power.helpers().digest(manifests[saved["name"]]),
                    nodeId=power.helpers().container_node_id(root, value),
                )
            targets.append(saved)
    return targets


def current_off_proof(target):
    if target["phase"] == "runtime-intent" and _release_recheck.get() == target.get(
        "runtime_request", {}
    ).get("requestId"):
        proof = target.get("release_off_proof")
    else:
        proof = (
            target.get("initialization_proof")
            if target["phase"] == "initialization-intent"
            else target.get("pending_proof")
        )
    if (
        proof is None
        or not 0
        <= (
            utc(operations.now()) - utc(datetime.fromisoformat(proof["observed_at"]))
        ).total_seconds()
        <= 60
    ):
        raise AccountingUnavailable(
            "Bound native deployment requires current independent no-writer inventory"
        )
    if proof.get("root") != target.get("root"):
        raise AccountingUnavailable("Native no-writer proof belongs to another root")
    return proof


def pending_storage_profile(db, project, row, target):
    from app.services import quota_storage

    measurement = quota_storage._native_measurement(
        db, project, [row], operations.now()
    )
    if row.dispatch_metadata.get("native_storage_evidence") != measurement.evidence():
        raise AccountingUnavailable(
            "Native pending physical allocation needs reconciliation"
        )
    proof = current_off_proof(target)
    power.validate_storage_inventory(db, project, proof, measurement)


def pending_profile(db, project, rows):
    """Only observed writer-free initialization may coexist with readiness."""
    if len(rows) != 1 or not rows[0].dispatch_metadata.get("native_deploy"):
        return False
    row = rows[0]
    if row.state == "confirmed":
        return False
    target, _ = validate(
        db, project, row, placed=project.state != "draft", physical=False
    )
    releasing = (
        target["phase"] == "runtime-intent"
        and _release_recheck.get() == row.operation_id
        and not target.get("release_handoff")
    )
    if not releasing and target["phase"] not in {
        "reserved",
        "create-intent",
        "bound",
        "initialization-intent",
    }:
        raise AccountingUnavailable(
            "Native runtime is awaiting independent confirmation"
        )
    if row.observed["cpu"] or row.observed["ram_bytes"]:
        raise AccountingUnavailable("Unconfirmed native runtime has observed compute")
    if target.get("root"):
        proof = current_off_proof(target)
        if (
            proof.get("generation") != row.operation_id
            or proof.get("request_id") != row.operation_id
        ):
            raise AccountingUnavailable(
                "Native no-writer proof belongs to another operation"
            )
    if any(row.observed.values()) or target.get("initialized_report"):
        pending_storage_profile(db, project, row, target)
    return True


def locked(db, project_id, operation_id):
    admission.lock_operations(db, [operation_id])
    project, _, _ = lock_project(db, project_id)
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    target, profile = validate(db, project, row, physical=False)
    return project, row, target, profile


def has_pending_handoff(db, project, job_status):
    """Recognize a completed worker's valid durable reconciliation ownership."""
    if project.state != "deploying" or job_status not in ("", "finished"):
        return False
    rows = operations.current_generation(operations._rows(db, project.id))
    if len(rows) != 1:
        return False
    row = rows[0]
    metadata = row.dispatch_metadata
    if (
        row.state != "reconciling"
        or metadata.get("adapter") != "kubevirt"
        or metadata.get("kind") != "deploy"
        or not metadata.get("worker_finished")
        or not metadata.get("worker_finished_at")
        or not metadata.get("claim")
        or not metadata.get("native_deploy")
    ):
        return False
    try:
        target, _ = validate(db, project, row, physical=False)
        return target["phase"] in {
            "create-intent",
            "bound",
            "initialization-intent",
            "runtime-intent",
        }
    except (AccountingUnavailable, ValueError, KeyError, TypeError):
        return False


def reconcile(project_id):
    from app.core.database import SessionLocal
    from app.services.quota_accounting import quotas_enabled

    if not quotas_enabled():
        return False

    with SessionLocal() as db:
        rows = operations.current_generation(operations._rows(db, project_id))
        chosen = [r for r in rows if r.dispatch_metadata.get("native_deploy")]
        if not chosen:
            return False
        row = chosen[0]
        if row.state == "confirmed":
            target, profile = deepcopy(row.dispatch_metadata["native_deploy"]), None
        else:
            if len(rows) != 1 or row.dispatch_metadata.get("cancelled"):
                return True
            target, profile = validate(
                db, db.get(Project, project_id), row, physical=False
            )
        operation_id = row.operation_id
        confirmed = row.state == "confirmed"
        provider = db.get(Provider, target["provider_id"])
        db.expunge(provider)
    if confirmed:
        try:
            power.with_client(provider, publish_confirmation, target, operation_id)
        except Exception:
            pass  # A later retained stop/resume episode owns its own intent.
        return False
    try:
        advance(project_id, operation_id, target, profile, provider)
    except (
        AccountingUnavailable,
        power.helpers().ManagedPowerError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return True  # Partial/ambiguous work retains allocations and reservation.
    return True


def advance(project_id, operation_id, target, profile, provider):
    from app.core.database import SessionLocal
    from app.services.quota_enforcement import _execution_guard

    with SessionLocal() as guard_db:
        with _execution_guard(guard_db, "native-deploy:" + project_id) as acquired:
            if not acquired:
                return
            row = (
                guard_db.query(QuotaReservation)
                .filter_by(operation_id=operation_id)
                .one()
            )
            if row.dispatch_metadata.get("cancelled"):
                return
            guard_db.rollback()
            return _advance(project_id, operation_id, target, profile, provider)


def _advance(project_id, operation_id, target, profile, provider):
    from app.core.database import SessionLocal

    phase = target["phase"]
    if phase == "reserved":
        return
    if phase == "create-intent":
        root = power.with_client(provider, bind, target, operation_id, profile)
        with SessionLocal() as db:
            _, row, current, _ = locked(db, project_id, operation_id)
            if current != target:
                return
            update(
                row,
                root=root,
                phase="bound",
                pending_proof={
                    "observed_at": operations.now().isoformat(),
                    "root": root,
                    "empty_inventory": True,
                    "request_id": operation_id,
                    "generation": operation_id,
                },
            )
            db.commit()
        return
    if phase == "bound":
        if target.get("sources"):
            from app.services.native_managed_images import prepare_sources

            if not prepare_sources(
                SessionLocal, operation_id, target, profile, provider
            ):
                return
        enroll_ready(project_id, operation_id, target)
        return
    if phase == "initialization-intent":
        power.with_client(
            provider, publish, target, operation_id, target["initialization_request"]
        )
        proof = power.with_client(provider, observe, target, operation_id)
        admit_runtime(project_id, operation_id, target, proof)
        return
    if phase == "runtime-intent":
        target = release_runtime(project_id, operation_id, target, provider)
        proof = power.with_client(provider, observe_runtime, target, operation_id)
        if confirm(project_id, operation_id, target, proof):
            power.with_client(
                provider,
                publish_confirmation,
                {**target, "running_proof": proof},
                operation_id,
            )
        return
    if phase == "confirmed":
        power.with_client(provider, publish_confirmation, target, operation_id)
        power.refresh_settled(project_id)


def release_probe(api, target, operation_id):
    root = power._live_root(
        api, {"root": target["root"], "generation": operation_id}, operation_id
    )
    current = power.helpers().intent(root)
    if current == target["runtime_request"]:
        return None
    if current != target["initialization_request"] or target.get("release_handoff"):
        raise AccountingUnavailable(
            "Native runtime handoff is unresolved; observation only"
        )
    return observe(api, target, operation_id)


def revalidate_initialized(db, row):
    """Expected policy denial retains already validated off/storage evidence."""
    try:
        admission.revalidate(db, row.operation_id, operations.now())
    except QuotaDenied as error:
        update(row, blocker="initialization_quota_denied")
        db.commit()
        raise AccountingUnavailable(
            "Native runtime release blocked by current quota policy"
        ) from error
    if (
        row.dispatch_metadata["native_deploy"].get("blocker")
        == "initialization_quota_denied"
    ):
        update(row, blocker=None)


def release_runtime(project_id, operation_id, target, provider):
    """One admitted CAS. Durable ambiguous handoffs are observed, never retried."""
    from app.core.database import SessionLocal
    from app.services import quota_storage

    proof = power.with_client(provider, release_probe, target, operation_id)
    if proof is None:
        if not target.get("release_handoff"):
            raise AccountingUnavailable("Native runtime marker has no original handoff")
        return target
    with SessionLocal() as db:
        project, row, current, _ = locked(db, project_id, operation_id)
        if target != current:
            raise AccountingUnavailable(
                "Native runtime release changed during verification"
            )
        validate(db, project, row)
        measurement = quota_storage._native_measurement(
            db, project, [row], operations.now()
        )
        power.validate_storage_inventory(db, project, proof, measurement)
        # This later complete report can contain more bytes than the original
        # initialization report. Charge it even when runtime cannot be released.
        quota_storage.apply_measurement(
            db, project, [row], measurement, operations.now()
        )
        if not measured_initialization(db, row, measurement):
            update(row, blocker="initialization_not_blank")
            db.commit()
            raise AccountingUnavailable("Native initialization is no longer blank")
        if measurement.allocated_bytes > target["initial_bound"]:
            update(row, blocker="initialization_bound_exceeded")
            db.commit()
            raise AccountingUnavailable(
                "Native runtime release exceeds startup allowance"
            )
        update(row, release_off_proof=proof)
        token = _release_recheck.set(operation_id)
        try:
            revalidate_initialized(db, row)
        finally:
            _release_recheck.reset(token)
        update(row, release_handoff=True)
        target = deepcopy(row.dispatch_metadata["native_deploy"])
        db.commit()
    power.with_client(
        provider, publish, target, operation_id, target["runtime_request"]
    )
    return target


def blank_initialization(source, project_id):
    """Only original blank images may pass first initialization, never COW import."""
    extra = source["retained_bindings"] + source.get("snapshot_evidence", [])
    if any(v["root"]["project_id"] == project_id for v in extra):
        return False
    keys = {
        digest(v["image_key"])
        for v in source["volume_bindings"]
        if v["root"]["project_id"] == project_id
    }
    images = [v for v in source["images"] if digest(v["key"]) in keys]
    return (
        bool(keys)
        and len(images) == len(keys)
        and all(
            not (v.get("parent") or v.get("snapshots") or v.get("children"))
            for v in images
        )
    )


def measured_blank_initialization(db, project_id, measurement):
    from app.models.kubevirt_storage import NativeStorageSnapshot

    source = db.get(NativeStorageSnapshot, measurement.report_id)
    return source is not None and blank_initialization(source.payload, project_id)


def measured_initialization(db, row, measurement):
    """Both initial admission and delayed release consume the same protected proof."""
    target = row.dispatch_metadata["native_deploy"]
    if not target.get("sources"):
        return measured_blank_initialization(db, row.project_id, measurement)
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.schemas.kubevirt_storage import CloneReference, VolumeBinding
    from app.services.native_clone_graph import binding_identity
    from app.services.native_clone_lineage import validated_lineage

    source = db.get(NativeStorageSnapshot, measurement.report_id)
    expected = set(target.get("clone_lineages", {}).values())
    if source is None or not expected:
        return False
    try:
        references = [
            CloneReference.model_validate(v)
            for v in source.payload.get("clone_references", [])
        ]
        proofs = [
            validated_lineage(db, r.lineage_id, r.digest)
            for r in references
            if str(r.lineage_id) in expected
        ]
        bindings = [
            binding_identity(VolumeBinding.model_validate(v))
            for v in source.payload["volume_bindings"]
        ]
    except ValueError:
        return False  # Expected denial must preserve the measurement already applied.
    return (
        {str(p.lineage_id) for p in proofs} == expected
        and all(
            p.operation_id == row.operation_id
            and p.binding.root.model_dump(mode="json") == target["root"]
            for p in proofs
        )
        and all(binding_identity(p.binding) in bindings for p in proofs)
    )


def enroll_ready(project_id, operation_id, target):
    from app.core.database import SessionLocal
    from app.services.quota_capacity_baseline import _native_identity

    with SessionLocal() as db:
        project, row, current, profile = locked(db, project_id, operation_id)
        if target != current:
            return
        identity = _native_identity(db, project)
        if identity["root"] != target["root"]:
            raise AccountingUnavailable(
                "Automated native enrollment bound another root"
            )
        # The expanded domain must again be complete before storage admission.
        validate(db, project, row)
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "native_storage_identity": identity,
        }
        if target.get("sources"):
            from app.services.native_managed_images import prepare_clones

            lineages = prepare_clones(
                db,
                row,
                {
                    **target,
                    "initialization_storage_class": profile.target.storage_class_name,
                },
            )
            update(row, clone_lineages=lineages)
        request = marker(target, operation_id, "initializing", profile)
        update(
            row,
            phase="initialization-intent",
            initialization_request=request,
            initialization_started_at=operations.now().isoformat(),
        )
        db.commit()


def admit_runtime(project_id, operation_id, target, proof):
    from app.core.database import SessionLocal
    from app.services import metering_service as metering
    from app.services import quota_storage

    with SessionLocal() as db:
        project, row, current, profile = locked(db, project_id, operation_id)
        if target != current:
            return
        validate(db, project, row)
        rows = [row]
        measurement = quota_storage._native_measurement(
            db, project, rows, operations.now()
        )
        power.validate_storage_inventory(db, project, proof, measurement)
        if utc(measurement.started_at) <= utc(
            datetime.fromisoformat(target["initialization_started_at"])
        ):
            raise AccountingUnavailable(
                "Native storage observation predates initialization"
            )
        quota_storage.apply_measurement(
            db, project, rows, measurement, operations.now()
        )
        update(
            row, initialization_proof=proof, initialized_report=measurement.evidence()
        )
        metering.reconcile_project(
            db,
            project,
            now=operations.now(),
            snapshot=power.billing_snapshot(db, project, [row]),
        )
        if not measured_initialization(db, row, measurement):
            update(row, blocker="initialization_not_blank")
            db.commit()  # Complete measured allocation stays charged, runtime stays off.
            return
        if measurement.allocated_bytes > target["initial_bound"]:
            update(row, blocker="initialization_bound_exceeded")
            db.commit()  # Count every observed byte, retain assets and block runtime.
            return
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "native_initialization_consumed": admission.initialization_reference(row),
        }
        revalidate_initialized(db, row)
        request = marker(target, operation_id, "runtime", profile)
        request.update(
            initializationDigest=proof["initialization_digest"],
            physicalReportId=measurement.report_id,
            physicalObservationId=digest(measurement.evidence()),
        )
        update(
            row,
            phase="runtime-intent",
            runtime_request=request,
            initialized_report=measurement.evidence(),
        )
        db.commit()


def observe_runtime(api, target, operation_id):
    return observe(api, target, operation_id, runtime=True)


def confirm(project_id, operation_id, target, proof):
    from app.core.database import SessionLocal
    from app.services import metering_service as metering
    from app.services import quota_storage

    with SessionLocal() as db:
        project, row, current, _ = locked(db, project_id, operation_id)
        if target != current or row.state not in admission.PENDING:
            return
        measurement = quota_storage._native_measurement(
            db, project, [row], operations.now()
        )
        power.validate_storage_inventory(db, project, proof, measurement)
        observed = datetime.fromisoformat(proof["observed_at"])
        if not operations._observation_follows_worker(row, observed):
            raise AccountingUnavailable(
                "Native running observation predates worker completion"
            )
        quota_storage.apply_measurement(
            db, project, [row], measurement, operations.now()
        )
        vector = Resources(**row.requested)
        vector.storage_bytes = measurement.allocated_bytes
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "native_running_proof": proof,
            "native_baseline_profile": {
                "profile": "configured-workloads-v1",
                "topology": deepcopy(project.topology),
                "targets": proof["targets"],
                "demand": proof["configured_profile"],
            },
        }
        admission.confirm(
            db,
            operation_id,
            vector,
            "native-running:" + proof["observation_id"],
            observed,
        )
        update(
            row,
            phase="confirmed",
            running_proof=proof,
            storage_inventory=proof["storage_inventory"],
        )
        power.mark_active(project, observed)
        metering.reconcile_project(
            db,
            project,
            now=observed,
            snapshot=power.billing_snapshot(db, project, [row], proof),
        )
        db.commit()
        return True


def publish_confirmation(api, target, operation_id):
    root = power._live_root(
        api, {"root": target["root"], "generation": operation_id}, operation_id
    )
    if power.helpers().intent(root) != target["runtime_request"]:
        raise AccountingUnavailable("Native confirmed deployment episode changed")
    state = deepcopy(root.get("status", {}).get("managedDeployment", {}))
    if state.get("confirmedObservationId") == target["running_proof"]["observation_id"]:
        return
    state["confirmedObservationId"] = target["running_proof"]["observation_id"]
    changes = [
        {"op": "add", "path": "/status/managedDeployment", "value": state},
        {"op": "add", "path": "/status/phase", "value": "Running"},
    ]
    api.patch(root, changes, status=True)
