"""Final review regressions through save, provider poll, ledger and stop paths."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api import projects
from app.core.auth import get_current_user
from app.main import app
from app.models.quota import QuotaAction, QuotaReadiness, QuotaReservation
from app.schemas.quota import AccountingUnavailable, Limits
from app.services import deploy_service, metering_service, quota_observations, ws_pubsub
from app.services import quota_accounting as accounting
from app.services import quota_admission as admission
from app.services import quota_enforcement as enforcement
from app.services import quota_operations as operations
from app.services.deploy_topology import _vm_domain_name
from app.services.quota_policy import set_limits
from tests.test_metering_api import _as_user, client
from tests.test_quota_entry_points import environment  # noqa: F401
from tests.test_quota_observations import SharedRedis


@pytest.fixture
def runtime(environment, monkeypatch):  # noqa: F811 - imported shared pytest fixture
    db, project, owner, org, host = environment

    class Clock(datetime):
        value = datetime(2026, 11, 11, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(cls.value.isoformat())

    monkeypatch.setattr(ws_pubsub, "datetime", Clock)
    monkeypatch.setattr(metering_service, "datetime", Clock)
    monkeypatch.setattr(operations, "now", Clock.now)
    monkeypatch.setattr(enforcement, "_now", Clock.now)
    monkeypatch.setattr(ws_pubsub, "_last_states", {})
    monkeypatch.setattr(quota_observations, "_client", SharedRedis())
    monkeypatch.setattr(ws_pubsub, "notify_project", Mock())
    monkeypatch.setattr(projects, "notify_project", Mock())
    monkeypatch.setattr(deploy_service, "notify_project", Mock())
    monkeypatch.setattr(deploy_service, "_get_deploy_progress_data", lambda pid: None)
    states, calls = {}, []
    monkeypatch.setattr(
        "app.services.troshkad_client.get_all_vm_states", lambda h: dict(states)
    )
    monkeypatch.setattr(
        deploy_service,
        "start_job",
        lambda h, path, params: calls.append((path, params)) or "job",
    )
    monkeypatch.setattr(deploy_service, "wait_for_job", lambda *a, **kw: {})
    # Only queue/provider transport is faked. Save, polling, resource admission,
    # metering, charge accrual, enforcement and stop orchestration remain real.
    queued = []
    monkeypatch.setattr(
        "app.core.redis.enqueue_job", lambda fn, *a, **kw: queued.append((fn, a))
    )
    _as_user(owner)
    yield SimpleNamespace(
        db=db,
        project=project,
        owner=owner,
        org=org,
        host=host,
        clock=Clock,
        states=states,
        calls=calls,
        queued=queued,
    )
    app.dependency_overrides.pop(get_current_user, None)
    db.rollback()
    db.query(QuotaAction).filter(
        QuotaAction.scope_id.in_([project.id, owner.id, org.id])
        | (QuotaAction.project_id == project.id)
    ).delete(synchronize_session=False)
    for marker in (
        db.query(QuotaReadiness).filter(QuotaReadiness.id != "baseline").all()
    ):
        if (marker.details or {}).get("project_id") == project.id or (
            marker.details or {}
        ).get("scope_id") == project.id:
            db.delete(marker)
    db.commit()


def deploy(r, count=1):
    r.project.topology = {
        "nodes": [
            {"id": f"vm-{i}", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}}
            for i in range(count)
        ],
        "edges": [],
    }
    operation_id = operations.prepare(r.db, r.project, "deploy", host_id=r.host.id)
    row = r.db.query(QuotaReservation).filter_by(operation_id=operation_id).one()
    r.project.host_id = r.host.id
    r.project.provider_id = r.host.provider_id
    r.project.deployed_topology = deepcopy(r.project.topology)
    r.project.state = "active"
    row.state = "reconciling"
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "claim": "provider-worker",
        "worker_finished": True,
        "worker_finished_at": r.clock.now().isoformat(),
    }
    r.db.commit()
    poll(r, "running")
    r.db.refresh(row)
    assert row.state == "confirmed"
    assert row.observed["cpu"] == count * 2
    return row


def poll(r, state, seconds=1):
    r.clock.value += timedelta(seconds=seconds)
    for node in r.project.deployed_topology["nodes"]:
        r.states[_vm_domain_name(r.project.id, node["id"])] = state
    r.db.commit()
    ws_pubsub._poll_active_projects()
    r.db.expire_all()


def stop_run(r):
    enforcement.evaluate(r.db, r.clock.now())
    r.db.commit()
    return (
        r.db.query(QuotaAction)
        .filter_by(project_id=r.project.id, kind="stop_run")
        .one()
    )


@pytest.mark.parametrize("state", ["shutting_down", "suspended", "crashed", "shut_off"])
def test_raw_provider_poll_controls_capacity_accounting_and_pending_stop(
    runtime, state
):
    r = runtime
    row = deploy(r)
    assert accounting.spend(r.db, "project", r.project.id, r.clock.now()) == 0
    r.project.budget_usd = 0
    run = stop_run(r)
    enforcement.reconcile_action(r.db, run.id, r.clock.now())
    assert r.calls == [
        ("/vms/stop", {"domain_name": _vm_domain_name(r.project.id, "vm-0")})
    ]
    assert run.state == "issued"
    poll(r, state)
    observed = ws_pubsub.get_cached_vm_states(r.project.id)
    assert observed["states"] == {"vm-0": state}
    assert ws_pubsub._last_states[r.project.id]["vm_states"] == {"vm-0": "stopped"}
    response = client.get(f"/api/v1/projects/{r.project.id}/vm-states")
    assert response.status_code == 200
    assert response.json()["states"] == {"vm-0": "stopped"}
    assert ws_pubsub.get_cached_vm_states(r.project.id)["states"] == {"vm-0": state}
    r.db.refresh(row)
    if state != "shut_off":
        assert row.observed["cpu"] == 2 and row.observed["ram_bytes"] == 4 * 2**30
        with pytest.raises(AccountingUnavailable, match="power observation"):
            accounting.spend(r.db, "project", r.project.id, r.clock.now())
        r.db.rollback()
        enforcement.reconcile_action(r.db, run.id, r.clock.now())
        assert run.state == "issued"
        assert admission.usage(r.db, "project", r.project.id)[0].cpu == 2
        poll(r, "shut_off")
    assert accounting.spend(r.db, "project", r.project.id, r.clock.now()) == 0
    enforcement.reconcile_action(r.db, run.id, r.clock.now())
    r.db.refresh(row)
    assert run.state == "completed"
    assert row.observed["cpu"] == row.observed["ram_bytes"] == 0
    assert row.observed["projects"] == 1
    assert len(r.calls) == 1


@pytest.mark.parametrize("remaining", [0, 1])
@pytest.mark.parametrize("queued_before_save", [False, True])
def test_saved_out_deployed_vms_stay_metered_and_budget_stoppable(
    runtime, remaining, queued_before_save
):
    r = runtime
    r.host.metering_rates = {"vcpu_hour": 1, "ram_gib_hour": 0, "disk_gib_hour": 0}
    row = deploy(r, count=2)
    r.project.budget_usd = Decimal("0.01")
    assert accounting.spend(r.db, "project", r.project.id, r.clock.now()) == 0
    r.db.commit()
    run = None
    if queued_before_save:
        poll(r, "running", seconds=20)
        run = stop_run(r)
        assert run.state == "pending"
    deployed = deepcopy(r.project.deployed_topology)
    response = client.patch(
        f"/api/v1/projects/{r.project.id}",
        json={"topology": {"nodes": deployed["nodes"][:remaining], "edges": []}},
    )
    assert response.status_code == 200, response.text
    r.db.refresh(r.project)
    assert len(r.project.topology["nodes"]) == remaining
    assert r.project.deployed_topology == deployed
    poll(r, "running", seconds=20)
    assert ws_pubsub.get_cached_vm_states(r.project.id)["states"] == {
        "vm-0": "running",
        "vm-1": "running",
    }
    spend = accounting.spend(r.db, "project", r.project.id, r.clock.now())
    assert spend >= Decimal("0.01")
    run = run or stop_run(r)
    enforcement.reconcile_action(r.db, run.id, r.clock.now())
    assert sorted(r.calls, key=lambda call: call[1]["domain_name"]) == sorted(
        [
            ("/vms/stop", {"domain_name": _vm_domain_name(r.project.id, f"vm-{i}")})
            for i in range(2)
        ],
        key=lambda call: call[1]["domain_name"],
    )
    assert run.state == "issued"
    poll(r, "shut_off")
    enforcement.reconcile_action(r.db, run.id, r.clock.now())
    assert run.state == "completed"
    r.db.refresh(row)
    assert row.observed["cpu"] == row.observed["ram_bytes"] == 0


@pytest.mark.parametrize("update", ["legacy_patch", "policy_service"])
def test_disabled_budget_increase_rearms_existing_stop_through_restart(
    runtime, monkeypatch, update
):
    r = runtime
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(metering_service, "quotas_enabled", lambda: False)
    r.project.host_id = r.host.id
    r.project.deployed_topology = deepcopy(r.project.topology)
    r.project.state, r.project.budget_usd = "active", 10
    stop = Mock()
    monkeypatch.setattr("app.services.project_timer._spawn_stop", stop)
    metering_service.apply_budget(r.project, 10)
    assert r.project.budget_stopped
    stop.assert_called_once_with(r.project.id)
    r.db.commit()
    deploy_service.stop_project_async(r.project.id)
    r.db.refresh(r.project)
    assert r.project.state == "stopped"
    if update == "legacy_patch":
        response = client.patch(
            f"/api/v1/projects/{r.project.id}", json={"budget_usd": 20}
        )
        assert response.status_code == 200, response.text
        r.db.refresh(r.project)
    else:
        set_limits(
            r.db,
            r.owner,
            "project",
            r.project.id,
            Limits(spending_usd=20),
            r.clock.now(),
        )
        r.db.commit()
    assert not r.project.budget_stopped and not r.project.budget_warned
    # Real route + worker lifecycle; provider startup is the only replaced work.
    monkeypatch.setattr(deploy_service, "_start_vms_via_troshkad", lambda *a: [])
    monkeypatch.setattr(deploy_service, "_setup_pxe_via_troshkad", lambda *a: None)
    monkeypatch.setattr(deploy_service, "cache_library_images", lambda *a: [])
    response = client.post(f"/api/v1/projects/{r.project.id}/start")
    assert response.status_code == 200, response.text
    fn, args = r.queued.pop()
    fn(*args)
    r.db.refresh(r.project)
    assert r.project.state == "active"
    metering_service.apply_budget(r.project, 15)
    assert stop.call_count == 1
    metering_service.apply_budget(r.project, 25)
    metering_service.apply_budget(r.project, 26)
    assert stop.call_count == 2 and r.project.budget_stopped


def test_display_only_update_cannot_publish_trusted_observation(runtime):
    r = runtime
    ws_pubsub._check_and_notify_project_changes(
        r.project.id,
        r.project,
        None,
        {"vm": "stopped"},
        {},
        {},
        observed_at=r.clock.now(),
    )
    assert ws_pubsub.get_cached_vm_states(r.project.id) is None


def test_enabled_finite_cap_increase_leaves_latch_to_durable_reconciliation(runtime):
    r = runtime
    deploy(r)
    r.project.budget_usd = 0
    run = stop_run(r)
    r.project.budget_stopped = True
    set_limits(
        r.db, r.owner, "project", r.project.id, Limits(spending_usd=20), r.clock.now()
    )
    r.db.commit()
    assert r.project.budget_stopped
    enforcement.reconcile_action(r.db, run.id, r.clock.now())
    assert run.state == "cancelled"
    assert not r.calls
