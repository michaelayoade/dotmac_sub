"""Prepaid period funding and settlement through Starter (ADR 0031).

Service-first prepaid slice: a confirmed ``dotmac-payments``
purchase-funding intent is accepted into ``dotmac-billing`` on the
prepaid funding lane and earmarked in full to named subscription
periods; the structural :class:`StarterPrepaidFundingLink` rows let
the renewal cycle discover each period's settlement. When a renewal
period invoice's line carries a funded ``billing_line_key``, the
period settles through Starter instead of legacy writers: the draft
issues through the prepaid issuance flavor (its obligation source
fact equals the earmark target), the earmarked settlement allocates
to the document, the Sub invoice mirrors the Starter collectible
position, and exactly one ``payment.received`` access signal is
emitted per funding confirmation for that invoice.

Intent opening stays with the payment caller (gateway or staff);
this module never opens intents and never writes legacy money state.
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
from dotmac_billing.earmark_contracts import (
    EarmarkSettlementV1,
    SettlementEarmarkTargetV1,
)
from dotmac_billing.earmarks import earmark_settlement
from dotmac_billing.errors import BillingError
from dotmac_billing.service import (
    accept_settlement,
    allocate_settlement,
    create_billing_account,
)
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, StarterPrepaidFundingLink
from app.models.event_store import EventStore
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.starter_issuance import issue_prepaid_invoice_via_starter
from app.services.billing.starter_policies import (
    StarterPrepaidFundingPolicies,
    resolve_starter_billing_policies,
    resolve_starter_prepaid_funding_policies,
    resolve_starter_settlement_policies,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "PrepaidPeriodTarget",
    "StarterPrepaidError",
    "StarterPrepaidFunding",
    "StarterPrepaidSettlement",
    "fund_prepaid_purchase",
    "funding_link_for_invoice",
    "settle_prepaid_period_invoice",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_OWNER = "financial.starter_prepaid"


class StarterPrepaidError(DomainError, ValueError):
    """The Starter prepaid lane refused or failed."""


def _error(suffix: str, message: str, **details: object) -> StarterPrepaidError:
    return StarterPrepaidError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class PrepaidPeriodTarget:
    """One funded subscription period, keyed by its renewal line key."""

    subscription_id: UUID
    billing_line_key: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class StarterPrepaidFunding:
    """Starter identities accepted for one prepaid purchase."""

    billing_account_id: UUID
    confirmation_id: UUID
    settlement_id: UUID
    links: tuple[StarterPrepaidFundingLink, ...]


@dataclass(frozen=True, slots=True)
class StarterPrepaidSettlement:
    """Starter identities for one settled prepaid period invoice."""

    document_id: UUID
    allocated: Decimal
    collectible_after: Decimal


def _confirmation_source(
    policies: StarterPrepaidFundingPolicies,
) -> ConfirmationSource:
    try:
        return ConfirmationSource(policies.prepaid_confirmation_source)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Prepaid confirmation source is not a Starter confirmation source",
            confirmation_source=policies.prepaid_confirmation_source,
        ) from exc


def _funding_lane(policies: StarterPrepaidFundingPolicies) -> SettlementFundingLane:
    try:
        return SettlementFundingLane(policies.prepaid_funding_lane)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Prepaid funding lane is not a Starter funding lane",
            funding_lane=policies.prepaid_funding_lane,
        ) from exc


def funding_link_for_invoice(
    db: Session, invoice: Invoice
) -> StarterPrepaidFundingLink | None:
    """The earmark link for this draft's single funded period line, if any."""
    keys = {
        line.billing_line_key
        for line in invoice.lines
        if line.is_active and line.billing_line_key
    }
    if len(keys) != 1:
        return None
    return db.execute(
        select(StarterPrepaidFundingLink).where(
            StarterPrepaidFundingLink.billing_line_key == next(iter(keys))
        )
    ).scalar_one_or_none()


