"""Starter settlement back-half for Starter-issued invoices (ADR 0031).

Pay-then-record completion of the pay link: authenticated provider
evidence (a signature-verified webhook already claimed by the caller)
is recorded as a confirmation in ``dotmac-payments``; the confirmed
cash is then accepted and allocated by ``dotmac-billing`` against the
Starter document. Sub builds no billing logic here — it verifies the
intent binding, presents the evidence, and mirrors the invoice row
(balance, status, paid instant) from the Starter collectible position,
which is the ledger authority. No legacy ``Payment`` is created for a
Starter-linked invoice.

The caller owns the transaction boundary: confirmation, settlement,
allocation, mirror, and audit commit or roll back together. A Starter
refusal propagates; there is no legacy fallback for a Starter-issued
invoice. All contract values resolve through ``starter_policies``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from dotmac_billing import (
    AcceptSettlementV1,
    GetCurrentReceivablePositionV1,
    get_current_receivable_position,
)
from dotmac_billing.commands import AllocationCommand
from dotmac_billing.contracts import SettlementFundingLane
from dotmac_billing.errors import BillingError
from dotmac_billing.service import accept_settlement, allocate_settlement
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_payments import (
    ConfirmationSource,
    GetConfirmedCashV1,
    GetPaymentIntentV1,
    PaymentError,
    PaymentPurpose,
    RecordConfirmation,
    get_confirmed_cash_v1,
    get_payment_intent_v1,
    record_confirmation,
)
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, StarterInvoiceLink
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.starter_policies import (
    StarterSettlementPolicies,
    resolve_starter_settlement_policies,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterSettlementError",
    "StarterSettlementResult",
    "settle_starter_invoice_payment",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class StarterSettlementError(DomainError, ValueError):
    """The Starter settlement lane refused or failed for this payment."""


def _error(suffix: str, message: str, **details: object) -> StarterSettlementError:
    return StarterSettlementError(
        code=f"financial.starter_settlement.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class StarterSettlementResult:
    """Starter identities recorded for one discharged (or part-paid) invoice."""

    confirmation_id: UUID
    settlement_id: UUID
    allocated: Decimal
    collectible_after: Decimal


def _funding_lane(policies: StarterSettlementPolicies) -> SettlementFundingLane:
    try:
        return SettlementFundingLane(policies.funding_lane)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Settlement funding lane is not a Starter funding lane",
            funding_lane=policies.funding_lane,
        ) from exc


def settle_starter_invoice_payment(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    intent_reference: str,
    external_reference: str,
    confirmed_amount: Decimal,
    occurred_at: datetime,
    observed_at: datetime,
) -> StarterSettlementResult:
    """Discharge one Starter-issued invoice from confirmed provider cash.

    ``intent_reference`` is the exact reference the payments intent was
    opened under at checkout; the observed intent binding must name this
    invoice, its account, and the invoice-settlement purpose before any
    confirmation is recorded.
    """

    policies = resolve_starter_settlement_policies(db)
    currency = Currency(invoice.currency.upper(), 2)
    try:
        confirmed = Money(confirmed_amount, currency)
    except MoneyError as exc:
        raise _error(
            "amount_unrepresentable",
            "Confirmed amount cannot be represented in the invoice currency",
            invoice_id=str(invoice.id),
        ) from exc
    try:
        observed_intent = get_payment_intent_v1(
            db, scope=_SCOPE, query=GetPaymentIntentV1(reference=intent_reference)
        )
        if observed_intent is None:
            raise _error(
                "intent_missing",
                "No Starter payments intent exists for this checkout reference",
                reference=intent_reference,
                invoice_id=str(invoice.id),
            )
        if (
            observed_intent.purpose is not PaymentPurpose.INVOICE_SETTLEMENT
            or observed_intent.target_reference != str(invoice.id)
            or observed_intent.payer_reference != str(invoice.account_id)
            or observed_intent.requested.currency != currency
        ):
            raise _error(
                "intent_binding_mismatch",
                "Starter payments intent does not bind this invoice checkout",
                reference=intent_reference,
                invoice_id=str(invoice.id),
            )
        confirmation = record_confirmation(
            db,
            scope=_SCOPE,
            command=RecordConfirmation(
                intent_id=observed_intent.intent_id,
                source=ConfirmationSource.PROVIDER_CALLBACK,
                external_reference=external_reference,
                confirmed=confirmed,
                observed_at=observed_at,
            ),
        )
        cash = get_confirmed_cash_v1(
            db, scope=_SCOPE, query=GetConfirmedCashV1(confirmation.id)
        )
        if cash.intent_id != observed_intent.intent_id or cash.confirmed != confirmed:
            raise _error(
                "intent_binding_mismatch",
                "Confirmed cash does not match the presented provider evidence",
                reference=intent_reference,
                invoice_id=str(invoice.id),
            )
        settlement = accept_settlement(
            db,
            scope=_SCOPE,
            command=AcceptSettlementV1(
                scope=_SCOPE,
                billing_account_id=link.billing_account_id,
                source_system=policies.settlement_source_system,
                source_settlement_key=str(confirmation.id),
                source_version=policies.settlement_source_version,
                amount=confirmed,
                occurred_at=occurred_at,
                observed_at=cash.observed_at,
                confirmation_evidence=policies.confirmation_evidence,
                funding_lane=_funding_lane(policies),
            ),
            accepted_confirmation_evidence=policies.accepted_confirmation_evidence,
        )
        allocate_settlement(
            db,
            scope=_SCOPE,
            command=AllocationCommand(
                settlement_id=settlement.id,
                document_id=link.document_id,
                amount=confirmed,
                occurred_at=occurred_at,
                source_ref=policies.settlement_source_ref(confirmation.id),
            ),
        )
        current = get_current_receivable_position(
            db,
            scope=_SCOPE,
            request=GetCurrentReceivablePositionV1(
                scope=_SCOPE,
                billing_account_id=link.billing_account_id,
                currency=currency,
            ),
        )
    except StarterSettlementError:
        raise
    except (BillingError, PaymentError) as exc:
        raise _error(
            "starter_refused",
            "Starter refused this settlement; the invoice was not discharged",
            invoice_id=str(invoice.id),
            reference=intent_reference,
        ) from exc
    assert current.position is not None  # allocation proved the account position
    collectible = current.position.collectible_receivable.amount

    invoice.balance_due = collectible
    if collectible <= 0:
        invoice.status = InvoiceStatus.paid
        if invoice.paid_at is None:
            invoice.paid_at = occurred_at
    elif invoice.status in (InvoiceStatus.issued, InvoiceStatus.overdue):
        invoice.status = InvoiceStatus.partially_paid
    # Same public event the legacy lane emits, minus the legacy payment
    # identity: enforcement, arrangements, and lifecycle handlers key off
    # account/invoice/amount, and receipt rendering simply stays
    # unresolved until the Starter receipt surface lands.
    emit_event(
        db,
        EventType.payment_received,
        {
            "amount": str(confirmed.amount),
            "currency": invoice.currency,
            "invoice_id": str(invoice.id),
            "status": "succeeded",
            "settlement_lane": "starter",
            "starter_confirmation_id": str(confirmation.id),
            "starter_settlement_id": str(settlement.id),
        },
        account_id=invoice.account_id,
        invoice_id=invoice.id,
    )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            action="starter_invoice_settlement",
            entity_type="invoice",
            entity_id=str(invoice.id),
            metadata_={
                "settlement_lane": "starter",
                "intent_reference": intent_reference,
                "external_reference": external_reference,
                "starter_confirmation_id": str(confirmation.id),
                "starter_settlement_id": str(settlement.id),
                "starter_document_id": str(link.document_id),
                "allocated": str(confirmed.amount),
                "collectible_after": str(collectible),
                "currency": invoice.currency,
            },
        ),
    )
    db.flush()
    return StarterSettlementResult(
        confirmation_id=confirmation.id,
        settlement_id=settlement.id,
        allocated=confirmed.amount,
        collectible_after=collectible,
    )
