"""Native demand is derived from deployment builders and never enables admission."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.schemas.quota import AccountingUnavailable


def test_configured_container_requests_are_tenant_demand_without_limits():
    from app.services.quota_native import _helpers, demand_for_manifest

    containers = _helpers()[3]
    body = containers.build_single_container_pod(
        "ns", {"id": "container-one", "image": "example/image"}, {}, {}, {}
    )
    demand = demand_for_manifest(body)
    assert demand.tenant_compute
    assert demand.cpu_millicores == 1000
    assert demand.ram_bytes == 512 * 2**20
    assert not demand.blockers
    assert "limits" not in body["spec"]["containers"][0]["resources"]


def test_fractional_configured_cpu_sums_before_millicore_or_environment_rounding(
    native_target,
):
    from app.services.quota_native import build_native_plan

    project, host = native_target
    project.topology = {
        "nodes": [
            {
                "id": "first-container",
                "type": "containerNode",
                "data": {"image": "example/tenant", "cpus": 0.5001},
            },
            {
                "id": "second-container",
                "type": "containerNode",
                "data": {"image": "example/tenant", "cpus": 0.4998},
            },
        ]
    }
    plan = build_native_plan(Mock(), project, host)
    assert plan.cpu == 1
    assert plan.tenant_complete
    assert any(not v.tenant_compute for v in plan.demands)
    assert not plan.complete  # First deployment/initial storage is a separate profile.


@pytest.fixture
def native_target():
    topology = {
        "nodes": [
            {"id": "vm-basic", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
            {
                "id": "vm-uefi",
                "type": "vmNode",
                "data": {"firmware": "uefi", "tpmEnabled": True, "recertEnabled": True},
            },
            {"id": "disk-one", "type": "storageNode", "data": {"size": 20}},
            {"id": "pod-disk", "type": "storageNode", "data": {"size": 3}},
            {
                "id": "network-one",
                "type": "networkNode",
                "data": {"cidr": "10.1.0.0/24", "externalAccess": True},
            },
            {
                "id": "showroom",
                "type": "containerNode",
                "data": {
                    "isShowroom": True,
                    "cpus": 0.25,
                    "memory": 128,
                    "image": "example/showroom",
                },
            },
            {
                "id": "pod-one",
                "type": "containerNode",
                "data": {
                    "isPod": True,
                    "podContainers": [
                        {
                            "image": "example/pod",
                            "mounts": [
                                {"diskNodeId": "pod-disk", "mountPath": "/data"}
                            ],
                        }
                    ],
                },
            },
            {
                "id": "ceph-one",
                "type": "cephClusterNode",
                "data": {"networkRef": "network-one", "osdCount": 2, "capacityGi": 100},
            },
        ],
        "edges": [{"source": "disk-one", "target": "vm-basic"}],
        "clusters": [{"id": "ocp-one", "type": "ocp"}],
    }
    project = SimpleNamespace(
        id="project-uuid",
        topology=topology,
        deployed_topology=None,
        provider_id="provider",
        host_id="native-host",
    )
    host = SimpleNamespace(
        id="native-host", host_type="kubevirt-cluster", provider_id="provider"
    )
    return project, host


def test_all_rendered_workloads_have_classified_demand(native_target):
    from app.services.quota_native import build_native_plan, resource_id

    project, host = native_target
    plan = build_native_plan(Mock(), project, host)
    rendered_ids = {resource_id(body) for body in plan.rendered_resources}
    assert rendered_ids == set(plan.generated_resource_ids)
    assert rendered_ids <= set(plan.demand_ids)
    assert not plan.unclassified_resources
    assert plan.cpu >= plan.guest_cpu == 4
    assert any("showroom" in item.resource_id for item in plan.demands)
    assert any("ceph" in item.resource_id for item in plan.demands)
    assert any("persistent-state" in item.resource_id for item in plan.demands)
    assert any("installer" in item.resource_id for item in plan.demands)
    assert not plan.complete
    assert plan.unsupported_reasons


def test_native_plan_retains_topology_and_rejects_changed_dispatch(native_target):
    from app.services.quota_native import build_native_plan, validate_native_plan

    project, host = native_target
    project.deployed_topology = project.topology
    project.topology = {"nodes": []}
    plan = build_native_plan(Mock(), project, host)
    assert plan.guest_cpu == 4
    validate_native_plan(Mock(), project, host, plan)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        plan.generation = "changed"
    plan.rendered_resources[0]["metadata"]["name"] = "changed-copy"
    validate_native_plan(Mock(), project, host, plan)
    project.deployed_topology["nodes"][0]["data"]["vcpus"] = 3
    with pytest.raises(AccountingUnavailable, match="changed"):
        validate_native_plan(Mock(), project, host, plan)


def test_missing_profiles_never_become_zero_storage(native_target):
    from app.services.quota_native import build_native_plan
    from app.services.quota_resources import native_resources

    plan = build_native_plan(Mock(), *native_target)
    storage = [item for item in plan.demands if item.provisioned_bytes is not None]
    assert storage
    assert all(item.initial_storage_bytes is None for item in storage)
    assert all(item.pool_key is None for item in storage)
    assert any(
        "storage_initialization_profile_missing" in reason
        for reason in plan.unsupported_reasons
    )
    assert any(
        "pool_identity_unresolved" in reason for reason in plan.unsupported_reasons
    )
    with pytest.raises(AccountingUnavailable):
        native_resources(plan)


def test_adapter_selection_distinguishes_retained_native(native_target):
    from app.services.quota_operations import select_adapter

    db = Mock()
    project, host = native_target
    assert select_adapter(db, project, host=host) == "kubevirt"
    host.host_type = "shared"
    db.get.return_value = SimpleNamespace(type="ocpvirt")
    assert select_adapter(db, project, host=host) == "nested"


def test_helpers_remain_inventory_but_do_not_count_as_tenant_compute():
    from app.services.quota_native import NativeDemand, NativeExecutionPlan
    from app.services.quota_resources import native_resources

    plan = NativeExecutionPlan(
        project_id="p",
        host_id="h",
        generation="generation",
        execution_digest="digest",
        topology_digest="topology",
        rendered_json="[]",
        demands=(
            NativeDemand("vm", "guest", 2000, 2**30),
            NativeDemand("helper-a", "helper", 100, 100),
            NativeDemand("helper-b", "helper", 250, 200),
        ),
    )
    assert plan.helper_millicores == 350
    assert native_resources(plan).cpu == 2
    assert native_resources(plan).ram_bytes == 2**30


def test_pool_aliases_require_consistent_logical_units():
    from app.services.quota_native import resolve_pool_alias

    aliases = {
        "class-a": [("fsid", 7, "rbd_allocated_extent_bytes")],
        "class-b": [("fsid", 7, "rbd_allocated_extent_bytes")],
    }
    assert (
        resolve_pool_alias("class-a", aliases)
        == resolve_pool_alias("class-b", aliases)
        == ("fsid", 7)
    )
    for candidates in (
        [("fsid", 7, "raw_physical_bytes")],
        [
            ("fsid", 7, "rbd_allocated_extent_bytes"),
            ("fsid", 8, "rbd_allocated_extent_bytes"),
        ],
        [],
    ):
        with pytest.raises(AccountingUnavailable):
            resolve_pool_alias("class-a", {"class-a": candidates})


def test_import_paths_block_warm_and_cold_capacity_without_fabricating_blank(
    native_target,
):
    from app.services.quota_native import build_native_plan

    project, host = native_target
    project.topology["nodes"][2]["data"].update(
        source="library",
        libraryItemId="image",
        resolvedS3Path="verified/source.qcow2",
        sourceSizeGb=30,
    )
    plan = build_native_plan(Mock(), project, host)
    assert not any(
        body["kind"] == "PersistentVolumeClaim"
        and "disk-one" in body["metadata"]["name"]
        for body in plan.rendered_resources
    )
    assert any(
        "warm_clone_source_capacity" in reason for reason in plan.unsupported_reasons
    )
    assert any(
        "full_copy_clone_profile_missing" in reason
        for reason in plan.unsupported_reasons
    )
    assert any(
        "cold_cache_requires_platform_reservation" in reason
        for reason in plan.unsupported_reasons
    )


def test_plan_repr_does_not_disclose_workload_credentials(native_target):
    from app.services.quota_native import build_native_plan

    project, host = native_target
    project.topology["nodes"][5]["data"]["env"] = {
        "PASSWORD": "sample-credential-never-display"
    }
    plan = build_native_plan(Mock(), project, host)
    assert "sample-credential-never-display" not in repr(plan)


def test_duplicate_topology_identity_never_silently_changes_inventory(native_target):
    from app.services.quota_native import build_native_plan

    project, host = native_target
    project.topology["nodes"].append(
        {"id": "vm-basic", "type": "vmNode", "data": {"vcpus": 10}}
    )
    with pytest.raises(AccountingUnavailable, match="identity"):
        build_native_plan(Mock(), project, host)


def test_rendered_init_peak_and_concurrent_workloads_use_exact_caps():
    from app.services.quota_native import demand_for_manifest

    def container(cpu, memory):
        return {"resources": {"limits": {"cpu": cpu, "memory": memory}}}

    pod = {
        "kind": "Pod",
        "metadata": {"namespace": "n", "name": "p"},
        "spec": {
            "containers": [container("100m", "100Mi"), container("250m", "100Mi")],
            "initContainers": [container("50m", "1Gi"), container("500m", "128Mi")],
        },
    }
    demand = demand_for_manifest(pod)
    assert demand.cpu_millicores == 500
    assert demand.ram_bytes == 2**30
    pod["spec"]["initContainers"][0]["restartPolicy"] = "Always"
    assert demand_for_manifest(pod).blockers
    pod["spec"]["containers"][0]["resources"] = {
        "requests": {"cpu": "1", "memory": "1Gi"}
    }
    assert demand_for_manifest(pod).cpu_millicores is None


def test_plan_rejects_changed_host_and_retains_explicit_empty_topology(native_target):
    from app.services.quota_native import build_native_plan, validate_native_plan

    project, host = native_target
    project.deployed_topology = {}
    plan = build_native_plan(Mock(), project, host)
    assert plan.guest_cpu == 0
    host.provider_id = "different-provider"
    with pytest.raises(AccountingUnavailable, match="changed"):
        validate_native_plan(Mock(), project, host, plan)


def test_namespace_uses_authoritative_provider_configuration(native_target):
    from app.services.quota_native import build_native_plan

    db = Mock()
    db.get.return_value = SimpleNamespace(
        get_credentials=lambda: {"project_prefix": "custom-"}
    )
    plan = build_native_plan(db, *native_target)
    assert {body["metadata"]["namespace"] for body in plan.rendered_resources} == {
        "custom-project-"
    }


def test_plan_preserves_declared_fractional_helper_requests(native_target):
    from app.services.quota_native import build_native_plan

    plan = build_native_plan(Mock(), *native_target)
    showroom = next(item for item in plan.demands if "ctr-showroom" in item.resource_id)
    assert showroom.requested_cpu_millicores == 250
    assert showroom.requested_ram_bytes == 128 * 2**20
    assert showroom.cpu_millicores == 250
    assert showroom.tenant_compute  # Configured requests are the selected quota basis.
    assert not plan.complete  # Unsupported lifecycle/import profiles remain blocked.


def test_ceph_restore_does_not_render_blank_appliance_claims(native_target):
    from app.services.quota_native import build_native_plan

    project, host = native_target
    project.topology["projectCephCapture"] = {"restore": {"mon": {"source": "runtime"}}}
    plan = build_native_plan(Mock(), project, host)
    assert not any(
        body["kind"] == "PersistentVolumeClaim" and "ceph" in body["metadata"]["name"]
        for body in plan.rendered_resources
    )
    assert any("ceph_restore" in reason for reason in plan.unsupported_reasons)


def test_deployment_container_wrappers_match_planned_manifests(
    native_target, monkeypatch
):
    from app.services.quota_native import _helpers, build_native_plan

    project, host = native_target
    plan = build_native_plan(Mock(), project, host)
    topo, k8s, *_ = _helpers()
    from handlers import container

    core_api = Mock()
    monkeypatch.setattr(container.client, "CoreV1Api", lambda: core_api)
    namespace = plan.rendered_resources[0]["metadata"]["namespace"]
    body = {
        "apiVersion": "troshka.redhat.com/v1alpha1",
        "kind": "TroshkaProject",
        "metadata": {
            "name": "project-project-",
            "namespace": namespace,
            "uid": "pending",
        },
    }
    disk_pvcs = {
        disk_id: topo.container_disk_pvc_name(ctr_id, disk_id)
        for ctr_id, disk_id, _ in topo.collect_container_disk_mounts(project.topology)
    }
    entries = topo.extract_containers(project.topology)
    topo.enrich_container_nics(project.topology, entries)
    topo.enrich_showroom_infra_networks(project.topology, entries)
    from helpers import managed_power

    api = Mock(get=Mock(return_value=body))
    token = managed_power._context.set((body, namespace, api))
    try:
        container.create_container_pods(
            namespace, entries, {}, k8s.owner_ref(body), disk_pvcs
        )
    finally:
        managed_power._context.reset(token)
    actual = [
        call.kwargs["body"] for call in core_api.create_namespaced_pod.call_args_list
    ]
    expected = [
        manifest for manifest in plan.rendered_resources if manifest["kind"] == "Pod"
    ]
    assert sorted(actual, key=lambda item: item["metadata"]["name"]) == sorted(
        expected, key=lambda item: item["metadata"]["name"]
    )


def test_native_only_first_deploy_is_denied_before_host_selection(native_target):
    from app.services.native_managed_power import prepare
    from app.services.quota_operations import select_adapter

    project, _ = native_target
    project.provider_id = None
    project.host_id = None
    project.topology["placement"] = {"requires_kubevirt": True}
    assert select_adapter(Mock(), project) == "kubevirt"
    with pytest.raises(AccountingUnavailable, match="first-deploy.*unavailable"):
        prepare(Mock(), project, "deploy")


def test_aggregate_overflow_is_accounting_unavailable():
    from app.services.quota_native import NativeDemand, NativeExecutionPlan
    from app.services.quota_resources import native_resources

    plan = NativeExecutionPlan(
        "p",
        "h",
        "g",
        "d",
        "t",
        "[]",
        (
            NativeDemand("a", "guest", 1, 2**63 - 1),
            NativeDemand("b", "guest", 1, 2**63 - 1),
        ),
    )
    with pytest.raises(AccountingUnavailable):
        native_resources(plan)


@pytest.mark.parametrize(
    "cpu", ["NaN", "Infinity", "-1", "invalid", "9223372036854775808"]
)
def test_malformed_native_quantities_fail_closed(cpu):
    from app.services.quota_native import demand_for_manifest

    pod = {
        "kind": "Pod",
        "metadata": {"namespace": "n", "name": "p"},
        "spec": {
            "containers": [{"resources": {"limits": {"cpu": cpu, "memory": "1Mi"}}}],
        },
    }
    with pytest.raises(AccountingUnavailable):
        demand_for_manifest(pod)


@pytest.mark.parametrize(
    "candidates", [[("fsid", 7)], None, [(["fsid"], 7, "rbd_allocated_extent_bytes")]]
)
def test_malformed_pool_alias_fails_closed(candidates):
    from app.services.quota_native import resolve_pool_alias

    with pytest.raises(AccountingUnavailable):
        resolve_pool_alias("class-a", {"class-a": candidates})


def test_pool_initialization_aggregate_overflow_is_unavailable():
    from app.services.quota_native import NativeDemand, NativeExecutionPlan

    demands = tuple(
        NativeDemand(
            name, "storage", initial_storage_bytes=2**63 - 1, pool_key=("fsid", 7)
        )
        for name in ("a", "b")
    )
    plan = NativeExecutionPlan("p", "h", "g", "d", "t", "[]", demands)
    with pytest.raises(AccountingUnavailable):
        _ = plan.pool_initialization_demands


def test_valid_provider_empty_retained_plan_is_universally_blocked(native_target):
    from app.services.quota_native import build_native_plan
    from app.services.quota_resources import native_resources

    project, host = native_target
    project.deployed_topology = {}
    db = Mock()
    db.get.return_value = SimpleNamespace(get_credentials=lambda: {})
    plan = build_native_plan(db, project, host)
    assert not any(
        "provider_namespace_configuration_unresolved" in reason
        for reason in plan.unsupported_reasons
    )
    assert any(
        "native_execution_capability_profile_unresolved" in reason
        for reason in plan.unsupported_reasons
    )
    assert not plan.complete
    with pytest.raises(AccountingUnavailable):
        native_resources(plan)


def _bounded_controller(kind, **settings):
    resources = {"cpu": "1", "memory": "1Gi"}
    return {
        "kind": kind,
        "metadata": {"namespace": "n", "name": "workload"},
        "spec": {
            **settings,
            "template": {
                "spec": {
                    "containers": [
                        {"resources": {"limits": resources, "requests": resources}}
                    ]
                }
            },
        },
    }


def test_deployment_surge_is_blocked_and_replicas_are_only_known_lower_bound():
    from app.services.quota_native import demand_for_manifest

    deployment = _bounded_controller(
        "Deployment",
        replicas=2,
        strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}},
    )
    demand = demand_for_manifest(deployment)
    assert demand.cpu_millicores == demand.requested_cpu_millicores == 2000
    assert demand.ram_bytes == demand.requested_ram_bytes == 2 * 2**30
    assert "controller_transient_concurrency_unresolved" in demand.blockers


@pytest.mark.parametrize(
    "settings,known_count",
    [
        ({"parallelism": 3}, 3),
        ({"parallelism": 3, "completions": 1}, 1),
        ({"parallelism": 3, "suspend": True}, 0),
    ],
)
def test_job_parallelism_is_known_lower_bound_with_controller_blocker(
    settings, known_count
):
    from app.services.quota_native import demand_for_manifest

    demand = demand_for_manifest(_bounded_controller("Job", **settings))
    assert (
        demand.cpu_millicores == demand.requested_cpu_millicores == known_count * 1000
    )
    assert demand.ram_bytes == demand.requested_ram_bytes == known_count * 2**30
    assert "controller_transient_concurrency_unresolved" in demand.blockers


@pytest.mark.parametrize(
    "kind,field",
    [("Deployment", "replicas"), ("Job", "parallelism"), ("Job", "completions")],
)
@pytest.mark.parametrize("invalid", [-1, True, "2", 1.5, None, 2**63])
def test_invalid_controller_counts_fail_closed(kind, field, invalid):
    from app.services.quota_native import demand_for_manifest

    manifest = _bounded_controller(kind, **{field: invalid})
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "limits": {"cpu": "1m", "memory": "1"},
    }
    with pytest.raises(AccountingUnavailable):
        demand_for_manifest(manifest)


def test_actual_vnc_deployment_retains_controller_concurrency_blocker(native_target):
    from app.services.quota_native import build_native_plan

    db = Mock()
    db.get.return_value = SimpleNamespace(get_credentials=lambda: {})
    plan = build_native_plan(db, *native_target)
    vnc = next(
        item
        for item in plan.demands
        if "Deployment:" in item.resource_id and "/vnc-proxy-" in item.resource_id
    )
    assert vnc.cpu_millicores == 500
    assert vnc.ram_bytes == 256 * 2**20
    assert "controller_transient_concurrency_unresolved" in vnc.blockers
