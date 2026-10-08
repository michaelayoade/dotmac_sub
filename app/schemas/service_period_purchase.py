"""Customer transport inputs for the typed prepaid purchase owners."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ServicePeriodPurchasePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: UUID
    period_count: StrictInt = Field(ge=1, le=12)


class ServicePeriodPurchaseCheckoutRequest(ServicePeriodPurchasePreviewRequest):
    preview_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    provider: Literal["paystack", "flutterwave"] | None = None
    payment_method_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
