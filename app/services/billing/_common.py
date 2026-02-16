"""Common helper functions for billing services.

This module provides validation and calculation helpers shared across
billing service modules.
"""

from decimal import Decimal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    CollectionAccount,
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    Payment,
    PaymentAllocation,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
    PaymentProvider,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.subscriber import Subscriber
from app.services.common import get_by_id, round_money


def _validate_account(db: Session, account_id: str):
    """Validate that a subscriber exists."""
    account = get_by_id(db, Subscriber, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")
    return account


def get_account_credit_balance(db: Session, account_id: str) -> Decimal:
    """Calculate the credit balance for an account from ledger entries.

    Credit balance is calculated as:
    - Sum of credit entries with no invoice (unallocated payments)
    - Minus any debit entries for refunds/adjustments

    Args:
        db: Database session
        account_id: The account to check

    Returns:
        Credit balance (positive means customer has credit)
    """
    from app.services.common import coerce_uuid

    # Get unallocated credits (payments not applied to invoices)
    credit_total = (
        db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
        .filter(LedgerEntry.account_id == coerce_uuid(account_id))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.is_active.is_(True))
        .scalar()
    ) or Decimal("0.00")

    # Get debits against unallocated credits (refunds)
    debit_total = (
        db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
        .filter(LedgerEntry.account_id == coerce_uuid(account_id))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.is_active.is_(True))
        .scalar()
    ) or Decimal("0.00")

    return round_money(Decimal(str(credit_total)) - Decimal(str(debit_total)))


def _validate_invoice_totals(data: dict):
    """Validate invoice monetary totals are consistent."""
    subtotal = data.get("subtotal")
    tax_total = data.get("tax_total")
    total = data.get("total")
    balance_due = data.get("balance_due")
    if subtotal is not None and subtotal < 0:
        raise HTTPException(status_code=400, detail="Subtotal must be non-negative")
    if tax_total is not None and tax_total < 0:
        raise HTTPException(status_code=400, detail="Tax total must be non-negative")
    if total is not None and total < 0:
        raise HTTPException(status_code=400, detail="Total must be non-negative")
    if balance_due is not None and balance_due < 0:
        raise HTTPException(status_code=400, detail="Balance due must be non-negative")
    if subtotal is not None and tax_total is not None and total is not None:
        if round_money(subtotal + tax_total) > round_money(total):
            raise HTTPException(
                status_code=400, detail="Total must cover subtotal and tax"
            )
    if balance_due is not None and total is not None and balance_due > total:
        raise HTTPException(status_code=400, detail="Balance due exceeds total")


def _validate_credit_note_totals(data: dict):
    """Validate credit note monetary totals are consistent."""
    subtotal = data.get("subtotal")
    tax_total = data.get("tax_total")
    total = data.get("total")
    applied_total = data.get("applied_total")
    if subtotal is not None and subtotal < 0:
        raise HTTPException(status_code=400, detail="Subtotal must be non-negative")
    if tax_total is not None and tax_total < 0:
        raise HTTPException(status_code=400, detail="Tax total must be non-negative")
    if total is not None and total < 0:
        raise HTTPException(status_code=400, detail="Total must be non-negative")
    if applied_total is not None and applied_total < 0:
        raise HTTPException(status_code=400, detail="Applied total must be non-negative")
    if subtotal is not None and tax_total is not None and total is not None:
        if round_money(subtotal + tax_total) > round_money(total):
            raise HTTPException(
                status_code=400, detail="Total must cover subtotal and tax"
            )
    if applied_total is not None and total is not None and applied_total > total:
        raise HTTPException(status_code=400, detail="Applied total exceeds total")


def _validate_invoice_line_amount(quantity: Decimal, unit_price: Decimal, amount: Decimal | None):
    """Validate and calculate invoice line amount."""
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than 0")
    if unit_price < 0:
        raise HTTPException(status_code=400, detail="Unit price must be non-negative")
    expected = round_money(quantity * unit_price)
    if amount is None:
        return expected
    if amount < 0:
        raise HTTPException(status_code=400, detail="Amount must be non-negative")
    if round_money(amount) != expected:
        raise HTTPException(status_code=400, detail="Amount must equal quantity * unit price")
    return round_money(amount)


def _resolve_tax_rate(db: Session, tax_rate_id: str | None) -> TaxRate | None:
    """Look up a tax rate by ID."""
    if not tax_rate_id:
        return None
    rate = get_by_id(db, TaxRate, tax_rate_id)
    if not rate:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    return rate


def _validate_invoice_currency(invoice: Invoice, currency: str | None):
    """Validate that currency matches the invoice."""
    if currency and invoice.currency != currency:
        raise HTTPException(status_code=400, detail="Currency does not match invoice")


