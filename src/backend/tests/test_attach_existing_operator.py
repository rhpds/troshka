"""Registration verifies an existing installation without installing anything."""

import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from app.api.providers import router
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.host import Host
from app.models.provider import Provider
from app.services.providers import kubevirt
from tests.conftest import TestSession

IMAGE = "quay.io/redhat-gpte/troshka-operator:v2026.11.13-123456"


@pytest.fixture
def attach(monkeypatch):
    db = TestSession()
    provider = Provider(name=f"attach-{uuid.uuid4()}", type="kubevirt", state="active")
    provider.set_credentials(
        {
            "api_url": "https://api.test:6443",
            "token": "secret",
            "namespace": "custom-operator",
        }
    )
    db.add(provider)
    db.commit()
    user = NS(role="admin")
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_user] = lambda: user
    deployment = NS(
        metadata=NS(generation=4, deletion_timestamp=None, uid="deployment-uid"),
        spec=NS(
            replicas=1,
            selector=NS(
                match_labels={"app": "troshka-operator"}, match_expressions=None
            ),
            template=NS(spec=NS(containers=[NS(name="operator", image=IMAGE)])),
        ),
        status=NS(
            observed_generation=4,
            available_replicas=1,
            ready_replicas=1,
            updated_replicas=1,
            replicas=1,
            conditions=[NS(type="Available", status="True")],
        ),
    )
    pod = NS(
        metadata=NS(
            deletion_timestamp=None,
            owner_references=[
                NS(kind="ReplicaSet", name="operator-rs", controller=True)
            ],
        ),
        spec=NS(containers=[NS(name="operator", image=IMAGE)]),
        status=NS(
            phase="Running",
            container_statuses=[NS(name="operator", image=IMAGE, ready=True)],
        ),
    )
    apps = Mock(spec=["read_namespaced_deployment", "read_namespaced_replica_set"])
    apps.read_namespaced_deployment.return_value = deployment
    apps.read_namespaced_replica_set.return_value = NS(
        metadata=NS(
            owner_references=[
                NS(kind="Deployment", uid="deployment-uid", controller=True)
            ]
        )
    )
    core = Mock(spec=["list_namespaced_pod", "list_node"])
    core.list_namespaced_pod.return_value = NS(items=[pod])
    custom = Mock(spec=[])
    ext = Mock(spec=["read_custom_resource_definition"])
    ext.read_custom_resource_definition.return_value = NS(
        metadata=NS(deletion_timestamp=None),
        status=NS(conditions=[NS(type="Established", status="True")]),
    )
    monkeypatch.setattr(
        kubevirt, "_get_k8s_clients", Mock(return_value=(custom, core, "client"))
    )
    monkeypatch.setattr("kubernetes.client.AppsV1Api", Mock(return_value=apps))
    monkeypatch.setattr("kubernetes.client.ApiextensionsV1Api", Mock(return_value=ext))
    cpu = Mock(return_value=(32, 65536))
    storage = Mock(return_value=1024)
    eips = Mock(return_value=10)
    monkeypatch.setattr(kubevirt, "_query_cluster_capacity", cpu)
    monkeypatch.setattr(kubevirt, "_query_ceph_storage_gb", storage)
    monkeypatch.setattr(kubevirt, "_query_metallb_capacity", eips)
    install = Mock(side_effect=AssertionError("must not install"))
    monkeypatch.setattr(kubevirt, "_deploy_operator", install)
    monkeypatch.setattr(kubevirt.KubeVirtDriver, "provision_host", install)
    url = f"/providers/{provider.id}/attach-operator"
    yield NS(
        db=db,
        provider=provider,
        user=user,
        client=TestClient(api),
        url=url,
        deployment=deployment,
        pod=pod,
        apps=apps,
        core=core,
        ext=ext,
        cpu=cpu,
        storage=storage,
        eips=eips,
        install=install,
    )
    install.assert_not_called()
    db.rollback()
    db.query(Host).filter_by(provider_id=provider.id).delete()
    db.delete(provider)
    db.commit()
    db.close()


def post(a, image=IMAGE):
    return a.client.post(a.url, json={"expected_operator_image": image})


def test_attach_success_and_idempotent_refresh(attach):
    a = attach
    response = post(a)
    assert response.status_code == 200, response.text
    host = a.db.query(Host).filter_by(provider_id=a.provider.id).one()
    assert response.json() == {
        "host_id": host.id,
        "status": "connected",
        "verified_image": IMAGE,
    }
    assert (
        host.total_vcpus,
        host.total_ram_mb,
        host.storage_size_gb,
        host.max_eips,
    ) == (32, 65536, 1024, 10)
    assert host.host_type == host.instance_type == "kubevirt-cluster"
    host.accepting_work = False
    host.agent_token = "existing-owner-token"
    host.used_vcpus = 7
    a.db.commit()
    a.cpu.return_value = (40, 70000)
    assert post(a).json()["host_id"] == host.id
    assert a.db.query(Host).filter_by(provider_id=a.provider.id).count() == 1
    assert (
        host.accepting_work,
        host.agent_token,
        host.used_vcpus,
        host.total_vcpus,
    ) == (
        False,
        "existing-owner-token",
        7,
        40,
    )
    a.apps.read_namespaced_deployment.assert_called_with(
        name="troshka-operator", namespace="custom-operator", _request_timeout=10
    )
    assert {
        call.args[0] for call in a.ext.read_custom_resource_definition.call_args_list
    } == {
        f"{plural}.troshka.redhat.com"
        for plural in (
            "troshkaprojects",
            "troshkavms",
            "troshkanetworks",
            "troshkancephs",
        )
    }
    a.cpu.assert_called_with(a.core, strict=True)


