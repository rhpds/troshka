"""Durable admission for the supported single-host nested VM execution path.

An operation may be delivered repeatedly, but has one committed dispatch claim.
An ambiguous claimed worker is reconciled, never dispatched again on a timer.
Unsupported adapters fail before placement. Cleanup never requires readiness.
"""

import json
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException

from app.models.host import Host
from app.models.project import Project
from app.models.quota import QuotaAllocation, QuotaReservation
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services.quota_resources import requested_resources

current_operation = ContextVar("quota_operation", default=None)


def now():
    return datetime.now(UTC)


def unavailable(message):
    raise HTTPException(409, {"reason": "accounting_unavailable", "message": message})


def deny_unsupported(operation):
    if accounting.quotas_enabled():
        raise HTTPException(
            409,
            {
                "reason": "unsupported_quota_operation",
                "message": f"{operation} is unavailable with quotas: its resource transition is not supported",
            },
        )


def unsupported_job(function):
    @wraps(function)
    def run(*args, **kwargs):
        deny_unsupported(function.__name__)
        return function(*args, **kwargs)

    return run


def _http_denial(error):
    if isinstance(error, QuotaDenied):
        # Aggregate binding constraint only; never another member's identities.
        return HTTPException(
            409,
            {
                "reason": error.reason,
                "scope": error.scope,
                "dimension": error.dimension,
                "used": str(error.used),
                "reserved": str(error.reserved),
                "requested": str(error.requested),
                "limit": None if error.limit is None else str(error.limit),
            },
        )
    return HTTPException(
        409, {"reason": "accounting_unavailable", "message": str(error)}
    )


