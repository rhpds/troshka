"""Physical evidence changes storage only; compute still requires admission."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.host import Host
from app.models.managed_group import ManagedGroup
from app.models.project import Project
from app.models.quota import (
    QuotaAction,
    QuotaAllocation,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.models.user import User
from app.schemas.quota import AccountingUnavailable, QuotaDenied, Resources
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_enforcement as enforcement
from app.services import quota_operations as operations
from app.services import quota_storage as storage
from app.services.quota_baseline import VERSION as BASELINE

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def evidence(project, generation="deploy", amount=4096, free=90 * 2**30):
    return {
        "version": storage.VERSION,
        "complete": True,
        "project_id": project.id,
        "generation_id": generation,
        "host_id": project.host_id,
        "observed_at": NOW.isoformat(),
        "observation_id": str(uuid4()),
        "writable_pool": "device-1",
        "bases": [],
        "files": [
            {
                "path": f"/var/lib/troshka/vms/{project.id}/retained.qcow2",
                "device": "device-1",
                "inode": "1",
                "allocated_bytes": amount,
            }
        ],
        "pools": [
            {
                "id": "device-1",
                "capacity_bytes": 100 * 2**30,
                "free_bytes": free,
                "platform_allocated_bytes": 0,
            }
        ],
    }


@pytest.fixture
def ledger(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(accounting, "spend", lambda *args: 0)
    monkeypatch.setattr(operations, "now", lambda: NOW)
    with Session(engine) as db:
        org = ManagedGroup(name="COW", normalized_name="cow", is_organization=True)
        db.add(org)
        db.flush()
        owner = User(email="cow@test.invalid", organization_id=org.id)
        host = Host(state="active", billing_mode="shared")
        db.add_all([owner, host])
        db.flush()
        project = Project(
            name="COW",
            owner_id=owner.id,
            host_id=host.id,
            state="active",
            topology={"nodes": []},
        )
        db.add(project)
        db.add(
            QuotaReadiness(
                id="baseline",
                ready=True,
                baseline_version=BASELINE,
                details={"accounting_ready": True},
            )
        )
        db.flush()
        vector = Resources(
            cpu=2, ram_bytes=4 * 2**30, storage_bytes=1024, projects=1
        ).model_dump()
        root = QuotaReservation(
            operation_id="deploy",
            project_id=project.id,
            owner_id=owner.id,
            organization_id=org.id,
            state="confirmed",
            requested=vector,
            observed=vector,
            observation_id="compute",
            dispatch_metadata={
                "kind": "deploy",
                "generation_id": "deploy",
                "worker_finished": True,
                "worker_finished_at": (NOW - timedelta(seconds=2)).isoformat(),
                "calls": {},
                "storage_version": storage.VERSION,
                "provisioned_storage_bytes": 100 * 2**30,
            },
        )
        allocation = QuotaAllocation(
            operation_id="deploy",
            project_id=project.id,
            owner_id=owner.id,
            organization_id=org.id,
            requested=vector,
            observed=vector,
            observation_id="compute",
            observed_at=NOW - timedelta(seconds=1),
            state="active",
        )
        db.add_all([root, allocation])
        db.flush()
        yield db, project, owner, org, host, root
    engine.dispose()


def test_growth_updates_only_storage_and_counts_retained_files(ledger):
    db, project, _, _, _, root = ledger
    observed = evidence(project, amount=12 * 2**30)
    assert storage.apply_measurement(db, project, [root], observed, NOW)
    assert admission.usage(db, "project", project.id)[0] == Resources(
        cpu=2, ram_bytes=4 * 2**30, storage_bytes=12 * 2**30, projects=1
    )
    root.observed = {**root.observed, "cpu": 0, "ram_bytes": 0}
    next_observation = {
        **observed,
        "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    storage.apply_measurement(
        db, project, [root], next_observation, NOW + timedelta(seconds=1)
    )
    assert root.observed["storage_bytes"] == 12 * 2**30
    assert root.observed["cpu"] == 0
    with pytest.raises(admission.OperationConflict, match="no live reservation"):
        admission.confirm(
            db, "deploy", Resources(cpu=5), "new-compute", NOW + timedelta(seconds=2)
        )


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "incomplete",
        "stale",
        "future",
        "generation",
        "host",
        "escape",
        "duplicate",
    ],
)
def test_invalid_evidence_never_replaces_usage(ledger, change):
    db, project, _, _, _, root = ledger
    item = evidence(project)
    if change == "legacy":
        item["version"] = "virtual-v1"
    if change == "incomplete":
        item["complete"] = False
    if change == "stale":
        item["observed_at"] = (NOW - timedelta(minutes=5)).isoformat()
    if change == "future":
        item["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    if change == "generation":
        item["generation_id"] = "old-deploy"
    if change == "host":
        item["host_id"] = "wrong"
    if change == "escape":
        item["files"][0]["path"] = "/var/lib/troshka/vms/other/disk"
    if change == "duplicate":
        item["files"].append(deepcopy(item["files"][0]))
    with pytest.raises(AccountingUnavailable):
        storage.apply_measurement(db, project, [root], item, NOW)
    assert root.observed["storage_bytes"] == 1024


def test_multiple_start_operations_do_not_duplicate_project_files(ledger):
    db, project, owner, org, _, root = ledger
    zero = Resources().model_dump()
    start = QuotaReservation(
        operation_id="start",
        project_id=project.id,
        owner_id=owner.id,
        organization_id=org.id,
        requested=zero,
        observed=zero,
        state="confirmed",
        dispatch_metadata={**root.dispatch_metadata, "kind": "start"},
    )
    db.add(start)
    db.add(
        QuotaAllocation(
            operation_id="start",
            project_id=project.id,
            owner_id=owner.id,
            organization_id=org.id,
            requested=zero,
            observed=zero,
            state="stopped",
            observed_at=NOW - timedelta(seconds=1),
        )
    )
    db.flush()
    storage.apply_measurement(db, project, [root, start], evidence(project), NOW)
    assert admission.usage(db, "project", project.id)[0].storage_bytes == 4096
    assert start.observed["storage_bytes"] == 0


def test_storage_exhaustion_distinct_cause_fences_and_pool_stop_reuses_coordinator(
    ledger, monkeypatch
):
    db, project, _, org, _, root = ledger
    storage.apply_measurement(
        db, project, [root], evidence(project, amount=8192, free=1024), NOW
    )
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, storage_bytes=4096))
    db.flush()
    current, _ = enforcement._evaluate_project(db, project, NOW, {})
    assert len(current) == 2
    status = enforcement.project_status(db, project.id)
    assert status["storage_state"] == "storage_exceeded_shutdown_pending"
    assert status["spending_state"] == "within_budget"
    run = db.query(QuotaAction).filter_by(kind="stop_run").one()
    assert enforcement.fence_action(db, run.id, NOW)
    assert root.dispatch_metadata["cancelled"] is True
    with pytest.raises(QuotaDenied) as denied:
        admission._check_scope(db, "organization", org.id, Resources(cpu=1), NOW)
    assert denied.value.dimension == "storage_bytes"


def test_stale_evidence_blocks_new_growth_but_retains_proven_storage_cause(ledger):
    db, project, _, org, _, root = ledger
    storage.apply_measurement(db, project, [root], evidence(project, amount=8192), NOW)
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, storage_bytes=4096))
    db.flush()
    first, _ = enforcement._evaluate_project(db, project, NOW, {})
    later, unavailable = enforcement._evaluate_project(
        db, project, NOW + timedelta(minutes=2), {}
    )
    assert first == later and unavailable
    with pytest.raises(AccountingUnavailable):
        storage.scope_evidence(db, "organization", org.id, NOW + timedelta(minutes=2))


def test_pool_pending_reservations_and_initial_allowance(ledger):
    db, project, owner, org, host, _ = ledger
    topology = {
        "nodes": [
            {"id": "vm", "type": "vmNode"},
            {"id": "disk", "type": "storageNode", "data": {"size": 1000}},
        ],
        "edges": [{"source": "vm", "target": "disk"}],
    }
    assert storage.initial_allocation(topology) == 80 * 2**20
    vector = Resources(storage_bytes=2**30).model_dump()
    db.add(
        QuotaReservation(
            operation_id="pending-other",
            project_id=project.id,
            owner_id=owner.id,
            organization_id=org.id,
            requested=vector,
            observed=Resources().model_dump(),
            state="pending",
            dispatch_metadata={"storage_host_id": host.id},
        )
    )
    db.flush()
    with pytest.raises(AccountingUnavailable, match="headroom"):
        storage.check_pool(db, evidence(project, free=6 * 2**30), requested=1)


def test_old_baseline_cannot_enable_physical_quotas(ledger):
    db, *_ = ledger
    db.get(QuotaReadiness, "baseline").baseline_version = "operator-baseline-v1"
    with pytest.raises(AccountingUnavailable, match="baseline"):
        accounting.require_ready(db)


def test_fake_agent_transport_retains_original_time_and_generation(ledger, monkeypatch):
    _, project, _, _, host, _ = ledger
    from app.services import troshkad_client

    monkeypatch.setattr(operations, "now", lambda: NOW)

    def transport(h, method, path, body, timeout, retries):
        assert h.id == host.id and method == "POST" and path == "/storage/inventory"
        assert body == {"project_id": project.id, "generation_id": "deploy"}
        assert timeout == 5 and retries == 1
        return evidence(project)

    monkeypatch.setattr(troshkad_client, "troshkad_request", transport)
    observed = storage.fetch(host, project.id, "deploy")
    assert observed["observed_at"] == NOW.isoformat()
    assert len(observed["observation_id"]) == 64


def test_pending_provider_growth_is_charged_without_releasing_compute_reservation(
    ledger,
):
    db, project, _, _, _, root = ledger
    root.state = "dispatched"
    root.dispatched_at = NOW - timedelta(seconds=1)
    root.dispatch_metadata = {
        **root.dispatch_metadata,
        "claim": "worker",
        "worker_finished": False,
    }
    storage.apply_measurement(
        db, project, [root], evidence(project, amount=2**30), NOW
    )
    assert root.state == "dispatched" and root.observed["storage_bytes"] == 2**30
    assert root.observed["cpu"] == 2
    smaller = evidence(project, amount=0)
    smaller["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    storage.apply_measurement(db, project, [root], smaller, NOW + timedelta(seconds=1))
    assert root.observed["storage_bytes"] == 2**30
    root.dispatch_metadata = {
        **root.dispatch_metadata,
        "worker_finished": True,
        "worker_finished_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    admission.confirm(
        db,
        "deploy",
        Resources(**root.observed),
        "compute-finished",
        NOW + timedelta(seconds=2),
    )
    assert root.state == "confirmed"


def test_pool_change_and_missing_deployed_disk_cannot_release_storage(ledger):
    db, project, _, _, _, root = ledger
    first = evidence(project)
    storage.apply_measurement(db, project, [root], first, NOW)
    changed = deepcopy(first)
    changed["pools"][0]["id"] = changed["writable_pool"] = changed["files"][0][
        "device"
    ] = "new-device"
    changed["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(AccountingUnavailable, match="pool identity"):
        storage.apply_measurement(
            db, project, [root], changed, NOW + timedelta(seconds=1)
        )
    root.dispatch_metadata = {
        **root.dispatch_metadata,
        "resources": {"disks": {first["files"][0]["path"]: 100}},
    }
    changed = {
        **first,
        "files": [],
        "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    with pytest.raises(AccountingUnavailable, match="disks missing"):
        storage.apply_measurement(
            db, project, [root], changed, NOW + timedelta(seconds=1)
        )
    assert root.observed["storage_bytes"] == 4096


def test_lowering_storage_cap_stops_but_cpu_ram_reductions_do_not(ledger):
    db, project, _, org, _, root = ledger
    storage.apply_measurement(db, project, [root], evidence(project, amount=8192), NOW)
    policy = QuotaPolicy(
        scope="organization", scope_id=org.id, storage_bytes=16384, cpu=0, ram_bytes=0
    )
    db.add(policy)
    db.flush()
    assert enforcement._evaluate_project(db, project, NOW, {})[0] == set()
    policy.storage_bytes, policy.revision = 4096, 2
    db.flush()
    causes, _ = enforcement._evaluate_project(db, project, NOW, {})
    assert len(causes) == 1
    cause = db.query(QuotaAction).filter_by(kind="stop").one()
    assert cause.details["reason"] == "physical_storage_limit"
    policy.storage_bytes, policy.revision = 16384, 3
    db.flush()
    assert enforcement._evaluate_project(db, project, NOW, {})[0] == set()
    assert cause.state == "cancelled"


def test_baseline_storage_and_slot_survive_completed_stop_and_next_observation(
    ledger, monkeypatch
):
    db, project, _, org, _, root = ledger
    project.topology = {
        "nodes": [{"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}]
    }
    root.dispatch_metadata = {
        **root.dispatch_metadata,
        "known_empty": False,
        "provenance": BASELINE,
    }
    storage.apply_measurement(db, project, [root], evidence(project, amount=8192), NOW)
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, storage_bytes=4096))
    db.flush()
    enforcement._evaluate_project(db, project, NOW, {})
    run = db.query(QuotaAction).filter_by(kind="stop_run").one()
    off_at = NOW + timedelta(seconds=1)
    monkeypatch.setattr(
        "app.services.ws_pubsub.get_cached_vm_states",
        lambda _: {
            "states": {"vm": "shut_off"},
            "observed_at": off_at,
            "observation_id": "baseline-off",
        },
    )
    assert not enforcement._prepare_stop(db, run, off_at)
    assert run.state == "completed"
    assert root.observed == Resources(storage_bytes=8192, projects=1).model_dump()
    retained = operations.current_generation(operations._rows(db, project.id))
    assert [row.operation_id for row in retained] == ["deploy"]
    next_at = NOW + timedelta(seconds=2)
    next_evidence = {
        **evidence(project, amount=12288),
        "observed_at": next_at.isoformat(),
    }
    assert storage.apply_measurement(db, project, retained, next_evidence, next_at)
    assert admission.usage(db, "project", project.id)[0] == Resources(
        storage_bytes=12288, projects=1
    )


def _provider_guard(ledger, monkeypatch, kind):
    db, project, owner, org, host, root = ledger
    project.state = "deploying" if kind == "deploy" else "starting"
    project.topology = {
        "nodes": [{"id": "vm", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}]
    }
    metadata = {
        **root.dispatch_metadata,
        "desired_digest": operations.digest(project.topology),
        "storage_pool_id": "device-1",
        "resources": {"vms": {"domain": {"cpu": 2, "ram_mb": 4096}}, "disks": {}},
    }
    root.dispatch_metadata = metadata
    row = root
    if kind == "start":
        root.observed = {**root.observed, "cpu": 0, "ram_bytes": 0}
        db.query(QuotaAllocation).filter_by(
            operation_id="deploy"
        ).one().observed = root.observed
        row = QuotaReservation(
            operation_id="start",
            project_id=project.id,
            owner_id=owner.id,
            organization_id=org.id,
            state="dispatched",
            requested=Resources(cpu=2, ram_bytes=4 * 2**30).model_dump(),
            observed=Resources().model_dump(),
            dispatch_metadata={**metadata, "kind": "start"},
        )
        db.add(row)
    row.state, row.dispatched_at = "dispatched", NOW - timedelta(seconds=1)
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "claim": "worker",
        "worker_finished": False,
    }
    monkeypatch.setattr(operations, "supported", lambda *args, **kwargs: None)
    db.flush()
    return row


@pytest.mark.parametrize("kind", ["deploy", "start"])
@pytest.mark.parametrize("scope", ["project", "user", "organization"])
def test_provider_intent_denied_by_its_fresh_physical_observation(
    ledger, monkeypatch, kind, scope
):
    db, project, owner, org, host, root = ledger
    row = _provider_guard(ledger, monkeypatch, kind)
    identity = {"project": project.id, "user": owner.id, "organization": org.id}[scope]
    db.add(QuotaPolicy(scope=scope, scope_id=identity, storage_bytes=4096))
    monkeypatch.setattr(storage, "fetch", lambda *args: evidence(project, amount=8192))
    with pytest.raises(QuotaDenied) as denied:
        operations._record_agent_intent(
            db,
            row.operation_id,
            "denied",
            host,
            "/vms/create",
            {"domain_name": "domain", "vcpus": 2, "ram_mb": 4096},
        )
    assert denied.value.scope == scope
    assert root.observed["storage_bytes"] == 8192
    assert row.dispatch_metadata["calls"] == {}
    if kind == "start":
        assert row.observed["storage_bytes"] == 0


def test_denied_start_commits_measurement_without_intent_and_locks_whole_generation_first(
    ledger, monkeypatch
):
    from sqlalchemy.orm import sessionmaker

    from app.services import quota_locks

    db, project, _, org, host, root = ledger
    row = _provider_guard(ledger, monkeypatch, "start")
    db.add(QuotaPolicy(scope="organization", scope_id=org.id, storage_bytes=4096))
    db.commit()
    events = []
    original_operations, original_project = (
        admission.lock_operations,
        quota_locks.lock_project,
    )

    def operation_locks(session, identities):
        events.append(("operations", set(identities)))
        return original_operations(session, identities)

    def identity_locks(session, identity, **kwargs):
        events.append(("identity", identity))
        return original_project(session, identity, **kwargs)

    monkeypatch.setattr(admission, "lock_operations", operation_locks)
    monkeypatch.setattr(quota_locks, "lock_project", identity_locks)
    monkeypatch.setattr(storage, "fetch", lambda *args: evidence(project, amount=8192))
    monkeypatch.setattr(
        "app.core.database.SessionLocal", sessionmaker(bind=db.get_bind())
    )
    provider_calls = []

    @operations.tracked_agent_job
    def provider(*args, **kwargs):
        provider_calls.append(args)
        return "unexpected-job"

    token = operations.current_operation.set(row.operation_id)
    try:
        with pytest.raises(QuotaDenied):
            provider(host, "/vms/start", {"domain_name": "domain"})
    finally:
        operations.current_operation.reset(token)
    db.expire_all()
    assert (
        db.query(QuotaAllocation)
        .filter_by(operation_id="deploy")
        .one()
        .observed["storage_bytes"]
        == 8192
    )
    assert (
        db.query(QuotaReservation)
        .filter_by(operation_id="start")
        .one()
        .dispatch_metadata["calls"]
        == {}
    )
    assert not provider_calls
    first_identity = next(
        index for index, event in enumerate(events) if event[0] == "identity"
    )
    assert events[0] == ("operations", {"deploy", "start"})
    assert not any(event[0] == "operations" for event in events[first_identity:])


def test_mid_deploy_cap_reduction_blocks_pending_remainder_without_double_counting(
    ledger, monkeypatch
):
    db, project, _, org, host, _ = ledger
    row = _provider_guard(ledger, monkeypatch, "deploy")
    row.requested = {**row.requested, "storage_bytes": 80 * 2**20}
    policy = QuotaPolicy(
        scope="organization", scope_id=org.id, storage_bytes=80 * 2**20
    )
    db.add(policy)
    clock = [NOW]
    monkeypatch.setattr(operations, "now", lambda: clock[0])
    monkeypatch.setattr(
        storage,
        "fetch",
        lambda *args: {
            **evidence(project, amount=2**20),
            "observed_at": clock[0].isoformat(),
        },
    )
    # 1 MiB used + 79 MiB committed remainder == 80 MiB. The command is
    # materializing its existing reservation, not requesting another 80 MiB.
    operations._record_agent_intent(
        db,
        row.operation_id,
        "allowed",
        host,
        "/vms/create",
        {"domain_name": "domain", "vcpus": 2, "ram_mb": 4096},
    )
    used, reserved = admission.usage(db, "organization", org.id)
    assert (used.storage_bytes, reserved.storage_bytes) == (2**20, 79 * 2**20)
    policy.storage_bytes = 40 * 2**20
    clock[0] += timedelta(seconds=1)
    with pytest.raises(QuotaDenied):
        operations._record_agent_intent(
            db,
            row.operation_id,
            "denied",
            host,
            "/vms/start",
            {"domain_name": "domain"},
        )
    assert set(row.dispatch_metadata["calls"]) == {"allowed"}
    policy.storage_bytes = 2**20
    with pytest.raises(QuotaDenied):
        storage.check_allowance(db, "organization", org.id, clock[0])


def test_pool_lock_delay_rejects_original_observation_without_refreshing_it(
    ledger, monkeypatch
):
    db, project, *_ = ledger
    clock = [NOW]
    item = evidence(project)
    monkeypatch.setattr(operations, "now", lambda: clock[0])

    def delayed_lock(*args):
        clock[0] += timedelta(seconds=61)

    monkeypatch.setattr(storage, "_pool_lock", delayed_lock)
    storage.validate(item, project.id, "deploy", project.host_id, NOW)
    with pytest.raises(AccountingUnavailable, match="stale"):
        storage.check_pool(db, item)
    assert item["observed_at"] == NOW.isoformat()
