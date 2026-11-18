"""Namespace cleanup consumes typed Kubernetes lists without item TypeMeta."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import native_deployment_origin as origin
from app.services import native_origin_inventory_transport as transport
from app.services import native_project_namespace_cleanup as cleanup
from app.services.native_namespace_client import OWN_KINDS, NamespaceClient


@pytest.fixture
def raw_client(monkeypatch):
    pages, calls = {}, []
    client = NamespaceClient(
        SimpleNamespace(
            default_headers={},
            configuration=SimpleNamespace(host="https://cluster.invalid"),
            update_params_for_auth=Mock(),
            rest_client=SimpleNamespace(pool_manager=object()),
        )
    )

    def read(pool, url, **kwargs):
        calls.append((url, kwargs))
        resource = url.rsplit("/", 1)[1]
        items = deepcopy(pages.get(resource, []))
        from app.services.native_managed_power import helpers

        kind, version = next(
            (k, v)
            for k, (v, plural) in (helpers().KINDS | helpers().CLEANUP_KINDS).items()
            if plural == resource
        )
        return (
            200,
            json.dumps(
                dict(
                    kind=kind + "List",
                    apiVersion=version,
                    metadata=dict(resourceVersion="42"),
                    items=items,
                )
            ).encode(),
        )

    monkeypatch.setattr(transport, "read", read)
    return client, pages, calls


def item(name="child", uid="child-uid"):
    return dict(metadata=dict(name=name, uid=uid, namespace="ns", resourceVersion="12"))


@pytest.mark.parametrize("budget", ["requests", "bytes"])
def test_new_observation_keeps_independent_finite_budgets(
    raw_client, monkeypatch, budget
):
    from app.services import native_origin_namespace_cleanup as inventory

    original, _, calls = raw_client
    original.rows("PersistentVolumeClaim", "ns")
    saved = original.deadline, original.requests, original.response_bytes
    clock = [original.deadline + 180]
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    fresh = original.new_observation()
    assert fresh.client is original.client and fresh is not original
    assert (
        fresh.deadline == clock[0] + 55 and fresh.requests == fresh.response_bytes == 0
    )
    fresh.rows("PersistentVolumeClaim", "ns")
    if budget == "requests":
        fresh.requests = inventory.MAX_REQUESTS
    else:
        fresh.response_bytes = 32 * 1024 * 1024
    with pytest.raises(
        origin.AccountingUnavailable, match="budget exceeded|aggregate response limit"
    ):
        fresh.rows("PersistentVolumeClaim", "ns")
    assert (original.deadline, original.requests, original.response_bytes) == saved
    with pytest.raises(origin.AccountingUnavailable, match="budget exceeded"):
        original.rows("PersistentVolumeClaim", "ns")


@pytest.mark.parametrize(
    "kind",
    [
        "PersistentVolumeClaim",
        "VolumeSnapshot",
        *OWN_KINDS,
        "VirtualMachine",
        "VirtualMachineInstance",
        "Pod",
    ],
)
def test_rows_normalize_requested_resource_identity(raw_client, kind):
    client, pages, calls = raw_client
    resource = client.resource(kind)
    pages[resource["resource"]] = [item()]
    rows = client.rows(kind, "ns")
    assert rows == [{**item(), "kind": kind, "apiVersion": resource["version"]}]
    assert (
        calls[0][0]
        == "https://cluster.invalid"
        + resource["path"]
        + "/namespaces/ns/"
        + resource["resource"]
    )
    assert calls[0][1]["fields"] == [("limit", "100")]
    assert calls[0][1]["retries"] is False


@pytest.mark.parametrize(
    "mutation", ["kind", "version", "namespace", "duplicate", "uid", "name"]
)
@pytest.mark.parametrize("consumer", ["rows", "required_inventory"])
def test_rows_reject_inconsistent_item_identity(raw_client, mutation, consumer):
    client, pages, _ = raw_client
    value = item()
    if mutation == "kind":
        value["kind"] = "Secret"
    elif mutation == "version":
        value["apiVersion"] = "other.invalid/v1"
    elif mutation == "namespace":
        value["metadata"]["namespace"] = "foreign"
    elif mutation in {"uid", "name"}:
        value["metadata"].pop(mutation)
    pages["persistentvolumeclaims"] = (
        [value, deepcopy(value)] if mutation == "duplicate" else [value]
    )
    with pytest.raises(origin.AccountingUnavailable):
        if consumer == "rows":
            client.rows("PersistentVolumeClaim", "ns")
        else:
            cleanup._required_inventory(client, "ns")


def test_required_inventory_and_target_consumers_accept_raw_lists(raw_client):
    client, pages, _ = raw_client
    for kind in ("PersistentVolumeClaim", "VolumeSnapshot", *OWN_KINDS):
        pages[client.resource(kind)["resource"]] = [item(kind.lower(), kind + "-uid")]
    rows = cleanup._required_inventory(client, "ns")
    assert {row["kind"] for row in rows} == {
        "PersistentVolumeClaim",
        "VolumeSnapshot",
        *OWN_KINDS,
    }
    pages["virtualmachines"] = [item("vm", "vm-uid")]
    work = SimpleNamespace(authority=SimpleNamespace(namespace="ns"))
    targets = cleanup._targets(client, work, ["VirtualMachine"])
    assert [(t.kind, t.name, t.uid, t.resource_version) for t in targets] == [
        ("VirtualMachine", "vm", "vm-uid", "12")
    ]
