"""Fresh capacity imports run only against isolated fixture databases."""

from copy import deepcopy
from datetime import timedelta

import pytest

from app.models.project import Project
from app.models.quota import (
    QuotaAllocation,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.schemas.quota import AccountingUnavailable, Resources
from app.services import quota_accounting as accounting
from app.services import quota_baseline as legacy
from app.services import quota_capacity_baseline as capacity
from app.services import quota_native as native
from tests.test_quota_backfill import (
    NOW,
    _stamp_schema,
    cli,
    observed_resources,
    snapshot,
)
from tests.test_quota_backfill import (
    sample as sample_fixture,
)
from tests.test_quota_concurrency import postgres as postgres_fixture
from tests.test_quota_native_storage import (
    NOW as NATIVE_NOW,
)
from tests.test_quota_native_storage import (
    ingest,
    revised,
)
from tests.test_quota_native_storage import (
    native_case as native_case_fixture,
)
from tests.test_quota_native_storage import (
    native_ledger as native_ledger_fixture,
)
from tests.test_quota_storage import evidence as storage_evidence

sample = sample_fixture
native_case = native_case_fixture
native_ledger = native_ledger_fixture
postgres = postgres_fixture


def fresh(db, now=NOW):
    db.add(
        QuotaReadiness(
            id="unified-groups-v1",
            ready=False,
            details={
                "accounting_started_at": (now - timedelta(minutes=10)).isoformat(),
                "digest": "immutable-conversion-receipt",
            },
        )
    )
    db.commit()


def reviewed(db, projects, now=NOW):
    source = capacity.inventory(db)
    entries = {}
    for item in source["projects"]:
        if item["empty"]:
            continue
        if item.get("adapter") != "nested":
            continue
        project = next(p for p in projects if p.id == item["project_id"])
        physical = storage_evidence(
            project, generation=item["generation_id"], amount=30 * 2**30
        )
        physical["observed_at"] = (now - timedelta(seconds=1)).isoformat()
        physical["files"][0]["path"] = next(iter(observed_resources(project)["disks"]))
        entries[project.id] = {
            "adapter": "nested",
            "generation_id": item["generation_id"],
            "worker_fenced_at": (now - timedelta(seconds=4)).isoformat(),
            "jobs_checked_at": (now - timedelta(seconds=3)).isoformat(),
            "jobs": [],
            "compute_observation": {
                "host_id": project.host_id,
                "source": "independent-virsh-inventory",
                "observation_id": "compute:" + project.id,
                "observed_at": now.isoformat(),
                "resources": observed_resources(project),
                "states": {"vm": "running" if project.state == "active" else "shutoff"},
            },
            "storage_observation": physical,
        }
    return {
        "version": capacity.VERSION,
        "inventory_digest": source["inventory_digest"],
        "fresh_receipt": source["fresh_receipt"],
        "maintenance": {
            "writers_stopped": True,
            "since": (now - timedelta(minutes=1)).isoformat(),
            "evidence": "fixture independently fenced all Troshka writers and enumerated jobs",
        },
        "projects": entries,
    }


def test_fresh_capacity_dryrun_rehearses_above_policy_without_history(sample):
    db, projects, *_ = sample
    fresh(db)
    db.add(
        QuotaPolicy(
            scope="organization",
            scope_id=projects[0].owner.organization_id,
            storage_bytes=20 * 2**30,
        )
    )
    db.commit()
    manifest = reviewed(db, projects)
    before = snapshot(db)
    result = capacity.run(db, evidence=manifest, now=NOW)
    assert result["capacity_ready"]
    assert result["allocation_count"] == 2
    assert result["overages"][0]["used"] == 60 * 2**30
    assert snapshot(db) == before


def managed_inventory(project, identity, generation):
    """A complete sanitized stopped recording, evaluated by real power validators."""
    import json

    from app.services.native_managed_power import helpers

    power = helpers()
    root_identity = identity["root"]
    namespace = root_identity["namespace"]
    root = {
        "kind": "TroshkaProject",
        "apiVersion": power.KINDS["TroshkaProject"][0],
        "metadata": {
            "name": "project",
            "namespace": namespace,
            "uid": root_identity["project_cr_uid"],
            "generation": root_identity["deployment_generation"],
        },
        "spec": {"projectId": project.id, "topology": project.topology or {}},
        "status": {"phase": "Running"},
    }
    request = {
        "version": 1,
        "mode": "stopped",
        "requestId": "reviewed-native-off",
        "allocationGeneration": generation,
    }
    root["metadata"]["annotations"] = {power.ANNOTATION: json.dumps(request)}
    supplied = {kind: [] for kind in power.KINDS}
    supplied["TroshkaProject"] = [root]
    supplied["Namespace"] = [
        {
            "kind": "Namespace",
            "apiVersion": "v1",
            "metadata": {"name": name, "uid": uid},
        }
        for name, uid in (
            (namespace, root_identity["namespace_uid"]),
            ("kube-system", root_identity["cluster_key"]),
        )
    ]
    # Helpers exist with no CPU/RAM limits: infrastructure overhead does not
    # belong to configured guest quotas; complete off still covers each helper.
    for prefix in ("dnsmasq", "gateway", "exec", "vnc"):
        supplied["Deployment"].append(
            {
                "kind": "Deployment",
                "apiVersion": "apps/v1",
                "metadata": {
                    "name": prefix + "-project",
                    "namespace": namespace,
                    "uid": prefix + "-" + project.id,
                    "generation": 1,
                    "ownerReferences": [
                        {
                            "apiVersion": root["apiVersion"],
                            "kind": root["kind"],
                            "uid": root["metadata"]["uid"],
                            "name": "project",
                            "controller": True,
                        }
                    ],
                },
                "spec": {"replicas": 0},
                "status": {"observedGeneration": 1},
            }
        )
    rows = {kind: supplied[kind] for kind in power.INVENTORY}
    state = power.snapshot(root, rows, {}, request)
    state.update(producerDrained=True, state="off")
    root["status"]["managedPower"] = state
    return supplied


def native_reviewed(db, now=NATIVE_NOW):
    source = capacity.inventory(db)
    entries = {}
    for item in source["projects"]:
        project = db.get(Project, item["project_id"])
        generation = item["generation_id"]
        identity = item["native_storage_identity"]
        measurement = native.storage_measurement(db, project, generation, identity, now)
        entries[project.id] = {
            "adapter": native.ADAPTER,
            "generation_id": generation,
            "worker_fenced_at": (
                measurement.started_at - timedelta(seconds=2)
            ).isoformat(),
            "jobs_checked_at": (
                measurement.started_at - timedelta(seconds=1)
            ).isoformat(),
            "jobs": [],
            "compute_observation": {
                "host_id": project.host_id,
                "source": "independent-managed-inventory",
                "observation_id": "complete-managed-off:" + project.id,
                "observed_at": now.isoformat(),
                "started_at": (now - timedelta(seconds=1)).isoformat(),
                "request_id": "reviewed-native-off",
                "managed_inventory": managed_inventory(project, identity, generation),
            },
            "storage_observation": {
                "identity": identity,
                "measurement": measurement.evidence(),
            },
        }
    return {
        "version": capacity.VERSION,
        "inventory_digest": source["inventory_digest"],
        "fresh_receipt": source["fresh_receipt"],
        "maintenance": {
            "writers_stopped": True,
            "since": (now - timedelta(minutes=1)).isoformat(),
            "evidence": "all Troshka producers fenced; complete original job inventory",
        },
        "projects": entries,
    }


@pytest.fixture
def fresh_native(native_ledger, monkeypatch):
    q = native_ledger
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    ingest(q)
    with q.sessions() as db:
        fresh(db, NATIVE_NOW)
    return q


def test_native_existing_ids_above_limits_and_capacity_only(fresh_native):
    q = fresh_native
    with q.sessions() as db:
        db.add(QuotaPolicy(scope="organization", scope_id=q.org_id, storage_bytes=20))
        db.commit()
        from app.models.kubevirt_storage import NativeRetainedBinding

        bindings = capacity.inventory(db)["native_retained_bindings"]
        assert len(bindings) == db.query(NativeRetainedBinding).count() > 0
        assert all(
            item["coverage"] in {"project", "platform_cache"} for item in bindings
        )
        manifest = native_reviewed(db)
        before = {
            row.operation_id: (
                row.id,
                deepcopy(row.requested),
                deepcopy(row.dispatch_metadata),
            )
            for row in db.query(QuotaReservation)
        }
        protected = capacity.preservation(db)
        result = capacity.run(db, evidence=manifest, now=NATIVE_NOW, apply=True)
        assert result["capacity_ready"], result
        assert not result["runtime_ready"] and not result["ready"]
        assert not result["accounting_ready"] and not result["enforcement_enabled"]
        assert result["overages"][0]["used"] == 80
        assert capacity.preservation(db) == protected
        for row in db.query(QuotaReservation):
            original = before[row.operation_id]
            assert (row.id, row.requested) == original[:2]
            assert all(
                row.dispatch_metadata[key] == value
                for key, value in original[2].items()
            )
            assert row.observed["cpu"] == row.observed["ram_bytes"] == 0
            assert row.observed["projects"] == 1
            assert (
                row.dispatch_metadata["storage_version"] == native.MEASUREMENT_VERSION
            )
        with pytest.raises(AccountingUnavailable):
            accounting.require_ready(db)


def test_native_bootstrap_uses_proposed_generation_without_dispatch(fresh_native):
    q = fresh_native
    with q.sessions() as db:
        db.query(QuotaAllocation).delete()
        db.query(QuotaReservation).delete()
        db.commit()
        manifest = native_reviewed(db)
        result = capacity.run(db, evidence=manifest, now=NATIVE_NOW, apply=True)
        assert result["capacity_ready"], result
        assert db.query(QuotaReservation).count() == 2
        for row in db.query(QuotaReservation):
            assert row.operation_id.startswith("fresh-capacity:" + row.project_id)
            assert row.requested == row.observed
            assert row.dispatch_metadata["known_empty"] is False
            assert "calls" not in row.dispatch_metadata


@pytest.mark.parametrize(
    "failure",
    [
        "active",
        "missing_helper",
        "runtime",
        "latest_partial",
        "owner",
        "snapshot",
        "unknown_adapter",
    ],
)
def test_native_unverified_capacity_preserves_every_row(fresh_native, failure):
    q = fresh_native
    with q.sessions() as db:
        manifest = native_reviewed(db)
        first = manifest["projects"][q.project_ids[0]]
        if failure == "active":
            del first["compute_observation"]["managed_inventory"]
        elif failure == "missing_helper":
            first["compute_observation"]["managed_inventory"]["Deployment"].pop()
        elif failure == "runtime":
            first["compute_observation"]["managed_inventory"]["Deployment"][0]["spec"][
                "replicas"
            ] = 1
        elif failure == "unknown_adapter":
            first["adapter"] = "unknown"
        elif failure == "owner":
            db.get(Project, q.project_ids[0]).owner_id = q.admin_id
            db.commit()
        else:
            from app.schemas.kubevirt_storage import Blocker
            from tests.kubevirt_storage_helpers import with_retained_snapshot

            report = (
                revised(with_retained_snapshot(q.report))
                if failure == "snapshot"
                else revised(
                    q.report,
                    started_at=q.report.started_at + timedelta(seconds=1),
                    completed_at=q.report.completed_at + timedelta(seconds=1),
                    collection_errors=[
                        Blocker(code="backend_graph_recheck_unavailable")
                    ],
                )
            )
            ingest(q, report)
        before = snapshot(db)
        result = capacity.run(db, evidence=manifest, now=NATIVE_NOW, apply=True)
        assert not result["capacity_ready"], result
        assert snapshot(db) == before


def test_fresh_apply_replays_without_any_changes(sample):
    db, projects, *_ = sample
    fresh(db)
    manifest = reviewed(db, projects)
    result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
    assert result["capacity_ready"]
    before = snapshot(db)
    assert capacity.run(db, evidence=manifest, now=NOW, apply=True) == result
    assert snapshot(db) == before
    rows = db.query(QuotaReservation).all()
    assert len(rows) == 2
    assert db.query(QuotaAllocation).count() == 2
    assert {r.organization_id for r in rows} == {projects[0].owner.organization_id}


@pytest.mark.parametrize(
    "failure", ["omitted", "stale", "unknown", "storage_override", "jobs", "digest"]
)
def test_failure_rolls_back_entire_inventory(sample, failure):
    db, projects, *_ = sample
    fresh(db)
    manifest = reviewed(db, projects)
    first = manifest["projects"][projects[0].id]
    if failure == "omitted":
        del manifest["projects"][projects[1].id]
    elif failure == "stale":
        first["compute_observation"]["observed_at"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
    elif failure == "unknown":
        first["compute_observation"]["cpu_override"] = 999
    elif failure == "storage_override":
        first["storage_observation"]["files"][0]["rate"] = 999
    elif failure == "jobs":
        first["jobs"] = [{"id": "unresolved", "status": "unknown"}]
    else:
        manifest["inventory_digest"] = "changed"
    before = snapshot(db)
    result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
    assert not result["capacity_ready"]
    assert result["blockers"]
    assert snapshot(db) == before


def test_import_never_enters_history_dispatch_or_money_paths(sample, monkeypatch):
    from app.services import quota_admission as admission

    db, projects, *_ = sample
    fresh(db)
    manifest = reviewed(db, projects)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Historical import, admission or accrual entered during capacity baseline"
        )

    for name in (
        "_history",
        "_cutover",
        "_default_identity",
        "_import_live",
        "_live_snapshot",
    ):
        monkeypatch.setattr(legacy, name, forbidden)
    monkeypatch.setattr(accounting, "accrue", forbidden)
    monkeypatch.setattr(accounting, "spend", forbidden)
    monkeypatch.setattr(admission, "reserve", forbidden)
    result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
    assert result["capacity_ready"], result


def test_existing_deploy_start_pair_and_released_history_are_preserved(sample):
    db, projects, owner, org, *_ = sample
    fresh(db)
    assert capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)[
        "capacity_ready"
    ]
    root = db.query(QuotaReservation).filter_by(project_id=projects[0].id).one()
    root_allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=root.operation_id).one()
    )
    compute = {**root.observed, "storage_bytes": 0, "projects": 0}
    root.observed = root_allocation.observed = {
        **root.observed,
        "cpu": 0,
        "ram_bytes": 0,
    }
    start = QuotaReservation(
        operation_id="retained-original-start",
        project_id=root.project_id,
        owner_id=owner.id,
        organization_id=org.id,
        requested=compute,
        observed=compute,
        observation_id="prior-start-observation",
        state="confirmed",
        dispatch_metadata={
            **root.dispatch_metadata,
            "kind": "start",
            "queue_job_id": "preserved-queue-id",
        },
    )
    old = QuotaReservation(
        operation_id="released-old-generation",
        project_id=root.project_id,
        owner_id=owner.id,
        organization_id=org.id,
        requested=compute,
        observed=Resources().model_dump(),
        state="released",
        dispatch_metadata={"kind": "deploy", "generation_id": "old"},
    )
    db.add_all(
        [
            start,
            old,
            QuotaAllocation(
                operation_id=start.operation_id,
                project_id=root.project_id,
                owner_id=owner.id,
                organization_id=org.id,
                requested=compute,
                observed=compute,
                observation_id=start.observation_id,
                observed_at=NOW,
                state="active",
            ),
        ]
    )
    db.commit()
    ids = {
        row.operation_id: (
            row.id,
            deepcopy(row.requested),
            row.dispatch_metadata.get("queue_job_id"),
        )
        for row in db.query(QuotaReservation)
    }
    manifest = reviewed(db, projects, NOW + timedelta(seconds=2))
    for entry in manifest["projects"].values():
        entry["compute_observation"]["observation_id"] += ":later"
    result = capacity.run(
        db, evidence=manifest, now=NOW + timedelta(seconds=2), apply=True
    )
    assert result["capacity_ready"], result
    assert {
        row.operation_id: (
            row.id,
            row.requested,
            row.dispatch_metadata.get("queue_job_id"),
        )
        for row in db.query(QuotaReservation)
    } == ids
    assert len(result["coverage"][projects[0].id]["operation_ids"]) == 2
    assert db.get(QuotaReservation, old.id).state == "released"


