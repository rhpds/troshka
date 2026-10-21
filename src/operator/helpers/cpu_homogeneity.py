"""Pin project VMs to one host CPU model for nested-virt / CCLM safety.

KubeVirt ``host-passthrough`` (nestedVirt) exposes the physical node's CPU.
On mixed EPYC clusters (Milan + Rome), VMs in one Troshka project can land on
different generations and nested live migration fails. When a project needs
homogeneity, pick one ``host-model-cpu.node.kubevirt.io/<model>`` label with
enough capacity and stamp it on every TroshkaVM.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client

logger = logging.getLogger(__name__)

HOST_MODEL_LABEL_PREFIX = "host-model-cpu.node.kubevirt.io/"


def project_needs_cpu_homogeneity(topology: dict, vms: list[dict] | None = None) -> bool:
    """True when nested virt / kubevirt OCP labs need a shared host CPU model."""
    placement = topology.get("placement") or {}
    if placement.get("requires_kubevirt") or placement.get("requiresKubevirt"):
        return True
    for vm in vms or []:
        if vm.get("nestedVirt"):
            return True
    for node in topology.get("nodes") or []:
        if node.get("type") != "vmNode":
            continue
        data = node.get("data") or {}
        if data.get("nestedVirt"):
            return True
    return False


def host_model_label(model: str) -> str:
    """KubeVirt node label key for a host-model CPU name."""
    return f"{HOST_MODEL_LABEL_PREFIX}{model}"


def cpu_model_node_selector(model: str) -> dict[str, str]:
    """Required nodeSelector pinning virt-launchers to one host CPU model."""
    return {host_model_label(model): "true"}


def inventory_host_cpu_models(nodes: list[Any]) -> dict[str, dict[str, int]]:
    """Map host-model name → aggregate allocatable cpu (milli) and memory (bytes).

    ``nodes`` are ``V1Node`` objects (or duck-typed with ``metadata`` / ``status``).
    Only schedulable workers with a host-model label are counted.
    """
    inventory: dict[str, dict[str, int]] = {}
    for node in nodes:
        meta = getattr(node, "metadata", None) or {}
        status = getattr(node, "status", None) or {}
        labels = getattr(meta, "labels", None) or {}
        if isinstance(meta, dict):
            labels = meta.get("labels") or {}
        if _node_unschedulable(node):
            continue
        models = [
            key[len(HOST_MODEL_LABEL_PREFIX) :]
            for key in labels
            if key.startswith(HOST_MODEL_LABEL_PREFIX)
        ]
        if not models:
            continue
        cpu_m, mem_b = _allocatable(status)
        for model in models:
            slot = inventory.setdefault(model, {"cpu_m": 0, "memory_b": 0, "nodes": 0})
            slot["cpu_m"] += cpu_m
            slot["memory_b"] += mem_b
            slot["nodes"] += 1
    return inventory


def pick_host_cpu_model(
    inventory: dict[str, dict[str, int]],
    *,
    min_cpu_m: int = 0,
    min_memory_b: int = 0,
) -> str | None:
    """Choose the best host-model for a project.

    Prefers models that meet the project's rough capacity request, then the
    most nodes (spread), then most CPU. Returns None if inventory is empty.
    """
    if not inventory:
        return None

    def _score(item: tuple[str, dict[str, int]]) -> tuple:
        model, stats = item
        fits = int(
            stats.get("cpu_m", 0) >= min_cpu_m
            and stats.get("memory_b", 0) >= min_memory_b
        )
        return (fits, stats.get("nodes", 0), stats.get("cpu_m", 0), model)

    ranked = sorted(inventory.items(), key=_score, reverse=True)
    best_model, best_stats = ranked[0]
    if (min_cpu_m or min_memory_b) and (
        best_stats.get("cpu_m", 0) < min_cpu_m
        or best_stats.get("memory_b", 0) < min_memory_b
    ):
        logger.warning(
            "No host CPU model meets project request "
            "(need cpu_m=%s memory_b=%s); best=%s stats=%s",
            min_cpu_m,
            min_memory_b,
            best_model,
            best_stats,
        )
    return best_model


def project_resource_request(vms: list[dict]) -> tuple[int, int]:
    """Rough project request: sum VM cpus (as milli) and memory (Mi → bytes)."""
    cpu_m = 0
    memory_b = 0
    for vm in vms:
        cpu_m += int(vm.get("cpus") or 0) * 1000
        memory_b += int(vm.get("memory") or 0) * 1024 * 1024
    return cpu_m, memory_b


def choose_project_cpu_host_model(
    core_api: client.CoreV1Api,
    topology: dict,
    vms: list[dict],
) -> str | None:
    """List nodes and pick a host CPU model when the project needs homogeneity."""
    if not project_needs_cpu_homogeneity(topology, vms):
        return None
    try:
        node_list = core_api.list_node()
        nodes = getattr(node_list, "items", None) or []
    except client.ApiException as e:
        logger.warning("Cannot list nodes for CPU homogeneity: %s", e)
        return None
    inventory = inventory_host_cpu_models(nodes)
    if not inventory:
        logger.warning(
            "No host-model-cpu.node.kubevirt.io/* labels on schedulable nodes; "
            "skipping CPU homogeneity pin"
        )
        return None
    min_cpu_m, min_memory_b = project_resource_request(vms)
    model = pick_host_cpu_model(
        inventory, min_cpu_m=min_cpu_m, min_memory_b=min_memory_b
    )
    if model:
        logger.info(
            "Selected host CPU model %s for project homogeneity "
            "(inventory=%s request_cpu_m=%s request_memory_b=%s)",
            model,
            {k: v.get("nodes") for k, v in inventory.items()},
            min_cpu_m,
            min_memory_b,
        )
    return model


def _node_unschedulable(node: Any) -> bool:
    spec = getattr(node, "spec", None)
    if spec is None and isinstance(node, dict):
        spec = node.get("spec") or {}
    if isinstance(spec, dict):
        return bool(spec.get("unschedulable"))
    return bool(getattr(spec, "unschedulable", False))


def _allocatable(status: Any) -> tuple[int, int]:
    alloc = getattr(status, "allocatable", None)
    if alloc is None and isinstance(status, dict):
        alloc = status.get("allocatable") or {}
    if alloc is None:
        return 0, 0
    if not isinstance(alloc, dict):
        # V1NodeStatus.allocatable is a dict-like
        try:
            alloc = dict(alloc)
        except Exception:
            return 0, 0
    return _parse_cpu_milli(alloc.get("cpu")), _parse_memory_bytes(alloc.get("memory"))


def _parse_cpu_milli(value: Any) -> int:
    if value is None:
        return 0
    s = str(value).strip()
    if s.endswith("m"):
        try:
            return int(s[:-1])
        except ValueError:
            return 0
    try:
        # cores (possibly fractional)
        return int(float(s) * 1000)
    except ValueError:
        return 0


def _parse_memory_bytes(value: Any) -> int:
    if value is None:
        return 0
    s = str(value).strip()
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }
    for suffix, mult in units.items():
        if s.endswith(suffix):
            try:
                return int(float(s[: -len(suffix)]) * mult)
            except ValueError:
                return 0
    try:
        return int(s)
    except ValueError:
        return 0
