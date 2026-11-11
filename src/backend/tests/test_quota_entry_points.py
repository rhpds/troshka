"""Admission at real API/service boundaries; only infrastructure is faked."""

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.host import Host
from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaPolicy, QuotaReadiness, QuotaReservation
from app.models.user import User
from app.services import quota_accounting
from tests import conftest


@pytest.fixture
def environment(monkeypatch, isolated_quota_database):
    monkeypatch.setattr(quota_accounting, "quotas_enabled", lambda: True)
    db = conftest.TestSession()
    existing = db.get(QuotaReadiness, "baseline")
    readiness_before = (
        {
            "ready": existing.ready,
            "details": existing.details,
            "checked_at": existing.checked_at,
            "baseline_version": existing.baseline_version,
        }
        if existing
        else None
    )
    provider = Provider(name=str(uuid4()), type="ocpvirt")
    db.add(provider)
    db.flush()
    org = ManagedGroup(
        name=str(uuid4()), normalized_name=str(uuid4()), is_organization=True
    )
    db.add(org)
    db.flush()
    owner = User(email=f"{uuid4()}@test.invalid", organization_id=org.id)
    db.add(owner)
    db.flush()
    host = Host(
        ip_address="192.0.2.1",
        host_type="shared",
        state="active",
        agent_status="connected",
        uplink_mtu=1500,
        total_vcpus=8,
        total_ram_mb=16384,
        provider_id=provider.id,
        metering_rates={"vcpu_hour": 0, "ram_gib_hour": 0, "disk_gib_hour": 0},
    )
    db.add(host)
    db.flush()
    project = Project(
        name="quota-basic",
        owner_id=owner.id,
        state="draft",
        topology={
            "nodes": [{"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}],
            "edges": [],
        },
    )
    db.add(project)
    db.merge(
        QuotaReadiness(
            id="baseline",
            ready=True,
            baseline_version="operator-baseline-v2-allocated",
            details={"accounting_ready": True},
        )
    )
    db.flush()
    project_id, owner_id, org_id, host_id, provider_id = (
        project.id,
        owner.id,
        org.id,
        host.id,
        provider.id,
    )
    yield db, project, owner, org, host
    db.rollback()
    from app.models.metering import MeteringInterval, ProjectInvoice
    from app.models.pattern import Pattern
    from app.models.quota import QuotaAllocation, QuotaCharge

    for model in (
        QuotaAllocation,
        QuotaReservation,
        QuotaCharge,
        MeteringInterval,
        ProjectInvoice,
    ):
        db.query(model).filter_by(project_id=project_id).delete(
            synchronize_session=False
        )
    db.query(Pattern).filter_by(owner_id=owner_id).delete(synchronize_session=False)
    db.query(QuotaPolicy).filter(
        QuotaPolicy.scope_id.in_([project_id, owner_id, org_id])
    ).delete(synchronize_session=False)
    db.query(Project).filter_by(id=project_id).delete(synchronize_session=False)
    db.query(Host).filter_by(id=host_id).delete(synchronize_session=False)
    db.query(Provider).filter_by(id=provider_id).delete(synchronize_session=False)
    db.query(User).filter_by(id=owner_id).delete(synchronize_session=False)
    db.query(ManagedGroup).filter_by(id=org_id).delete(synchronize_session=False)
    if readiness_before is None:
        db.query(QuotaReadiness).filter_by(id="baseline").delete(
            synchronize_session=False
        )
    else:
        db.merge(QuotaReadiness(id="baseline", **readiness_before))
    db.commit()
    db.close()


@pytest.mark.parametrize(
    "dimension,reason",
    [
        ("cpu", "resource_limit"),
        ("ram_bytes", "resource_limit"),
        ("storage_bytes", "resource_limit"),
        ("projects", "resource_limit"),
        ("spending_usd", "spending_limit"),
    ],
)
def test_deploy_denied_before_placement(environment, monkeypatch, dimension, reason):
    from app.api import projects

    db, project, owner, org, host = environment
    owner.role = "admin"
    project.topology = {
        "nodes": project.topology["nodes"]
        + [
            {
                "id": "disk",
                "type": "storageNode",
                "data": {"size": 10, "format": "qcow2"},
            }
        ],
        "edges": [
            {"source": "disk", "target": "vm", "targetHandle": "disk-controller-0"}
        ],
    }
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, **{dimension: 0}))
    placement = Mock(side_effect=AssertionError("provider reached before admission"))
    monkeypatch.setattr(projects, "place_project", placement)
    with pytest.raises(HTTPException) as error:
        projects.deploy_project(project.id, owner, db)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == reason
    placement.assert_not_called()