@pytest.mark.parametrize(
    "failure",
    [
        "same_observation",
        "pool",
        "growth",
        "pairing",
        "attribution",
        "generation",
        "orphan",
    ],
)
def test_retained_ambiguity_is_never_repaired_by_baseline(sample, failure):
    from uuid import uuid4

    db, projects, *_ = sample
    fresh(db)
    assert capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)[
        "capacity_ready"
    ]
    row = db.query(QuotaReservation).filter_by(project_id=projects[0].id).one()
    allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
    )
    later = NOW + timedelta(seconds=2)
    if failure == "growth":
        row.observed = allocation.observed = {**row.observed, "cpu": 0}
    elif failure == "pairing":
        db.delete(allocation)
    elif failure == "attribution":
        row.organization_id = allocation.organization_id = str(uuid4())
    elif failure == "generation":
        db.add(
            QuotaReservation(
                operation_id="unsettled-other-generation",
                project_id=row.project_id,
                owner_id=row.owner_id,
                organization_id=row.organization_id,
                requested=Resources(cpu=1).model_dump(),
                observed=Resources().model_dump(),
                state="pending",
                dispatch_metadata={"generation_id": "different"},
            )
        )
    elif failure == "orphan":
        db.add(
            QuotaAllocation(
                operation_id="deleted-project-allocation",
                project_id=str(uuid4()),
                owner_id=row.owner_id,
                organization_id=row.organization_id,
                requested=Resources(storage_bytes=1).model_dump(),
                observed=Resources(storage_bytes=1).model_dump(),
                state="stopped",
            )
        )
    db.commit()
    manifest = reviewed(db, projects, later)
    first = manifest["projects"][projects[0].id]
    if failure == "same_observation":
        first["storage_observation"]["files"][0]["allocated_bytes"] += 1
    elif failure == "pool":
        physical = first["storage_observation"]
        physical["writable_pool"] = physical["pools"][0]["id"] = "different-pool"
        physical["files"][0]["device"] = "different-pool"
    if failure != "same_observation":
        for entry in manifest["projects"].values():
            entry["compute_observation"]["observation_id"] += ":later"
    before = snapshot(db)
    result = capacity.run(db, evidence=manifest, now=later, apply=True)
    assert not result["capacity_ready"], result
    assert snapshot(db) == before