def digest(topology):
    keys = {
        "vcpus",
        "ram",
        "size",
        "format",
        "libraryItemId",
        "snapshotItemId",
        "patternId",
        "patternDiskId",
    }
    mapping = [
        {
            "id": node["id"],
            "type": node.get("type"),
            "data": {
                key: value
                for key, value in (node.get("data") or {}).items()
                if key in keys
            },
        }
        for node in topology.get("nodes", [])
    ]
    return sha256(
        json.dumps(
            {"nodes": mapping, "edges": topology.get("edges", [])},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def supported(db, project, *, host_id=None, deployed=False):
    """Reject configurations whose actual allocations/power cannot be bounded."""
    topology = (
        (project.deployed_topology if deployed else None) or project.topology or {}
    )
    if project.mesh_subnet_id or project.host_assignments:
        raise AccountingUnavailable("Multi-host quota observations are not supported")
    # These features allocate runners/BMC/operator resources outside the VM map.
    forbidden = (
        "ocp",
        "clusters",
        "ocpClusters",
        "workloads",
        "showroom",
        "recert",
        "bastion",
        "_deploy_recert",
        "externalIps",
    )
    if any(topology.get(key) for key in forbidden):
        raise AccountingUnavailable(
            "Installer, Showroom and runner overhead require a quota adapter"
        )
    for node in topology.get("nodes", []):
        _supported_node(node)
    if project.provider_id:
        _supported_provider(db, project.provider_id)
    selected = host_id or project.host_id
    if selected:
        host = db.get(Host, selected)
        if not host or host.host_type == "kubevirt-cluster":
            raise AccountingUnavailable(
                "Native/operator quota allocation is not supported"
            )
        _supported_provider(db, host.provider_id)
        if host.billing_mode != "shared":
            raise AccountingUnavailable(
                "Dedicated backing-host ownership requires retained billing observations"
            )
    if project.disks:
        raise AccountingUnavailable(
            "Legacy disk metadata needs verified allocation identity before quota use"
        )


def _supported_node(node):
    data = node.get("data") or {}
    if node.get("type") not in {"vmNode", "storageNode", "networkNode"}:
        raise AccountingUnavailable("Only basic nested VMs are supported with quotas")
    if node.get("type") == "storageNode" and data.get("format", "qcow2") != "qcow2":
        raise AccountingUnavailable(
            "Physical storage quotas support qcow2 COW disks only; raw/copy storage needs an adapter"
        )
    flags = (
        "bmcIp",
        "bmc",
        "bmcEnabled",
        "deferred",
        "deferredWorker",
        "deferOcpInstall",
        "ocpRole",
        "ocpCluster",
        "showroom",
        "antiAffinity",
    )
    if any(data.get(key) for key in flags):
        raise AccountingUnavailable(
            "External power, installers and multi-host placement require a quota adapter"
        )
    if data.get("subtype") == "bmc" or any(
        value for key, value in data.items() if key.startswith("ocp")
    ):
        raise AccountingUnavailable(
            "BMC/operator configurations require a quota adapter"
        )


def _supported_provider(db, provider_id):
    from app.models.provider import Provider

    provider = db.get(Provider, provider_id) if provider_id else None
    if not provider or provider.type not in {"aws", "ec2", "gcp", "azure", "ocpvirt"}:
        raise AccountingUnavailable("Provider is not supported by nested VM quotas")


def virtual_sizes(db, topology):
    """Require virtual capacity evidence; sparse image size is not capacity."""
    from app.models.library import LibraryItemDisk

    sizes = {}
    for node in topology.get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        data = node.get("data") or {}
        if (
            data.get("patternId")
            or data.get("patternDiskId")
            or data.get("imageId")
            or data.get("image")
        ):
            raise AccountingUnavailable(
                "Pattern/image storage requires verified virtual-size evidence"
            )
        item_id = data.get("libraryItemId") or data.get("snapshotItemId")
        if not item_id:
            continue
        disks = db.query(LibraryItemDisk).filter_by(library_item_id=item_id).all()
        if len(disks) != 1 or not disks[0].virtual_size_bytes:
            raise AccountingUnavailable(
                "Image virtual capacity must be verified before reservation"
            )
        sizes[node["id"]] = (disks[0].virtual_size_bytes + 2**30 - 1) // 2**30
    return sizes


def _rows(db, project_id):
    return (
        db.query(QuotaReservation)
        .filter_by(project_id=project_id)
        .populate_existing()
        .all()
    )


def current_generation(rows):
    """Keep every worker fence in a retained generation, leaving retired history intact."""
    retained = {
        _generation(row)
        for row in rows
        if row.state not in {"confirmed", "released"} or any(row.observed.values())
    }
    return [row for row in rows if _generation(row) in retained]


def _generation(row):
    return row.dispatch_metadata.get("generation_id") or row.operation_id


def _start_generation(rows):
    generations = {_generation(row) for row in current_generation(rows)}
    if len(generations) != 1:
        raise AccountingUnavailable(
            "Current deployment generation requires reconciliation"
        )
    return generations.pop()


def resource_map(project, topology):
    from types import SimpleNamespace

    from app.services.deploy_topology import _disk_path, _find_vm_disks, _vm_domain_name

    result = {"vms": {}, "disks": {}}
    for node in topology.get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        data = node.get("data") or {}
        result["vms"][_vm_domain_name(project.id, node["id"])] = {
            "cpu": data.get("vcpus", 2),
            "ram_mb": data.get("ram", 4) * 1024,
        }
        for disk in _find_vm_disks(node["id"], topology):
            for pool in (None, SimpleNamespace(mode="shared")):
                path = _disk_path(
                    project.id, node["id"], disk["node_id"], disk["format"], pool
                )
                result["disks"][path] = disk["size_gb"]
    return result


def known_empty(db, project):
    """Admission captured an empty draft; no claimed or ambiguous provider work."""
    rows = current_generation(_rows(db, project.id))
    return (
        bool(rows)
        and all(
            r.state == "pending"
            and r.dispatch_metadata.get("known_empty")
            and not r.dispatch_metadata.get("claim")
            for r in rows
        )
        and not (
            db.query(QuotaAllocation)
            .filter_by(project_id=project.id)
            .filter(QuotaAllocation.state != "released")
            .first()
        )
    )


def prepare(db, project, kind, *, host_id=None):
    """Reserve before placement/state changes. Caller commits before dispatch."""
    if not accounting.quotas_enabled():
        return None
    try:
        supported(db, project, host_id=host_id)
        operation_id = str(uuid4())
        # The operation advisory lock is always before identity locks.
        admission.lock_operations(db, (operation_id,))
        from app.services.quota_locks import lock_project

        project, _, _ = lock_project(db, project.id)
        topology, sizes = _normalize_image_sizes(db, project)
        rows = _rows(db, project.id)
        if any(r.state in admission.PENDING for r in rows):
            raise AccountingUnavailable(
                "An earlier allocation is pending reconciliation"
            )
        current = _current_resources(db, project, kind, rows, topology)
        increment = requested_resources(
            topology, kind, current=current, virtual_sizes=sizes
        )
        from app.services import quota_storage

        increment.storage_bytes = (
            quota_storage.initial_allocation(topology) if kind == "deploy" else 0
        )
        generation_id = operation_id if kind == "deploy" else _start_generation(rows)
        admission.reserve(db, project.id, operation_id, increment, now())
        row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
        row.dispatch_metadata = {
            "kind": kind,
            "desired_digest": digest(topology),
            "virtual_sizes": sizes,
            "known_empty": kind == "deploy",
            "calls": {},
            "worker_finished": False,
            "resources": resource_map(project, topology),
            "generation_id": generation_id,
        }
        from app.services.quota_resources import topology_resources

        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "storage_version": quota_storage.VERSION,
            "provisioned_storage_bytes": topology_resources(
                topology, virtual_sizes=sizes
            ).storage_bytes,
        }
        db.flush()
        return operation_id
    except (AccountingUnavailable, QuotaDenied, admission.OperationConflict) as error:
        raise _http_denial(error) from error


def _normalize_image_sizes(db, project):
    import copy

    topology = copy.deepcopy(project.topology or {})
    sizes = virtual_sizes(db, topology)
    for node in topology.get("nodes", []):
        if node["id"] in sizes:
            node["data"]["size"] = max(node["data"].get("size", 10), sizes[node["id"]])
    project.topology = topology
    return topology, sizes


def _current_resources(db, project, kind, rows, topology):
    if kind == "deploy":
        if project.state != "draft" or any(any(r.observed.values()) for r in rows):
            raise AccountingUnavailable("Deploy requires a verified empty draft")
        return Resources()
    if kind == "start":
        rows = current_generation(rows)
        if not rows or any(
            r.dispatch_metadata.get("desired_digest") != digest(topology) for r in rows
        ):
            raise AccountingUnavailable(
                "A verified matching deployed allocation is required"
            )
        # A whole-project target is the only operation subtracting aggregate usage.
        return admission.usage(db, "project", project.id)[0]
    raise AccountingUnavailable("Unsupported quota operation")


def placement_guard(db, project, operation_id):
    if not accounting.quotas_enabled():
        return
    if not operation_id:
        raise AccountingUnavailable("Placement requires a durable quota operation")
    admission.revalidate(db, operation_id, now())
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    if row.state != "pending" or row.dispatch_metadata.get("claim"):
        raise AccountingUnavailable("Claimed operations cannot repeat placement")
    supported(db, project)


def claim(db, project_id, operation_id, kind):
    if not operation_id:
        raise AccountingUnavailable(
            "Worker requires its original quota operation; recovery must reconcile"
        )
    admission.revalidate(db, operation_id, now())
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    project = db.get(Project, project_id)
    _validate_claim_target(db, row, project, project_id, kind)
    metadata = dict(row.dispatch_metadata)
    metadata["claim"] = str(uuid4())
    row.dispatch_metadata = metadata
    row.state = "dispatched"
    row.dispatched_at = now()
    db.commit()


def _validate_claim_target(db, row, project, project_id, kind):
    metadata = row.dispatch_metadata
    if row.project_id != project_id or metadata.get("kind") != kind:
        raise AccountingUnavailable("Quota operation does not match this worker")
    if project.state != {"deploy": "deploying", "start": "starting"}[kind]:
        raise AccountingUnavailable(
            "Project no longer accepts this allocation operation"
        )
    if row.state != "pending" or metadata.get("claim") or metadata.get("cancelled"):
        raise AccountingUnavailable(
            "Operation already claimed; reconcile before further allocation"
        )
    supported(db, project)
    if metadata["desired_digest"] != digest(project.topology or {}):
        raise AccountingUnavailable("Desired topology changed after reservation")
    # Re-resolve image evidence before dispatch; newly larger images cannot slip through.
    if virtual_sizes(db, project.topology or {}) != metadata["virtual_sizes"]:
        raise AccountingUnavailable("Image capacity changed after reservation")
    from app.services.quota_storage import bind_host

    bind_host(db, project, db.get(Host, project.host_id), row.operation_id)


def guarded_job(kind):
    def decorate(function):
        @wraps(function)
        def run(project_id, *args, quota_operation_id=None, **kwargs):
            if not accounting.quotas_enabled():
                return function(project_id, *args, **kwargs)
            from app.core.database import SessionLocal

            with SessionLocal() as db:
                claim(db, project_id, quota_operation_id, kind)
            token = current_operation.set(quota_operation_id)
            try:
                return function(project_id, *args, **kwargs)
            finally:
                current_operation.reset(token)
                with SessionLocal() as db:
                    admission.lock_operations(db, (quota_operation_id,))
                    row = (
                        db.query(QuotaReservation)
                        .filter_by(operation_id=quota_operation_id)
                        .one()
                    )
                    row.dispatch_metadata = {
                        **row.dispatch_metadata,
                        "worker_finished": True,
                        "worker_finished_at": now().isoformat(),
                    }
                    if row.state in admission.PENDING:
                        row.state = "reconciling"
                    db.commit()
                reconcile(project_id)

        return run

    return decorate


def _settled(row):
    calls = row.dispatch_metadata.get("calls", {})
    return row.dispatch_metadata.get("worker_finished") and all(
        call.get("terminal") for call in calls.values()
    )


def reconcile(project_id):
    """Conservative full-environment compute observation, never project.state."""
    if not accounting.quotas_enabled():
        return
    from app.core.database import SessionLocal
    from app.services.ws_pubsub import get_cached_vm_states

    snapshot = get_cached_vm_states(project_id) or {}
    timestamp = snapshot.get("observed_at")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    if not isinstance(timestamp, datetime):
        return
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if not project:
            return
        rows = _lock_observation_batch(db, project_id)
        if rows is None or not all(
            _observation_follows_worker(row, timestamp) for row in rows
        ):
            return
        power = _whole_project_power(project, snapshot)
        if power is None:
            return
        _confirm_power(db, rows, power, snapshot, timestamp)
        db.commit()

    from app.services import quota_storage

    quota_storage.reconcile_project(project_id)


def _confirm_power(db, rows, power, snapshot, timestamp):
    for row in rows:
        vector = Resources(
            **(row.observed if row.state == "confirmed" else row.requested)
        )
        if row.dispatch_metadata.get("storage_measured_at"):
            vector.storage_bytes = row.observed["storage_bytes"]
        if power == "off":
            vector.cpu = vector.ram_bytes = 0
        identity = snapshot.get("observation_id") or timestamp.isoformat()
        admission.confirm(db, row.operation_id, vector, f"power:{identity}", timestamp)


def _lock_observation_batch(db, project_id):
    rows = _rows(db, project_id)
    identities = {row.operation_id for row in rows}
    admission.lock_operations(db, identities)
    from app.services.quota_locks import lock_project

    lock_project(db, project_id, require_organization=False)
    current = _rows(db, project_id)
    if {row.operation_id for row in current} != identities:
        return None  # Retry the full batch; never take new operation locks here.
    return current_generation(current)


def _observation_follows_worker(row, timestamp):
    finished = row.dispatch_metadata.get("worker_finished_at")
    # This protects capacity on which newer zero-delta work may already rely.
    return bool(
        _settled(row)
        and finished
        and accounting.utc(timestamp) > accounting.utc(datetime.fromisoformat(finished))
    )


def retained_topology(project):
    """Provider identity follows deployment, never a subsequent editor save.

    Before deployment records its snapshot, admission's topology edit fence
    protects the pending target. An explicitly empty deployed map stays empty.
    """
    deployed = getattr(project, "deployed_topology", None)
    return deployed if deployed is not None else (project.topology or {})


def _whole_project_power(project, snapshot):
    states = snapshot.get("states", {})
    expected = {
        node["id"]
        for node in retained_topology(project).get("nodes", [])
        if node.get("type") == "vmNode"
    }
    if not expected or not expected.issubset(states):
        return None
    off_states = {"shut_off", "shut off", "shutoff", "stopped", "off", "hibernated"}
    if all(states[value] in off_states for value in expected):
        return "off"
    if all(states[value] in {"running", "paused"} for value in expected):
        return "on"
    # Shutdown-in-progress, suspended and crashed are not proof of release.
    # Keep existing reservations/allocations until a genuine off observation.
    return None


def recover(project_id):
    """Recover only never-claimed work; claimed/lost work stays reconciling."""
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job
    from app.services.deploy_service import deploy_project_async, start_project_async

    with SessionLocal() as db:
        rows = _rows(db, project_id)
        pending = [
            r
            for r in rows
            if r.state == "pending" and not r.dispatch_metadata.get("cancelled")
        ]
        if len(pending) != 1:
            raise AccountingUnavailable(
                "Interrupted work requires provider reconciliation; no redispatch"
            )
        row = pending[0]
        function = (
            deploy_project_async
            if row.dispatch_metadata["kind"] == "deploy"
            else start_project_async
        )
        return enqueue_job(
            function,
            project_id,
            project_id=project_id,
            quota_operation_id=row.operation_id,
        )


GROWTH_COMMANDS = {
    "/vms/create",
    "/vms/start",
    "/vms/resume",
    "/vms/reboot",
    "/vms/reconfigure",
    "/disks/create",
    "/disks/resize",
    "/containers/create",
    "/containers/start",
    "/pods/create",
    "/pods/start",
    "/bmc/setup",
    "/bmc/create-bridge",
    "/snapshots/create",
    "/snapshots/capture",
    "/patterns/capture-direct",
    "/vms/file-pull-snapshot",
    "/nbd/export",
    "/nbd/pull-flatten",
    "/images/cache",
}


def tracked_agent_job(function):
    """Persist intent BEFORE provider dispatch, including an ambiguous HTTP result."""

    @wraps(function)
    def run(host, path, params, *args, **kwargs):
        if not accounting.quotas_enabled() or path not in GROWTH_COMMANDS:
            return function(host, path, params, *args, **kwargs)
        operation_id = current_operation.get()
        if not operation_id:
            raise AccountingUnavailable(
                "Provider allocation requires a claimed quota operation"
            )
        from app.core.database import SessionLocal

        ticket = str(uuid4())
        with SessionLocal() as db:
            try:
                _record_agent_intent(db, operation_id, ticket, host, path, params)
            except QuotaDenied:
                # A denied provider call still leaves its trusted physical growth
                # charged. No intent/provider mutation has been recorded yet.
                db.commit()
                raise
            db.commit()
        result = function(host, path, params, *args, **kwargs)
        with SessionLocal() as db:
            admission.lock_operations(db, (operation_id,))
            row = (
                db.query(QuotaReservation)
                .filter_by(operation_id=operation_id)
                .populate_existing()
                .one()
            )
            calls = dict(row.dispatch_metadata["calls"])
            calls[ticket] = {**calls[ticket], "job_id": result}
            row.dispatch_metadata = {**row.dispatch_metadata, "calls": calls}
            db.commit()
        return result

    return run


def _record_agent_intent(db, operation_id, ticket, host, path, params):
    project, row, rows = _lock_agent_generation(db, operation_id)
    _validate_agent_target(db, project, row, host)
    _observe_before_dispatch(db, project, row, rows, host)
    _validate_provider_params(row, path, params)
    _append_agent_intent(row, ticket, host, path, params)


def _lock_agent_generation(db, operation_id):
    """Lock the full operation set before any current membership identity."""
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    rows = _lock_observation_batch(db, row.project_id)
    if rows is None or not any(value.operation_id == operation_id for value in rows):
        raise AccountingUnavailable(
            "Provider generation changed; retry the complete admission phase"
        )
    from app.services.quota_locks import lock_project

    project, owner, organization = lock_project(db, row.project_id)
    if organization is None:
        raise AccountingUnavailable("Project requires an assigned owner organization")
    row = (
        db.query(QuotaReservation)
        .filter_by(operation_id=operation_id)
        .populate_existing()
        .one()
    )
    if (row.owner_id, row.organization_id) != (owner.id, organization.id):
        raise AccountingUnavailable(
            "Provider allocation attribution changed; reconcile before dispatch"
        )
    return project, row, rows


def _validate_agent_target(db, project, row, host):
    if row.state != "dispatched" or not row.dispatch_metadata.get("claim"):
        raise AccountingUnavailable("Provider allocation has no live dispatch claim")
    if row.dispatch_metadata.get("cancelled"):
        raise AccountingUnavailable("Provider allocation cancelled for cleanup")
    supported(db, project, host_id=host.id)
    if project.host_id != host.id or row.dispatch_metadata["desired_digest"] != digest(
        project.topology or {}
    ):
        raise AccountingUnavailable(
            "Provider allocation differs from the admitted environment"
        )
    if project.state not in {"deploying", "starting"}:
        raise AccountingUnavailable("Project no longer accepts provider allocations")


def _observe_before_dispatch(db, project, row, rows, host):
    from app.services import quota_storage

    evidence = quota_storage.fetch(
        host, project.id, row.dispatch_metadata["generation_id"]
    )
    if row.dispatch_metadata.get("storage_pool_id") != evidence["writable_pool"]:
        raise AccountingUnavailable("Physical storage pool changed after admission")
    quota_storage.apply_measurement(db, project, rows, evidence, now())
    quota_storage.check_pool(db, evidence)
    for scope, identity in (
        ("project", project.id),
        ("user", row.owner_id),
        ("organization", row.organization_id),
    ):
        quota_storage.check_allowance(db, scope, identity, now())
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "storage_admission_evidence": evidence,
    }