def fund_prepaid_purchase(
    db: Session,
    *,
    subscriber_id: UUID,
    purchase_ref: str,
    intent_reference: str,
    external_reference: str,
    confirmed_amount: Decimal,
    currency_code: str,
    targets: tuple[PrepaidPeriodTarget, ...],
    occurred_at: datetime,
) -> StarterPrepaidFunding:
    """Accept one confirmed purchase and earmark it to period targets."""
    if not targets:
        raise _error("no_targets", "Prepaid funding needs at least one period target")
    if sum((t.amount for t in targets), Decimal("0")) != confirmed_amount:
        raise _error(
            "targets_do_not_cover_purchase",
            "Prepaid period targets must earmark the confirmed amount in full",
        )
    policies = resolve_starter_billing_policies(db)
    funding_policies = resolve_starter_prepaid_funding_policies(db)
    settlement_policies = resolve_starter_settlement_policies(db)
    currency = Currency(currency_code.upper(), policies.default_minor_units)

    existing = (
        db.execute(
            select(StarterPrepaidFundingLink).where(
                StarterPrepaidFundingLink.purchase_ref == purchase_ref
            )
        )
        .scalars()
        .all()
    )
    if existing:
        first = existing[0]
        return StarterPrepaidFunding(
            billing_account_id=first.billing_account_id,
            confirmation_id=first.confirmation_id,
            settlement_id=first.settlement_id,
            links=tuple(existing),
        )

    try:
        confirmed = Money(confirmed_amount, currency)
    except MoneyError as exc:
        raise _error(
            "confirmed_cash_mismatch",
            "Confirmed amount cannot be represented in the purchase currency",
            reference=intent_reference,
        ) from exc
    try:
        observed_intent = get_payment_intent_v1(
            db, scope=_SCOPE, query=GetPaymentIntentV1(reference=intent_reference)
        )
        if observed_intent is None:
            raise _error(
                "intent_binding_mismatch",
                "No Starter payments intent exists for this purchase reference",
                reference=intent_reference,
            )
        if (
            observed_intent.purpose is not PaymentPurpose.PURCHASE_FUNDING
            or observed_intent.target_reference != purchase_ref
            or observed_intent.payer_reference != str(subscriber_id)
            or observed_intent.requested.currency != currency
        ):
            raise _error(
                "intent_binding_mismatch",
                "Payment intent is not bound to this prepaid purchase",
                reference=intent_reference,
            )
        confirmation = record_confirmation(
            db,
            scope=_SCOPE,
            command=RecordConfirmation(
                intent_id=observed_intent.intent_id,
                source=_confirmation_source(funding_policies),
                external_reference=external_reference,
                confirmed=confirmed,
                observed_at=occurred_at,
            ),
        )
        cash = get_confirmed_cash_v1(
            db, scope=_SCOPE, query=GetConfirmedCashV1(confirmation.id)
        )
    except StarterPrepaidError:
        raise
    except PaymentError as exc:
        raise _error(
            "starter_refused",
            "Starter payments refused the prepaid funding confirmation",
            reference=intent_reference,
        ) from exc
    if cash.intent_id != observed_intent.intent_id or cash.confirmed != confirmed:
        raise _error(
            "confirmed_cash_mismatch",
            "Confirmed cash does not match the prepaid purchase amount",
            reference=intent_reference,
        )
    try:
        account = create_billing_account(
            db,
            scope=_SCOPE,
            external_account_ref=policies.account_ref(subscriber_id),
            currency=currency.code,
            minor_units=currency.minor_units,
        )
        settlement = accept_settlement(
            db,
            scope=_SCOPE,
            command=AcceptSettlementV1(
                scope=_SCOPE,
                billing_account_id=account.id,
                source_system=settlement_policies.settlement_source_system,
                source_settlement_key=str(confirmation.id),
                source_version=settlement_policies.settlement_source_version,
                amount=confirmed,
                occurred_at=occurred_at,
                observed_at=cash.observed_at,
                confirmation_evidence=settlement_policies.confirmation_evidence,
                funding_lane=_funding_lane(funding_policies),
            ),
            accepted_confirmation_evidence=(
                settlement_policies.accepted_confirmation_evidence
            ),
        )
        earmark_settlement(
            db,
            scope=_SCOPE,
            command=EarmarkSettlementV1(
                scope=_SCOPE,
                billing_account_id=account.id,
                settlement_id=settlement.id,
                amount=confirmed,
                producer_ref=purchase_ref,
                targets=tuple(
                    SettlementEarmarkTargetV1(
                        source_system=policies.source_system,
                        source_kind=policies.prepaid_source_kind,
                        source_fact_id=policies.prepaid_source_fact_id(
                            target.billing_line_key
                        ),
                        source_fact_version=policies.source_fact_version,
                        amount=Money(target.amount, currency),
                    )
                    for target in targets
                ),
                source_ref=funding_policies.prepaid_earmark_source_ref(confirmation.id),
                occurred_at=occurred_at,
            ),
        )
    except BillingError as exc:
        raise _error(
            "starter_refused",
            "Starter billing refused the prepaid funding acceptance",
            reference=intent_reference,
        ) from exc

    links = tuple(
        StarterPrepaidFundingLink(
            tenant_id=OPERATOR_TENANT_ID,
            subscription_id=target.subscription_id,
            billing_line_key=target.billing_line_key,
            source_fact_id=policies.prepaid_source_fact_id(target.billing_line_key),
            billing_account_id=account.id,
            settlement_id=settlement.id,
            confirmation_id=confirmation.id,
            purchase_ref=purchase_ref,
        )
        for target in targets
    )
    db.add_all(links)
    db.flush()
    return StarterPrepaidFunding(
        billing_account_id=account.id,
        confirmation_id=confirmation.id,
        settlement_id=settlement.id,
        links=links,
    )


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