def test_source_drift_rejects_reviewed_owner_org_topology_and_rates(sample):
    db, projects, *_ = sample
    fresh(db)
    manifest = reviewed(db, projects)
    projects[0].budget_usd = 999
    db.commit()
    before = snapshot(db)
    result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
    assert (
        not result["capacity_ready"]
        and "inventory changed" in result["blockers"][0]["message"]
    )
    assert snapshot(db) == before


def test_native_newer_review_reconciles_original_operation_ids(fresh_native):
    q = fresh_native
    with q.sessions() as db:
        assert capacity.run(
            db, evidence=native_reviewed(db), now=NATIVE_NOW, apply=True
        )["capacity_ready"]
        ids = {r.operation_id: r.id for r in db.query(QuotaReservation)}
    later = revised(
        q.report,
        started_at=q.report.started_at + timedelta(seconds=1),
        completed_at=q.report.completed_at + timedelta(seconds=1),
        images=[
            image.model_copy(
                update={
                    "collected_at": image.collected_at + timedelta(seconds=1),
                    "allocated_bytes": 40
                    if image.key.image_id == "child-1"
                    else image.allocated_bytes,
                }
            )
            for image in q.report.images
        ],
    )
    ingest(q, later)
    with q.sessions() as db:
        manifest = native_reviewed(db, NATIVE_NOW + timedelta(seconds=2))
        for entry in manifest["projects"].values():
            entry["compute_observation"]["observation_id"] += ":later"
        result = capacity.run(
            db, evidence=manifest, now=NATIVE_NOW + timedelta(seconds=2), apply=True
        )
        assert result["capacity_ready"], result
        assert {r.operation_id: r.id for r in db.query(QuotaReservation)} == ids
        first = db.query(QuotaReservation).filter_by(project_id=q.project_ids[0]).one()
        assert first.observed["storage_bytes"] == 40


