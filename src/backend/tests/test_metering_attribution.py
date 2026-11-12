"""Identity is retained by source writers even while limits are disabled."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.models.elastic_ip import ElasticIp
from app.models.host import Host
from app.models.managed_group import ManagedGroup
from app.models.metering import MeteringInterval
from app.models.project import Project
from app.models.provider import Provider
from app.models.quota import QuotaCharge
from app.models.user import User
from app.schemas.quota import AccountingUnavailable
from app.services import metering_service as metering
from app.services import quota_accounting as accounting
from app.services import quota_baseline as baseline

NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def sample(isolated_quota_database, monkeypatch):
    monkeypatch.setattr(metering, "quotas_enabled", lambda: False)
    monkeypatch.setattr(accounting, "quotas_enabled", lambda: False)
    monkeypatch.setattr(metering, "metering_enabled", lambda: True)
    with isolated_quota_database() as db:
        groups = [
            ManagedGroup(name=name, normalized_name=name, is_organization=True)
            for name in ("original", "destination")
        ]
        db.add_all(groups)
        db.flush()
        user = User(email="identity@test.invalid", organization_id=groups[0].id)
        db.add(user)
        db.flush()
        project = Project(name="retained", owner_id=user.id, state="active")
        db.add(project)
        db.flush()
        snap = {
            "provider_type": "aws",
            "project_state": "active",
            "host_rates": {"vcpu_hour": 3, "ram_gib_hour": 0},
            "vms": [{"id": "vm", "vcpus": 2, "ram_gib": 0, "running": True}],
            "disks": [],
            "eips": [],
            "ceph": [],
        }
        monkeypatch.setattr(metering, "build_snapshot", lambda *_: snap)
        yield db, project, user, groups, snap


def test_disabled_open_source_retains_original_group_after_owner_changes(sample):
    db, project, user, groups, snap = sample
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    opened = db.query(MeteringInterval).filter_by(kind="vcpu").one()
    assert opened.quota_attribution["organization_id"] == groups[0].id
    user.organization_id = groups[1].id
    db.flush()
    accounting.accrue(db, project.id, NOW + timedelta(hours=1))
    charge = db.query(QuotaCharge).filter_by(kind="vcpu").one()
    assert charge.organization_id == groups[0].id
    assert charge.amount == Decimal("6")


def test_unassigned_legacy_meter_keeps_unknown_identity_and_invoice_amount(sample):
    db, project, user, _groups, snap = sample
    user.organization_id = None
    db.flush()
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    assert all(row.quota_attribution is None for row in db.query(MeteringInterval))
    invoice = metering.freeze_invoice(db, project, now=NOW + timedelta(hours=2))
    assert invoice.total_usd == Decimal("12")
    assert all(row.quota_attribution is None for row in db.query(MeteringInterval))


def test_disabled_freeze_keeps_stamped_charges_after_source_delete(sample):
    db, project, _user, groups, snap = sample
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    invoice = metering.freeze_invoice(db, project, now=NOW + timedelta(hours=2))
    db.query(MeteringInterval).delete(synchronize_session=False)
    db.delete(project)
    db.flush()
    assert invoice.total_usd == Decimal("12")
    assert {row.organization_id for row in db.query(QuotaCharge)} == {groups[0].id}
    assert sum(row.amount for row in db.query(QuotaCharge)) == Decimal("12")


@pytest.mark.parametrize("retained", [False, True])
def test_unstamped_history_does_not_invent_current_employer(sample, retained):
    db, project, _user, _groups, _snap = sample
    source = MeteringInterval(
        project_id=project.id,
        kind="vcpu",
        resource_id="old",
        qty=2,
        unit_rate=3,
        started_at=NOW,
        ended_at=NOW + timedelta(hours=1),
    )
    db.add(source)
    db.flush()
    with pytest.raises(AccountingUnavailable, match="attribution"):
        accounting.accrue(db, project.id, NOW + timedelta(hours=1), retained=retained)
    assert source.quota_attribution is None
    assert db.query(QuotaCharge).count() == 0


def test_baseline_preserves_source_stamp_and_charge_identity(sample):
    db, project, user, groups, snap = sample
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    user.organization_id = groups[1].id
    db.flush()
    baseline._history_project(db, project, NOW + timedelta(hours=1))
    assert all(
        row.quota_attribution["organization_id"] == groups[0].id
        for row in db.query(MeteringInterval)
    )
    assert {row.organization_id for row in db.query(QuotaCharge)} == {groups[0].id}


def test_disabled_freeze_identity_failure_cannot_be_swallowed(sample, monkeypatch):
    db, project, _user, _groups, _snap = sample
    from app.services import quota_locks

    def changed(*_args, **_kwargs):
        raise quota_locks.ScopeChanged("identity changed")

    monkeypatch.setattr(quota_locks, "lock_metering_project", changed)
    with pytest.raises(quota_locks.ScopeChanged):
        metering.safe_freeze_invoice(db, project)


def test_disabled_record_deletion_rolls_back_on_identity_change(sample, monkeypatch):
    db, project, _user, _groups, _snap = sample
    from app.services import deploy_service, quota_locks

    monkeypatch.setattr(deploy_service, "notify_project", lambda *_: None)
    project_id = project.id
    db.commit()

    def changed(*_args, **_kwargs):
        raise quota_locks.ScopeChanged("identity changed")

    monkeypatch.setattr(quota_locks, "lock_metering_project", changed)
    with pytest.raises(quota_locks.ScopeChanged):
        deploy_service._delete_project_record(project_id)
    db.expire_all()
    assert db.get(Project, project_id) is not None


def test_disabled_record_deletion_retains_stamped_source_identity(sample, monkeypatch):
    db, project, _user, groups, snap = sample
    from app.services import deploy_service

    monkeypatch.setattr(deploy_service, "notify_project", lambda *_: None)
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    project_id, organization_id = project.id, groups[0].id
    db.commit()
    deploy_service._delete_project_record(project_id)
    db.expire_all()
    charges = db.query(QuotaCharge).all()
    assert charges and {row.organization_id for row in charges} == {organization_id}
    assert db.get(Project, project_id) is None


def test_disabled_metering_freeze_retains_stamped_source_identity(sample, monkeypatch):
    db, project, _user, groups, snap = sample
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    monkeypatch.setattr(metering, "metering_enabled", lambda: False)
    metering.safe_freeze_invoice(db, project)
    charges = db.query(QuotaCharge).all()
    assert charges and {row.organization_id for row in charges} == {groups[0].id}


def test_timer_delete_retains_stamped_source_identity(sample, monkeypatch):
    db, project, _user, groups, snap = sample
    from app.services import project_timer

    monkeypatch.setattr(project_timer, "_notify", lambda *_: None)
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    project_id, organization_id = project.id, groups[0].id
    project.state = "draft"  # No infrastructure/provider operation in this test.
    db.commit()
    project_timer._delete_project(db, project)
    charges = db.query(QuotaCharge).all()
    assert charges and {row.organization_id for row in charges} == {organization_id}
    assert db.get(Project, project_id) is None


def test_provider_cleanup_retains_stamped_source_identity(sample):
    db, project, _user, groups, snap = sample
    from app.api.providers import _cleanup_kubevirt_db_resources

    provider = Provider(name="isolated", type="kubevirt")
    db.add(provider)
    db.flush()
    host = Host(provider_id=provider.id)
    db.add(host)
    db.flush()
    project.host_id = host.id
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    project_id, organization_id = project.id, groups[0].id
    _cleanup_kubevirt_db_resources(db, provider, {}, None)
    db.flush()
    charges = db.query(QuotaCharge).all()
    assert charges and {row.organization_id for row in charges} == {organization_id}
    assert db.get(Project, project_id) is None


@pytest.mark.parametrize("deletion", ["timer", "api"])
@pytest.mark.parametrize("associated", [False, True])
def test_eip_cleanup_commit_cannot_discard_transferred_source(
    sample, monkeypatch, deletion, associated
):
    """The real EIP helper commits before a second session transfers membership."""
    assert_eip_commit_retains_transfer(sample, monkeypatch, deletion, associated)


def assert_eip_commit_retains_transfer(sample, monkeypatch, deletion, associated):
    """Share the real committing-helper interleaving across SQLite and PostgreSQL."""
    db, project, user, groups, snap = sample
    from app.api import projects
    from app.services import eip_service, project_timer
    from app.services.organization_service import _lock_transfer

    monkeypatch.setattr(project_timer, "_notify", lambda *_: None)
    monkeypatch.setattr(projects, "notify_project", lambda *_: None)
    monkeypatch.setattr(
        eip_service,
        "get_provider_driver",
        lambda _: SimpleNamespace(release_eip=lambda *_args, **_kwargs: None),
    )
    metering.reconcile_project(db, project, now=NOW, snapshot=snap)
    project.state = "draft"  # Metadata-only deletion; no stop or dispatch.
    provider = Provider(name="eip-fixture", type="aws")
    db.add(provider)
    db.flush()
    host = Host(provider_id=provider.id)
    db.add(host)
    db.flush()
    db.add(
        ElasticIp(
            provider_id=provider.id,
            project_id=project.id,
            canvas_eip_id="fixture",
            allocation_id="fixture",
            public_ip="192.0.2.1",
            state="associated" if associated else "allocated",
            host_id=host.id if associated else None,
        )
    )
    project_id, owner_id, destination = project.id, user.id, groups[1].id
    db.commit()
    # This branch preserves open sources during the initial invoice retention.
    monkeypatch.setattr(metering, "metering_enabled", lambda: False)
    sessions = sessionmaker(bind=db.get_bind())
    commits, successor_ids = [], []

    def transfer_after_final_eip_commit(_db):
        commits.append(True)
        expected_commits = 2 if associated else 1
        if len(commits) != expected_commits:
            return
        with sessions.begin() as transfer:
            owner, _ = _lock_transfer(transfer, owner_id, destination)
            accounting.split_attribution(
                transfer, project_id, datetime.now(UTC), destination
            )
            owner.organization_id = destination
            successor_ids.extend(
                row.id
                for row in transfer.query(MeteringInterval).filter_by(
                    project_id=project_id, ended_at=None
                )
            )

    event.listen(db, "after_commit", transfer_after_final_eip_commit)
    try:
        if deletion == "timer":
            project_timer._delete_project(db, project)
        else:
            projects.delete_project(project_id, user, db)
    finally:
        event.remove(db, "after_commit", transfer_after_final_eip_commit)
    assert (
        successor_ids
    ), "the committing real EIP helper must expose the transfer boundary"
    assert db.get(Project, project_id) is None
    assert db.query(ElasticIp).filter_by(project_id=project_id).count() == 0
    charges = (
        db.query(QuotaCharge).filter(QuotaCharge.source_id.in_(successor_ids)).all()
    )
    assert charges and {row.organization_id for row in charges} == {destination}
    assert sum(row.amount for row in charges) > 0
