"""Independent native completion and durable enforcement integration."""

import importlib.util
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.models.host import Host
from app.models.metering import MeteringInterval
from app.models.provider import Provider
from app.models.quota import QuotaCharge
from app.services import native_managed_power as native
from app.services import quota_accounting as accounting
from app.services import quota_enforcement as enforcement
from tests import test_quota_enforcement as enforcement_tests

NOW = enforcement_tests.NOW
actions = enforcement_tests.actions
compute = enforcement_tests.compute
ledger = enforcement_tests.ledger


@pytest.fixture
def managed(ledger):
    power = native.helpers()
    path = Path(__file__).resolve().parents[2] / "operator/tests/test_managed_power.py"
    spec = importlib.util.spec_from_file_location("managed_power_test_support", path)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    api = support.lifecycle.__wrapped__()
    db, project, user, org, _ = ledger
    provider = Provider(name="managed-native", type="kubevirt")
    db.add(provider)
    db.flush()
    host = Host(host_type="kubevirt-cluster", provider_id=provider.id)
    db.add(host)
    db.flush()
    project.host_id, project.provider_id = host.id, provider.id
    row = compute(db, project, user, org)
    root = api.values["TroshkaProject", "project"]
    root["spec"]["projectId"] = project.id
    del root["metadata"]["annotations"][power.ANNOTATION]
    root_identity = {
        "project_id": project.id,
        "cluster_key": "cluster",
        "namespace": "ns",
        "namespace_uid": "namespace",
        "project_cr_uid": "project-uid",
        "deployment_generation": 1,
    }
    identity = {
        "root": root_identity,
        "host_id": host.id,
        "provider_id": provider.id,
        "domain_id": "domain",
        "enrollment_id": "enrollment",
    }
    row.dispatch_metadata = {
        **row.dispatch_metadata,
        "kind": "deploy",
        "adapter": "kubevirt",
        "native_storage_identity": identity,
    }
    for name, uid in (("ns", "namespace"), ("kube-system", "cluster")):
        value = support.resource("Namespace", name)
        value["metadata"]["uid"] = uid
        api.values["Namespace", name] = value
    context = native.retained_context(db, project, [row])
    return power, api, context, row, support


@pytest.mark.parametrize("change", [None, "missing", "ambiguous", "generation"])
def test_expected_stop_selects_exact_episode_with_prior_history(
    managed, ledger, change
):
    from app.models.quota import QuotaAction
    from app.schemas.quota import AccountingUnavailable

    db, project, _, _, _ = ledger
    row = managed[3]
    row.observation_id = "managed-off:current-stop"
    generation = native.operations._generation(row)
    requests = ["previous-stop", "current-stop"]
    if change == "missing":
        requests = ["previous-stop", "another-stop"]
    elif change == "ambiguous":
        requests.append("current-stop")
    for index, request_id in enumerate(requests):
        db.add(
            QuotaAction(
                scope="project",
                scope_id=project.id,
                project_id=project.id,
                policy_revision=index,
                period_key="test",
                kind="stop_run",
                threshold=100,
                state="completed",
                details={
                    "managed_stop_id": request_id,
                    "managed_off_proof": {
                        "request_id": request_id,
                        "generation": "wrong" if change == "generation" else generation,
                        "inventory_digest": request_id + "-digest",
                    },
                },
            )
        )
    db.flush()
    if change is None:
        assert native.expected_stop(db, project, [row]) == (
            "current-stop",
            "current-stop-digest",
        )
    else:
        with pytest.raises(AccountingUnavailable, match="stop episode"):
            native.expected_stop(db, project, [row])


def stop(managed):
    power, api, context, _, _ = managed
    proof = native.stop_and_observe(
        api, context, "stop-token", clock=lambda: NOW + timedelta(seconds=1)
    )
    assert proof is None  # Accepted annotation alone cannot release compute.
    power.reconcile_stop(api, api.get("TroshkaProject", "ns", "project"))
    return api.get("TroshkaProject", "ns", "project")["status"]["managedPower"]


def settle(managed):
    _, api, _, _, _ = managed
    for value in api.values.values():
        if value["kind"] == "Deployment":
            value["status"]["observedGeneration"] = value["metadata"]["generation"]


def observe(managed):
    _, api, context, _, _ = managed
    return native.stop_and_observe(
        api, context, "stop-token", clock=lambda: NOW + timedelta(seconds=1)
    )


def test_annotation_ack_is_not_physical_shutdown(managed):
    power, _, _, row, _ = managed
    stop(managed)
    with pytest.raises(power.ManagedPowerError, match="controller_unsettled"):
        observe(managed)
    assert row.observed["cpu"] == 2
    settle(managed)
    assert observe(managed)["generation"] == row.dispatch_metadata["generation_id"]


