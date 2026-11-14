"""Exercise the actual bounded PowerClient through its HTTP response boundary."""

import json
from types import SimpleNamespace

import pytest
from helpers.managed_power import ManagedPowerError, PowerClient


def transport(payload):
    calls = []
    closed = []
    response = SimpleNamespace(
        status=200,
        read=lambda limit: json.dumps(payload).encode(),
        close=lambda: closed.append(True),
    )

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    client = SimpleNamespace(
        default_headers={},
        configuration=SimpleNamespace(host="https://synthetic.invalid"),
        update_params_for_auth=lambda headers, query, methods: None,
        rest_client=SimpleNamespace(pool_manager=SimpleNamespace(request=request)),
    )
    return PowerClient(client), calls, closed


@pytest.mark.parametrize(
    ("kind", "namespace", "returned_namespace", "accepted", "path"),
    [
        ("PersistentVolume", None, None, True, "/api/v1/persistentvolumes"),
        ("Namespace", None, None, True, "/api/v1/namespaces"),
        (
            "PersistentVolumeClaim",
            "original",
            "original",
            True,
            "/api/v1/namespaces/original/persistentvolumeclaims",
        ),
        ("PersistentVolume", None, "unexpected", False, "/api/v1/persistentvolumes"),
        ("Namespace", None, "unexpected", False, "/api/v1/namespaces"),
        (
            "PersistentVolumeClaim",
            "original",
            None,
            False,
            "/api/v1/namespaces/original/persistentvolumeclaims",
        ),
        (
            "PersistentVolumeClaim",
            "original",
            "replacement",
            False,
            "/api/v1/namespaces/original/persistentvolumeclaims",
        ),
    ],
)
def test_real_power_client_list_scope(
    kind, namespace, returned_namespace, accepted, path
):
    metadata = {"name": "original", "uid": "original-uid"}
    if returned_namespace is not None:
        metadata["namespace"] = returned_namespace
    api, calls, closed = transport({"metadata": {}, "items": [{"metadata": metadata}]})
    if accepted:
        rows = api.rows(kind, namespace)
        assert rows[0]["metadata"] == metadata
        assert rows[0]["kind"] == kind and rows[0]["apiVersion"] == "v1"
    else:
        with pytest.raises(
            ManagedPowerError, match="managed_inventory_namespace_changed"
        ):
            api.rows(kind, namespace)
    assert len(calls) == 1 and closed == [True]
    method, url, options = calls[0]
    assert method == "GET" and url == "https://synthetic.invalid" + path
    assert options["fields"] == [("limit", "1001")]
    assert options["retries"] is False and options["redirect"] is False


@pytest.mark.parametrize(
    ("kind", "namespace"),
    [
        ("PersistentVolumeClaim", None),
        ("Pod", ""),
        ("PersistentVolume", "original"),
        ("Namespace", "original"),
    ],
)
def test_real_power_client_rejects_wrong_requested_scope_before_transport(
    kind, namespace
):
    api, calls, closed = transport({"metadata": {}, "items": []})
    with pytest.raises(ManagedPowerError, match="managed_inventory_scope_invalid"):
        api.rows(kind, namespace)
    assert calls == [] and closed == []


@pytest.mark.parametrize("condition", ["paginated", "duplicate_uid"])
def test_real_power_client_cluster_lists_remain_complete_and_unique(condition):
    value = {"metadata": {"name": "original", "uid": "original-uid"}}
    payload = {"metadata": {}, "items": [value]}
    if condition == "paginated":
        payload["metadata"]["continue"] = "unread-page"
    else:
        payload["items"].append(value)
    api, _, closed = transport(payload)
    with pytest.raises(ManagedPowerError):
        api.rows("PersistentVolume", None)
    assert closed == [True]
