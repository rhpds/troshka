from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from app.services.kubevirt_network_removal import validate_network_removal


def inventory():
    custom, core, apps = MagicMock(), MagicMock(), MagicMock()
    custom.get_namespaced_custom_object.return_value = {
        "metadata": {"uid": "network-uid"},
        "status": {"nadName": "net-abcdefgh-nad"},
    }
    custom.list_namespaced_custom_object.return_value = {"items": []}
    core.list_namespaced_pod.return_value = {"items": []}
    apps.list_namespaced_deployment.return_value = {"items": []}
    apps.list_namespaced_replica_set.return_value = {"items": []}
    return custom, core, apps


REMOVED = [{"id": "abcdefgh-1234", "data": {}}]


def deployment(name="bmc", owners=None):
    return {
        "metadata": {
            "name": name,
            "uid": "deployment-uid",
            "ownerReferences": owners or [],
        },
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"k8s.v1.cni.cncf.io/networks": "net-abcdefgh-nad"}
                }
            }
        },
    }


def test_unused_removal_and_repeat_are_read_only():
    clients = inventory()
    for _ in range(2):
        validate_network_removal(*clients, "ns", REMOVED)
    clients[0].delete_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize(
    "client_index,method",
    [
        (0, "get_namespaced_custom_object"),
        (0, "list_namespaced_custom_object"),
        (1, "list_namespaced_pod"),
        (2, "list_namespaced_deployment"),
        (2, "list_namespaced_replica_set"),
    ],
)
def test_every_inventory_read_fails_closed(client_index, method):
    clients = inventory()
    getattr(clients[client_index], method).side_effect = RuntimeError("private detail")
    with pytest.raises(ValueError, match="Unable to verify") as error:
        validate_network_removal(*clients, "ns", REMOVED)
    assert "private detail" not in str(error.value)


