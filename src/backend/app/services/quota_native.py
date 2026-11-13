"""Native demand inspection and enrolled measurements; never authorize growth.

Known compute is a lower bound while any demand is blocked. Demand inspection
keeps PVC capacity separate from nullable initialization allocation. Measurements
consume only complete enrolled graphs; neither nested qcow2 allowances nor raw
Ceph capacity can supply native allocation.
"""

import importlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.schemas.quota import AccountingUnavailable

METRIC = "rbd_allocated_extent_bytes"
MAX_QUANTITY = 2**63 - 1
ADAPTER = "kubevirt"
MEASUREMENT_VERSION = "native-rbd-measurement-v1"
CONFIGURED_PROFILE = "configured-workloads-v1"


@dataclass(frozen=True)
class NativeStorageMeasurement:
    """Exact attributed allocation, never a provisioned size or known subtotal."""

    project_id: str
    host_id: str
    provider_id: str
    generation_id: str
    identity: dict
    report_id: str
    started_at: datetime
    completed_at: datetime
    observed_at: datetime
    pools: tuple[tuple[str, int], ...]
    allocated_bytes: int
    current_enrollment_id: str | None = None

    def evidence(self):
        value = {
            "version": MEASUREMENT_VERSION,
            "metric": METRIC,
            "project_id": self.project_id,
            "host_id": self.host_id,
            "provider_id": self.provider_id,
            "generation_id": self.generation_id,
            "identity": self.identity,
            "report_id": self.report_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "pools": [list(pool) for pool in self.pools],
            "allocated_bytes": self.allocated_bytes,
        }
        if self.current_enrollment_id is not None:
            value["current_enrollment_id"] = self.current_enrollment_id
        return value