def settle_prepaid_period_invoice(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterPrepaidFundingLink,
    issued_at: datetime,
) -> StarterPrepaidSettlement:
    """Issue one funded period draft and allocate its earmark in Starter."""
    policies = resolve_starter_billing_policies(db)
    currency = Currency(invoice.currency.upper(), policies.default_minor_units)
    starter_link = issue_prepaid_invoice_via_starter(
        db,
        invoice=invoice,
        issued_at=issued_at,
        billing_line_key=link.billing_line_key,
    )
    try:
        funding_policies = resolve_starter_prepaid_funding_policies(db)
        allocate_settlement(
            db,
            scope=_SCOPE,
            command=AllocationCommand(
                settlement_id=link.settlement_id,
                document_id=starter_link.document_id,
                amount=Money(invoice.total, currency),
                occurred_at=issued_at,
                source_ref=funding_policies.prepaid_allocation_source_ref(
                    link.source_fact_id
                ),
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
    except BillingError as exc:
        raise _error(
            "starter_refused",
            "Starter billing refused the prepaid period allocation",
            invoice_id=str(invoice.id),
        ) from exc
    collectible = (
        current.position.collectible_receivable.amount
        if current.position is not None
        else Decimal("0")
    )
    invoice.balance_due = collectible
    if collectible <= 0:
        invoice.status = InvoiceStatus.paid
        invoice.paid_at = issued_at
    elif invoice.total > 0:
        invoice.status = InvoiceStatus.partially_paid
    if not _access_signal_already_emitted(db, invoice.id, link.confirmation_id):
        emit_event(
            db,
            EventType.payment_received,
            {
                "amount": str(invoice.total),
                "currency": invoice.currency,
                "invoice_id": str(invoice.id),
                "status": "succeeded",
                "settlement_lane": "starter_prepaid",
                "starter_confirmation_id": str(link.confirmation_id),
                "starter_settlement_id": str(link.settlement_id),
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
        AuditEvents.stage(
            db,
            AuditEventCreate(
                action="starter_prepaid_period_settlement",
                entity_type="invoice",
                entity_id=str(invoice.id),
                metadata_={
                    "settlement_lane": "starter_prepaid",
                    "billing_line_key": link.billing_line_key,
                    "purchase_ref": link.purchase_ref,
                    "starter_confirmation_id": str(link.confirmation_id),
                    "starter_settlement_id": str(link.settlement_id),
                    "starter_document_id": str(starter_link.document_id),
                    "allocated": str(invoice.total),
                    "collectible_after": str(collectible),
                    "currency": invoice.currency,
                },
            ),
        )
    db.flush()
    return StarterPrepaidSettlement(
        document_id=starter_link.document_id,
        allocated=invoice.total,
        collectible_after=collectible,
    )