@pytest.mark.parametrize(
    "image",
    [
        "image",
        "image:latest",
        "image:production",
        "image:main",
        "image@sha256:abc",
        "image:stable",
        "image:",
        "image:bad tag",
        "image:v1@sha256:abc",
    ],
)
def test_attach_requires_named_image(attach, image):
    assert post(attach, image).status_code == 422
    attach.apps.read_namespaced_deployment.assert_not_called()


def test_attach_admin_only(attach):
    attach.user.role = "operator"
    assert post(attach).status_code == 403


def test_attach_wrong_provider(attach):
    attach.provider.type = "ocpvirt"
    attach.db.commit()
    assert post(attach).status_code == 400


def test_attach_missing_provider(attach):
    attach.url = f"/providers/{uuid.uuid4()}/attach-operator"
    assert post(attach).status_code == 404


@pytest.mark.parametrize(
    "failure",
    [
        "absent",
        "unready",
        "stale",
        "image",
        "pod_image",
        "pod_unready",
        "wrong_owner",
        "missing_crd",
        "unestablished_crd",
        "cpu",
        "storage",
        "eips",
        "zero",
    ],
)
def test_attach_fails_before_host_mutation(attach, failure):
    a = attach
    if failure == "absent":
        a.apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    elif failure == "unready":
        a.deployment.status.ready_replicas = 0
    elif failure == "stale":
        a.deployment.status.observed_generation = 3
    elif failure == "image":
        a.deployment.spec.template.spec.containers[0].image = "image:wrong"
    elif failure == "pod_image":
        a.pod.status.container_statuses[0].image = "image:wrong"
    elif failure == "pod_unready":
        a.pod.status.container_statuses[0].ready = False
    elif failure == "wrong_owner":
        a.apps.read_namespaced_replica_set.return_value.metadata.owner_references[
            0
        ].uid = "someone-else"
    elif failure == "missing_crd":
        a.ext.read_custom_resource_definition.side_effect = ApiException(status=404)
    elif failure == "unestablished_crd":
        a.ext.read_custom_resource_definition.return_value.status.conditions[
            0
        ].status = "False"
    elif failure == "zero":
        a.cpu.return_value = (0, 0)
    else:
        getattr(a, failure).side_effect = RuntimeError(
            "secret credentials must not appear"
        )
    response = post(a)
    assert response.status_code == 503, response.text
    assert "secret" not in response.text
    assert a.db.query(Host).filter_by(provider_id=a.provider.id).count() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"host_type": "shared"},
        {"state": "provisioning"},
        {"instance_id": "other"},
        {"agent_status": "provisioning"},
    ],
)
def test_attach_rejects_conflicting_host(attach, change):
    values = dict(
        provider_id=attach.provider.id,
        host_type="kubevirt-cluster",
        instance_type="kubevirt-cluster",
        state="active",
        agent_status="connected",
        instance_id="https://api.test:6443",
        total_vcpus=5,
    )
    values.update(change)
    host = Host(**values)
    attach.db.add(host)
    attach.db.commit()
    assert post(attach).status_code == 409
    assert host.total_vcpus == 5


def test_attach_discovery_failure_preserves_existing_host(attach):
    assert post(attach).status_code == 200
    host = attach.db.query(Host).filter_by(provider_id=attach.provider.id).one()
    host.agent_status = "disconnected"
    attach.db.commit()
    attach.cpu.return_value = (99, 99000)
    attach.storage.side_effect = RuntimeError("unavailable")
    assert post(attach).status_code == 503
    attach.db.refresh(host)
    assert (host.total_vcpus, host.agent_status) == (32, "disconnected")


def test_attach_rejects_duplicate_hosts(attach):
    assert post(attach).status_code == 200
    attach.db.add(Host(provider_id=attach.provider.id))
    attach.db.commit()
    assert post(attach).status_code == 409


def test_attached_provider_cannot_delete_shared_operator(attach):
    assert post(attach).status_code == 200
    response = attach.client.delete(f"/providers/{attach.provider.id}")
    assert response.status_code == 409
    assert attach.db.get(Provider, attach.provider.id) is not None


@pytest.mark.parametrize(
    "helper,client",
    [
        ("_query_cluster_capacity", "list_node"),
        ("_query_metallb_capacity", "list_cluster_custom_object"),
    ],
)
def test_strict_capacity_propagates_api_errors(helper, client):
    api = Mock()
    getattr(api, client).side_effect = RuntimeError("denied")
    with pytest.raises(RuntimeError, match="denied"):
        getattr(kubevirt, helper)(api, strict=True)


def test_strict_ceph_missing_toolbox_is_failure():
    api = Mock()
    api.list_namespaced_pod.return_value = NS(items=[])
    with pytest.raises(ValueError, match="unavailable"):
        kubevirt._query_ceph_storage_gb(api, strict=True)