@pytest.mark.parametrize(
    "name", ["start_vm", "unpause_vm", "restart_vm", "redeploy_vm"]
)
def test_direct_vm_growth_explicitly_denied_before_provider(
    environment, monkeypatch, name
):
    from app.api import projects

    db, project, owner, _, host = environment
    provider = Mock(
        side_effect=AssertionError("unsupported VM growth reached provider")
    )
    monkeypatch.setattr(projects, "start_job", provider)
    with pytest.raises(HTTPException) as error:
        getattr(projects, name)(project_id=project.id, vm_id="vm", user=owner, db=db)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "unsupported_quota_operation"
    provider.assert_not_called()


def test_raw_worker_cannot_allocate_without_durable_identity(environment, monkeypatch):
    from app.schemas.quota import AccountingUnavailable
    from app.services import deploy_service

    provider = Mock(side_effect=AssertionError("unreserved worker reached provider"))
    monkeypatch.setattr(deploy_service, "_deploy_project_inner", provider)
    with pytest.raises(AccountingUnavailable):
        deploy_service.deploy_project_async(environment[1].id)
    provider.assert_not_called()


def test_placement_cannot_provision_without_reservation(environment, monkeypatch):
    from app.schemas.quota import AccountingUnavailable
    from app.services import placement

    db, project, _, _, host = environment
    provider = Mock(
        side_effect=AssertionError("placement provisioned without reservation")
    )
    monkeypatch.setattr(placement, "_select_host", provider)
    with pytest.raises(AccountingUnavailable):
        placement.place_project(db, project, host_id=host.id)
    provider.assert_not_called()


@pytest.mark.parametrize(
    "name,extra",
    [
        ("reconfigure_project", {}),
        ("redeploy_project", {}),
        ("wipe_disk", {"vm_id": "vm", "disk_node_id": "disk", "restart": True}),
        ("start_container", {"container_id": "c"}),
        ("restart_container", {"container_id": "c"}),
        ("redeploy_container", {"container_id": "c"}),
    ],
)
def test_unsupported_transition_stops_at_route(environment, name, extra):
    from app.api import projects

    db, project, owner, _, _ = environment
    with pytest.raises(HTTPException) as error:
        getattr(projects, name)(project_id=project.id, user=owner, db=db, **extra)
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "unsupported_quota_operation"


def test_delete_does_not_freeze_before_async_cleanup(environment, monkeypatch):
    from app.api import projects
    from app.models.metering import MeteringInterval
    from app.services import metering_service

    db, project, owner, _, host = environment
    project.host_id = host.id
    project.state = "active"
    row = MeteringInterval(
        project_id=project.id,
        kind="vcpu",
        resource_id="vm",
        qty=2,
        unit_rate=1,
        started_at=datetime.now(UTC),
    )
    db.add(row)
    frozen = Mock(side_effect=AssertionError("cannot freeze before cleanup"))
    monkeypatch.setattr(metering_service, "safe_freeze_invoice", frozen)
    monkeypatch.setattr("app.core.redis.enqueue_job", Mock())
    projects.delete_project(project.id, owner, db)
    assert row.ended_at is None
    frozen.assert_not_called()