def _recalculate_invoice_totals(db: Session, invoice: Invoice):
    """Recalculate invoice totals from lines and payments."""
    lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.is_active.is_(True))
        .all()
    )
    if not lines:
        total_lines = (
            db.query(func.count(InvoiceLine.id))
            .filter(InvoiceLine.invoice_id == invoice.id)
            .scalar()
        )
        if total_lines == 0:
            subtotal = round_money(Decimal(str(invoice.subtotal or Decimal("0.00"))))
            tax_total = round_money(Decimal(str(invoice.tax_total or Decimal("0.00"))))
            total = round_money(Decimal(str(invoice.total or subtotal + tax_total)))
            invoice.subtotal = subtotal
            invoice.tax_total = tax_total
            invoice.total = total
        else:
            invoice.subtotal = Decimal("0.00")
            invoice.tax_total = Decimal("0.00")
            invoice.total = Decimal("0.00")
    else:
        # Pre-fetch all tax rates used by lines to avoid N+1 queries
        tax_rate_ids = {line.tax_rate_id for line in lines if line.tax_rate_id}
        tax_rates_map = {}
        if tax_rate_ids:
            tax_rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_rate_ids)).all()
            tax_rates_map = {rate.id: rate for rate in tax_rates}

        subtotal = Decimal("0.00")
        tax_total = Decimal("0.00")
        for line in lines:
            amount = round_money(line.amount)
            subtotal += amount
            if line.tax_rate_id:
                rate = tax_rates_map.get(line.tax_rate_id)
                if rate:
                    rate_percent = Decimal(str(rate.rate))
                    if line.tax_application != TaxApplication.exempt:
                        tax_amount = round_money(amount * rate_percent / Decimal("100.00"))
                        if line.tax_application == TaxApplication.inclusive:
                            tax_amount = round_money(
                                amount
                                - (
                                    amount
                                    / (Decimal("1.00") + rate_percent / Decimal("100.00"))
                                )
                            )
                        tax_total += tax_amount
        subtotal = round_money(subtotal)
        tax_total = round_money(tax_total)
        invoice.subtotal = subtotal
        invoice.tax_total = tax_total
        invoice.total = round_money(subtotal + tax_total)

    paid_amount = (
        db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .scalar()
    )
    paid_amount = round_money(Decimal(str(paid_amount)))
    credit_amount = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.invoice_id == invoice.id)
        .scalar()
    )
    credit_amount = round_money(Decimal(str(credit_amount)))
    invoice.balance_due = max(Decimal("0.00"), round_money(invoice.total - paid_amount - credit_amount))
    if invoice.balance_due <= 0:
        invoice.status = InvoiceStatus.paid
        if not invoice.paid_at:
            invoice.paid_at = datetime.now(timezone.utc)
    elif paid_amount > 0 or credit_amount > 0:
        invoice.status = InvoiceStatus.partially_paid


