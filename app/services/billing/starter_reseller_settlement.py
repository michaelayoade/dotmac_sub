"""Starter settlement for reseller consolidated payments (ADR 0031/0032).

One reseller party pays for many subscriber accounts. The confirmed
consolidated payment binds exactly one ``dotmac-payments`` intent
(payer = the reseller's party reference, target = the consolidated
billing account); only its authenticated confirmation settles, as
exact per-subscriber slices accepted and allocated on each
subscriber's own Starter billing account. Slice 1 admits only exact
full-document allocations against Starter-linked invoices whose
account collectible is exactly the sliced total, so every invoice
mirror stays Starter-owned. Any Starter-touched consolidated payment
outside that shape is refused here, never legacy-fallback; balances
and partial shapes stay on the legacy writer until a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from dotmac_billing import AcceptSettlementV1
from dotmac_billing.commands import AllocationCommand
from dotmac_billing.contracts import SettlementFundingLane
from dotmac_billing.errors import BillingError
from dotmac_billing.service import (
    accept_settlement as billing_accept_settlement,
)
from dotmac_billing.service import (
    allocate_settlement,
    create_billing_account,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_payments import (
    ConfirmationSource,
    GetConfirmedCashV1,
    GetPaymentIntentV1,
    OpenPaymentIntent,
    PaymentError,
    PaymentPurpose,
    RecordConfirmation,
    get_confirmed_cash_v1,
    get_payment_intent_v1,
    open_payment_intent,
    record_confirmation,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingAccount,
    Invoice,
    InvoiceStatus,
    StarterInvoiceLink,
)
from app.models.event_store import EventStore
from app.models.subscriber import Reseller
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.starter_policies import (
    has_starter_invoice_link,
    resolve_starter_billing_policies,
    resolve_starter_reseller_settlement_policies,
    resolve_starter_settlement_policies,
    starter_ledger_live_enabled,
)
from app.services.billing.starter_positions import starter_account_collectible
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "ResellerSettlementSlice",
    "StarterResellerSettlement",
    "StarterResellerSettlementError",
    "reseller_settlement_eligibility_reason",
    "settle_reseller_consolidated",
    "touches_starter_invoices",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class StarterResellerSettlementError(DomainError, ValueError):
    """A refusal at the reseller consolidated Starter boundary."""


def _error(
    suffix: str, message: str, **details: object
) -> StarterResellerSettlementError:
    return StarterResellerSettlementError(
        code=f"financial.starter_reseller_settlement.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class ResellerSettlementSlice:
    invoice_id: UUID
    subscriber_id: UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class StarterResellerSettlement:
    intent_reference: str
    confirmation_id: UUID
    settled_invoice_ids: tuple[UUID, ...]


def touches_starter_invoices(db: Session, invoice_ids: list[UUID]) -> bool:
    return any(has_starter_invoice_link(db, invoice_id) for invoice_id in invoice_ids)


def reseller_settlement_eligibility_reason(
    db: Session,
    *,
    account: BillingAccount,
    slices: tuple[ResellerSettlementSlice, ...],
    total: Decimal,
    external_reference: str | None,
) -> str | None:
    """Why this consolidated payment cannot take the Starter lane, else None.

    Decided before any write: a Starter-touched payment that is
    ineligible is refused, never legacy-fallback.
    """

    if not starter_ledger_live_enabled(db):
        return "Starter ledger live switch is OFF"
    if not slices:
        return "consolidated payment has no invoice slices"
    if not _clean(external_reference):
        return "consolidated payment has no external settlement reference"
    reseller = db.get(Reseller, account.reseller_id)
    if reseller is None or reseller.party_id is None:
        return "reseller has no canonical party identity"
    policies = resolve_starter_billing_policies(db)
    currency = Currency(account.currency.upper(), policies.default_minor_units)
    if total <= 0 or total != sum((s.amount for s in slices), Decimal("0")):
        return "consolidated slices do not reconcile to the confirmed total"
    by_subscriber: dict[UUID, Decimal] = {}
    link_accounts: dict[UUID, UUID] = {}
    for slice_ in slices:
        invoice = db.get(Invoice, slice_.invoice_id)
        if invoice is None:
            return "consolidated slice names a missing invoice"
        if invoice.account_id != slice_.subscriber_id:
            return "consolidated slice account does not own its invoice"
        if not has_starter_invoice_link(db, invoice.id):
            return "mixed consolidated payments stay on the legacy lane in this slice"
        if invoice.currency.upper() != currency.code:
            return "consolidated slices span currencies in this slice"
        if invoice.status not in (InvoiceStatus.issued, InvoiceStatus.overdue):
            return "only open invoices settle in this slice"
        if slice_.amount <= 0 or slice_.amount != invoice.balance_due:
            return "partial consolidated slices stay on the legacy lane in this slice"
        by_subscriber[slice_.subscriber_id] = (
            by_subscriber.get(slice_.subscriber_id, Decimal("0")) + slice_.amount
        )
        link = db.execute(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice.id
            )
        ).scalar_one()
        link_accounts[slice_.subscriber_id] = link.billing_account_id
    for subscriber_id, sliced in by_subscriber.items():
        collectible = starter_account_collectible(
            db,
            billing_account_id=link_accounts[subscriber_id],
            currency_code=currency.code,
        )
        if collectible != sliced:
            return (
                "subscriber has other open Starter obligations; the account "
                "mirror cannot stay exact in this slice"
            )
    return None


def _clean(value: object) -> str:
    return str(value or "").strip()


def _confirmation_source(policies) -> ConfirmationSource:
    try:
        return ConfirmationSource(policies.reseller_confirmation_source)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Reseller confirmation source is not a Starter confirmation source",
            confirmation_source=policies.reseller_confirmation_source,
        ) from exc


def _funding_lane(raw: str) -> SettlementFundingLane:
    try:
        return SettlementFundingLane(raw)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Settlement funding lane is not a Starter funding lane",
            funding_lane=raw,
        ) from exc


def _access_signal_already_emitted(
    db: Session, invoice_id: UUID, confirmation_id: UUID
) -> bool:
    return (
        db.execute(
            select(EventStore.id).where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice_id,
                EventStore.payload["starter_confirmation_id"].astext
                == str(confirmation_id),
            )
        ).first()
        is not None
    )


def settle_reseller_consolidated(
    db: Session,
    *,
    account: BillingAccount,
    payment_id: UUID,
    slices: tuple[ResellerSettlementSlice, ...],
    external_reference: str,
    confirmed_amount: Decimal,
    occurred_at: datetime,
) -> StarterResellerSettlement:
    """Accept and allocate one confirmed reseller payment across subscribers."""

    billing_policies = resolve_starter_billing_policies(db)
    settlement_policies = resolve_starter_settlement_policies(db)
    policies = resolve_starter_reseller_settlement_policies(db)
    currency = Currency(account.currency.upper(), billing_policies.default_minor_units)
    reseller = db.get(Reseller, account.reseller_id)
    assert reseller is not None and reseller.party_id is not None
    reference = policies.reseller_intent_reference(payment_id)

    try:
        confirmed = Money(confirmed_amount, currency)
    except MoneyError as exc:
        raise _error(
            "confirmed_cash_mismatch",
            "Confirmed amount cannot be represented in the account currency",
            reference=reference,
        ) from exc
    payer_ref = billing_policies.subject_ref(
        party_id=reseller.party_id, subscriber_id=account.reseller_id
    )
    try:
        observed_intent = get_payment_intent_v1(
            db, scope=_SCOPE, query=GetPaymentIntentV1(reference=reference)
        )
        if observed_intent is None:
            open_payment_intent(
                db,
                scope=_SCOPE,
                command=OpenPaymentIntent(
                    reference=reference,
                    payer_reference=payer_ref,
                    purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                    target_reference=policies.reseller_target_ref(account.id),
                    requested=confirmed,
                    provider_type=policies.reseller_intent_provider_type,
                    channel=policies.reseller_intent_channel,
                    opened_at=occurred_at,
                    expires_at=occurred_at
                    + timedelta(hours=policies.reseller_intent_expiry_hours),
                ),
            )
            observed_intent = get_payment_intent_v1(
                db, scope=_SCOPE, query=GetPaymentIntentV1(reference=reference)
            )
        if observed_intent is None or (
            observed_intent.purpose is not PaymentPurpose.INVOICE_SETTLEMENT
            or observed_intent.target_reference
            != policies.reseller_target_ref(account.id)
            or observed_intent.payer_reference != payer_ref
            or observed_intent.requested.currency != currency
        ):
            raise _error(
                "intent_binding_mismatch",
                "Payment intent is not bound to this consolidated billing account",
                reference=reference,
            )
        confirmation = record_confirmation(
            db,
            scope=_SCOPE,
            command=RecordConfirmation(
                intent_id=observed_intent.intent_id,
                source=_confirmation_source(policies),
                external_reference=external_reference,
                confirmed=confirmed,
                observed_at=occurred_at,
            ),
        )
        cash = get_confirmed_cash_v1(
            db, scope=_SCOPE, query=GetConfirmedCashV1(confirmation.id)
        )
    except (PaymentError, ValueError) as exc:
        raise _error(
            "starter_refused",
            "Starter payments refused the consolidated confirmation",
            reference=reference,
        ) from exc
    if cash.intent_id != observed_intent.intent_id or cash.confirmed != confirmed:
        raise _error(
            "confirmed_cash_mismatch",
            "Confirmed cash does not match the bound reseller intent",
            reference=reference,
        )
    settled: list[UUID] = []
    try:
        for slice_ in slices:
            invoice = db.get(Invoice, slice_.invoice_id)
            assert invoice is not None  # eligibility proved it
            link = db.execute(
                select(StarterInvoiceLink).where(
                    StarterInvoiceLink.invoice_id == invoice.id
                )
            ).scalar_one()
            billing_account = create_billing_account(
                db,
                scope=_SCOPE,
                external_account_ref=billing_policies.account_ref(slice_.subscriber_id),
                currency=currency.code,
                minor_units=currency.minor_units,
            )
            slice_key = policies.reseller_slice_source_key(
                confirmation_id=confirmation.id,
                subscriber_id=slice_.subscriber_id,
            )
            settlement = billing_accept_settlement(
                db,
                scope=_SCOPE,
                command=AcceptSettlementV1(
                    scope=_SCOPE,
                    billing_account_id=billing_account.id,
                    source_system=settlement_policies.settlement_source_system,
                    source_settlement_key=slice_key,
                    source_version=settlement_policies.settlement_source_version,
                    amount=Money(slice_.amount, currency),
                    occurred_at=occurred_at,
                    observed_at=cash.observed_at,
                    confirmation_evidence=settlement_policies.confirmation_evidence,
                    funding_lane=_funding_lane(settlement_policies.funding_lane),
                ),
                accepted_confirmation_evidence=(
                    settlement_policies.accepted_confirmation_evidence
                ),
            )
            allocate_settlement(
                db,
                scope=_SCOPE,
                command=AllocationCommand(
                    settlement_id=settlement.id,
                    document_id=link.document_id,
                    amount=Money(slice_.amount, currency),
                    occurred_at=occurred_at,
                    source_ref=policies.reseller_allocation_source_ref(slice_key),
                ),
            )
            collectible = starter_account_collectible(
                db,
                billing_account_id=billing_account.id,
                currency_code=currency.code,
            )
            invoice.balance_due = collectible
            if collectible <= 0:
                invoice.status = InvoiceStatus.paid
                if invoice.paid_at is None:
                    invoice.paid_at = occurred_at
            if not _access_signal_already_emitted(db, invoice.id, confirmation.id):
                emit_event(
                    db,
                    EventType.payment_received,
                    {
                        "amount": str(slice_.amount),
                        "currency": invoice.currency,
                        "invoice_id": str(invoice.id),
                        "status": "succeeded",
                        "settlement_lane": "starter",
                        "settlement_kind": "reseller_consolidated",
                        "starter_confirmation_id": str(confirmation.id),
                        "starter_settlement_id": str(settlement.id),
                    },
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )
                AuditEvents.stage(
                    db,
                    AuditEventCreate(
                        action="starter_reseller_consolidated_settlement",
                        entity_type="invoice",
                        entity_id=str(invoice.id),
                        metadata_={
                            "settlement_lane": "starter",
                            "intent_reference": reference,
                            "external_reference": external_reference,
                            "starter_confirmation_id": str(confirmation.id),
                            "starter_settlement_id": str(settlement.id),
                            "starter_document_id": str(link.document_id),
                            "allocated": str(slice_.amount),
                            "collectible_after": str(collectible),
                            "currency": invoice.currency,
                            "billing_account_id": str(account.id),
                        },
                    ),
                )
            settled.append(invoice.id)
    except BillingError as exc:
        raise _error(
            "starter_refused",
            "Starter billing refused the consolidated settlement",
            reference=reference,
        ) from exc
    db.flush()
    return StarterResellerSettlement(
        intent_reference=reference,
        confirmation_id=confirmation.id,
        settled_invoice_ids=tuple(settled),
    )