def test_mixed_native_and_nested_inventory_has_exact_scope_totals(fresh_native):
    from app.models.host import Host
    from app.models.provider import Provider
    from tests.test_quota_backfill import topology

    q = fresh_native
    with q.sessions() as db:
        provider = Provider(name="nested", type="aws")
        db.add(provider)
        db.flush()
        host = Host(provider_id=provider.id, billing_mode="shared", state="ready")
        db.add(host)
        db.flush()
        nested = [
            Project(
                name="nested-" + state,
                owner_id=q.owner_id,
                host_id=host.id,
                topology=topology(),
                deployed_topology=topology(),
                state=state,
            )
            for state in ("active", "stopped")
        ]
        db.add_all(nested)
        db.commit()
        manifest = native_reviewed_mixed(db, nested)
        result = capacity.run(db, evidence=manifest, now=NATIVE_NOW, apply=True)
        assert result["capacity_ready"], result
        assert (
            result["scopes"]["organization:" + q.org_id]["used"]
            == Resources(
                cpu=2,
                ram_bytes=4 * 2**30,
                projects=4,
                storage_bytes=60 * 2**30 + 80,
            ).model_dump()
        )


def native_reviewed_mixed(db, nested):
    # Build native evidence independently without pretending nested projects are
    # enrolled roots; combine the reviewed adapters under one source digest.
    source = capacity.inventory(db)
    entries = {}
    for item in source["projects"]:
        if item["adapter"] != native.ADAPTER:
            continue
        project = db.get(Project, item["project_id"])
        identity = item["native_storage_identity"]
        measurement = native.storage_measurement(
            db, project, item["generation_id"], identity, NATIVE_NOW
        )
        generation = item["generation_id"]
        entries[project.id] = {
            "adapter": native.ADAPTER,
            "generation_id": generation,
            "worker_fenced_at": (
                measurement.started_at - timedelta(seconds=2)
            ).isoformat(),
            "jobs_checked_at": (
                measurement.started_at - timedelta(seconds=1)
            ).isoformat(),
            "jobs": [],
            "compute_observation": {
                "host_id": project.host_id,
                "source": "managed-independent",
                "observation_id": "mixed-off:" + project.id,
                "observed_at": NATIVE_NOW.isoformat(),
                "started_at": (NATIVE_NOW - timedelta(seconds=1)).isoformat(),
                "request_id": "reviewed-native-off",
                "managed_inventory": managed_inventory(project, identity, generation),
            },
            "storage_observation": {
                "identity": identity,
                "measurement": measurement.evidence(),
            },
        }
    manifest = reviewed(db, nested, NATIVE_NOW)
    manifest["projects"].update(entries)
    return manifest