def _validate_payment_channel(db: Session, channel_id: str | None) -> PaymentChannel | None:
    if not channel_id:
        return None
    channel = get_by_id(db, PaymentChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Payment channel not found")
    if not channel.is_active:
        raise HTTPException(status_code=400, detail="Payment channel is inactive")
    return channel


def _validate_collection_account(
    db: Session, collection_account_id: str | None, currency: str | None
) -> CollectionAccount | None:
    if not collection_account_id:
        return None
    account = get_by_id(db, CollectionAccount, collection_account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Collection account not found")
    if not account.is_active:
        raise HTTPException(status_code=400, detail="Collection account is inactive")
    if currency and account.currency != currency:
        raise HTTPException(
            status_code=400, detail="Collection account currency does not match payment"
        )
    return account


def _resolve_payment_channel(
    db: Session,
    payment_channel_id: str | None,
    payment_method_id: str | None,
    provider_id: str | None,
) -> PaymentChannel | None:
    if payment_channel_id:
        return _validate_payment_channel(db, payment_channel_id)
    if payment_method_id:
        method = get_by_id(db, PaymentMethod, payment_method_id)
        if method and method.payment_channel_id:
            return _validate_payment_channel(db, str(method.payment_channel_id))
    if provider_id:
        query = (
            db.query(PaymentChannel)
            .filter(PaymentChannel.provider_id == provider_id)
            .filter(PaymentChannel.is_active.is_(True))
        )
        channel = query.filter(PaymentChannel.is_default.is_(True)).first()
        if channel:
            return channel
        candidates = query.all()
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise HTTPException(
                status_code=400,
                detail="Multiple payment channels match provider; set a default",
            )
    return None


def _resolve_collection_account(
    db: Session,
    channel: PaymentChannel | None,
    currency: str | None,
    collection_account_id: str | None,
) -> CollectionAccount | None:
    if collection_account_id:
        return _validate_collection_account(db, collection_account_id, currency)
    if not channel:
        return None
    account_query = (
        db.query(PaymentChannelAccount)
        .filter(PaymentChannelAccount.channel_id == channel.id)
        .filter(PaymentChannelAccount.is_active.is_(True))
    )
    if currency:
        exact = (
            account_query.filter(PaymentChannelAccount.currency == currency)
            .order_by(PaymentChannelAccount.is_default.desc(), PaymentChannelAccount.priority.desc())
            .first()
        )
        if exact:
            return _validate_collection_account(
                db, str(exact.collection_account_id), currency
            )
    fallback = (
        account_query.filter(PaymentChannelAccount.currency.is_(None))
        .order_by(PaymentChannelAccount.is_default.desc(), PaymentChannelAccount.priority.desc())
        .first()
    )
    if fallback:
        return _validate_collection_account(
            db, str(fallback.collection_account_id), currency
        )
    if channel.default_collection_account_id:
        return _validate_collection_account(
            db, str(channel.default_collection_account_id), currency
        )
    return None


def _recalculate_credit_note_totals(db: Session, credit_note: CreditNote):
    """Recalculate credit note totals from lines and applications."""
    lines = (
        db.query(CreditNoteLine)
        .filter(CreditNoteLine.credit_note_id == credit_note.id)
        .filter(CreditNoteLine.is_active.is_(True))
        .all()
    )
    if lines:
        # Pre-fetch all tax rates used by lines to avoid N+1 queries
        tax_rate_ids = {line.tax_rate_id for line in lines if line.tax_rate_id}
        tax_rates_map = {}
        if tax_rate_ids:
            tax_rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_rate_ids)).all()
            tax_rates_map = {rate.id: rate for rate in tax_rates}

        subtotal = Decimal("0.00")
        tax_total = Decimal("0.00")
        for line in lines:
            amount = round_money(line.amount)
            subtotal += amount
            if line.tax_rate_id:
                rate = tax_rates_map.get(line.tax_rate_id)
                if rate:
                    rate_percent = Decimal(str(rate.rate))
                    if line.tax_application != TaxApplication.exempt:
                        tax_amount = round_money(amount * rate_percent / Decimal("100.00"))
                        if line.tax_application == TaxApplication.inclusive:
                            tax_amount = round_money(
                                amount
                                - (
                                    amount
                                    / (Decimal("1.00") + rate_percent / Decimal("100.00"))
                                )
                            )
                        tax_total += tax_amount
        subtotal = round_money(subtotal)
        tax_total = round_money(tax_total)
        credit_note.subtotal = subtotal
        credit_note.tax_total = tax_total
        credit_note.total = round_money(subtotal + tax_total)
    else:
        credit_note.subtotal = round_money(Decimal(str(credit_note.subtotal)))
        credit_note.tax_total = round_money(Decimal(str(credit_note.tax_total)))
        credit_note.total = round_money(Decimal(str(credit_note.total)))
    applied_total = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.credit_note_id == credit_note.id)
        .scalar()
    )
    applied_total = round_money(Decimal(str(applied_total)))
    credit_note.applied_total = applied_total
    if applied_total > credit_note.total:
        raise HTTPException(status_code=400, detail="Applied total exceeds credit note total")
    if credit_note.status not in {CreditNoteStatus.draft, CreditNoteStatus.void}:
        if applied_total <= 0:
            credit_note.status = CreditNoteStatus.issued
        elif applied_total < credit_note.total:
            credit_note.status = CreditNoteStatus.partially_applied
        else:
            credit_note.status = CreditNoteStatus.applied


def _validate_payment_linkages(db: Session, account_id: str, invoice_id: str | None, payment_method_id: str | None):
    """Validate payment relationships to account, invoice, and method."""
    _validate_account(db, account_id)
    if invoice_id:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != account_id:
            raise HTTPException(status_code=400, detail="Invoice does not belong to account")
    if payment_method_id:
        method = get_by_id(db, PaymentMethod, payment_method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        if str(method.account_id) != account_id:
            raise HTTPException(
                status_code=400, detail="Payment method does not belong to account"
            )


def _validate_payment_provider(db: Session, provider_id: str | None):
    """Validate that a payment provider exists and is active."""
    if not provider_id:
        return None
    provider = get_by_id(db, PaymentProvider, provider_id)
    if not provider or not provider.is_active:
        raise HTTPException(status_code=404, detail="Payment provider not found")
    return provider


def _validate_ledger_linkages(db: Session, account_id: str, invoice_id: str | None, payment_id: str | None):
    """Validate ledger entry relationships to account, invoice, and payment."""
    _validate_account(db, account_id)
    if invoice_id:
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != account_id:
            raise HTTPException(status_code=400, detail="Invoice does not belong to account")
    if payment_id:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if str(payment.account_id) != account_id:
            raise HTTPException(status_code=400, detail="Payment does not belong to account")