def test_confirmed_cleanup_closes_meters_and_retains_history(environment, monkeypatch):
    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaCharge
    from app.services import quota_cleanup

    db, project, owner, _, host = environment
    project.host_id = host.id
    project.state = "error"
    start = datetime(2026, 11, 11, 10, tzinfo=UTC)
    end = datetime(2026, 11, 11, 11, tzinfo=UTC)
    row = MeteringInterval(
        project_id=project.id,
        kind="vcpu",
        resource_id="vm",
        qty=2,
        unit_rate=1,
        started_at=start,
    )
    db.add(row)
    db.flush()
    quota_cleanup.finalize_absence(db, project, end)
    assert quota_accounting.utc(row.ended_at) == end
    charges = db.query(QuotaCharge).filter_by(project_id=project.id).all()
    assert sum(c.amount for c in charges) == 2
    db.delete(project)
    db.flush()
    assert db.query(QuotaCharge).filter_by(project_id=project.id).count() == 1


def test_ops_clusters_and_pending_topology_edits_are_rejected(environment):
    from app.api.projects import update_project
    from app.schemas.project import ProjectUpdate
    from app.services import quota_operations

    db, project, owner, _, _ = environment
    project.topology = {**project.topology, "clusters": [{"id": "cluster"}]}
    with pytest.raises(HTTPException) as error:
        quota_operations.prepare(db, project, "deploy")
    assert error.value.detail["reason"] == "accounting_unavailable"
    project.topology = {
        key: value for key, value in project.topology.items() if key != "clusters"
    }
    quota_operations.prepare(db, project, "deploy")
    with pytest.raises(HTTPException):
        update_project(project.id, ProjectUpdate(topology={"nodes": []}), owner, db)


@pytest.mark.parametrize(
    "name,args",
    [
        ("job_start_infra_then_vm", ("project", "host", "vm")),
        ("job_cache_and_start_vm", ("project", "host", "vm")),
        ("job_redeploy_bg", ("project", {}, "host", "rebuild")),
        ("job_vm_redeploy_bg", ("project", "host", "vm")),
        ("job_reconfigure_bg", ("project", "host", [])),
        ("job_container_redeploy_bg", ("project", "container")),
    ],
)
def test_unsupported_worker_cannot_bypass_route(environment, name, args):
    from app.workers import jobs

    with pytest.raises(HTTPException) as error:
        getattr(jobs, name)(*args)
    assert error.value.detail["reason"] == "unsupported_quota_operation"


def test_image_capacity_is_resolved_before_reservation(environment):
    from app.models.library import Library, LibraryItem, LibraryItemDisk
    from app.services import quota_operations

    db, project, owner, org, _ = environment
    library = Library(owner_id=owner.id, type="personal")
    db.add(library)
    db.flush()
    image = LibraryItem(
        library_id=library.id,
        name="qcow",
        type="image",
        format="qcow2",
        state="available",
    )
    db.add(image)
    db.flush()
    disk = LibraryItemDisk(
        library_item_id=image.id,
        s3_key="image.qcow2",
        format="qcow2",
        size_bytes=1024,
        virtual_size_bytes=40 * 2**30,
    )
    db.add(disk)
    db.flush()
    project.topology = {
        "nodes": project.topology["nodes"]
        + [
            {
                "id": "disk",
                "type": "storageNode",
                "data": {
                    "size": 10,
                    "format": "qcow2",
                    "source": "library",
                    "libraryItemId": image.id,
                },
            }
        ],
        "edges": [{"source": "vm", "sourceHandle": "dp-controller", "target": "disk"}],
    }
    operation_id = quota_operations.prepare(db, project, "deploy")
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    assert row.requested["storage_bytes"] == 80 * 2**20
    assert row.dispatch_metadata["provisioned_storage_bytes"] == 40 * 2**30
    assert project.topology["nodes"][-1]["data"]["size"] == 40
    assert set(row.dispatch_metadata["resources"]["disks"].values()) == {40}