def test_fresh_cli_selection_preflight_and_default_rollback(
    sample, monkeypatch, capsys, tmp_path
):
    import json

    db, projects, *_ = sample
    fresh(db)
    _stamp_schema(db)
    module = cli()
    monkeypatch.setattr(module, "database_engine", lambda _: db.get_bind())
    monkeypatch.setattr(db.get_bind(), "dispose", lambda: None)
    assert module.main(["--database-url", "sqlite://", "--inventory"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["baseline_kind_required"]
        == "fresh-capacity"
    )
    assert (
        module.main(
            [
                "--database-url",
                "sqlite://",
                "--baseline-kind",
                "fresh-capacity",
                "--inventory",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["version"] == capacity.VERSION
    before = snapshot(db)
    report = module.preflight(db)
    assert report["baseline_kind_required"] == "fresh-capacity"
    assert report["baseline_version_required"] == capacity.VERSION
    assert snapshot(db) == before
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(reviewed(db, projects)))
    assert (
        module.main(
            [
                "--database-url",
                "sqlite://",
                "--baseline-kind",
                "fresh-capacity",
                "--evidence",
                str(path),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert snapshot(db) == before


def test_fresh_cli_schema_gate_precedes_orm(monkeypatch, capsys):
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    module = cli()
    monkeypatch.setattr(module, "database_engine", lambda _: engine)
    monkeypatch.setattr(
        module, "_backend", lambda: pytest.fail("Old schema must precede ORM loading")
    )
    assert (
        module.main(
            [
                "--database-url",
                "sqlite://",
                "--baseline-kind",
                "fresh-capacity",
                "--inventory",
            ]
        )
        == 2
    )
    assert '"schema"' in capsys.readouterr().out


def test_cli_recognizes_native_measurement_by_adapter(fresh_native):
    q = fresh_native
    with q.sessions() as db:
        assert capacity.run(
            db, evidence=native_reviewed(db), now=NATIVE_NOW, apply=True
        )["capacity_ready"]
        module = cli()
        assert module._retained_operations(db, fresh=True, now=NATIVE_NOW) == []
        row = db.query(QuotaReservation).first()
        row.dispatch_metadata = {**row.dispatch_metadata, "adapter": "unrecognized"}
        db.commit()
        assert len(module._retained_operations(db, fresh=True, now=NATIVE_NOW)) == 1


def test_current_billing_is_validated_without_accruing_old_history(sample, monkeypatch):
    from datetime import datetime

    from app.models.metering import MeteringInterval

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    db, projects, *_ = sample
    fresh(db)
    for interval in db.query(MeteringInterval):
        interval.started_at = NOW - timedelta(minutes=5)
        interval.quota_attribution = accounting.attribution(
            db, db.get(Project, interval.project_id)
        )
    db.commit()
    before = capacity.preservation(db)
    result = capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)
    assert (
        result["capacity_ready"]
        and result["accounting_ready"]
        and result["runtime_ready"]
    ), result
    assert capacity.preservation(db) == before
    monkeypatch.setattr(capacity, "datetime", FixedDatetime)
    assert legacy.check_ready(db)["ready"]
    accounting.require_ready(db)


def test_exact_replay_does_not_make_old_native_observation_current(
    fresh_native, monkeypatch
):
    from datetime import datetime

    class StaleDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NATIVE_NOW + timedelta(minutes=16)

    q = fresh_native
    with q.sessions() as db:
        manifest = native_reviewed(db)
        original = capacity.run(db, evidence=manifest, now=NATIVE_NOW, apply=True)
        before = snapshot(db)
        assert (
            capacity.run(
                db,
                evidence=manifest,
                now=NATIVE_NOW + timedelta(minutes=16),
                apply=True,
            )
            == original
        )
        assert snapshot(db) == before
        monkeypatch.setattr(capacity, "datetime", StaleDatetime)
        gate = capacity.check_ready(db)
        assert not gate["capacity_ready"] and not gate["ready"]
        assert "stale" in gate["message"]


def test_same_time_nested_storage_conflict_cannot_be_accepted_with_new_compute_id(
    sample,
):
    db, projects, *_ = sample
    fresh(db)
    original = reviewed(db, projects)
    assert capacity.run(db, evidence=original, now=NOW, apply=True)["capacity_ready"]
    later = reviewed(db, projects, NOW + timedelta(seconds=2))
    for project_id, entry in later["projects"].items():
        entry["compute_observation"]["observation_id"] += ":new-compute"
        entry["storage_observation"] = deepcopy(
            original["projects"][project_id]["storage_observation"]
        )
    later["projects"][projects[0].id]["storage_observation"]["files"][0][
        "allocated_bytes"
    ] += 4096
    before = snapshot(db)
    result = capacity.run(
        db, evidence=later, now=NOW + timedelta(seconds=2), apply=True
    )
    assert not result["capacity_ready"], result
    assert "Skipped physical measurement differs" in result["blockers"][0]["message"]
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "kind", ["open_source", "pending_operation", "retained_binding"]
)
def test_orphan_work_is_explicit_inventory_and_blocks_atomic_import(sample, kind):
    from uuid import uuid4

    from app.models.kubevirt_storage import NativeRetainedBinding
    from app.models.metering import MeteringInterval

    db, projects, owner, org, *_ = sample
    fresh(db)
    deleted = str(uuid4())
    if kind == "open_source":
        db.add(
            MeteringInterval(
                project_id=deleted,
                kind="disk",
                resource_id="orphan-disk",
                qty=1,
                unit_rate="0.01",
                started_at=NOW - timedelta(minutes=2),
            )
        )
        field = "open_sources"
    elif kind == "pending_operation":
        db.add(
            QuotaReservation(
                operation_id="orphan-pending",
                project_id=deleted,
                owner_id=owner.id,
                organization_id=org.id,
                state="pending",
                requested=Resources(cpu=2).model_dump(),
                observed=Resources().model_dump(),
                dispatch_metadata={"generation_id": "orphan-pending"},
            )
        )
        field = "retained_operations"
    else:
        db.add(
            NativeRetainedBinding(
                key="orphan-binding",
                root_key="missing-root",
                domain_id=str(uuid4()),
                payload={"retained": "unknown-owner"},
            )
        )
        field = "native_retained_bindings"
    db.commit()
    source = capacity.inventory(db)
    assert source[field]
    manifest = reviewed(db, projects)
    before = snapshot(db)
    result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
    assert not result["capacity_ready"], result
    assert snapshot(db) == before


def test_enabled_readiness_uses_current_storage_after_accrual_and_new_reservation(
    sample, monkeypatch, capsys
):
    from datetime import datetime

    from app.models.metering import MeteringInterval
    from app.models.quota import QuotaCharge
    from app.services import quota_admission as admission
    from app.services import quota_operations as operations
    from app.services import quota_storage as storage

    class RuntimeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=2)

    db, projects, *_ = sample
    fresh(db)
    for interval in db.query(MeteringInterval):
        interval.started_at = NOW - timedelta(minutes=5)
        interval.quota_attribution = accounting.attribution(
            db, db.get(Project, interval.project_id)
        )
    db.commit()
    manifest = reviewed(db, projects)
    assert capacity.run(db, evidence=manifest, now=NOW, apply=True)["ready"]
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(capacity, "datetime", RuntimeDatetime)
    for project in projects[:2]:
        rows = operations.current_generation(operations._rows(db, project.id))
        current = deepcopy(manifest["projects"][project.id]["storage_observation"])
        current["observed_at"] = RuntimeDatetime.now().isoformat()
        current["observation_id"] += ":current"
        storage.apply_measurement(db, project, rows, current, RuntimeDatetime.now())
        accounting.accrue(db, project.id, RuntimeDatetime.now())
    projects[0].name = "renamed-after-baseline"
    db.commit()
    assert db.query(QuotaCharge).count() > 0
    assert legacy.check_ready(db)["ready"]
    accounting.require_ready(db)
    # Charges/cursors above were written by real accrual; admission can read
    # those durable charges without asking a provider from this offline test.
    monkeypatch.setattr(accounting, "refresh", lambda *args: None)
    admission.reserve(
        db,
        projects[2].id,
        "ordinary-next-reservation",
        Resources(cpu=1),
        RuntimeDatetime.now(),
    )
    db.commit()
    accounting.require_ready(db)
    _stamp_schema(db)
    module = cli()
    monkeypatch.setattr(module, "database_engine", lambda _: db.get_bind())
    monkeypatch.setattr(db.get_bind(), "dispose", lambda: None)
    assert (
        module.main(
            [
                "--database-url",
                "sqlite://",
                "--baseline-kind",
                "fresh-capacity",
                "--check-ready",
            ]
        )
        == 0
    )
    assert '"enforcement_enabled": true' in capsys.readouterr().out
    # Immutable exact replay still refuses the changed source/ledger snapshot.
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    before = snapshot(db)
    replay = capacity.run(db, evidence=manifest, now=RuntimeDatetime.now(), apply=True)
    assert not replay["capacity_ready"]
    assert snapshot(db) == before


def test_exact_nested_measurement_noop_agrees_with_imported_bytes(sample):
    db, projects, *_ = sample
    fresh(db)
    original = reviewed(db, projects)
    assert capacity.run(db, evidence=original, now=NOW, apply=True)["capacity_ready"]
    later = reviewed(db, projects, NOW + timedelta(seconds=2))
    for project_id, entry in later["projects"].items():
        entry["compute_observation"]["observation_id"] += ":later-compute"
        entry["storage_observation"] = deepcopy(
            original["projects"][project_id]["storage_observation"]
        )
    result = capacity.run(
        db, evidence=later, now=NOW + timedelta(seconds=2), apply=True
    )
    assert result["capacity_ready"], result
    for project in projects[:2]:
        row = db.query(QuotaReservation).filter_by(project_id=project.id).one()
        allocation = (
            db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
        )
        assert (
            allocation.observed["storage_bytes"]
            == row.observed["storage_bytes"]
            == 30 * 2**30
        )


@pytest.mark.parametrize("failure", ["stale", "bytes", "owner"])
def test_enabled_readiness_requires_current_paired_physical_evidence(
    sample, monkeypatch, failure
):
    from datetime import datetime

    from app.models.metering import MeteringInterval

    class RuntimeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=2) if failure == "stale" else NOW

    db, projects, *_ = sample
    fresh(db)
    for interval in db.query(MeteringInterval):
        interval.started_at = NOW - timedelta(minutes=5)
        interval.quota_attribution = accounting.attribution(
            db, db.get(Project, interval.project_id)
        )
    db.commit()
    assert capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    row = db.query(QuotaReservation).filter_by(project_id=projects[0].id).one()
    allocation = (
        db.query(QuotaAllocation).filter_by(operation_id=row.operation_id).one()
    )
    if failure == "bytes":
        row.observed = allocation.observed = {**row.observed, "storage_bytes": 1}
    elif failure == "owner":
        allocation.owner_id = projects[2].id
    db.commit()
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(capacity, "datetime", RuntimeDatetime)
    before = snapshot(db)
    assert not capacity.check_ready(db)["ready"]
    with pytest.raises(AccountingUnavailable):
        accounting.require_ready(db)
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "failure",
    [
        "all_sources",
        "compute_source",
        "missing_rate",
        "changed_rate",
        "partial_compute",
        "complete",
    ],
)
def test_enabled_accounting_requires_complete_current_sources_and_rates(
    sample, monkeypatch, failure
):
    from datetime import datetime

    from app.models.host import Host
    from app.models.metering import MeteringInterval
    from app.services import metering_service as metering

    class CurrentDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    db, projects, *_ = sample
    fresh(db)
    for interval in db.query(MeteringInterval):
        interval.started_at = NOW - timedelta(minutes=5)
        interval.quota_attribution = accounting.attribution(
            db, db.get(Project, interval.project_id)
        )
    db.commit()
    assert capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    if failure in {"all_sources", "compute_source"}:
        query = db.query(MeteringInterval).filter_by(
            project_id=projects[0].id, ended_at=None
        )
        if failure == "compute_source":
            query = query.filter_by(kind="vcpu")
        query.delete(synchronize_session=False)
    elif failure in {"missing_rate", "changed_rate"}:
        host = db.get(Host, projects[0].host_id)
        rates = dict(host.metering_rates)
        if failure == "missing_rate":
            rates.pop("vcpu_hour")
            monkeypatch.setattr(metering, "load_type_rates", lambda _: {})
            monkeypatch.setattr(metering, "load_catalog", lambda: {})
            monkeypatch.setattr(metering, "kubevirt_unit_rates", lambda: {})
        else:
            rates["vcpu_hour"] = "0.2"
        host.metering_rates = rates
    elif failure == "partial_compute":
        operation = (
            db.query(QuotaReservation).filter_by(project_id=projects[0].id).one()
        )
        allocation = (
            db.query(QuotaAllocation)
            .filter_by(operation_id=operation.operation_id)
            .one()
        )
        operation.observed = allocation.observed = {**operation.observed, "cpu": 1}
    db.commit()
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(capacity, "datetime", CurrentDatetime)
    before = snapshot(db)
    gate = capacity.check_ready(db)
    assert gate["accounting_ready"] is (failure == "complete"), gate
    assert gate["ready"] is (failure == "complete"), gate
    if failure == "complete":
        accounting.require_ready(db)
    else:
        assert gate["capacity_ready"]
        with pytest.raises(AccountingUnavailable):
            accounting.require_ready(db)
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "state",
    ["error", "stopping", "destroying", "deploying", "draft", "active", "stopped"],
)
def test_retained_billing_requires_supported_state_without_erasing_demand(
    sample, monkeypatch, state
):
    from datetime import datetime

    from app.models.metering import MeteringInterval

    class CurrentDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    db, projects, *_ = sample
    fresh(db)
    for interval in db.query(MeteringInterval):
        interval.started_at = NOW - timedelta(minutes=5)
        interval.quota_attribution = accounting.attribution(
            db, db.get(Project, interval.project_id)
        )
    db.commit()
    assert capacity.run(db, evidence=reviewed(db, projects), now=NOW, apply=True)[
        "ready"
    ]
    project = projects[1] if state == "stopped" else projects[0]
    project.state = state
    if state == "error":
        db.query(MeteringInterval).filter_by(
            project_id=project.id, ended_at=None
        ).delete(synchronize_session=False)
    db.commit()
    allocation = db.query(QuotaAllocation).filter_by(project_id=project.id).one()
    assert allocation.observed["projects"] == 1
    assert allocation.observed["storage_bytes"] > 0
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: True)
    monkeypatch.setattr(capacity, "datetime", CurrentDatetime)
    before = snapshot(db)
    gate = capacity.check_ready(db)
    supported = state in {"active", "stopped"}
    assert gate["capacity_ready"], gate
    assert gate["accounting_ready"] is supported, gate
    assert gate["ready"] is supported, gate
    if supported:
        accounting.require_ready(db)
    else:
        with pytest.raises(AccountingUnavailable):
            accounting.require_ready(db)
    assert snapshot(db) == before