def test_incomplete_inventory_fails_closed():
    custom, core, apps = inventory()
    core.list_namespaced_pod.return_value = {
        "items": [],
        "metadata": {"continue": "next"},
    }
    with pytest.raises(ValueError, match="Unable to verify"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_missing_network_cannot_exempt_dns():
    from kubernetes.client.exceptions import ApiException

    custom, core, apps = inventory()
    custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    apps.list_namespaced_deployment.return_value = {
        "items": [
            deployment(
                "dnsmasq-net-abcdefgh",
                [{"uid": None, "kind": "TroshkaNetwork", "controller": True}],
            )
        ]
    }
    with pytest.raises(ValueError, match="prior Apply"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


@pytest.mark.parametrize("kind", ["helper", "vm", "vmi", "pod"])
def test_live_consumers_block_removal(kind):
    custom, core, apps = inventory()
    if kind == "helper":
        apps.list_namespaced_deployment.return_value = {"items": [deployment()]}
    elif kind == "pod":
        core.list_namespaced_pod.return_value = {
            "items": [deployment()["spec"]["template"]]
        }
    else:
        spec = {"networks": [{"multus": {"networkName": "ns/net-abcdefgh-nad"}}]}
        obj = {"spec": {"template": {"spec": spec}} if kind == "vm" else spec}
        custom.list_namespaced_custom_object.side_effect = lambda **kw: {
            "items": (
                [obj]
                if kw["plural"]
                == ("virtualmachines" if kind == "vm" else "virtualmachineinstances")
                else []
            )
        }
    with pytest.raises(ValueError, match="prior Apply"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_only_owned_dns_deployment_is_exempt():
    custom, core, apps = inventory()
    dep = deployment(
        "dnsmasq-net-abcdefgh",
        [{"uid": "network-uid", "kind": "TroshkaNetwork", "controller": True}],
    )
    apps.list_namespaced_deployment.return_value = {"items": [dep]}
    validate_network_removal(custom, core, apps, "ns", REMOVED)
    dep["metadata"]["ownerReferences"] = []
    with pytest.raises(ValueError):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_desired_vm_blocks_even_without_live_vm():
    custom, core, apps = inventory()
    custom.list_namespaced_custom_object.side_effect = lambda **kw: {
        "items": (
            [{"spec": {"nics": [{"networkRef": "net-abcdefgh"}]}}]
            if kw["plural"] == "troshkavms"
            else []
        )
    }
    with pytest.raises(ValueError, match="prior Apply"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_stale_nad_status_cannot_hide_canonical_consumer():
    custom, core, apps = inventory()
    custom.get_namespaced_custom_object.return_value["status"][
        "nadName"
    ] = "different-nad"
    apps.list_namespaced_deployment.return_value = {"items": [deployment()]}
    with pytest.raises(ValueError, match="Unable to verify"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_completed_rollout_history_allows_detached_network_removal():
    custom, core, apps = inventory()
    current = deployment()
    current["spec"]["template"]["metadata"]["annotations"] = {}
    old = deployment(
        "old-rs", [{"uid": "deployment-uid", "kind": "Deployment", "controller": True}]
    )
    old["metadata"]["uid"] = "old-rs-uid"
    old["spec"]["replicas"] = 0
    apps.list_namespaced_deployment.return_value = {"items": [current]}
    apps.list_namespaced_replica_set.return_value = {"items": [old]}
    validate_network_removal(custom, core, apps, "ns", REMOVED)
    old["spec"]["replicas"] = 1
    with pytest.raises(ValueError):
        validate_network_removal(custom, core, apps, "ns", REMOVED)
    old["spec"]["replicas"] = 0
    old["metadata"]["ownerReferences"] = []
    with pytest.raises(ValueError):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


@pytest.mark.parametrize(
    "annotation",
    [
        '[{"name":"net-abcdefgh-nad","namespace":""}]',
        '[{"name":"net-abcdefgh-nad","namespace":"ns"}]',
        "other, ns/net-abcdefgh-nad@net1",
    ],
)
def test_multus_attachment_spellings_block(annotation):
    custom, core, apps = inventory()
    dep = deployment()
    dep["spec"]["template"]["metadata"]["annotations"][
        "k8s.v1.cni.cncf.io/networks"
    ] = annotation
    apps.list_namespaced_deployment.return_value = {"items": [dep]}
    with pytest.raises(ValueError, match="prior Apply"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_dns_pod_requires_exact_owner_chain():
    custom, core, apps = inventory()
    dep = deployment(
        "dnsmasq-net-abcdefgh",
        [{"uid": "network-uid", "kind": "TroshkaNetwork", "controller": True}],
    )
    rs = deployment(
        "dns-rs", [{"uid": "deployment-uid", "kind": "Deployment", "controller": True}]
    )
    rs["metadata"]["uid"] = "rs-uid"
    pod = {
        "metadata": {
            "annotations": {"k8s.v1.cni.cncf.io/networks": "net-abcdefgh-nad"},
            "ownerReferences": [
                {"uid": "rs-uid", "kind": "ReplicaSet", "controller": True}
            ],
        }
    }
    apps.list_namespaced_deployment.return_value = {"items": [dep]}
    apps.list_namespaced_replica_set.return_value = {"items": [rs]}
    core.list_namespaced_pod.return_value = {"items": [pod]}
    validate_network_removal(custom, core, apps, "ns", REMOVED)
    pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    with pytest.raises(ValueError):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


def test_read_error_and_invalid_annotation_fail_closed_without_details():
    custom, core, apps = inventory()
    apps.list_namespaced_deployment.side_effect = RuntimeError("secret")
    with pytest.raises(ValueError, match="Unable to verify") as error:
        validate_network_removal(custom, core, apps, "ns", REMOVED)
    assert "secret" not in str(error.value)
    apps.list_namespaced_deployment.side_effect = None
    dep = deployment()
    dep["spec"]["template"]["metadata"]["annotations"][
        "k8s.v1.cni.cncf.io/networks"
    ] = "[secret"
    apps.list_namespaced_deployment.return_value = {"items": [dep]}
    with pytest.raises(ValueError, match="Unable to verify"):
        validate_network_removal(custom, core, apps, "ns", REMOVED)


@pytest.mark.parametrize("failure", ["network", "disk"])
def test_reconfigure_preflights_before_any_network_or_ceph_mutation(failure):
    from app.api.projects import _do_reconfigure_kubevirt

    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.side_effect = [
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]
    diff = {"removed_networks": [{"id": "unused00"}, *REMOVED]}
    custom, core, apps = inventory()
    apps.list_namespaced_deployment.return_value = {"items": [deployment()]}
    custom.get_namespaced_custom_object.side_effect = lambda **kw: {
        "metadata": {"uid": "network-uid"},
        "status": {"nadName": kw["name"] + "-nad"},
    }
    with ExitStack() as stack:
        patches = {
            "app.core.database.SessionLocal": {"return_value": session},
            "app.api.projects._resolve_kubevirt_reconfigure_disks": {
                "return_value": True
            },
            "app.services.deploy_topology.inject_showroom_gateway_port_forwards": {},
            "app.services.deploy_topology.diff_topologies": {"return_value": diff},
            "app.services.providers.kubevirt._get_k8s_clients": {
                "return_value": (custom, core, MagicMock())
            },
            "app.services.providers.kubevirt._project_ns": {"return_value": "ns"},
            "kubernetes.client.AppsV1Api": {"return_value": apps},
            "app.services.deploy_service._set_deploy_progress": {},
            "app.api.projects._get_kubevirt_project_cr": {"return_value": {}},
            "app.api.projects._find_changed_kubevirt_vms": {"return_value": []},
            "app.services.providers.kubevirt_capabilities.get_kubevirt_capabilities": {},
            "app.services.deploy_topology.validate_kubevirt_vm_disk_buses": {
                "return_value": "invalid disk" if failure == "disk" else None
            },
            "app.api.projects._kubevirt_reconfigure_exception": {},
            "app.api.projects._kubevirt_reconfigure_fail": {},
        }
        for target, kwargs in patches.items():
            stack.enter_context(patch(target, **kwargs))
        mutate = stack.enter_context(
            patch("app.api.projects._apply_kubevirt_network_and_ceph")
        )
        for _ in range(2):
            session.query.return_value.filter_by.return_value.first.side_effect = [
                MagicMock(),
                MagicMock(),
                MagicMock(),
            ]
            _do_reconfigure_kubevirt("p1", "h1", {"nodes": []}, {"nodes": []})
        mutate.assert_not_called()
        custom.delete_namespaced_custom_object.assert_not_called()