def test_queue_failure_retains_identity_and_changed_policy_blocks_delivery(
    environment, monkeypatch
):
    from app.api import projects
    from app.schemas.quota import QuotaDenied
    from app.services import deploy_service, troshkad_client

    db, project, owner, org, host = environment
    owner.role = "admin"
    from tests.test_quota_storage import evidence

    def inventory(_host, _method, _path, body=None, **kwargs):
        item = evidence(project, generation=body["generation_id"])
        item["files"] = []
        return item

    monkeypatch.setattr(troshkad_client, "troshkad_request", inventory)
    monkeypatch.setattr(
        "app.core.redis.enqueue_job",
        Mock(side_effect=RuntimeError("queue unavailable")),
    )
    with pytest.raises(RuntimeError):
        projects.deploy_project(project.id, owner, db, host_id=host.id)
    row = db.query(QuotaReservation).filter_by(project_id=project.id).one()
    assert row.state == "pending"
    operation_id = row.operation_id
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=0))
    db.commit()
    provider = Mock(side_effect=AssertionError("lowered quota reached provider"))
    monkeypatch.setattr(troshkad_client, "troshkad_request", provider)
    with pytest.raises(QuotaDenied):
        deploy_service.deploy_project_async(project.id, quota_operation_id=operation_id)
    provider.assert_not_called()
    db.expire_all()
    assert (
        db.query(QuotaReservation).filter_by(operation_id=operation_id).one().state
        == "pending"
    )


def test_collaborator_cannot_change_owner_project_limits(environment):
    from app.models.project import ProjectShare
    from app.schemas.quota import Limits
    from app.services.quota_policy import set_limits

    db, project, owner, org, _ = environment
    collaborator = User(email=f"{uuid4()}@test.invalid", organization_id=org.id)
    db.add(collaborator)
    db.flush()
    db.add(
        ProjectShare(project_id=project.id, user_id=collaborator.id, permission="edit")
    )
    db.flush()
    with pytest.raises(HTTPException) as error:
        set_limits(
            db, collaborator, "project", project.id, Limits(cpu=100), datetime.now(UTC)
        )
    assert error.value.status_code == 403
    assert (
        not db.query(QuotaPolicy)
        .filter_by(scope="project", scope_id=project.id)
        .first()
    )


def test_pattern_auto_and_bulk_are_admitted_before_placement(environment, monkeypatch):
    from app.api import patterns
    from app.schemas.pattern import PatternDeployRequest
    from app.services import placement

    db, project, _, org, _ = environment
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=0))
    db.commit()
    provider = Mock(side_effect=AssertionError("unadmitted pattern placement"))
    monkeypatch.setattr(placement, "place_project", provider)
    with pytest.raises(HTTPException) as error:
        patterns._start_auto_deploy(
            project, PatternDeployRequest(name=project.name, auto_deploy=True), db
        )
    assert error.value.detail["reason"] == "resource_limit"
    db.rollback()
    patterns._bulk_deploy_projects([project.id])
    provider.assert_not_called()
    assert db.query(QuotaReservation).filter_by(project_id=project.id).count() == 0


def test_unassigned_owner_and_unsupported_existing_inventory_block_growth(environment):
    from app.schemas.quota import AccountingUnavailable
    from app.services import quota_operations

    db, project, owner, _, host = environment
    owner.organization_id = None
    with pytest.raises(HTTPException) as error:
        quota_operations.prepare(db, project, "deploy")
    assert "organization" in error.value.detail["message"]
    project.state = "active"
    project.host_id = host.id
    host.host_type = "kubevirt-cluster"
    with pytest.raises(AccountingUnavailable):
        quota_accounting.require_ready(db)


def test_portal_and_external_runner_growth_explicitly_denied(environment):
    from app.api.portal import portal_vm_action
    from app.services.workloads.pod_launch import launch_runner_pod

    db, project, _, _, host = environment
    with pytest.raises(HTTPException) as error:
        portal_vm_action("token", "vm", "start", db)
    assert error.value.detail["reason"] == "unsupported_quota_operation"
    with pytest.raises(HTTPException):
        launch_runner_pod(
            host, project, ee_image="runner", command=[], files={}, networks=[]
        )


def test_disabled_quota_preserves_worker_path(environment, monkeypatch):
    from app.services import deploy_service

    monkeypatch.setattr(quota_accounting, "quotas_enabled", lambda: False)
    provider = Mock()
    monkeypatch.setattr(deploy_service, "_deploy_project_inner", provider)
    deploy_service.deploy_project_async(environment[1].id)
    provider.assert_called_once_with(environment[1].id, True, None)