def postgres_manifest(postgres):
    from app.models.host import Host
    from app.models.provider import Provider
    from tests.test_quota_backfill import topology

    sessions, (project_ids, *_), _ = postgres
    with sessions() as db:
        provider = Provider(name="disposable-nested", type="aws")
        db.add(provider)
        db.flush()
        host = Host(provider_id=provider.id, state="ready", billing_mode="shared")
        db.add(host)
        db.flush()
        project = db.get(Project, project_ids[0])
        project.host_id = host.id
        project.state = "active"
        project.topology = project.deployed_topology = topology()
        db.commit()
        fresh(db)
        return reviewed(db, db.query(Project).all())


def _postgres_enabled():
    import os

    return bool(os.getenv("QUOTA_TEST_POSTGRES_URL"))


@pytest.mark.skipif(
    not _postgres_enabled(), reason="disposable PostgreSQL URL not configured"
)
def test_postgresql_current_writer_causes_nowait_full_rollback(postgres):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    manifest = postgres_manifest(postgres)
    sessions, (projects, *_), _ = postgres
    with sessions() as writer, sessions() as importer:
        before = snapshot(importer)
        writer.execute(
            text("UPDATE projects SET state = 'deploying' WHERE id = :id"),
            {"id": projects[0]},
        )
        with pytest.raises(OperationalError):
            capacity.run(importer, evidence=manifest, now=NOW, apply=True)
        writer.rollback()
        assert snapshot(importer) == before


