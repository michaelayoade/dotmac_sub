"""API contracts for subscription billing treatments."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.catalog import BillingCycle
from app.models.subscription_billing_treatment import (
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingTreatment,
)


class BillingTreatmentPreviewRequest(BaseModel):
    treatment: SubscriptionBillingTreatment
    reason_code: BillingTreatmentReason
    reason: str = Field(min_length=1, max_length=2000)
    starts_at: datetime
    ends_at: datetime
    sponsor_reference: str | None = Field(default=None, max_length=200)
    cost_center: str | None = Field(default=None, max_length=100)


class BillingTreatmentPreviewRead(BaseModel):
    subscription_id: UUID
    account_id: UUID
    authorized_offer_id: UUID
    treatment: SubscriptionBillingTreatment
    reason_code: BillingTreatmentReason
    reason: str
    starts_at: datetime
    ends_at: datetime
    approval_policy_max_days: int
    maximum_recurring_amount: Decimal
    billing_cycle: BillingCycle
    currency: str
    sponsor_reference: str | None
    cost_center: str | None
    evaluated_at: datetime
    fingerprint: str


class BillingTreatmentConfirmRequest(BillingTreatmentPreviewRequest):
    preview_effective_at: datetime
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=500)


class BillingTreatmentRevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=500)


class BillingTreatmentRead(BaseModel):
    arrangement_id: UUID
    subscription_id: UUID
    account_id: UUID
    authorized_offer_id: UUID
    treatment: SubscriptionBillingTreatment
    reason_code: BillingTreatmentReason
    reason: str
    starts_at: datetime
    ends_at: datetime
    approval_policy_max_days: int
    maximum_recurring_amount: Decimal
    billing_cycle: BillingCycle
    currency: str
    sponsor_reference: str | None
    cost_center: str | None
    status: BillingTreatmentStatus
    approved_by: str
    approved_at: datetime
    revoked_by: str | None
    revoked_at: datetime | None
    revocation_reason: str | None


class BillingTreatmentOutcomeRead(BaseModel):
    arrangement_id: UUID
    subscription_id: UUID
    account_id: UUID
    treatment: SubscriptionBillingTreatment
    starts_at: datetime
    ends_at: datetime
    approval_policy_max_days: int
    maximum_recurring_amount: Decimal
    billing_cycle: BillingCycle
    currency: str
    status: BillingTreatmentStatus
    replayed: bool
