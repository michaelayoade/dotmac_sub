"""Read-only parity for an already-authorized legacy invoice checkout.

The legacy intent owns the checkout binding. Module lifecycle/confirmed money
are observations only; neither is used to admit settlement here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_payments import (
    GetPaymentIntentV1,
    PaymentPurpose,
    get_payment_intent_v1,
)
from sqlalchemy.orm import Session

from app.metrics import PAYMENT_INTENT_MODULE_PARITY
from app.models.billing import TopupIntent
from app.services.operator_tenant import OPERATOR_TENANT_ID


class IntentParityOutcome(StrEnum):
    MATCH = "match"
    MISSING = "missing"
    MISMATCH = "mismatch"


class IntentBindingField(StrEnum):
    PAYER = "payer"
    TARGET = "target"
    PURPOSE = "purpose"
    AMOUNT = "amount"
    CURRENCY = "currency"
    SCALE = "scale"
    PROVIDER = "provider"
    CHANNEL = "channel"
    OPENED_AT = "opened_at"
    EXPIRES_AT = "expires_at"


@dataclass(frozen=True, slots=True)
class AuthorizedInvoiceIntentBinding:
    """Frozen input after account authorization; never a provider observation.

    Scale is two, from TopupIntent.requested_amount Numeric(12, 2), rather
    than an inferred currency registry default. Account and invoice identity
    come only from the legacy record's structural foreign keys.
    """

    reference: str
    account_id: UUID | None
    invoice_reference: str | None
    requested_amount: Decimal
    currency: str
    provider_type: str
    channel: str | None
    opened_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class IntentBindingParity:
    outcome: IntentParityOutcome
    mismatched_fields: tuple[IntentBindingField, ...] = ()


def authorized_invoice_binding(intent: TopupIntent) -> AuthorizedInvoiceIntentBinding:
    """Adapt the authorized legacy record without rounding or refreshing TTL."""
    return AuthorizedInvoiceIntentBinding(
        reference=intent.reference,
        account_id=intent.account_id,
        invoice_reference=str(intent.invoice_id)
        if intent.invoice_id is not None
        else None,
        requested_amount=intent.requested_amount,
        currency=intent.currency,
        provider_type=intent.provider_type,
        channel=intent.channel,
        opened_at=intent.created_at,
        expires_at=intent.expires_at,
    )


def observe_invoice_intent_parity(
    db: Session, binding: AuthorizedInvoiceIntentBinding
) -> IntentBindingParity:
    """Query on the caller's session; database failures propagate unchanged."""
    observed = get_payment_intent_v1(
        db,
        scope=TenantScope(OPERATOR_TENANT_ID),
        query=GetPaymentIntentV1(reference=binding.reference),
    )
    if observed is None:
        result = IntentBindingParity(IntentParityOutcome.MISSING)
    else:
        comparisons = (
            (
                IntentBindingField.PAYER,
                observed.payer_reference == str(binding.account_id)
                if binding.account_id is not None
                else False,
            ),
            (
                IntentBindingField.TARGET,
                binding.invoice_reference is not None
                and observed.target_reference == binding.invoice_reference,
            ),
            (
                IntentBindingField.PURPOSE,
                observed.purpose is PaymentPurpose.INVOICE_SETTLEMENT,
            ),
            (
                IntentBindingField.AMOUNT,
                observed.requested.amount == binding.requested_amount,
            ),
            (
                IntentBindingField.CURRENCY,
                observed.requested.currency.code == binding.currency.upper(),
            ),
            (IntentBindingField.SCALE, observed.requested.currency.minor_units == 2),
            (
                IntentBindingField.PROVIDER,
                observed.provider_type.strip().upper()
                == binding.provider_type.strip().upper(),
            ),
            (
                IntentBindingField.CHANNEL,
                observed.channel.strip().upper() == binding.channel.strip().upper()
                if binding.channel is not None
                else False,
            ),
            (IntentBindingField.OPENED_AT, observed.opened_at == binding.opened_at),
            (IntentBindingField.EXPIRES_AT, observed.expires_at == binding.expires_at),
        )
        fields = tuple(field for field, matches in comparisons if not matches)
        result = IntentBindingParity(
            IntentParityOutcome.MISMATCH if fields else IntentParityOutcome.MATCH,
            fields,
        )
    PAYMENT_INTENT_MODULE_PARITY.labels(
        outcome=result.outcome.value, field="none"
    ).inc()
    for field in result.mismatched_fields:
        PAYMENT_INTENT_MODULE_PARITY.labels(
            outcome=result.outcome.value, field=field.value
        ).inc()
    return result