def _append_agent_intent(row, ticket, host, path, params):
    calls = dict(row.dispatch_metadata.get("calls", {}))
    action_key = path + ":" + str(params.get("domain_name") or params.get("path"))
    if any(call.get("action_key") == action_key for call in calls.values()):
        raise AccountingUnavailable(
            "This resource command was already dispatched; reconcile its original job"
        )
    calls[ticket] = {
        "path": path,
        "host_id": host.id,
        "terminal": False,
        "action_key": action_key,
    }
    row.dispatch_metadata = {**row.dispatch_metadata, "calls": calls}


def _validate_provider_params(row, path, params):
    mapping = row.dispatch_metadata["resources"]
    if path == "/images/cache":
        # The normal cache filter skips warm files. Never authorize a downloader:
        # it may replace an existing but short/corrupt file with a full image.
        raise AccountingUnavailable(
            "Shared base cache is cold or unverified; a platform administrator must warm and verify it before quota deployment"
        )
    if path in {"/vms/start", "/vms/create"}:
        vm = mapping["vms"].get(params.get("domain_name"))
        if vm is None or (
            path == "/vms/create"
            and (
                params.get("vcpus") != vm["cpu"] or params.get("ram_mb") != vm["ram_mb"]
            )
        ):
            raise AccountingUnavailable("VM capacity differs from its reservation")
        return
    if path == "/disks/create":
        _validate_disk_params(row, params)
        return
    raise AccountingUnavailable("Unsupported provider growth command")