def test_thread_fallback_keeps_operation_and_delivery_metadata(
    environment, monkeypatch
):
    from app.core import redis
    from app.services import quota_operations

    db, project, _, _, _ = environment
    operation_id = quota_operations.prepare(db, project, "deploy")
    db.commit()
    delivered = []

    class ImmediateThread:
        name = "quota-delivery-test"

        def __init__(self, target, args, kwargs, daemon):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(redis, "is_redis_available", lambda: False)
    monkeypatch.setattr(redis.threading, "Thread", ImmediateThread)
    redis.enqueue_job(
        lambda quota_operation_id: delivered.append(quota_operation_id),
        quota_operation_id=operation_id,
    )
    db.expire_all()
    row = db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    assert delivered == [operation_id]
    assert row.dispatch_metadata["delivery_id"] == ImmediateThread.name
    assert row.dispatch_metadata["delivery_kind"] == "thread"


@pytest.mark.parametrize(
    "path",
    [
        "/patterns/capture-direct",
        "/vms/file-pull-snapshot",
        "/nbd/export",
        "/nbd/pull-flatten",
    ],
)
def test_temporary_capture_storage_cannot_bypass_agent_boundary(
    environment, monkeypatch, path
):
    from app.schemas.quota import AccountingUnavailable
    from app.services import troshkad_client

    provider = Mock(
        side_effect=AssertionError("unreserved temporary capture allocation")
    )
    monkeypatch.setattr(troshkad_client, "troshkad_request", provider)
    with pytest.raises(AccountingUnavailable):
        troshkad_client.start_job(environment[4], path, {})
    provider.assert_not_called()


@pytest.mark.parametrize("boundary", ["api", "service"])
def test_nbd_pattern_capture_is_denied_before_buffer_provisioning(
    environment, monkeypatch, boundary
):
    from app.api import patterns
    from app.models.pattern import Pattern
    from app.schemas.pattern import PatternCreate
    from app.services import pattern_service

    db, project, owner, _, host = environment
    project.state, project.host_id = "active", host.id
    buffer = Mock(
        return_value=host
    )  # A configured buffer would select the NBD adapter.
    provider = Mock(side_effect=AssertionError("NBD capture reached infrastructure"))
    monkeypatch.setattr(pattern_service, "_get_pattern_buffer", buffer)
    monkeypatch.setattr(pattern_service, "_capture_via_nbd", provider)
    monkeypatch.setattr(pattern_service, "_run_capture_pipeline", provider)
    monkeypatch.setattr("app.core.redis.enqueue_job", Mock())
    if boundary == "service":
        pattern = Pattern(
            name="queued-before-enablement",
            owner_id=owner.id,
            source_project_id=project.id,
            topology=project.topology,
            state="capturing",
        )
        db.add(pattern)
        db.commit()
    with pytest.raises(HTTPException) as error:
        if boundary == "api":
            patterns.create_pattern(
                PatternCreate(name="denied-capture", source_project_id=project.id),
                owner,
                db,
            )
        else:
            pattern_service.capture_pattern_disks(pattern.id, project.id)
    assert error.value.detail["reason"] == "unsupported_quota_operation"
    if boundary == "api":
        assert db.query(Pattern).filter_by(owner_id=owner.id).count() == 0
    buffer.assert_not_called()
    provider.assert_not_called()


