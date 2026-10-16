"""Immutable saved-authorization transport values; no financial decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.services.domain_errors import DomainError


class PaymentGatewayProviderStatus(str, Enum):
    """Allowlisted transaction statuses emitted by supported provider adapters."""

    success = "success"
    successful = "successful"
    succeeded = "succeeded"
    failed = "failed"
    abandoned = "abandoned"
    ongoing = "ongoing"
    pending = "pending"
    processing = "processing"
    queued = "queued"
    reversed = "reversed"


@dataclass(frozen=True, slots=True)
class InvoiceGatewayRoutingEvidence:
    """Sub-owned routing/provenance accompanying a module-owned binding."""

    account_id: UUID
    invoice_id: UUID
    invoice_number: str
    provider_type: str
    provider_id: UUID | None
    capability_binding_id: UUID
    payment_method_id: UUID | None
    created_by: str

    def checkout_metadata(self) -> dict[str, str]:
        """Serialize only the frozen checkout/provenance fields for transport."""
        metadata = {
            "payment_flow": "invoice_payment",
            "invoice_id": str(self.invoice_id),
            "invoice_number": self.invoice_number,
            "account_id": str(self.account_id),
            "capability_binding_id": str(self.capability_binding_id),
        }
        if self.provider_id is not None:
            metadata["provider_id"] = str(self.provider_id)
        if self.payment_method_id is not None:
            metadata["payment_method_id"] = str(self.payment_method_id)
        return metadata


@dataclass(frozen=True, slots=True)
class IntentChargeMetadata:
    intent_id: UUID


@dataclass(frozen=True, slots=True)
class ResellerChargeMetadata:
    intent_id: UUID
    billing_account_id: UUID
    reseller_id: UUID
    payment_method_id: UUID | None


@dataclass(frozen=True, slots=True)
class AutopayChargeMetadata:
    invoice_id: UUID


ChargeMetadata = (
    InvoiceGatewayRoutingEvidence
    | IntentChargeMetadata
    | ResellerChargeMetadata
    | AutopayChargeMetadata
)


@dataclass(frozen=True, slots=True)
class ChargeAuthorizationCommand:
    authorization_code: str = field(repr=False)
    email: str = field(repr=False)
    amount_subunits: int
    currency: str
    reference: str
    metadata: ChargeMetadata
    checkout_binding_id: UUID | None

    def __post_init__(self) -> None:
        if type(self.amount_subunits) is not int or self.amount_subunits <= 0:
            raise ChargeProtocolError("charge_amount_invalid")
        if not self.authorization_code or not self.email or not self.reference:
            raise ChargeProtocolError("charge_identity_incomplete")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ChargeProtocolError("charge_currency_invalid")
        if self.checkout_binding_id is not None and not isinstance(
            self.checkout_binding_id, UUID
        ):
            raise ChargeProtocolError("charge_binding_invalid")


@dataclass(frozen=True, slots=True)
class ObserveAuthorizationChargeReference:
    reference: str
    checkout_binding_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.reference:
            raise ChargeProtocolError("charge_reference_missing")
        if self.checkout_binding_id is not None and not isinstance(
            self.checkout_binding_id, UUID
        ):
            raise ChargeProtocolError("charge_binding_invalid")


class ProviderAcceptance(str, Enum):
    accepted = "accepted"
    rejected = "rejected"
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class ChargeAuthorizationObservation:
    """Provider facts only. Acceptance and transaction status are distinct."""

    acceptance: ProviderAcceptance
    provider_status: PaymentGatewayProviderStatus | None
    reference: str | None
    gateway_response: str | None = None
    message: str | None = None
    amount_subunits: int | None = None
    currency: str | None = None
    fee_subunits: int | None = None
    paid_at: datetime | None = None


class ChargeProtocolError(DomainError, ValueError):
    """Missing or contradictory transport facts; never raw provider payloads."""

    def __init__(self, reason: str) -> None:
        super().__init__(code=f"integration.payments.{reason}", message=reason)