@pytest.mark.parametrize(
    "phase,deleting",
    [
        ("Running", False),
        ("Pending", False),
        ("Unknown", False),
        ("Succeeded", False),
        ("Running", True),
    ],
)
def test_runtime_presence_always_blocks_compute_release(managed, phase, deleting):
    power, api, _, _, support = managed
    stop(managed)
    settle(managed)
    vm = api.get("VirtualMachine", "ns", "vm-runtime")
    vmi = support.resource(
        "VirtualMachineInstance", "runtime", vm, status={"phase": phase}
    )
    if deleting:
        vmi["metadata"]["deletionTimestamp"] = "2026-11-12T00:00:00Z"
    api.values["VirtualMachineInstance", "runtime"] = vmi
    with pytest.raises(power.ManagedPowerError, match="runtime_present"):
        observe(managed)


@pytest.mark.parametrize(
    "changed", ["uid", "generation", "namespace", "token", "topology", "extra_helper"]
)
def test_changed_identity_or_inventory_rejects_completion(managed, changed):
    power, api, _, _, support = managed
    stop(managed)
    settle(managed)
    root = api.values["TroshkaProject", "project"]
    if changed == "uid":
        root["metadata"]["uid"] = "replacement"
    elif changed == "generation":
        root["metadata"]["generation"] += 1
    elif changed == "namespace":
        api.values["Namespace", "ns"]["metadata"]["uid"] = "replacement"
    elif changed == "token":
        root["metadata"]["annotations"][power.ANNOTATION] = root["metadata"][
            "annotations"
        ][power.ANNOTATION].replace("stop-token", "other")
    elif changed == "topology":
        root["spec"]["topology"] = {"changed": True}
    else:
        value = support.resource(
            "Deployment", "exec-extra", root, {"replicas": 0}, {"observedGeneration": 1}
        )
        api.values["Deployment", "exec-extra"] = value
    with pytest.raises(power.ManagedPowerError):
        observe(managed)


def test_storage_is_retained_through_shutdown_and_proof(managed):
    _, api, _, _, support = managed
    root = api.get("TroshkaProject", "ns", "project")
    pvc = support.resource(
        "PersistentVolumeClaim", "disk", root, {"volumeName": "pv"}, {"phase": "Bound"}
    )
    pv = support.resource(
        "PersistentVolume",
        "pv",
        spec={
            "claimRef": {k: pvc["metadata"][k] for k in ("name", "namespace", "uid")}
        },
    )
    api.values["PersistentVolumeClaim", "disk"] = deepcopy(pvc)
    api.values["PersistentVolume", "pv"] = deepcopy(pv)
    stop(managed)
    settle(managed)
    assert observe(managed)
    assert api.values["PersistentVolumeClaim", "disk"] == pvc
    assert api.values["PersistentVolume", "pv"] == pv
    assert all(
        key[0]
        not in {
            "PersistentVolumeClaim",
            "PersistentVolume",
            "DataVolume",
            "TroshkaNetwork",
        }
        for _, key, _ in api.calls
    )