@pytest.mark.skipif(
    not _postgres_enabled(), reason="disposable PostgreSQL URL not configured"
)
def test_postgresql_duplicate_concurrent_apply_creates_one_generation(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    manifest = postgres_manifest(postgres)
    sessions, *_ = postgres
    barrier = Barrier(2)

    def apply():
        with sessions() as db:
            barrier.wait(timeout=5)
            return capacity.run(db, evidence=manifest, now=NOW, apply=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert all(result["capacity_ready"] for result in results), results
    assert results[0] == results[1]
    with sessions() as db:
        assert (
            db.query(QuotaAllocation).count() == db.query(QuotaReservation).count() == 1
        )
        assert db.query(QuotaReadiness).filter_by(id="baseline").count() == 1


@pytest.mark.skipif(
    not _postgres_enabled(), reason="disposable PostgreSQL URL not configured"
)
@pytest.mark.parametrize("writer", ["transfer", "report"])
def test_postgresql_source_identity_or_report_writer_cannot_mix_import(
    postgres, writer
):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.models.kubevirt_storage import NativeCollectionDomain

    manifest = postgres_manifest(postgres)
    sessions, (_, users, _, destination), _ = postgres
    domain_id = str(uuid4())
    if writer == "report":
        with sessions.begin() as db:
            db.add(
                NativeCollectionDomain(
                    id=domain_id, enrollment_id=str(uuid4()), conflicted=False
                )
            )
        with sessions() as db:
            manifest = reviewed(db, db.query(Project).all())
    with sessions() as source, sessions() as importer:
        before = snapshot(importer)
        if writer == "transfer":
            source.execute(
                text(
                    "UPDATE users SET organization_id = :organization WHERE id = :user"
                ),
                {"organization": destination, "user": users[0]},
            )
        else:
            source.execute(
                text(
                    "UPDATE native_collection_domains SET conflicted = true WHERE id = :domain"
                ),
                {"domain": domain_id},
            )
        with pytest.raises(OperationalError):
            capacity.run(importer, evidence=manifest, now=NOW, apply=True)
        source.rollback()
        assert snapshot(importer) == before


@pytest.mark.skipif(
    not _postgres_enabled(), reason="disposable PostgreSQL URL not configured"
)
def test_postgresql_global_lock_order_precedes_identity_and_domain_locks(postgres):
    from hashlib import sha256

    from sqlalchemy import event

    manifest = postgres_manifest(postgres)
    sessions, _, engine = postgres
    events = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if (
            "pg_advisory_xact_lock" in statement
            or statement.startswith("LOCK TABLE")
            or "FOR UPDATE" in statement
        ):
            events.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with sessions() as db:
            identities = sorted(capacity._operation_set(db, capacity.inventory(db)))
            result = capacity.run(db, evidence=manifest, now=NOW, apply=True)
            assert result["capacity_ready"], result
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    keys = [
        int.from_bytes(sha256(value.encode()).digest()[:8], "big", signed=True)
        for value in identities
    ]
    assert [parameters["key"] for _, parameters in events[: len(keys)]] == keys
    assert events[len(keys)][0].startswith("LOCK TABLE")
    locked = [
        statement.split("FROM ", 1)[1].split()[0]
        for statement, _ in events
        if "FOR UPDATE" in statement
    ]
    assert locked[:4] == [
        "users",
        "managed_groups",
        "projects",
        "native_collection_domains",
    ]