def test_retained_draft_cleanup_and_invoice_after_organization_unassignment(
    environment,
):
    from datetime import timedelta

    from app.api import projects
    from app.models.metering import MeteringInterval, ProjectInvoice
    from app.models.quota import QuotaCharge

    db, project, owner, org, _ = environment
    end = datetime.now(UTC)
    identity = quota_accounting.attribution(db, project)
    db.add(
        MeteringInterval(
            project_id=project.id,
            kind="vcpu",
            resource_id="old-vm",
            qty=2,
            unit_rate=1,
            started_at=end - timedelta(hours=1),
            ended_at=end,
            quota_attribution=identity,
        )
    )
    owner.organization_id = None
    db.get(QuotaReadiness, "baseline").ready = False
    db.commit()
    project_id = project.id
    projects.delete_project(project_id, owner, db)
    assert db.get(Project, project_id) is None
    assert (
        sum(
            row.amount for row in db.query(QuotaCharge).filter_by(project_id=project_id)
        )
        == 2
    )
    assert {
        row.organization_id
        for row in db.query(QuotaCharge).filter_by(project_id=project_id)
    } == {org.id}
    assert (
        db.query(ProjectInvoice).filter_by(project_id=project_id).one().total_usd == 2
    )


def test_current_generation_keeps_zero_delta_worker_fences():
    from types import SimpleNamespace

    from app.services.quota_operations import current_generation

    holder = SimpleNamespace(
        operation_id="deploy",
        state="confirmed",
        observed={"cpu": 2, "projects": 1},
        dispatch_metadata={"generation_id": "deployment"},
    )
    zero_start = SimpleNamespace(
        operation_id="start",
        state="confirmed",
        observed={},
        dispatch_metadata={"generation_id": "deployment"},
    )
    retired = SimpleNamespace(
        operation_id="old",
        state="confirmed",
        observed={},
        dispatch_metadata={"generation_id": "retired"},
    )
    assert current_generation([retired, holder, zero_start]) == [holder, zero_start]


@pytest.mark.parametrize(
    "module,function,args",
    [
        (
            "app.services.migration_service",
            "migrate_project",
            ("p", "source", "target"),
        ),
        (
            "app.services.migration_service",
            "_do_migrate_project",
            ("p", "source", "target"),
        ),
        ("app.services.snapshot_service", "capture_vm_disks", ("item", "p", "vm")),
        ("app.services.providers.kubevirt", "create_ops_pod", (None, "p", {}, {})),
        ("app.api.projects", "_do_reconfigure_bg", ("p", "h", [])),
        ("app.api.projects", "_do_redeploy_bg", ("p", "h", "vm")),
        ("app.services.deploy_service", "redeploy_container_bg", ("p", "container")),
    ],
)
def test_unsupported_service_boundary_rejects_before_infrastructure(
    environment, module, function, args
):
    import importlib

    with pytest.raises(HTTPException) as error:
        getattr(importlib.import_module(module), function)(*args)
    assert error.value.detail["reason"] == "unsupported_quota_operation"


def test_quota_provider_does_not_retry_ambiguous_mutation(environment, monkeypatch):
    from app.services import troshkad_client

    db, project, _, _, host = environment
    provider = Mock(side_effect=troshkad_client.TroshkadError("timed out"))
    monkeypatch.setattr(troshkad_client, "troshkad_request", provider)
    monkeypatch.setattr(troshkad_client, "_DRAIN_RETRY_TIMEOUT", -1)
    # The unwrapped provider operation still uses quota-mode transport semantics.
    with pytest.raises(troshkad_client.TroshkadError):
        troshkad_client.start_job.__wrapped__(host, "/vms/create", {})
    assert provider.call_count == 1
    assert provider.call_args.kwargs["retries"] == 1


