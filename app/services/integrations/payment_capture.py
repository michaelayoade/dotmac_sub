"""Read-only PSP capture values; these observations confer no cash authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from app.services.domain_errors import DomainError


class CaptureProvider(StrEnum):
    PAYSTACK = "paystack"
    FLUTTERWAVE = "flutterwave"


class CaptureRefusal(StrEnum):
    UNBOUND = "unbound"
    UNSUPPORTED = "unsupported"
    STALE_BINDING = "stale_binding"
    PROVIDER_FAILURE = "provider_failure"
    INVALID_RECORD = "invalid_record"
    REFERENCE_MISMATCH = "reference_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"


class ProviderAmountUnit(StrEnum):
    MINOR = "minor"
    MAJOR = "major"


class ProviderFeeField(StrEnum):
    PAYSTACK_FEES = "fees"
    FLUTTERWAVE_APP_FEE = "app_fee"


class PaymentCaptureError(DomainError):
    def __init__(self, reason: CaptureRefusal) -> None:
        super().__init__(
            code=f"payment_capture_{reason.value}",
            message="Provider evidence capture was refused.",
            retryable=False,
        )
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AssertedMerchantNamespace:
    """Caller assertion only; no issuer attestation or provider-observed identity."""

    namespace: str
    assertion_reference: UUID
    installation_id: UUID
    config_revision_id: UUID
    config_digest: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.namespace)
            or not all(
                isinstance(value, UUID)
                for value in (
                    self.assertion_reference,
                    self.installation_id,
                    self.config_revision_id,
                )
            )
            or not re.fullmatch(r"[a-f0-9]{64}", self.config_digest)
        ):
            raise PaymentCaptureError(CaptureRefusal.UNBOUND)


@dataclass(frozen=True, slots=True)
class PaymentCaptureQuery:
    """Expected amount is in major currency units, compared without rounding."""

    provider: CaptureProvider
    reference: str
    installation_id: UUID
    capability_binding_id: UUID
    config_revision_id: UUID
    config_digest: str
    expected_amount: Decimal
    expected_currency: str
    asserted_merchant_namespace: AssertedMerchantNamespace | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, CaptureProvider)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", self.reference)
            or not all(
                isinstance(value, UUID)
                for value in (
                    self.installation_id,
                    self.capability_binding_id,
                    self.config_revision_id,
                )
            )
            or not isinstance(self.expected_amount, Decimal)
            or not self.expected_amount.is_finite()
            or self.expected_amount < 0
            or len(self.expected_currency) != 3
            or not self.expected_currency.isascii()
            or not self.expected_currency.isupper()
            or not self.expected_currency.isalpha()
            or len(self.config_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.config_digest)
        ):
            raise PaymentCaptureError(CaptureRefusal.INVALID_RECORD)


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """Allowlisted RESPONSE-DERIVED record, never original response/webhook bytes.

    Amount and fees retain provider units; Paystack uses minor units and
    Flutterwave uses major units. Missing fees and timestamps stay missing.
    """

    provider: CaptureProvider
    envelope_status: Literal[True, "success"]
    transaction_id: str
    reference: str
    transaction_status: str
    amount: Decimal
    currency: str
    fee: Decimal | None
    fee_field: ProviderFeeField
    amount_unit: ProviderAmountUnit
    provider_created_at: datetime | None
    provider_paid_at: datetime | None
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class CaptureProvenance:
    installation_id: UUID
    capability_binding_id: UUID
    capability_id: str
    connector_key: str
    connector_version: str
    manifest_digest: str
    config_revision_id: UUID
    config_digest: str
    asserted_merchant_namespace: AssertedMerchantNamespace


@dataclass(frozen=True, slots=True)
class PaymentCaptureOutcome:
    refusal: CaptureRefusal | None
    record: ProviderRecord | None = None
    provenance: CaptureProvenance | None = None

    def __post_init__(self) -> None:
        if (self.refusal is None) != (self.record is not None) or (
            self.record is None
        ) != (self.provenance is None):
            raise PaymentCaptureError(CaptureRefusal.INVALID_RECORD)
