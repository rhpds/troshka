"""Pure provisioned workload vectors, independent of backing-host billing.

VM RAM/storage sizes are GiB, container memory is MiB. The current CPU schema
stores whole vCPUs: fractional container CPUs are summed exactly then rounded UP
once per environment, never truncated. Pods, shared graph disks, unresolved image
virtual sizes and external/BMC power configurations deliberately fail closed.
"""

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from app.schemas.quota import AccountingUnavailable, Resources

GIB = 2**30
MIB = 2**20


def _quantity(value, unit=1) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value)) * unit
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise AccountingUnavailable("Invalid resource quantity") from exc


def _compute(node):
    data = node.get("data") or {}
    if node["type"] == "vmNode":
        if any(
            data.get(key)
            for key in (
                "deferred",
                "deferredWorker",
                "deferOcpInstall",
                "bmcIp",
                "bmc",
                "bmcEnabled",
            )
        ):
            raise AccountingUnavailable(
                "External compute power requires a guarded adapter"
            )
        return _quantity(data.get("vcpus", 2)), _quantity(data.get("ram", 4), GIB)
    if data.get("isPod") or data.get("podContainers") or data.get("initContainers"):
        raise AccountingUnavailable("Pod resource envelope is not mapped")
    return _quantity(data.get("cpus", 1)), _quantity(data.get("memory", 512), MIB)


def _storage(node, consumers, virtual_sizes):
    if not consumers:
        return Decimal(0)  # Unattached editor nodes do not allocate a disk.
    if len(consumers) != 1:
        raise AccountingUnavailable(
            "Shared graph disk requires a provider allocation map"
        )
    data = node.get("data") or {}
    size = _quantity(data.get("size", 10), GIB)
    if any(
        data.get(key)
        for key in (
            "libraryItemId",
            "imageId",
            "patternId",
            "patternDiskId",
            "snapshotItemId",
            "image",
        )
    ):
        if node["id"] not in virtual_sizes:
            raise AccountingUnavailable(
                "Image virtual capacity must be resolved before reservation"
            )
    if node["id"] in virtual_sizes:
        size = max(size, _quantity(virtual_sizes[node["id"]], GIB))
    return size


def topology_resources(
    topology: dict, *, state="active", virtual_sizes=None
) -> Resources:
    """Absolute target vector, never proof that existing infrastructure was freed."""
    if state in ("draft", "destroyed"):
        return Resources()
    if state not in ("active", "stopped"):
        raise AccountingUnavailable("Unsupported resource state")
    nodes = topology.get("nodes", [])
    by_id = {node["id"]: node for node in nodes}
    if len(by_id) != len(nodes):
        raise AccountingUnavailable("Duplicate topology identity")
    if any(
        node.get("type")
        not in ("vmNode", "containerNode", "storageNode", "networkNode")
        for node in nodes
    ):
        raise AccountingUnavailable("Unsupported topology resource mapping")
    consumers = {}
    for edge in topology.get("edges", []):
        for disk, target in (
            (edge.get("source"), edge.get("target")),
            (edge.get("target"), edge.get("source")),
        ):
            if by_id.get(disk, {}).get("type") == "storageNode" and by_id.get(
                target, {}
            ).get("type") in ("vmNode", "containerNode"):
                consumers.setdefault(disk, set()).add(target)
    for node in nodes:
        for mount in (node.get("data") or {}).get("mounts", []):
            disk = mount.get("diskNodeId")
            if disk not in by_id:
                raise AccountingUnavailable("Missing volume identity")
            consumers.setdefault(disk, set()).add(node["id"])
    cpu = ram = storage = Decimal(0)
    for node in nodes:
        if node["type"] in ("vmNode", "containerNode"):
            count, memory = _compute(node)
            cpu += count
            ram += memory
        elif node["type"] == "storageNode":
            storage += _storage(
                node, consumers.get(node["id"], set()), virtual_sizes or {}
            )
    return Resources(
        cpu=int(cpu.to_integral_value(rounding=ROUND_CEILING))
        if state == "active"
        else 0,
        ram_bytes=int(ram.to_integral_value(rounding=ROUND_CEILING))
        if state == "active"
        else 0,
        storage_bytes=int(storage.to_integral_value(rounding=ROUND_CEILING)),
        projects=1,
    )


def requested_resources(
    topology: dict,
    operation: str,
    *,
    current: Resources | None = None,
    virtual_sizes=None,
) -> Resources:
    """Positive growth only; cleanup vectors never perform a ledger release."""
    if operation in ("draft", "stop", "destroy"):
        return Resources()
    if operation not in ("deploy", "start", "reconfigure", "redeploy"):
        raise AccountingUnavailable("Unsupported resource operation")
    target = topology_resources(topology, virtual_sizes=virtual_sizes)
    if operation == "start":
        target = target.model_copy(update={"storage_bytes": 0, "projects": 0})
    previous = current or Resources()
    return Resources(
        **{
            name: max(0, getattr(target, name) - getattr(previous, name))
            for name in Resources.model_fields
        }
    )