@pytest.mark.parametrize("container_only", [False, True])
def test_enforcement_releases_only_compute_after_full_proof(
    ledger, managed, monkeypatch, container_only
):
    _, api, _, row, support = managed
    notify = Mock()
    monkeypatch.setattr("app.services.ws_pubsub.notify_project", notify)
    db, project, _, _, _ = ledger
    if container_only:
        del api.values["VirtualMachine", "vm-runtime"]
        del api.values["TroshkaVM", "vm"]
        support.add_container(api)
    meters = []
    for kind in ("vcpu", "ram", "disk"):
        meter = MeteringInterval(
            project_id=project.id,
            kind=kind,
            resource_id=kind,
            qty=1,
            unit_rate=1,
            provider_type="kubevirt",
            host_id=project.host_id,
            started_at=NOW - timedelta(minutes=1),
            quota_attribution=accounting.attribution(db, project),
        )
        db.add(meter)
        meters.append(meter)
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    db.commit()
    module = native.helpers()

    def execute(provider, context, token):
        assert not db.in_transaction()
        assert db.get_bind() is not None
        assert (
            native.stop_and_observe(
                api, context, token, clock=lambda: NOW + timedelta(seconds=1)
            )
            is None
        )
        module.reconcile_stop(api, api.get("TroshkaProject", "ns", "project"))
        settle(managed)
        for key in [k for k in api.values if k[0] == "Pod"]:
            del api.values[key]  # Graceful deletion finishes independently.
        return native.stop_and_observe(
            api, context, token, clock=lambda: NOW + timedelta(seconds=1)
        )

    monkeypatch.setattr(native, "execute", execute)
    monkeypatch.setattr(enforcement, "_now", lambda: NOW + timedelta(seconds=2))
    enforcement.reconcile_action(db, run.id, NOW)
    db.refresh(run)
    db.refresh(row)
    assert run.state == "completed"
    assert row.observed == {
        "cpu": 0,
        "ram_bytes": 0,
        "storage_bytes": 10,
        "projects": 1,
    }
    assert (
        run.details["managed_off_proof"]["request_id"] == run.details["managed_stop_id"]
    )
    assert project.state == "stopped"
    assert notify.call_args.args[1]["state"] == "stopped"
    for meter in meters:
        db.refresh(meter)
        assert (meter.ended_at is None) == (meter.kind == "disk")
    before = sum(
        c.amount
        for c in db.query(QuotaCharge)
        .filter(QuotaCharge.kind.in_(("vcpu", "ram")))
        .all()
    )
    accounting.accrue(db, project.id, NOW + timedelta(hours=1))
    after = sum(
        c.amount
        for c in db.query(QuotaCharge)
        .filter(QuotaCharge.kind.in_(("vcpu", "ram")))
        .all()
    )
    assert before == after and before > 0


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_stop_episode_stable_across_retry_and_rearmed_on_reopen(
    ledger, managed, terminal
):
    db, project, _, _, _ = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    assert enforcement._prepare_stop(db, run, NOW)
    first = run.details["managed_stop_id"]
    attempt = run.operation_id
    assert enforcement._prepare_stop(db, run, NOW + timedelta(seconds=31))
    assert run.details["managed_stop_id"] == first and run.operation_id != attempt
    assert run.attempts == 2
    assert run.next_attempt_at == NOW + timedelta(seconds=91)
    run.state = terminal
    enforcement._ensure_run(db, project, [], NOW + timedelta(seconds=32))
    assert run.attempts == 0
    assert enforcement._prepare_stop(db, run, NOW + timedelta(seconds=33))
    assert run.details["managed_stop_id"] != first
    assert run.attempts == 1
    assert run.next_attempt_at == NOW + timedelta(seconds=63)


@pytest.mark.parametrize("state", ["completed", "cancelled", "issued"])
@pytest.mark.parametrize("has_compute", [True, False])
def test_explicit_stop_resets_attempts_only_for_new_episode(
    ledger, managed, monkeypatch, state, has_compute
):
    db, project, _, _, _ = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    assert enforcement._prepare_stop(db, run, NOW)
    original_stop = run.details["managed_stop_id"]
    run.state, run.attempts = state, 4
    deadline = run.next_attempt_at
    monkeypatch.setattr(enforcement, "_has_compute", lambda *args: has_compute)
    enforcement.request_native_stop(db, project)
    new_episode = state in {"completed", "cancelled"} and has_compute
    assert run.attempts == (0 if new_episode else 4)
    assert run.details.get("managed_stop_id") == (
        None if new_episode else original_stop
    )
    if state == "issued":
        assert accounting.utc(run.next_attempt_at) == accounting.utc(deadline)


def test_policy_correction_before_first_dispatch_cancels(ledger, managed):
    db, project, _, _, _ = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    project.budget_usd = None
    assert not enforcement._prepare_stop(db, run, NOW)
    assert run.state == "cancelled" and "managed_stop_id" not in run.details


def test_policy_correction_keeps_already_issued_stop_fenced(ledger, managed):
    db, project, _, _, _ = ledger
    project.budget_usd = 0
    enforcement.evaluate(db, NOW)
    run = actions(db, project, "stop_run")[0]
    assert enforcement._prepare_stop(db, run, NOW)
    token = run.details["managed_stop_id"]
    project.budget_usd = None
    db.commit()
    assert enforcement.fence_action(db, run.id, NOW)
    assert run.details["managed_stop_id"] == token
    assert run.state == "issued"


def test_stale_complete_observation_fails_closed(managed):
    power, api, context, _, _ = managed
    stop(managed)
    settle(managed)
    times = iter((NOW, NOW + timedelta(seconds=61)))
    with pytest.raises(power.ManagedPowerError, match="observation_stale"):
        native.stop_and_observe(api, context, "stop-token", clock=lambda: next(times))


def test_replaced_storage_identity_blocks_completion(managed):
    power, api, _, _, support = managed
    root = api.get("TroshkaProject", "ns", "project")
    dv = support.resource("DataVolume", "retained", root, status={"phase": "Succeeded"})
    api.values["DataVolume", "retained"] = dv
    stop(managed)
    settle(managed)
    api.values["DataVolume", "retained"]["metadata"]["uid"] = "replacement"
    with pytest.raises(power.ManagedPowerError, match="retained_identity_changed"):
        observe(managed)
