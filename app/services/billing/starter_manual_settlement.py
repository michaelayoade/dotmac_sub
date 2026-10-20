"""Staff-recorded manual payments against Starter-issued invoices.

Manual lane (ADR 0031): a staff-recorded payment whose confirmed
allocation lands in full on one Starter-linked invoice opens a
``dotmac-payments`` intent bound to that invoice, records the staff
evidence as a confirmation (policy-resolved source, default
``TRANSFER_PROOF``), and discharges through the shared Starter
settlement owner — billing acceptance, allocation, mirror, and the
exactly-once access signal are identical to the webhook lane. No
legacy ``Payment`` or legacy ledger entry is written; the manual
recording owner presents the Starter confirmation as its payment
projection. Replays of one manual recording derive the same intent
reference from its idempotency key, so the settlement replay returns
the same Starter identities.

The caller (manual payment recording) owns eligibility, duplicate
review, locking, and the transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_payments import (
    ConfirmationSource,
    OpenPaymentIntent,
    PaymentError,
    PaymentPurpose,
    open_payment_intent,
)
from sqlalchemy.orm import Session

from app.models.billing import Invoice, StarterInvoiceLink
from app.services.billing.starter_policies import (
    StarterManualSettlementPolicies,
    resolve_starter_billing_policies,
    resolve_starter_manual_settlement_policies,
)
from app.services.billing.starter_settlement import (
    StarterSettlementResult,
    settle_starter_invoice_payment,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterManualSettlement",
    "StarterManualSettlementError",
    "record_manual_starter_payment",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_OWNER = "financial.starter_manual_settlement"


class StarterManualSettlementError(DomainError, ValueError):
    """The Starter manual settlement lane refused or failed."""


def _error(
    suffix: str, message: str, **details: object
) -> StarterManualSettlementError:
    return StarterManualSettlementError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class StarterManualSettlement:
    """Starter identities recorded for one manual invoice payment."""

    intent_reference: str
    settlement: StarterSettlementResult


def _confirmation_source(
    policies: StarterManualSettlementPolicies,
) -> ConfirmationSource:
    try:
        return ConfirmationSource(policies.manual_confirmation_source)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Manual confirmation source is not a Starter confirmation source",
            confirmation_source=policies.manual_confirmation_source,
        ) from exc


def record_manual_starter_payment(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    amount: Decimal,
    external_reference: str,
    occurred_at: datetime,
    idempotency_key: str,
) -> StarterManualSettlement:
    """Open a bound intent and discharge one Starter invoice manually."""
    policies = resolve_starter_manual_settlement_policies(db)
    billing_policies = resolve_starter_billing_policies(db)
    currency = Currency(invoice.currency.upper(), billing_policies.default_minor_units)
    try:
        requested = Money(amount, currency)
    except MoneyError as exc:
        raise _error(
            "amount_unrepresentable",
            "Manual amount cannot be represented in the invoice currency",
            invoice_id=str(invoice.id),
        ) from exc
    reference = policies.manual_intent_reference(idempotency_key)
    try:
        open_payment_intent(
            db,
            scope=_SCOPE,
            command=OpenPaymentIntent(
                reference=reference,
                payer_reference=str(invoice.account_id),
                purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                target_reference=str(invoice.id),
                requested=requested,
                provider_type=policies.manual_intent_provider_type,
                channel=policies.manual_intent_channel,
                opened_at=occurred_at,
                expires_at=occurred_at
                + timedelta(hours=policies.manual_intent_expiry_hours),
            ),
        )
    except PaymentError as exc:
        raise _error(
            "starter_refused",
            "Starter payments refused the manual payment intent",
            invoice_id=str(invoice.id),
            reference=reference,
        ) from exc
    settlement = settle_starter_invoice_payment(
        db,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=external_reference,
        confirmed_amount=amount,
        occurred_at=occurred_at,
        observed_at=occurred_at,
        confirmation_source=_confirmation_source(policies),
    )
    return StarterManualSettlement(intent_reference=reference, settlement=settlement)
