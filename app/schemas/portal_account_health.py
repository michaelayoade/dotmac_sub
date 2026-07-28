"""Transport contract for the canonical customer account/service-health view."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.service_status import ServiceStatusAction
from app.schemas.status_presentation import StatusPresentation
from app.services.ui_contracts import StateKind

T = TypeVar("T")


class StateRead(BaseModel, Generic[T]):
    """Wire representation of a value and its availability/freshness."""

    model_config = ConfigDict(from_attributes=True)

    kind: StateKind
    value: T | None = None
    as_of: datetime | None = None


class MoneyAmountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    amount: Decimal
    currency: str


class ReceivableLaneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    currency: str
    outstanding: Decimal
    overdue: Decimal
    overdue_count: int


class PortalFinancialHealthRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    billing_mode: StateRead[str]
    billing_mode_reason: str
    receivables: StateRead[list[ReceivableLaneRead]]
    prepaid_funding: StateRead[MoneyAmountRead]
    prepaid_funding_reason: str


class PortalSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    binding: str
    observed_at: datetime | None
    framed_ip_address: str | None
    nas_device_id: UUID | None


class PortalConnectionDiagnosisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    status_presentation: StatusPresentation
    headline: str
    message: str
    advice: str | None
    medium: str | None
    area_outage: bool
    checked_at: datetime


class PortalPendingServiceChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    status: str
    target_offer_name: str
    effective_date: date
    delivery_mode: Literal[
        "commercial_only", "remote_reprovision", "field_migration", "unknown"
    ]
    delivery_state: str
    target_service_address: str | None = None
    field_fee_amount: Decimal | None = None
    field_fee_currency: str | None = None


class PortalServiceHealthRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subscription_id: UUID
    offer_name: str
    lifecycle: StatusPresentation
    billing_mode: str | None
    access_state: str
    access: StatusPresentation
    access_reason: str
    session: PortalSessionRead
    session_presentation: StatusPresentation
    connection: StateRead[PortalConnectionDiagnosisRead]
    next_charge_at: datetime | None
    expires_at: datetime | None
    next_action: ServiceStatusAction | None
    pending_change: PortalPendingServiceChangeRead | None


class PortalAccountHealthRead(BaseModel):
    """One account and all operationally-current services, self-scoped by API."""

    model_config = ConfigDict(from_attributes=True)

    account_id: UUID
    account_number: str | None
    subscriber_number: str | None
    display_name: str
    lifecycle: StatusPresentation
    financial: PortalFinancialHealthRead
    services: list[PortalServiceHealthRead]
    primary_action: ServiceStatusAction | None
    as_of: datetime
