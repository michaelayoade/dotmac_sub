"""Customer service-status schema — the single truthful "is my service good,
and when (if ever) does it lapse" view.

Service expiry in this system is NOT date-driven: prepaid lapses on balance
exhaustion (consumption-driven, surfaced here via balance + grace/deactivation
timers), postpaid lapses only via dunning on overdue invoices. `next_charge_at`
is the next charge/invoice date, never an expiry.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.status_presentation import StatusPresentation


class ServiceStatusActionKind(StrEnum):
    """Semantic customer action; clients only map it to their own route."""

    top_up = "top_up"
    pay_invoices = "pay_invoices"
    view_usage = "view_usage"
    contact_support = "contact_support"


class ServiceStatusAction(BaseModel):
    """Server-owned action hint derived from access and financial policy."""

    kind: ServiceStatusActionKind
    label: str
    message: str
    amount: Decimal | None = None
    currency: str = "NGN"
    # True only when completing this action clears every known access hold.
    restores_service: bool = False


class ServiceStatusItem(BaseModel):
    """Per-subscription truthful status for the caller's current services."""

    subscription_id: UUID
    offer_name: str | None = None
    status: str
    status_presentation: StatusPresentation
    billing_mode: str
    # Currently providing service (RADIUS/connectivity allowed).
    usable: bool
    # The date the service genuinely lapses if nothing changes, or null when it
    # has none. Never derived from next_charge_at.
    expires_at: datetime | None = None
    # Next billing event. Informational — NOT an expiry.
    next_charge_at: datetime | None = None
    # ok | low_balance | overdue | fair_usage | administrative_hold |
    # customer_hold | fraud_review | system_hold | multiple_holds | suspended |
    # stopped | ended
    reason: str
    action: ServiceStatusAction | None = None


class ServiceStatusResponse(BaseModel):
    """Account-level billing health plus per-service status."""

    as_of: datetime
    billing_mode: str
    currency: str = "NGN"

    # Prepaid health (null for postpaid accounts).
    balance: Decimal | None = None
    min_balance: Decimal | None = None
    low_balance: bool = False
    grace_until: datetime | None = None
    deactivation_at: datetime | None = None

    # Postpaid health (null/false for prepaid accounts).
    outstanding: Decimal | None = None
    oldest_overdue_due_at: datetime | None = None
    in_dunning: bool = False

    services: list[ServiceStatusItem] = Field(default_factory=list)
    # One account-level action for compact surfaces such as the mobile
    # dashboard. It prioritizes unavailable services over preventative nudges.
    primary_action: ServiceStatusAction | None = None