def _measurement_domain(db, project_id, identity):
    from app.models.kubevirt_storage import NativeCollectionDomain, NativeProjectRoot

    roots = db.query(NativeProjectRoot).filter_by(project_id=project_id).all()
    if len(roots) != 1:
        raise AccountingUnavailable("Native storage requires one retained project root")
    root = roots[0]
    domain = (
        db.query(NativeCollectionDomain)
        .filter_by(id=root.domain_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if domain is None or identity.get("domain_id") != domain.id:
        raise AccountingUnavailable("Native storage collection domain changed")
    if identity.get("enrollment_id") != domain.enrollment_id:
        from app.services.kubevirt_storage_enrollment import retained_enrollment

        try:
            retained_enrollment(db, domain, identity)
        except (ValueError, KeyError, TypeError) as error:
            raise AccountingUnavailable("Native storage enrollment changed") from error
    return root, domain


def _measurement_binding(db, project, root, domain, identity):
    from app.models.host import Host
    from app.models.kubevirt_storage import NativeEnrollment
    from app.models.provider import Provider
    from app.models.user import User
    from app.schemas.kubevirt_storage_enrollment import VerifiedEnrollment
    from app.services.kubevirt_storage_enrollment import digest, root_identity

    enrollment = db.get(NativeEnrollment, domain.enrollment_id)
    if enrollment is None or enrollment.domain_id != domain.id:
        raise AccountingUnavailable("Native enrollment unavailable")
    manifest = VerifiedEnrollment.model_validate(enrollment.payload)
    if manifest.digest != enrollment.digest or manifest.digest != digest(
        manifest.model_dump(mode="json", exclude={"digest"})
    ):
        raise AccountingUnavailable("Native enrollment identity changed")
    expected = [p for p in manifest.request.projects if str(p.project_id) == project.id]
    pinned = [r for r in manifest.pins.project_roots if str(r.project_id) == project.id]
    if len(expected) != 1 or len(pinned) != 1:
        raise AccountingUnavailable("Native enrollment project identity ambiguous")
    expected, pinned = expected[0], pinned[0]
    host = db.get(Host, project.host_id) if project.host_id else None
    owner = db.get(User, project.owner_id) if project.owner_id else None
    provider = db.get(Provider, host.provider_id) if host else None
    if (
        provider is None
        or host is None
        or provider.type != "kubevirt"
        or owner is None
        or project.host_id != str(expected.host_id)
        or provider.id != str(expected.provider_id)
        or root.provider_id != provider.id
        or project.provider_id not in (None, provider.id)
        or project.owner_id != str(expected.owner_id)
        or owner.organization_id
        != (str(expected.organization_id) if expected.organization_id else None)
    ):
        raise AccountingUnavailable("Native storage host/provider/ownership changed")
    if (
        root.payload != root_identity(pinned)
        or identity.get("root") != pinned.model_dump(mode="json")
        or identity.get("host_id") != host.id
        or identity.get("provider_id") != provider.id
    ):
        raise AccountingUnavailable(
            "Native retained namespace/project identity changed"
        )
    if pinned.deployment_generation is None:
        raise AccountingUnavailable("Native project generation is unknown")
    return host, provider, pinned, manifest


def _measurement_report(db, domain, roots, manifest, now):
    from app.models.kubevirt_storage import NativeStorageSnapshot
    from app.services.kubevirt_storage_enrollment import digest, root_key, utc
    from app.services.kubevirt_storage_usage import MAX_REPORT_AGE, domain_evidence

    graph, source, failures = domain_evidence(db, domain, roots)
    report = db.get(NativeStorageSnapshot, domain.latest_report_id) if source else None
    if failures or graph is None or source is None or report is None:
        raise AccountingUnavailable("Native latest collection attempt is unavailable")
    if (
        {r.key for r in roots} != {root_key(r) for r in source.project_roots}
        or set(source.pool_keys) != set(manifest.pins.pool_keys)
        or source.collector_id not in manifest.pins.collector_ids
    ):
        raise AccountingUnavailable("Native enrolled graph inventory is incomplete")
    if (
        report.domain_id != domain.id
        or report.enrollment_id != domain.enrollment_id
        or str(source.report_id) != report.id
        or digest(source) != report.digest
        or utc(domain.latest_started_at) != utc(report.started_at)
        or utc(source.started_at) != utc(report.started_at)
        or utc(source.completed_at) != utc(report.completed_at)
    ):
        raise AccountingUnavailable("Native report identity changed")
    times = [utc(source.started_at), *[utc(i.collected_at) for i in source.images]]
    if not (
        min(times) >= utc(now) - MAX_REPORT_AGE
        and utc(source.started_at)
        <= min(times)
        <= max(times)
        <= utc(source.completed_at)
        and utc(source.completed_at) <= utc(report.imported_at) <= utc(now)
    ):
        raise AccountingUnavailable(
            "Native source observation is stale or future-dated"
        )
    if (
        any(
            p.coverage != "complete" or p.allocated_bytes is None or p.blockers
            for p in graph.projects
        )
        or any(p.allocated_bytes is None for p in graph.platform_pools)
        or source.collection_errors
    ):
        raise AccountingUnavailable("Native graph coverage is incomplete")
    return graph, source, report, min(times)


def storage_measurement(db, project, generation_id, identity, now):
    """Read current enrolled evidence under operation/membership locks held by caller.

    The final domain lock serializes imports with reconciliation/evaluation. No
    network I/O, quota writes, or implicit enrollment is performed here.
    """
    from app.models.kubevirt_storage import NativeProjectRoot

    try:
        root, domain = _measurement_domain(db, project.id, identity)
        host, provider, pinned, manifest = _measurement_binding(
            db, project, root, domain, identity
        )
        roots = db.query(NativeProjectRoot).filter_by(domain_id=domain.id).all()
        graph, source, report, observed = _measurement_report(
            db, domain, roots, manifest, now
        )
        supplied = [
            r
            for r in [
                *source.project_roots,
                *[b.root for b in source.volume_bindings + source.retained_bindings],
                *[s.root for s in source.snapshot_evidence],
            ]
            if str(r.project_id) == project.id
        ]
        if not supplied or any(r != pinned for r in supplied):
            raise AccountingUnavailable("Native report project generation changed")
        projects = [p for p in graph.projects if str(p.project_id) == project.id]
        if len(projects) != 1 or projects[0].namespace_uid != pinned.namespace_uid:
            raise AccountingUnavailable("Native project graph attribution unavailable")
        amount = projects[0].allocated_bytes
        if amount is None:
            raise AccountingUnavailable("Native exact allocation unavailable")
        pools = tuple(sorted((p.ceph_fsid, p.pool_id) for p in set(source.pool_keys)))
        if not pools:
            raise AccountingUnavailable("Native physical pool identity unavailable")
        return NativeStorageMeasurement(
            project.id,
            host.id,
            provider.id,
            generation_id,
            identity,
            report.id,
            source.started_at,
            source.completed_at,
            observed,
            pools,
            amount,
            domain.enrollment_id
            if domain.enrollment_id != identity.get("enrollment_id")
            else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise AccountingUnavailable("Malformed native storage evidence") from error


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _helpers():
    """Both checkout and Containerfile.backend ship the same helpers package."""
    directory = Path(__file__).resolve().parents[3] / "operator"
    package = directory / "helpers"
    loaded = sys.modules.get("helpers")
    loaded_path = getattr(loaded, "__file__", None)
    if loaded and (not loaded_path or Path(loaded_path).resolve().parent != package):
        raise AccountingUnavailable("Conflicting native render helpers package")
    if not package.is_dir():
        raise AccountingUnavailable("Packaged native render helpers unavailable")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return tuple(
        importlib.import_module(f"helpers.{name}")
        for name in (
            "topology",
            "k8s",
            "kubevirt",
            "containers",
            "vnc",
            "ceph_appliance",
        )
    )


def resource_id(body):
    metadata = body["metadata"]
    return f"{body['kind']}:{metadata['namespace']}/{metadata['name']}"


@dataclass(frozen=True)
class NativeDemand:
    resource_id: str
    classification: str
    cpu_millicores: int | None = None
    ram_bytes: int | None = None
    provisioned_bytes: int | None = None
    initial_storage_bytes: int | None = None
    pool_key: tuple[str, int] | None = None
    blockers: tuple[str, ...] = ()
    requested_cpu_millicores: int | None = None
    requested_ram_bytes: int | None = None
    quota_workload: bool = False
    configured_cpu: str | None = None

    @property
    def tenant_compute(self):
        return self.classification == "guest" or self.quota_workload

    def __post_init__(self):
        if self.classification not in {"guest", "helper", "storage", "platform"}:
            raise AccountingUnavailable("Unclassified native resource")
        for value in (
            self.cpu_millicores,
            self.ram_bytes,
            self.provisioned_bytes,
            self.initial_storage_bytes,
            self.requested_cpu_millicores,
            self.requested_ram_bytes,
        ):
            if value is not None and (
                type(value) is not int or not 0 <= value <= MAX_QUANTITY
            ):
                raise AccountingUnavailable("Invalid native demand quantity")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.pool_key is not None:
            object.__setattr__(self, "pool_key", tuple(self.pool_key))


@dataclass(frozen=True)
class NativePoolDemand:
    pool_key: tuple[str, int] | None
    resource_ids: tuple[str, ...]
    initial_storage_bytes: int | None

    def __post_init__(self):
        value = self.initial_storage_bytes
        if value is not None and (
            type(value) is not int or not 0 <= value <= MAX_QUANTITY
        ):
            raise AccountingUnavailable(
                "Native pool initialization aggregate is invalid"
            )


@dataclass(frozen=True)
class NativeExecutionPlan:
    project_id: str
    host_id: str
    generation: str
    execution_digest: str
    topology_digest: str
    rendered_json: str = field(repr=False)
    demands: tuple[NativeDemand, ...]

    def __post_init__(self):
        object.__setattr__(self, "demands", tuple(self.demands))

    @property
    def rendered_resources(self):
        # Return copies; mutation cannot change the retained execution payload.
        return json.loads(self.rendered_json)

    @property
    def generated_resource_ids(self):
        return tuple(resource_id(body) for body in self.rendered_resources)

    @property
    def demand_ids(self):
        return tuple(item.resource_id for item in self.demands)

    @property
    def unclassified_resources(self):
        return tuple(sorted(set(self.generated_resource_ids) - set(self.demand_ids)))

    @property
    def unsupported_reasons(self):
        reasons = [
            f"{item.resource_id}:{reason}"
            for item in self.demands
            for reason in item.blockers
        ]
        for item in self.demands:
            if item.classification in {"guest", "helper"} and (
                item.cpu_millicores is None or item.ram_bytes is None
            ):
                reasons.append(f"{item.resource_id}:compute_envelope_unresolved")
            if (
                item.classification in {"storage", "platform"}
                and item.initial_storage_bytes is None
            ):
                reasons.append(
                    f"{item.resource_id}:storage_initialization_profile_missing"
                )
            if item.classification in {"storage", "platform"} and item.pool_key is None:
                reasons.append(f"{item.resource_id}:pool_identity_unresolved")
        return tuple(sorted(set(reasons)))

    @property
    def complete(self):
        return not self.unsupported_reasons and not self.unclassified_resources

    @property
    def helper_millicores(self):
        return sum(
            item.cpu_millicores or 0
            for item in self.demands
            if item.classification == "helper"
        )

    @property
    def guest_cpu(self):
        return _ceil(
            sum(
                item.cpu_millicores or 0
                for item in self.demands
                if item.classification == "guest"
            ),
            1000,
        )

    @property
    def cpu(self):
        """Known concurrent lower bound, never an admissible vector if blocked."""
        return _ceil(
            sum(
                Decimal(item.configured_cpu)
                if item.configured_cpu is not None
                else Decimal(item.cpu_millicores or 0) / 1000
                for item in self.demands
                if item.tenant_compute
            )
        )

    @property
    def tenant_complete(self):
        return all(
            not item.blockers
            and item.cpu_millicores is not None
            and item.ram_bytes is not None
            for item in self.demands
            if item.tenant_compute
        )

    @property
    def pool_initialization_demands(self):
        groups = {}
        for item in self.demands:
            if item.classification in {"storage", "platform"}:
                groups.setdefault(item.pool_key, []).append(item)
        return tuple(
            NativePoolDemand(
                key,
                tuple(item.resource_id for item in rows),
                (
                    None
                    if any(item.initial_storage_bytes is None for item in rows)
                    else sum(item.initial_storage_bytes for item in rows)
                ),
            )
            for key, rows in groups.items()
        )


def _ceil(value, divisor=1):
    return int((Decimal(value) / divisor).to_integral_value(rounding=ROUND_CEILING))


def _quantity(value, *, cpu=False):
    """Kubernetes quantity parser, never binary/decimal GiB substitution."""
    from kubernetes.utils.quantity import parse_quantity

    try:
        parsed = parse_quantity(str(value)) * (1000 if cpu else 1)
        result = _ceil(parsed)
        if parsed < 0 or result > MAX_QUANTITY:
            raise ValueError
        return result
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise AccountingUnavailable("Invalid rendered native quantity") from exc


def resolve_pool_alias(storage_class, aliases):
    """Consume independently verified aliases; never infer pools from class names.

    Each alias evidence row is (Ceph FSID, numeric pool ID, metric). This pure
    boundary deliberately accepts no ceph-df raw/replicated capacity conversion.
    Callers must independently establish freshness and identity before supplying
    evidence. build_native_plan has no such source yet and leaves pools unknown.
    """
    if not isinstance(aliases, dict):
        raise AccountingUnavailable("Invalid native pool alias evidence")
    candidates = aliases.get(storage_class, ())
    if not isinstance(candidates, (tuple, list)):
        raise AccountingUnavailable("Invalid native pool alias evidence")
    keys = set()
    for candidate in candidates:
        if not isinstance(candidate, (tuple, list)) or len(candidate) != 3:
            raise AccountingUnavailable("Invalid native pool alias evidence")
        fsid, pool_id, metric = candidate
        if (
            not isinstance(fsid, str)
            or not fsid
            or type(pool_id) is not int
            or not 0 <= pool_id <= MAX_QUANTITY
            or metric != METRIC
        ):
            raise AccountingUnavailable("Native pool identity or units unavailable")
        keys.add((fsid, pool_id))
    if len(keys) != 1:
        raise AccountingUnavailable("Native pool alias is unresolved or ambiguous")
    return next(iter(keys))


def _controller_count(spec, field, default):
    count = spec.get(field, default)
    if type(count) is not int or not 0 <= count <= MAX_QUANTITY:
        raise AccountingUnavailable("Invalid native controller workload count")
    return count


def _pod_concurrency(body):
    """Known steady concurrency only; rollout/replacement overlap is unresolved."""
    kind, spec = body["kind"], body["spec"]
    if kind == "Pod":
        return 1, []
    blockers = ["controller_transient_concurrency_unresolved"]
    if kind == "Deployment":
        return _controller_count(spec, "replicas", 1), blockers
    if kind != "Job":
        raise AccountingUnavailable("Unsupported native workload controller")
    count = _controller_count(spec, "parallelism", 1)
    if "completions" in spec:
        count = min(count, _controller_count(spec, "completions", 1))
    suspended = spec.get("suspend", False)
    if type(suspended) is not bool:
        raise AccountingUnavailable("Invalid native Job suspend state")
    return (0 if suspended else count), blockers


def _pod_demand(body):
    """Per-Pod caps at known concurrency are lower bounds for blocked controllers."""
    spec = body["spec"]
    replicas, blockers = _pod_concurrency(body)
    pod = spec if body["kind"] == "Pod" else spec["template"]["spec"]
    regular = [_container_envelope(item) for item in pod.get("containers", [])]
    init = [_container_envelope(item) for item in pod.get("initContainers", [])]
    quantities = []
    for dimension in (0, 1):
        peak = _pod_dimension(regular, init, dimension, replicas)
        quantities.append(peak)
        if peak is None:
            blockers.append("tested_bounded_compute_profile_missing")
    if pod.get("overhead") or any(
        item.get("restartPolicy") == "Always" for item in pod.get("initContainers", [])
    ):
        blockers.append("pod_sidecar_or_runtime_overhead_profile_missing")
    requests = _pod_requested_envelope(pod, replicas)
    if body["kind"] == "Pod" and body.get("metadata", {}).get("labels", {}).get(
        "app"
    ) in {"troshka-container", "troshka-pod"}:
        return NativeDemand(
            resource_id(body),
            "helper",
            *requests,
            blockers=()
            if all(v is not None for v in requests)
            else ("configured_pod_resources_unmapped",),
            requested_cpu_millicores=requests[0],
            requested_ram_bytes=requests[1],
            quota_workload=True,
            configured_cpu=_configured_pod_cpu(pod, requests),
        )
    return NativeDemand(
        resource_id(body),
        "helper",
        *quantities,
        blockers=tuple(sorted(set(blockers))),
        requested_cpu_millicores=requests[0],
        requested_ram_bytes=requests[1],
    )


def _configured_pod_cpu(pod, requests):
    from kubernetes.utils.quantity import parse_quantity

    if pod.get("initContainers") or requests[0] is None:
        return None
    return str(
        sum(
            parse_quantity(c["resources"]["requests"]["cpu"])
            for c in pod.get("containers", [])
        )
    )


def _pod_dimension(regular, init, dimension, replicas):
    if any(item[dimension] is None for item in regular + init):
        return None
    # Filter only after the unknown check; no absent quantity becomes zero.
    app_values = [item[dimension] for item in regular if item[dimension] is not None]
    init_values = [item[dimension] for item in init if item[dimension] is not None]
    return max(sum(app_values), max(init_values, default=0)) * replicas


def _pod_requested_envelope(pod, replicas):
    regular = [
        _container_envelope(item, "requests") for item in pod.get("containers", [])
    ]
    init = [
        _container_envelope(item, "requests") for item in pod.get("initContainers", [])
    ]
    return tuple(
        _pod_dimension(regular, init, dimension, replicas) for dimension in (0, 1)
    )


def _container_envelope(container, kind="limits"):
    limits = container.get("resources", {}).get(kind, {})
    # Requests alone do not bound actual concurrent helper compute consumption.
    return tuple(
        _quantity(limits[name], cpu=name == "cpu") if name in limits else None
        for name in ("cpu", "memory")
    )


def demand_for_manifest(body):
    identity = resource_id(body)
    if body["kind"] == "VirtualMachine":
        domain = body["spec"]["template"]["spec"]["domain"]
        cpu = domain["cpu"]
        cores = cpu.get("cores", 1) * cpu.get("sockets", 1) * cpu.get("threads", 1)
        return NativeDemand(
            identity,
            "guest",
            _quantity(cores, cpu=True),
            _quantity(domain["resources"]["requests"]["memory"]),
        )
    if body["kind"] == "PersistentVolumeClaim":
        return NativeDemand(
            identity,
            "storage",
            provisioned_bytes=_quantity(
                body["spec"]["resources"]["requests"]["storage"]
            ),
            blockers=(
                "storage_initialization_profile_missing",
                "pool_identity_unresolved",
            ),
        )
    return _pod_demand(body)


def _blocked(identity, classification, *reasons):
    return NativeDemand(identity, classification, blockers=tuple(reasons))


def _render_networks(body, topology, helpers):
    topo, k8s, *_ = helpers
    namespace = body["metadata"]["namespace"]
    networks = topo.extract_networks(topology)
    resources = []
    gateways = {}
    for network in networks:
        name = f"net-{network['id'][:8]}"
        cr = {
            "metadata": {"name": name, "namespace": namespace, "uid": "pending"},
            "apiVersion": "troshka.redhat.com/v1alpha1",
            "kind": "TroshkaNetwork",
            "spec": network,
        }
        resources.append(k8s.build_dnsmasq_deployment(cr))
        if network.get("externalAccess"):
            gateways[f"{name}-nad"] = {
                "ip": network.get("gateway", ""),
                "cidr": network.get("cidr", "10.0.0.0/24"),
            }
    if gateways:
        forwards = next(
            (
                node.get("data", {}).get("portForwards", [])
                for node in topology.get("nodes", [])
                if node.get("data", {}).get("subtype") == "gateway"
            ),
            [],
        )
        resources.append(
            k8s.build_gateway_deployment(
                body, list(gateways), gateways, port_forwards=forwards
            )
        )
    first = next(
        (
            network
            for network in networks
            if network.get("networkType", "standard") != "bmc"
        ),
        None,
    )
    if first:
        resources.append(
            k8s.build_exec_deployment(
                body,
                f"net-{first['id'][:8]}-nad",
                cidr=first.get("cidr", "10.0.0.0/24"),
            )
        )
    return resources


def _render_vm_disks(cr, namespace, k8s, kv):
    resources, blocked, mapping = [], [], {}
    for disk in cr["spec"]["disks"]:
        pvc = k8s.vm_disk_pvc_name(cr["metadata"]["name"], disk["id"])
        mapping[disk["id"]] = pvc
        if disk.get("patternImage") or disk.get("libraryImage"):
            blocked.extend(_import_demands(namespace, pvc))
        else:
            resources.append(kv.build_blank_pvc(pvc, namespace, disk["sizeGb"]))
    if cr["spec"].get("cdrom", {}).get("s3Path"):
        mapping["cdrom"] = f"{cr['metadata']['name']}-cdrom"
        blocked.extend(_import_demands(namespace, mapping["cdrom"]))
    return resources, blocked, mapping


def _vm_runtime_demands(vm, identity):
    blocked = [
        _blocked(
            f"{identity}/virt-launcher",
            "helper",
            "virt_launcher_overhead_profile_missing",
            "runtime_cpu_model_and_network_capabilities_unresolved",
        )
    ]
    if vm.get("firmware", "bios").startswith("uefi"):
        blocked.append(
            _blocked(
                f"{identity}/persistent-state",
                "storage",
                "controller_tpm_uefi_storage_profile_missing",
                "pool_identity_unresolved",
            )
        )
    for flag, reason in (
        ("bmcEnabled", "bmc_profile_and_runtime_network_missing"),
        ("guestfishCommands", "guestfish_transient_and_storage_growth_profile_missing"),
    ):
        if vm.get(flag):
            blocked.append(_blocked(f"{identity}/{flag}", "helper", reason))
    return blocked


def _render_vms(body, topology, helpers):
    topo, k8s, kv, *_ = helpers
    namespace = body["metadata"]["namespace"]
    disks, cdroms = topo.resolve_vm_disks(topology)
    nics = topo.resolve_nic_networks(topology)
    resources, blocked = [], []
    for vm in topo.extract_vms(topology):
        cr = topo.build_vm_cr(
            vm,
            disks,
            cdroms,
            nics,
            None,
            namespace,
            body["metadata"]["name"],
            body,
            topology,
        )
        disk_resources, disk_blocked, mapping = _render_vm_disks(cr, namespace, k8s, kv)
        resources.extend(disk_resources)
        blocked.extend(disk_blocked)
        resources.append(kv.build_kubevirt_vm(cr, mapping, {}, None))
        blocked.extend(_vm_runtime_demands(vm, resource_id(resources[-1])))
        if vm.get("recertEnabled") and any(
            disk.get("patternImage") for disk in cr["spec"]["disks"]
        ):
            root = mapping[cr["spec"]["disks"][0]["id"]]
            resources.append(
                kv.build_recert_job(
                    cr["metadata"]["name"], namespace, root, vm_name=vm["name"]
                )
            )
    return resources, blocked


def _import_demands(namespace, pvc):
    return [
        _blocked(
            f"DataVolume:{namespace}/{pvc}",
            "storage",
            "warm_clone_source_capacity_and_identity_unresolved",
            "full_copy_clone_profile_missing",
            "pool_identity_unresolved",
        ),
        _blocked(
            f"CDI:{namespace}/{pvc}",
            "helper",
            "cdi_transient_concurrency_profile_missing",
        ),
        _blocked(
            f"CacheImport:{namespace}/{pvc}",
            "platform",
            "cold_cache_requires_platform_reservation",
            "cache_identity_and_import_profile_missing",
        ),
    ]


def _render_containers(body, topology, helpers):
    topo, k8s, kv, containers, *_ = helpers
    namespace = body["metadata"]["namespace"]
    resources, disk_pvcs = [], {}
    for ctr_id, disk_id, size in topo.collect_container_disk_mounts(topology):
        name = topo.container_disk_pvc_name(ctr_id, disk_id)
        resources.append(kv.build_blank_pvc(name, namespace, size))
        disk_pvcs[disk_id] = name
    entries = topo.extract_containers(topology)
    topo.enrich_container_nics(topology, entries)
    topo.enrich_showroom_infra_networks(topology, entries)
    for entry in entries:
        build = (
            containers.build_pod_group
            if entry.get("isPod")
            else containers.build_single_container_pod
        )
        resources.append(build(namespace, entry, {}, k8s.owner_ref(body), disk_pvcs))
    return resources


def _render_ceph(body, topology, helpers):
    topo, *_, ceph = helpers
    spec = topo.extract_ceph_cluster(topology)
    if not spec:
        return [], []
    namespace = body["metadata"]["namespace"]
    if not spec.get("networkNad"):
        return [], [
            _blocked(
                f"Ceph:{namespace}/appliance",
                "helper",
                "ceph_network_reference_unresolved",
            )
        ]
    if (topology.get("projectCephCapture") or {}).get("restore"):
        return [], [
            _blocked(
                f"Ceph:{namespace}/restore-storage",
                "storage",
                "ceph_restore_source_identity_and_profile_missing",
                "pool_identity_unresolved",
            ),
            _blocked(
                f"Ceph:{namespace}/restore-jobs",
                "helper",
                "ceph_restore_transient_concurrency_profile_missing",
            ),
        ]
    cr = {
        **body,
        "kind": "TroshkaCeph",
        "metadata": {**body["metadata"], "name": "project-ceph"},
        "spec": spec,
    }
    resources = [ceph.build_mon_pvc(cr), *ceph.build_osd_pvcs(cr)]
    # Image selection is a runtime controller input. A placeholder image would
    # create a different final plan, so do not fabricate an appliance manifest.
    blocked = [
        _blocked(
            f"Ceph:{body['metadata']['namespace']}/appliance",
            "helper",
            "ceph_runtime_image_and_compute_profile_missing",
            "ceph_export_bootstrap_restore_jobs_unbounded",
        )
    ]
    return resources, blocked


def _native_namespace(db, project_id, host):
    from app.models.provider import Provider

    provider = db.get(Provider, host.provider_id) if host.provider_id else None
    config = provider.get_credentials() if provider is not None else None
    if not isinstance(config, dict):
        # Do not guess the default prefix when provider config is unavailable.
        return f"unresolved-{project_id[:8]}", [
            _blocked(
                "Runtime:provider-namespace",
                "helper",
                "provider_namespace_configuration_unresolved",
            )
        ]
    return f"{config.get('project_prefix', 'troshka-')}{project_id[:8]}", []


def _topology_blockers(topology, namespace):
    blocked = []
    if topology.get("clusters") or topology.get("ocp") or topology.get("workloads"):
        blocked.append(
            _blocked(
                f"Runtime:{namespace}/installer-ops",
                "helper",
                "installer_workload_runner_inputs_and_profile_missing",
            )
        )
    if any(
        node.get("data", {}).get("tpm") or node.get("data", {}).get("tpmEnabled")
        for node in topology.get("nodes", [])
    ):
        blocked.append(
            _blocked(
                f"Runtime:{namespace}/tpm",
                "storage",
                "tpm_not_supported_by_native_vm_renderer",
                "pool_identity_unresolved",
            )
        )
    supported_nodes = {
        "vmNode",
        "containerNode",
        "networkNode",
        "storageNode",
        "cephClusterNode",
        "ocpClusterNode",
    }
    for node in topology.get("nodes", []):
        if node.get("type") not in supported_nodes:
            blocked.append(
                _blocked(
                    f"Topology:{node['id']}", "helper", "native_node_renderer_unmapped"
                )
            )
    return blocked


def build_native_plan(db, project, host) -> NativeExecutionPlan:
    """Inspect retained deployment using actual builders, without provider I/O.

    db is intentionally not queried for native storage reports. Later verified
    execution-profile/pool inputs must be distinct from reporting tables.
    """
    if not host or host.host_type != "kubevirt-cluster":
        raise AccountingUnavailable("Native plan requires a native host")
    retained = getattr(project, "deployed_topology", None)
    topology = json.loads(
        _json(retained if retained is not None else project.topology or {})
    )
    ids = [node.get("id") for node in topology.get("nodes", [])]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise AccountingUnavailable("Invalid or duplicate native topology identity")
    helpers = _helpers()
    namespace, provider_blocked = _native_namespace(db, project.id, host)
    body = {
        "apiVersion": "troshka.redhat.com/v1alpha1",
        "kind": "TroshkaProject",
        "metadata": {
            "name": f"project-{project.id[:8]}",
            "namespace": namespace,
            "uid": "pending",
        },
        "spec": {"projectId": project.id},
    }
    resources = _render_networks(body, topology, helpers)
    vms, blocked = _render_vms(body, topology, helpers)
    # No topology/provider combination establishes tested execution capability
    # at this milestone, even an empty retained topology with bounded VNC caps.
    blocked.append(
        _blocked(
            f"Runtime:{namespace}/execution-capability",
            "helper",
            "native_execution_capability_profile_unresolved",
        )
    )
    blocked.extend(provider_blocked)
    resources.extend(vms)
    resources.extend(_render_containers(body, topology, helpers))
    resources.append(
        helpers[4].build_vnc_proxy_deployment(
            body["metadata"]["name"], namespace, owner_body=body
        )
    )
    ceph, ceph_blocked = _render_ceph(body, topology, helpers)
    resources.extend(ceph)
    blocked.extend(ceph_blocked)
    blocked.extend(_topology_blockers(topology, namespace))
    resources.sort(key=resource_id)
    demands = tuple(
        sorted(
            [*map(demand_for_manifest, resources), *blocked],
            key=lambda item: item.resource_id,
        )
    )
    if len({item.resource_id for item in demands}) != len(demands):
        raise AccountingUnavailable("Native generated resource identity collision")
    topology_digest = _hash(topology)
    payload = {
        "project_id": project.id,
        "host_id": host.id,
        "provider_id": host.provider_id,
        "topology_digest": topology_digest,
        "resources": resources,
        "demands": [item.__dict__ for item in demands],
    }
    return NativeExecutionPlan(
        project.id,
        host.id,
        str(uuid4()),
        _hash(payload),
        topology_digest,
        _json(resources),
        demands,
    )


def validate_native_plan(db, project, host, plan):
    """Reject mutated topology, render outputs or placement before dispatch.

    Does not authorize dispatch; Task 5 must persist/coordinate this generation.
    """
    current = build_native_plan(db, project, host)
    if current.execution_digest != plan.execution_digest:
        raise AccountingUnavailable("Native execution plan changed after reservation")