def _validate_disk_params(row, params):
    capacity = row.dispatch_metadata["resources"]["disks"].get(params.get("path"))
    size = params.get("size_gb")
    if (
        capacity is None
        or not isinstance(size, (int, float))
        or size > capacity
        or size < 0
    ):
        raise AccountingUnavailable("Disk capacity exceeds its reservation")
    if params.get("format", "qcow2") != "qcow2" or set(params) - {
        "path",
        "size_gb",
        "format",
        "backing_file",
    }:
        raise AccountingUnavailable("Unsupported physical disk creation options")
    evidence = row.dispatch_metadata.get("storage_admission_evidence", {})
    if params.get("backing_file") and not any(
        item["path"] == params["backing_file"] for item in evidence.get("bases", [])
    ):
        raise AccountingUnavailable(
            "Backing image lacks verified shared-base allocation evidence"
        )


def record_terminal(host_id, job_id, status):
    operation_id = current_operation.get()
    if (
        not accounting.quotas_enabled()
        or not operation_id
        or status not in {"completed", "failed", "cancelled"}
    ):
        return
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        admission.lock_operations(db, (operation_id,))
        row = (
            db.query(QuotaReservation)
            .filter_by(operation_id=operation_id)
            .populate_existing()
            .one()
        )
        calls = dict(row.dispatch_metadata.get("calls", {}))
        for key, call in calls.items():
            if call.get("job_id") == job_id and call.get("host_id") == host_id:
                calls[key] = {**call, "terminal": True, "status": status}
        row.dispatch_metadata = {**row.dispatch_metadata, "calls": calls}
        db.commit()


def guard_topology_edit(db, project):
    """Serialize autosave with admission so an accepted target cannot be enlarged."""
    if not accounting.quotas_enabled():
        return
    from app.services.quota_locks import lock_project

    lock_project(db, project.id, require_organization=False)
    if any(row.state in admission.PENDING for row in _rows(db, project.id)):
        unavailable(
            "Topology is locked while allocation is pending or requires reconciliation"
        )


def record_delivery(operation_id, delivery_id, kind):
    if not accounting.quotas_enabled() or not operation_id:
        return
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        admission.lock_operations(db, (operation_id,))
        row = (
            db.query(QuotaReservation)
            .filter_by(operation_id=operation_id)
            .populate_existing()
            .one()
        )
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "delivery_id": delivery_id,
            "delivery_kind": kind,
        }
        db.commit()
