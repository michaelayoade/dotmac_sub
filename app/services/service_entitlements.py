from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerSource,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.services.common import round_money, to_decimal


def ensure_prepaid_entitlements_for_paid_invoice(
    db: Session, invoice: Invoice
) -> list[ServiceEntitlement]:
    """Create prepaid service entitlements for a paid prepaid invoice.

    This is intentionally idempotent on ``source_invoice_line_id`` so webhook,
    billing-run, and later draft-settlement retries cannot double-grant service.
    """

    if (
        not invoice.is_active
        or invoice.status != InvoiceStatus.paid
        or to_decimal(invoice.balance_due) > 0
    ):
        return []

    created: list[ServiceEntitlement] = []
    lines = (
        db.query(InvoiceLine)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .all()
    )
    lines = _base_subscription_lines(lines)
    for line in lines:
        starts_at, ends_at = _line_period(invoice, line)
        if starts_at is None or ends_at is None:
            continue
        existing = (
            db.query(ServiceEntitlement)
            .filter(ServiceEntitlement.source_invoice_line_id == line.id)
            .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
            .first()
        )
        if existing is not None:
            continue
        entitlement = ServiceEntitlement(
            account_id=invoice.account_id,
            subscription_id=line.subscription_id,
            source_invoice_id=invoice.id,
            source_invoice_line_id=line.id,
            starts_at=starts_at,
            ends_at=ends_at,
            amount_funded=round_money(to_decimal(line.amount)),
            currency=invoice.currency or "NGN",
            status=ServiceEntitlementStatus.active,
            metadata_={"source": "paid_prepaid_invoice"},
        )
        db.add(entitlement)
        created.append(entitlement)
    if created:
        db.flush()
    return created


def ensure_prepaid_entitlement_for_wallet_debit(
    db: Session,
    *,
    subscription: Subscription,
    ledger_entry: LedgerEntry,
    starts_at: datetime,
    ends_at: datetime,
) -> ServiceEntitlement | None:
    """Create prepaid service entitlement for a direct wallet-funded renewal."""

    if ledger_entry.source != LedgerSource.invoice or not ledger_entry.is_active:
        return None
    metadata = ledger_entry.memo or ""
    candidates = (
        db.query(ServiceEntitlement)
        .filter(ServiceEntitlement.subscription_id == subscription.id)
        .filter(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .filter(ServiceEntitlement.starts_at == starts_at)
        .filter(ServiceEntitlement.ends_at == ends_at)
        .all()
    )
    for candidate in candidates:
        if (candidate.metadata_ or {}).get("source_ledger_entry_id") == str(
            ledger_entry.id
        ):
            return candidate
    entitlement = ServiceEntitlement(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        starts_at=starts_at,
        ends_at=ends_at,
        amount_funded=round_money(to_decimal(ledger_entry.amount)),
        currency=ledger_entry.currency or "NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={
            "source": "wallet_prepaid_renewal",
            "source_ledger_entry_id": str(ledger_entry.id),
            "memo": metadata,
        },
    )
    db.add(entitlement)
    db.flush()
    return entitlement


def _base_subscription_lines(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    base_lines = [
        line
        for line in lines
        if (line.metadata_ or {}).get("kind") == "base_subscription"
    ]
    if base_lines:
        return base_lines
    billable_lines = [
        line for line in lines if round_money(to_decimal(line.amount)) > 0
    ]
    if len(billable_lines) == 1:
        return billable_lines
    if len(lines) == 1:
        return lines
    return []


def _line_period(
    invoice: Invoice, line: InvoiceLine
) -> tuple[datetime | None, datetime | None]:
    metadata = line.metadata_ or {}
    starts_at = _coerce_datetime(metadata.get("billing_period_start"))
    ends_at = _coerce_datetime(metadata.get("billing_period_end"))
    if starts_at is not None and ends_at is not None:
        return starts_at, ends_at
    return invoice.billing_period_start, invoice.billing_period_end


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
