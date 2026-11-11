"""Shared exact-unit quota contracts; absence and zero have distinct meanings."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Scope = Literal["organization", "user", "project"]
Quantity = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Money = Annotated[
    Decimal, Field(ge=0, max_digits=38, decimal_places=12, allow_inf_nan=False)
]


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    cpu: Quantity = 0
    ram_bytes: Quantity = 0
    storage_bytes: Quantity = 0
    projects: Quantity = 0


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    cpu: Quantity | None = None
    ram_bytes: Quantity | None = None
    storage_bytes: Quantity | None = None
    projects: Quantity | None = None
    spending_usd: Money | None = None


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope
    scope_id: str
    limits: Limits
    used: Resources
    reserved: Resources
    spent_usd: Money | None
    as_of: datetime | None
    reset_at: datetime | None
    accounting_status: Literal["ready", "unavailable"]
    enforcement_status: str
    enforcement_enabled: bool
    baseline_ready: bool
    can_edit: bool
    cost_center_id: str | None
    policy_revision: int
    spending_state: str
    action_state: str | None = None
    action_counts: dict[str, int] = Field(default_factory=dict)
    cause_ids: list[str] = Field(default_factory=list)
    action_reasons: list[str] = Field(default_factory=list)
    storage_state: str = "storage_unavailable"
    storage_accounting_status: Literal["ready", "unavailable"] = "unavailable"
    provisioned_storage_bytes: Quantity | None = None
    storage_as_of: datetime | None = None
    platform_storage_bytes: Quantity | None = None


class QuotaDenied(Exception):  # noqa: N818 -- shared service contract
    """Service denial; routes must filter scope details by caller authorization."""

    def __init__(
        self,
        *,
        scope: Scope,
        scope_id: str,
        dimension: str,
        used: int | Decimal,
        reserved: int | Decimal,
        requested: int | Decimal,
        limit: int | Decimal | None,
        reason: str,
    ):
        self.scope = scope
        self.scope_id = scope_id
        self.dimension = dimension
        self.used = used
        self.reserved = reserved
        self.requested = requested
        self.limit = limit
        self.reason = reason
        super().__init__(reason)


class AccountingUnavailable(Exception):  # noqa: N818 -- shared service contract
    """Reliable accounting is unavailable; never interpret this as zero spend."""
