"""Unit tests for project host-CPU homogeneity helpers."""

import subprocess
import sys
from pathlib import Path

import pytest
from helpers.cpu_homogeneity import (
    cpu_model_node_selector,
    host_model_label,
    inventory_host_cpu_models,
    pick_host_cpu_model,
    project_needs_cpu_homogeneity,
    project_resource_request,
)


@pytest.mark.parametrize("existing_model", [None, "EPYC-Rome"])
def test_cpu_inheritance_handles_real_kopf_body_without_mutation(existing_model):
    # The operator test conftest replaces kopf with MagicMock. A fresh process
    # exercises the real read-only handler input and real decorator imports.
    script = """
import copy
import sys
from unittest.mock import patch
from kopf._cogs.structs.bodies import Body
from handlers.vm import _ensure_cpu_host_model

raw = {"metadata": {"name": "test-vm"}, "spec": {"nics": [{"name": "nic0"}]}}
if sys.argv[1] != "None":
    raw["spec"]["cpuHostModel"] = sys.argv[1]
before = copy.deepcopy(raw)
body = Body(raw)
with patch("handlers.vm.client.CustomObjectsApi") as api:
    api.return_value.list_namespaced_custom_object.return_value = {
        "items": [{"status": {"cpuHostModel": "EPYC-Milan"}}]
    }
    enriched = _ensure_cpu_host_model(body, "test-namespace")
assert enriched["spec"]["cpuHostModel"] == (sys.argv[1] if sys.argv[1] != "None" else "EPYC-Milan")
enriched["spec"]["nics"][0]["networkRef"] = "resolved"
assert raw == before
assert dict(body) == before
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(existing_model)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class _Meta:
    def __init__(self, labels):
        self.labels = labels


class _Status:
    def __init__(self, allocatable):
        self.allocatable = allocatable


class _Spec:
    def __init__(self, unschedulable=False):
        self.unschedulable = unschedulable


class _Node:
    def __init__(self, labels, allocatable, unschedulable=False):
        self.metadata = _Meta(labels)
        self.status = _Status(allocatable)
        self.spec = _Spec(unschedulable)


def test_project_needs_homogeneity_requires_kubevirt():
    assert project_needs_cpu_homogeneity({"placement": {"requires_kubevirt": True}})


def test_project_needs_homogeneity_nested_virt_vm():
    assert project_needs_cpu_homogeneity({}, [{"nestedVirt": True}])


def test_project_needs_homogeneity_topology_nested_virt():
    topo = {"nodes": [{"type": "vmNode", "data": {"nestedVirt": True}}]}
    assert project_needs_cpu_homogeneity(topo)


def test_project_needs_homogeneity_false_by_default():
    assert not project_needs_cpu_homogeneity(
        {"nodes": [{"type": "vmNode", "data": {"cpus": 2}}]},
        [{"cpus": 2}],
    )


def test_inventory_and_pick_prefers_model_with_capacity():
    nodes = [
        _Node(
            {"host-model-cpu.node.kubevirt.io/EPYC-Rome": "true"},
            {"cpu": "8", "memory": "16Gi"},
        ),
        _Node(
            {"host-model-cpu.node.kubevirt.io/EPYC-Milan": "true"},
            {"cpu": "32", "memory": "64Gi"},
        ),
        _Node(
            {"host-model-cpu.node.kubevirt.io/EPYC-Milan": "true"},
            {"cpu": "32", "memory": "64Gi"},
        ),
        _Node(
            {"host-model-cpu.node.kubevirt.io/EPYC-Rome": "true"},
            {"cpu": "8", "memory": "16Gi"},
            unschedulable=True,
        ),
    ]
    inv = inventory_host_cpu_models(nodes)
    assert inv["EPYC-Milan"]["nodes"] == 2
    assert inv["EPYC-Rome"]["nodes"] == 1
    # Large request fits Milan only
    model = pick_host_cpu_model(inv, min_cpu_m=40_000, min_memory_b=80 * 1024**3)
    assert model == "EPYC-Milan"


def test_pick_falls_back_to_largest_when_nothing_fits():
    inv = {
        "EPYC-Rome": {"cpu_m": 8000, "memory_b": 16 * 1024**3, "nodes": 1},
    }
    assert pick_host_cpu_model(inv, min_cpu_m=999_999) == "EPYC-Rome"


def test_cpu_model_node_selector():
    assert cpu_model_node_selector("EPYC-Milan") == {
        host_model_label("EPYC-Milan"): "true"
    }


def test_project_resource_request():
    cpu_m, mem_b = project_resource_request(
        [{"cpus": 4, "memory": 8192}, {"cpus": 2, "memory": 4096}]
    )
    assert cpu_m == 6000
    assert mem_b == (8192 + 4096) * 1024 * 1024