@pytest.mark.parametrize("unassign_before_cleanup", [False, True])
def test_supported_deploy_through_actual_worker_and_provider_observation(
    environment, monkeypatch, unassign_before_cleanup
):
    from datetime import timedelta

    from app.api import projects
    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaAllocation, QuotaCharge
    from app.services import (
        deploy_service,
        quota_cleanup,
        quota_operations,
        troshkad_client,
        ws_pubsub,
    )

    db, project, owner, org, host = environment
    owner.role = "admin"
    project.topology = {
        "nodes": project.topology["nodes"]
        + [
            {
                "id": "disk",
                "type": "storageNode",
                "data": {"size": 10, "format": "qcow2"},
            }
        ],
        "edges": [{"source": "vm", "sourceHandle": "dp-controller", "target": "disk"}],
    }
    # A retired never-claimed generation has no worker completion timestamp and
    # must not prevent later admission/observation or erase its durable history.
    cancelled_operation = quota_operations.prepare(db, project, "deploy")
    assert quota_cleanup.complete_empty(db, project)
    db.commit()
    jobs = {}
    states = {}
    calls = []
    retained_disk = False

    def provider(_host, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/storage/inventory":
            from tests.test_quota_storage import evidence

            item = evidence(project, generation=body["generation_id"], amount=4096)
            item["files"][0]["path"] = next(
                path
                for path in quota_operations.resource_map(project, project.topology)[
                    "disks"
                ]
                if "/shared/" not in path
            )
            item["host_id"] = _host.id
            if not states:
                item["files"] = []
            return item
        if path == "/vms/states":
            return {
                "domains": {name: {"state": state} for name, state in states.items()}
            }
        if path == "/host/disk-usage":
            return {
                "free_bytes": 1024**4,
                "total_bytes": 2 * 1024**4,
                "used_pct": 0,
            }
        if path.startswith("/commands"):
            job_id = str(uuid4())
            result = {}
            if path == "/commands/vms/create":
                states[body["domain_name"]] = "shut_off"
            if path == "/commands/vms/start":
                states[body["domain_name"]] = "running"
            if path == "/commands/vms/stop":
                states[body["domain_name"]] = "shut_off"
            if path == "/commands/vms/state":
                result = {"state": states.get(body["domain_name"], "not_found")}
            if path == "/commands/vms/destroy":
                states.pop(body["domain_name"], None)
            if path == "/commands/files/stat":
                result = {"exists": retained_disk}
            jobs[job_id] = {"status": "completed", "result": result, "output": ""}
            return {"job_id": job_id}
        if path.startswith("/jobs/"):
            return jobs[path.rsplit("/", 1)[1]]
        return {}

    def queue(function, *args, **kwargs):
        kwargs.pop("project_id", None)
        kwargs.pop("host_id", None)
        return function(*args, **kwargs)

    monkeypatch.setattr(troshkad_client, "troshkad_request", provider)
    monkeypatch.setattr("app.core.redis.enqueue_job", queue)
    response = projects.deploy_project(project.id, owner, db, host_id=host.id)
    db.expire_all()
    project = db.get(Project, project.id)
    assert project.state == "active", project.deploy_error
    reservation = (
        db.query(QuotaReservation)
        .filter_by(project_id=project.id, state="reconciling")
        .one()
    )
    assert reservation.dispatch_metadata["claim"]
    assert all(
        call["terminal"] for call in reservation.dispatch_metadata["calls"].values()
    )
    assert [
        path
        for _, path, _ in calls
        if path in {"/commands/vms/create", "/commands/vms/start"}
    ] == ["/commands/vms/create", "/commands/vms/start"]
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "running"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    db.expire_all()
    allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=reservation.operation_id).one()
    )
    assert allocation.observed["cpu"] == 2
    assert quota_accounting.utc(allocation.observed_at) == observed
    assert response["status"] == "deploying"
    assert allocation.observed["projects"] == 1
    assert allocation.observed["storage_bytes"] == 4096
    assert any(path == "/commands/disks/create" for _, path, _ in calls)

    count_before_duplicate = len(calls)
    from app.schemas.quota import AccountingUnavailable
    from app.services.quota_admission import OperationConflict

    with pytest.raises((AccountingUnavailable, OperationConflict)):
        deploy_service.deploy_project_async(
            project.id, quota_operation_id=reservation.operation_id
        )
    assert len(calls) == count_before_duplicate

    projects.stop_project(project.id, owner, db)
    db.expire_all()
    assert db.get(Project, project.id).state == "stopped"
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "shut_off"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    db.expire_all()
    assert (
        db.query(QuotaAllocation)
        .filter_by(operation_id=reservation.operation_id)
        .one()
        .observed["cpu"]
        == 0
    )
    assert (
        db.query(QuotaAllocation)
        .filter_by(operation_id=reservation.operation_id)
        .one()
        .observed["storage_bytes"]
        == 4096
    )

    projects.start_project(project.id, owner, db)
    db.expire_all()
    assert db.get(Project, project.id).state == "active"
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "running"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    db.expire_all()
    assert (
        sum(
            row.observed["cpu"]
            for row in db.query(QuotaAllocation).filter_by(project_id=project.id)
        )
        == 2
    )

    # Cleanup must proceed even after readiness is removed and a zero cap applies.
    retained_meter = MeteringInterval(
        project_id=project.id,
        kind="disk",
        resource_id="retained-cleanup-evidence",
        qty=1,
        unit_rate=1,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        quota_attribution=quota_accounting.attribution(db, project),
    )
    db.add(retained_meter)
    db.get(QuotaReadiness, "baseline").ready = False
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, cpu=0))
    if unassign_before_cleanup:
        owner.organization_id = None
    db.commit()
    retained_disk = True
    with pytest.raises(HTTPException) as error:
        projects.undeploy_project(project.id, owner, db)
    assert error.value.detail["reason"] == "accounting_unavailable"
    db.expire_all()
    assert any(
        row.observed["storage_bytes"]
        for row in db.query(QuotaAllocation).filter_by(project_id=project.id)
    )
    assert db.get(Project, project.id).state != "draft"
    retained_disk = False
    projects.undeploy_project(project.id, owner, db)
    db.expire_all()
    assert db.get(Project, project.id).state == "draft"
    assert not any(
        any(row.observed.values())
        for row in db.query(QuotaAllocation).filter_by(project_id=project.id)
    )
    assert db.get(MeteringInterval, retained_meter.id).ended_at is not None
    assert (
        sum(
            row.amount for row in db.query(QuotaCharge).filter_by(project_id=project.id)
        )
        >= 1
    )
    assert (
        db.query(QuotaReservation)
        .filter_by(operation_id=cancelled_operation)
        .one()
        .dispatch_metadata.get("claim")
        is None
    )

    # The supported replacement workflow preserves old generations, changes
    # resource topology, then deploys/stops/starts the new target successfully.
    from copy import deepcopy

    from app.schemas.project import ProjectUpdate

    owner.organization_id = org.id
    db.get(QuotaReadiness, "baseline").ready = True
    db.query(QuotaPolicy).filter_by(scope="organization", scope_id=org.id).delete()
    changed = deepcopy(project.topology)
    changed["nodes"][0]["data"]["vcpus"] = 3
    changed["nodes"][1]["data"]["size"] = 12
    projects.update_project(project.id, ProjectUpdate(topology=changed), owner, db)
    monkeypatch.setattr(ws_pubsub, "get_cached_vm_states", lambda _: None)
    projects.deploy_project(project.id, owner, db, host_id=host.id)
    db.expire_all()
    assert db.get(Project, project.id).state == "active"
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "running"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    projects.stop_project(project.id, owner, db)
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "shut_off"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    db.expire_all()
    projects.start_project(project.id, owner, db)
    db.expire_all()
    assert db.get(Project, project.id).state == "active"
    observed = datetime.now(UTC)
    monkeypatch.setattr(
        ws_pubsub,
        "get_cached_vm_states",
        lambda _: {"states": {"vm": "running"}, "observed_at": observed},
    )
    quota_operations.reconcile(project.id)
    db.expire_all()
    assert (
        sum(
            row.observed["cpu"]
            for row in db.query(QuotaAllocation).filter_by(project_id=project.id)
        )
        == 3
    )
    if unassign_before_cleanup:
        owner.organization_id = None
    db.commit()
    project_id = project.id
    projects.delete_project(project_id, owner, db)
    db.expire_all()
    assert db.get(Project, project_id) is None
    assert not any(
        any(row.observed.values())
        for row in db.query(QuotaAllocation).filter_by(project_id=project_id)
    )
    assert (
        sum(
            row.amount for row in db.query(QuotaCharge).filter_by(project_id=project_id)
        )
        >= 1
    )
    assert {
        row.organization_id
        for row in db.query(QuotaCharge).filter_by(project_id=project_id)
    } == {org.id}
