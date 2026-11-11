"""Quota persistence contracts: exact units, idempotency, retained history."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.quota import (
    QuotaAction,
    QuotaAllocation,
    QuotaAudit,
    QuotaCharge,
    QuotaPolicy,
    QuotaReadiness,
    QuotaReservation,
)
from app.schemas.quota import AccountingUnavailable, Limits, QuotaDenied, Resources


def test_zero_and_unlimited_are_distinct():
    assert Limits().cpu is None
    assert Limits(cpu=0).cpu == 0
    assert Limits(spending_usd=Decimal("0")).spending_usd == Decimal("0")
    assert Resources().model_dump() == dict(
        cpu=0, ram_bytes=0, storage_bytes=0, projects=0
    )


@pytest.mark.parametrize(
    "cls,values",
    [
        (Limits, {"cpu": -1}),
        (Resources, {"ram_bytes": -1}),
        (Limits, {"spending_usd": "-0.01"}),
        (Limits, {"cpu": 1.5}),
        (Resources, {"cpu": True}),
        (Limits, {"ram_gib": 1}),
        (Resources, {"unknown": 1}),
        (Resources, {"cpu": 2**63}),
        (Limits, {"spending_usd": "NaN"}),
        (Limits, {"spending_usd": "Infinity"}),
        (Limits, {"spending_usd": "0.0000000000001"}),
        (Limits, {"spending_usd": "100000000000000000000000000"}),
    ],
)
def test_invalid_quantities_are_rejected(cls, values):
    with pytest.raises(ValidationError):
        cls(**values)


def test_decimal_precision_and_serialization():
    limits = Limits(spending_usd="123.123456789123")
    assert limits.spending_usd == Decimal("123.123456789123")
    assert limits.model_dump(mode="json")["spending_usd"] == "123.123456789123"
    with pytest.raises(ValidationError):
        limits.cpu = -1


def test_denials_preserve_structured_constraint():
    denial = QuotaDenied(
        scope="organization",
        scope_id="org",
        dimension="cpu",
        used=2,
        reserved=1,
        requested=2,
        limit=4,
        reason="limit_exceeded",
    )
    assert isinstance(denial, Exception)
    assert denial.scope_id == "org"
    assert denial.requested == 2
    assert denial.reason == "limit_exceeded"
    assert isinstance(AccountingUnavailable("missing rate"), Exception)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def identity():
    return dict(
        project_id=str(uuid.uuid4()),
        owner_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
    )


@pytest.mark.parametrize("model", [QuotaReservation, QuotaAllocation])
def test_operation_identity_cannot_double_count(db, model):
    values = identity()
    db.add(
        model(
            operation_id="same-operation", requested={"cpu": 1}, observed={}, **values
        )
    )
    db.commit()
    db.add(model(operation_id="same-operation", requested={}, observed={}, **values))
    with pytest.raises(IntegrityError):
        db.flush()


def test_policy_is_unique_and_nonnegative(db):
    db.add(QuotaPolicy(scope="user", scope_id=str(uuid.uuid4()), cpu=-1))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    scope_id = str(uuid.uuid4())
    db.add(
        QuotaPolicy(
            scope="user", scope_id=scope_id, spending_usd=Decimal("0.000000000001")
        )
    )
    db.commit()
    policy = db.query(QuotaPolicy).one()
    assert policy.spending_usd == Decimal("0.000000000001")
    db.add(QuotaPolicy(scope="user", scope_id=scope_id))
    with pytest.raises(IntegrityError):
        db.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("spending_usd", Decimal("NaN")),
        ("spending_usd", Decimal("Infinity")),
        ("spending_usd", Decimal("0.0000000000001")),
    ],
)
def test_policy_persistence_rejects_unrepresentable_money(field, value):
    with pytest.raises(ValueError):
        QuotaPolicy(scope="user", scope_id=str(uuid.uuid4()), **{field: value})


@pytest.mark.parametrize("field", ["quantity", "rate", "amount"])
def test_charge_persistence_rejects_nonfinite_values(field):
    with pytest.raises(ValueError):
        QuotaCharge(**{field: Decimal("NaN")})


@pytest.mark.parametrize("model", [QuotaReservation, QuotaAllocation])
def test_invalid_resource_vectors_do_not_enter_ledger(model):
    with pytest.raises(ValueError):
        model(operation_id="invalid", requested={"cpu": -1}, **identity())


@pytest.mark.parametrize("model", [QuotaReservation, QuotaAllocation])
def test_unknown_operation_state_is_rejected(db, model):
    db.add(model(operation_id="bad-state", state="lost-and-free", **identity()))
    with pytest.raises(IntegrityError):
        db.flush()


def test_charge_segments_retain_snapshot_and_deduplicate(db):
    values = dict(
        **identity(),
        project_name="deleted project",
        owner_name="former owner",
        organization_name="old organization",
        source="interval",
        source_id="meter-1",
        start_at=datetime(2026, 11, 1, tzinfo=UTC),
        end_at=datetime(2026, 11, 2, tzinfo=UTC),
        quantity=Decimal("2"),
        rate=Decimal("0.1"),
        amount=Decimal("0.2"),
        kind="cpu",
    )
    db.add(QuotaCharge(**values))
    db.commit()
    assert db.query(QuotaCharge).one().amount == Decimal("0.2")
    assert not QuotaCharge.__table__.foreign_keys
    db.add(QuotaCharge(**values))
    with pytest.raises(IntegrityError):
        db.flush()


def test_durable_actions_are_idempotent_and_readiness_defaults_closed(db):
    values = dict(
        scope="organization",
        scope_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        policy_revision=1,
        period_key="2026-11",
        kind="stop",
        threshold=100,
    )
    db.add(QuotaAction(**values))
    db.add(QuotaReadiness(id="baseline"))
    db.add(
        QuotaAudit(
            actor_id=str(uuid.uuid4()),
            scope="organization",
            scope_id=values["scope_id"],
            action="set_limits",
            before={},
            after={"cpu": 1},
        )
    )
    db.commit()
    assert db.get(QuotaReadiness, "baseline").ready is False
    db.add(QuotaAction(**values))
    with pytest.raises(IntegrityError):
        db.flush()
