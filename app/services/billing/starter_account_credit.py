"""Atomic account-credit bridge for Starter-issued invoices.

The legacy payment owner proves and reserves exact settled credit before a
draft is issued. After Starter creates the receivable, this participant lands
each reserved slice in Starter's available-credit lane, applies it to the
document, and records the matching Sub payment allocation. All writes share
the caller's transaction, so neither ledger can consume the money alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from dotmac_billing import AcceptSettlementV1
from dotmac_billing.commands import ApplyAvailableCreditCommand
from dotmac_billing.contracts import SettlementFundingLane
from dotmac_billing.errors import BillingError
from dotmac_billing.service import accept_settlement, apply_available_credit
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from sqlalchemy.orm import Session

from app.models.billing import Invoice, Payment, PaymentStatus, StarterInvoiceLink
from app.services.billing.account_credit import (
    AccountCreditApplications,
    InvoiceIssuanceFundingReservation,
)
from app.services.billing.starter_policies import (
    StarterAccountCreditPolicies,
    resolve_starter_account_credit_policies,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterAccountCreditResult",
    "StarterAccountCreditError",
    "apply_reserved_account_credit",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)


class StarterAccountCreditError(DomainError, ValueError):
    """The exact cross-ledger account-credit application failed closed."""


def _error(suffix: str, message: str, **details: object) -> StarterAccountCreditError:
    return StarterAccountCreditError(
        code=f"financial.starter_account_credit.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class StarterAccountCreditResult:
    """Exact payment-backed credit consumed for one Starter document."""

    invoice_id: UUID
    document_id: UUID
    applied: Decimal
    invoice_remaining: Decimal
    allocation_ids: tuple[UUID, ...]


def _funding_lane(policies: StarterAccountCreditPolicies) -> SettlementFundingLane:
    try:
        lane = SettlementFundingLane(policies.funding_lane)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Account-credit funding lane is not a Starter funding lane",
            funding_lane=policies.funding_lane,
        ) from exc
    if lane is not SettlementFundingLane.AVAILABLE_CREDIT:
        raise _error(
            "configuration_invalid",
            "Account credit must enter Starter's available-credit lane",
            funding_lane=policies.funding_lane,
        )
    return lane


def apply_reserved_account_credit(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    reservation: InvoiceIssuanceFundingReservation,
    occurred_at: datetime,
) -> StarterAccountCreditResult:
    """Apply one pre-issuance reservation to a newly issued Starter document."""

    if (
        invoice.id != reservation.invoice_id
        or invoice.account_id != reservation.account_id
        or link.invoice_id != invoice.id
        or reservation.reserved_amount <= Decimal("0.00")
    ):
        raise _error(
            "reservation_mismatch",
            "Starter account-credit reservation does not match the issued invoice",
            invoice_id=str(invoice.id),
        )

    policies = resolve_starter_account_credit_policies(db)
    lane = _funding_lane(policies)
    currency = Currency(reservation.currency, 2)
    sources: list[tuple[Payment, UUID, Decimal, datetime]] = []
    for source in reservation.sources:
        payment = db.get(Payment, source.payment_id)
        settlement = payment.settlement if payment is not None else None
        if (
            payment is None
            or settlement is None
            or payment.account_id != invoice.account_id
            or payment.status is not PaymentStatus.succeeded
            or not payment.is_active
            or payment.currency.upper() != reservation.currency
        ):
            raise _error(
                "source_changed",
                "Reserved payment credit changed before Starter could apply it",
                payment_id=str(source.payment_id),
                invoice_id=str(invoice.id),
            )
        sources.append((payment, settlement.id, source.amount, settlement.created_at))

    try:
        for _payment, settlement_id, amount, observed_at in sources:
            money = Money(amount, currency)
            idempotency_key = f"{settlement_id}:{invoice.id}"
            accept_settlement(
                db,
                scope=_SCOPE,
                command=AcceptSettlementV1(
                    scope=_SCOPE,
                    billing_account_id=link.billing_account_id,
                    source_system=policies.settlement_source_system,
                    source_settlement_key=policies.settlement_key(idempotency_key),
                    source_version=policies.settlement_source_version,
                    amount=money,
                    occurred_at=observed_at,
                    observed_at=observed_at,
                    confirmation_evidence=policies.confirmation_evidence,
                    funding_lane=lane,
                ),
                accepted_confirmation_evidence=(
                    policies.accepted_confirmation_evidence
                ),
            )
            apply_available_credit(
                db,
                scope=_SCOPE,
                command=ApplyAvailableCreditCommand(
                    billing_account_id=link.billing_account_id,
                    document_id=link.document_id,
                    amount=money,
                    occurred_at=occurred_at,
                    source_ref=policies.application_source_ref(idempotency_key),
                ),
            )
    except (BillingError, MoneyError) as exc:
        raise _error(
            "starter_refused",
            "Starter refused the reserved account-credit application",
            invoice_id=str(invoice.id),
        ) from exc

    legacy = AccountCreditApplications.apply_starter_invoice_issuance_funding(
        db,
        invoice,
        reservation=reservation,
    )
    return StarterAccountCreditResult(
        invoice_id=invoice.id,
        document_id=link.document_id,
        applied=legacy.applied,
        invoice_remaining=legacy.invoice_remaining,
        allocation_ids=tuple(UUID(value) for value in legacy.allocation_ids),
    )
