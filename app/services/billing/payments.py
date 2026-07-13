"""Payment and payment method management services."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    BankAccount,
    BankAccountType,
    CollectionAccount,
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.billing import (
    BankAccountCreate,
    BankAccountUpdate,
    CollectionAccountCreate,
    CollectionAccountUpdate,
    PaymentAllocationCreate,
    PaymentChannelAccountCreate,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentMethodCreate,
    PaymentMethodUpdate,
    PaymentUpdate,
)
from app.services import settings_spec
from app.services.billing._common import (
    _assert_invoice_allocatable,
    _recalculate_invoice_totals,
    _resolve_collection_account,
    _resolve_payment_channel,
    _validate_account,
    _validate_collection_account,
    _validate_invoice_currency,
    _validate_payment_channel,
    _validate_payment_linkages,
    _validate_payment_provider,
    lock_account,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.response import ListResponseMixin
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    ensure_prepaid_entitlements_for_paid_invoice,
)

logger = logging.getLogger(__name__)


# Allowed payment status transitions for the gateway/webhook-driven
# ``mark_status`` path. Gateways re-deliver and deliver out of order, so a late
# ``charge.success`` after a refund, or a late ``charge.failed`` after success,
# must NOT regress committed financial state. ``refunded``/``canceled`` are
# sinks; ``succeeded`` cannot go to ``failed`` here (use ``reverse_payment``).
_ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.pending: {
        PaymentStatus.succeeded,
        PaymentStatus.failed,
        PaymentStatus.canceled,
    },
    PaymentStatus.failed: {PaymentStatus.succeeded, PaymentStatus.canceled},
    PaymentStatus.succeeded: {
        PaymentStatus.refunded,
        PaymentStatus.partially_refunded,
    },
    # succeeded -> failed is deliberately ABSENT. This table guards ``mark_status``,
    # which is what provider webhooks drive, and a replayed or out-of-order webhook
    # must never regress a succeeded payment to failed
    # (see tests/test_payment_mark_status_guard.py).
    #
    # A genuine chargeback or bank reversal is a deliberate domain operation, not a
    # status flip, and goes through ``Refunds.reverse_payment`` — which has its own
    # guard and posts the reversing ledger entry. The escape hatch is the domain op,
    # not a hole in the table.
    PaymentStatus.partially_refunded: {PaymentStatus.refunded},
    PaymentStatus.refunded: set(),
    PaymentStatus.canceled: set(),
}


class PaymentMethods(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentMethodCreate):
        # Exactly one owner: account (customer subscriber) or reseller org
        # (Layer 3 #329). Validate the account only when account-owned.
        if (payload.account_id is None) == (payload.reseller_id is None):
            raise HTTPException(
                status_code=400,
                detail="Exactly one of account_id or reseller_id is required",
            )
        if payload.account_id is not None:
            _validate_account(db, str(payload.account_id))
        if payload.payment_channel_id:
            _validate_payment_channel(db, str(payload.payment_channel_id))
        if payload.is_default:
            owner_filter = (
                PaymentMethod.account_id == payload.account_id
                if payload.account_id is not None
                else PaymentMethod.reseller_id == payload.reseller_id
            )
            db.query(PaymentMethod).filter(
                owner_filter,
                PaymentMethod.is_default.is_(True),
            ).update({"is_default": False})
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "method_type" not in fields_set:
            default_method = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_payment_method_type"
            )
            if default_method:
                data["method_type"] = validate_enum(
                    default_method, PaymentMethodType, "method_type"
                )
        if data.get("token"):
            data["token"] = encrypt_credential(data["token"])
        method = PaymentMethod(**data)
        db.add(method)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def get(db: Session, method_id: str):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        return method

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentMethod)
        if account_id:
            query = query.filter(PaymentMethod.account_id == account_id)
        if is_active is None:
            query = query.filter(PaymentMethod.is_active.is_(True))
        else:
            query = query.filter(PaymentMethod.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentMethod.created_at,
                "method_type": PaymentMethod.method_type,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, method_id: str, payload: PaymentMethodUpdate):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = data.get("account_id", method.account_id)
        # Reseller-org-owned methods (Layer 3 #329) have no account_id; only
        # validate/scope by account when account-owned, else by reseller.
        if account_id is not None:
            _validate_account(db, str(account_id))
        if "payment_channel_id" in data:
            _validate_payment_channel(
                db,
                str(data["payment_channel_id"]) if data["payment_channel_id"] else None,
            )
        if data.get("is_default"):
            owner_filter = (
                PaymentMethod.account_id == account_id
                if account_id is not None
                else PaymentMethod.reseller_id == method.reseller_id
            )
            db.query(PaymentMethod).filter(
                owner_filter,
                PaymentMethod.id != method.id,
                PaymentMethod.is_default.is_(True),
            ).update({"is_default": False})
        if "token" in data and data["token"]:
            data["token"] = encrypt_credential(data["token"])
        for key, value in data.items():
            setattr(method, key, value)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def get_decrypted_token(db: Session, method_id: str) -> str | None:
        """Retrieve and decrypt the payment token for a payment method."""
        method = get_by_id(db, PaymentMethod, method_id)
        if not method or not method.token:
            return None
        return decrypt_credential(method.token)

    @staticmethod
    def delete(db: Session, method_id: str):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        method.is_active = False
        db.commit()


class BankAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BankAccountCreate):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "account_type" not in fields_set:
            default_account_type = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_bank_account_type"
            )
            if default_account_type:
                data["account_type"] = validate_enum(
                    default_account_type, BankAccountType, "account_type"
                )
        if payload.payment_method_id:
            method = get_by_id(db, PaymentMethod, payload.payment_method_id)
            if not method:
                raise HTTPException(status_code=404, detail="Payment method not found")
            if method.account_id != payload.account_id:
                raise HTTPException(
                    status_code=400, detail="Payment method does not belong to account"
                )
        if payload.is_default:
            db.query(BankAccount).filter(
                BankAccount.account_id == payload.account_id,
                BankAccount.is_default.is_(True),
            ).update({"is_default": False})
        if data.get("token"):
            data["token"] = encrypt_credential(data["token"])
        bank_account = BankAccount(**data)
        db.add(bank_account)
        db.commit()
        db.refresh(bank_account)
        return bank_account

    @staticmethod
    def get(db: Session, bank_account_id: str):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        return bank_account

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BankAccount)
        if account_id:
            query = query.filter(BankAccount.account_id == account_id)
        if is_active is None:
            query = query.filter(BankAccount.is_active.is_(True))
        else:
            query = query.filter(BankAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BankAccount.created_at, "bank_name": BankAccount.bank_name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, bank_account_id: str, payload: BankAccountUpdate):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = data.get("account_id", bank_account.account_id)
        _validate_account(db, str(account_id))
        payment_method_id = data.get(
            "payment_method_id", bank_account.payment_method_id
        )
        if payment_method_id:
            method = get_by_id(db, PaymentMethod, payment_method_id)
            if not method:
                raise HTTPException(status_code=404, detail="Payment method not found")
            if method.account_id != account_id:
                raise HTTPException(
                    status_code=400, detail="Payment method does not belong to account"
                )
        if data.get("is_default"):
            db.query(BankAccount).filter(
                BankAccount.account_id == account_id,
                BankAccount.id != bank_account.id,
                BankAccount.is_default.is_(True),
            ).update({"is_default": False})
        if "token" in data and data["token"]:
            data["token"] = encrypt_credential(data["token"])
        for key, value in data.items():
            setattr(bank_account, key, value)
        db.commit()
        db.refresh(bank_account)
        return bank_account

    @staticmethod
    def delete(db: Session, bank_account_id: str):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        bank_account.is_active = False
        db.commit()


def _create_payment_ledger_entry(
    db: Session,
    payment: Payment,
    invoice: Invoice | None = None,
    allocation_amount: Decimal | None = None,
) -> LedgerEntry | None:
    """Create a ledger entry for a payment or allocation.

    The ledger entry's ``account_id`` follows the invoice's subscriber when
    allocating to a specific invoice (correct for consolidated payments, where
    the payment itself has no single account). Unallocated-credit entries are
    only written for account-scoped payments; consolidated-payment surplus is
    held on ``BillingAccount.balance`` instead.
    """
    if invoice is None and payment.account_id is None:
        # Consolidated payment remainder goes to BillingAccount.balance,
        # not to a per-subscriber ledger entry.
        return None

    # Idempotency check: skip if an active ledger entry already exists for this
    # payment/invoice. If a prior allocation was voided/refunded, the soft
    # deleted entry can be reactivated by the caller below.
    existing_entry = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id == (invoice.id if invoice else None))
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.is_active.is_(True))
        .first()
    )
    if existing_entry:
        return existing_entry

    amount = allocation_amount if allocation_amount is not None else payment.amount
    memo = f"Payment {payment.id}"
    if invoice:
        memo = f"Payment {payment.id} applied to Invoice {invoice.invoice_number or invoice.id}"

    account_id = invoice.account_id if invoice is not None else payment.account_id

    entry = LedgerEntry(
        account_id=account_id,
        invoice_id=invoice.id if invoice else None,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=round_money(to_decimal(amount)),
        currency=payment.currency or "NGN",
        memo=memo,
    )
    db.add(entry)
    return entry


def _find_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
) -> PaymentAllocation | None:
    """Return the active allocation for a payment/invoice pair, if present."""
    return (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .first()
    )


def _find_inactive_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
) -> PaymentAllocation | None:
    """Return a soft-deleted allocation for re-use after void/refund reversal."""
    return (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(False))
        .first()
    )


def _apply_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
    amount: Decimal,
    *,
    memo: str | None = None,
) -> tuple[PaymentAllocation, Decimal]:
    """Create or reuse one payment allocation and its invoice ledger entry.

    Returns the allocation plus the amount that should reduce the payment's
    remaining allocatable balance.
    """
    existing = _find_payment_allocation(db, payment, invoice)
    if existing:
        # Idempotent re-runs must not recreate the invoice ledger credit or
        # report the old allocation as newly applied. The original allocation
        # and ledger effect already exist; returning 0 keeps callers from
        # consuming account credit a second time.
        return existing, Decimal("0.00")

    applied_amount = round_money(to_decimal(amount))
    inactive = _find_inactive_payment_allocation(db, payment, invoice)
    if inactive:
        inactive.amount = applied_amount
        inactive.memo = memo
        inactive.is_active = True
        entry = _create_payment_ledger_entry(db, payment, invoice, applied_amount)
        if entry is not None:
            entry.amount = applied_amount
            entry.currency = payment.currency or invoice.currency or "NGN"
            entry.is_active = True
        return inactive, applied_amount

    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=applied_amount,
        memo=memo,
    )
    db.add(allocation)
    _create_payment_ledger_entry(db, payment, invoice, applied_amount)
    return allocation, applied_amount


def _record_unallocated_payment_credit(
    db: Session,
    payment: Payment,
    remaining: Decimal,
) -> None:
    """Record the unallocated payment surplus.

    For an account-scoped payment, this writes a ledger entry against the
    payer's subscriber account. For a consolidated (billing-account-scoped)
    payment, the surplus increments ``BillingAccount.balance`` instead.
    """
    remaining = round_money(to_decimal(remaining))
    if remaining <= 0:
        return
    if payment.billing_account_id is not None:
        from app.services.billing.billing_accounts import BillingAccounts

        BillingAccounts.credit_balance(db, str(payment.billing_account_id), remaining)
        return
    _create_payment_ledger_entry(db, payment, None, remaining)


def _open_invoice_balance_exists(db: Session, account_id, currency: str) -> bool:
    return (
        db.query(Invoice.id)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .filter(Invoice.currency == currency)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .first()
        is not None
    )


def _existing_prepaid_renewal_debit(
    db: Session, payment: Payment
) -> LedgerEntry | None:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.source == LedgerSource.invoice)
        .filter(LedgerEntry.is_active.is_(True))
        .first()
    )


def _active_prepaid_monthly_subscription(
    db: Session,
    account_id,
) -> Subscription | None:
    rows = (
        db.query(Subscription)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .filter(Subscription.subscriber_id == account_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(CatalogOffer.billing_cycle == BillingCycle.monthly)
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(Subscription.created_at.asc(), Subscription.id.asc())
        .limit(2)
        .all()
    )
    if len(rows) != 1:
        return None
    return rows[0]


def _prepaid_monthly_charge_amount(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> tuple[Decimal, str, BillingCycle] | None:
    from app.services.billing._common import _calculate_tax_amount
    from app.services.billing_automation import (
        _default_tax_application,
        _effective_unit_price,
        _resolve_price,
        _resolve_tax_rate_id,
    )

    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        return None
    effective_cycle = cycle or BillingCycle.monthly
    if effective_cycle != BillingCycle.monthly:
        return None
    base = _effective_unit_price(subscription, amount, effective_at)
    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    if not tax_rate_id:
        return base, currency or "NGN", effective_cycle
    tax_rate = db.get(TaxRate, tax_rate_id)
    if tax_rate is None:
        return base, currency or "NGN", effective_cycle
    tax_application = _default_tax_application(db)
    tax_amount = _calculate_tax_amount(
        base, Decimal(str(tax_rate.rate)), tax_application
    )
    total = (
        base
        if tax_application == TaxApplication.inclusive
        else round_money(base + tax_amount)
    )
    return total, currency or "NGN", effective_cycle


def apply_prepaid_service_credit(
    db: Session,
    payment: Payment,
) -> bool:
    """Consume unallocated credit for one active prepaid monthly renewal.

    This is intentionally narrow: it runs only for succeeded account-scoped
    payments, only when no open invoice remains, and only when exactly one active
    prepaid monthly service exists. It leaves ambiguous wallet credit untouched.
    """
    if payment.account_id is None or payment.status != PaymentStatus.succeeded:
        return False
    if _existing_prepaid_renewal_debit(db, payment):
        return False
    currency = payment.currency or "NGN"
    if _open_invoice_balance_exists(db, payment.account_id, currency):
        return False
    subscription = _active_prepaid_monthly_subscription(db, payment.account_id)
    if subscription is None:
        return False

    effective_at = payment.paid_at or datetime.now(UTC)
    charge = _prepaid_monthly_charge_amount(db, subscription, effective_at)
    if charge is None:
        return False
    charge_amount, charge_currency, cycle = charge
    if charge_currency != currency:
        return False

    from app.services.billing._common import get_account_credit_balance
    from app.services.billing_automation import (
        _as_utc,
        _paid_coverage_end_for_subscription,
        _period_end,
    )

    # effective_at is never None here (payment.paid_at or now()), so _as_utc is
    # non-None — assert to narrow for the type checker.
    effective_utc = _as_utc(effective_at)
    assert effective_utc is not None
    paid_at_day = effective_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    next_billing = _as_utc(subscription.next_billing_at) or paid_at_day
    period_start = max(next_billing, paid_at_day)
    period_end = _period_end(period_start, cycle)
    paid_through = _paid_coverage_end_for_subscription(
        db,
        subscription.id,
        subscription.subscriber_id,
        period_start,
        period_end,
    )
    if paid_through and paid_through > period_start:
        if subscription.next_billing_at is None or next_billing < paid_through:
            subscription.next_billing_at = paid_through
        return False

    db.flush()
    available = get_account_credit_balance(
        db, str(payment.account_id), currency=currency
    )
    if round_money(available) < charge_amount:
        return False

    ledger_entry = LedgerEntry(
        account_id=payment.account_id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        category=LedgerCategory.internet_service,
        amount=charge_amount,
        currency=currency,
        effective_date=effective_at,
        memo=f"Prepaid service renewal {period_start.date()} - {period_end.date()}",
    )
    db.add(ledger_entry)
    db.flush()
    ensure_prepaid_entitlement_for_wallet_debit(
        db,
        subscription=subscription,
        ledger_entry=ledger_entry,
        starts_at=period_start,
        ends_at=period_end,
    )
    subscription.next_billing_at = period_end

    from app.services.account_lifecycle import compute_account_status

    compute_account_status(db, str(payment.account_id))
    return True


def _latest_successful_invoice_payment(db: Session, invoice: Invoice) -> Payment | None:
    return (
        db.query(Payment)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .filter(Payment.is_active.is_(True))
        .order_by(
            func.coalesce(
                Payment.paid_at,
                PaymentAllocation.created_at,
                Payment.created_at,
            ).desc(),
            Payment.created_at.desc(),
            Payment.id.desc(),
        )
        .first()
    )


def _invoice_subscription_lines(
    db: Session, invoice: Invoice
) -> tuple[Subscription, list[InvoiceLine]] | None:
    lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.subscription_id.is_not(None))
        .filter(InvoiceLine.is_active.is_(True))
        .all()
    )
    subscription_ids = {line.subscription_id for line in lines}
    if len(subscription_ids) != 1:
        return None
    subscription = db.get(Subscription, next(iter(subscription_ids)))
    if subscription is None or subscription.billing_mode != BillingMode.prepaid:
        return None
    return subscription, lines


def _base_subscription_invoice_lines(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    base_lines = [
        line
        for line in lines
        if (line.metadata_ or {}).get("kind") == "base_subscription"
    ]
    if base_lines:
        return base_lines
    billable_lines = [line for line in lines if round_money(line.amount) > 0]
    if len(billable_lines) == 1:
        return billable_lines
    if len(lines) == 1:
        return lines
    return []


def _prepaid_invoice_needs_payment_date_anchor(
    subscription: Subscription,
    invoice: Invoice,
    paid_at_day: datetime,
) -> bool:
    period_start = invoice.billing_period_start
    if period_start is None:
        return False
    period_start = (
        period_start if period_start.tzinfo else period_start.replace(tzinfo=UTC)
    )
    if paid_at_day <= period_start:
        return False

    lapsed_statuses = {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.expired,
    }
    if subscription.status in lapsed_statuses:
        return True

    next_billing = subscription.next_billing_at
    if next_billing is not None:
        next_billing = (
            next_billing if next_billing.tzinfo else next_billing.replace(tzinfo=UTC)
        )
        if next_billing <= paid_at_day:
            return True

    period_end = invoice.billing_period_end
    if period_end is not None:
        period_end = period_end if period_end.tzinfo else period_end.replace(tzinfo=UTC)
        if period_end <= paid_at_day:
            return True

    return False


def _prepaid_extension_delta_after_invoice(
    invoice: Invoice, subscription: Subscription
) -> timedelta:
    period_end = invoice.billing_period_end
    next_billing = subscription.next_billing_at
    if period_end is None or next_billing is None:
        return timedelta(0)
    period_end = period_end if period_end.tzinfo else period_end.replace(tzinfo=UTC)
    next_billing = (
        next_billing if next_billing.tzinfo else next_billing.replace(tzinfo=UTC)
    )
    if next_billing <= period_end:
        return timedelta(0)
    return next_billing - period_end


def _reanchor_paid_prepaid_invoice_if_lapsed(db: Session, invoice: Invoice) -> bool:
    """Start lapsed prepaid renewals from the settlement date.

    Prepaid customers should not lose paid entitlement to a historical unpaid
    period after they have already been suspended or otherwise lapsed. When a
    payment fully settles that renewal invoice, move the covered period to the
    payment date and advance the subscription from there.
    """
    if invoice.status != InvoiceStatus.paid:
        return False
    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        return False

    payment = _latest_successful_invoice_payment(db, invoice)
    if payment is None:
        return False
    effective_at = payment.paid_at or payment.created_at or datetime.now(UTC)
    paid_at_utc = (
        effective_at if effective_at.tzinfo else effective_at.replace(tzinfo=UTC)
    )
    paid_at_day = paid_at_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    resolved = _invoice_subscription_lines(db, invoice)
    if resolved is None:
        return False
    subscription, lines = resolved
    if not _prepaid_invoice_needs_payment_date_anchor(
        subscription, invoice, paid_at_day
    ):
        return False

    base_lines = _base_subscription_invoice_lines(lines)
    if not base_lines:
        return False

    from app.services.billing_automation import _period_end

    cycle = (
        subscription.offer.billing_cycle
        if subscription.offer and subscription.offer.billing_cycle
        else BillingCycle.monthly
    )
    extension_delta = _prepaid_extension_delta_after_invoice(invoice, subscription)
    old_period_start = invoice.billing_period_start
    old_period_end = invoice.billing_period_end
    new_period_start = paid_at_day
    new_period_end = _period_end(new_period_start, cycle)
    if old_period_start == new_period_start and old_period_end == new_period_end:
        return False

    invoice.billing_period_start = new_period_start
    invoice.billing_period_end = new_period_end
    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    for line in base_lines:
        metadata = dict(line.metadata_ or {})
        metadata["kind"] = metadata.get("kind") or "base_subscription"
        metadata["billing_period_start"] = new_period_start.isoformat()
        metadata["billing_period_end"] = new_period_end.isoformat()
        line.metadata_ = metadata
        line.description = (
            f"{offer_name} ({new_period_start.date()} - {new_period_end.date()})"
        )

    target_next_billing = new_period_end + extension_delta
    current_next = subscription.next_billing_at
    if current_next is None:
        subscription.next_billing_at = target_next_billing
    else:
        current_next = (
            current_next if current_next.tzinfo else current_next.replace(tzinfo=UTC)
        )
        if current_next < target_next_billing:
            subscription.next_billing_at = target_next_billing

    logger.info(
        "prepaid_invoice_reanchored_to_payment_date",
        extra={
            "event": "prepaid_invoice_reanchored_to_payment_date",
            "invoice_id": str(invoice.id),
            "subscription_id": str(subscription.id),
            "payment_id": str(payment.id),
            "old_period_start": old_period_start.isoformat(),
            "old_period_end": old_period_end.isoformat(),
            "new_period_start": new_period_start.isoformat(),
            "new_period_end": new_period_end.isoformat(),
            "extension_delta_seconds": extension_delta.total_seconds(),
            "new_next_billing_at": target_next_billing.isoformat(),
        },
    )
    return True


def _finalize_invoice_payment_effects(db: Session, invoice: Invoice) -> None:
    """Recompute invoice totals, restore eligible service, then derive account status."""
    _recalculate_invoice_totals(db, invoice)
    # Sessions use autoflush=False, so make the recomputed balance visible
    # before has_overdue_balance queries the database.
    db.flush()

    if invoice.status == InvoiceStatus.paid:
        _reanchor_paid_prepaid_invoice_if_lapsed(db, invoice)
        ensure_prepaid_entitlements_for_paid_invoice(db, invoice)

        from app.services import collections as collections_service

        if not collections_service.has_overdue_balance(db, str(invoice.account_id)):
            collections_service.restore_account_services(
                db, str(invoice.account_id), invoice_id=str(invoice.id)
            )

    from app.services.account_lifecycle import compute_account_status

    compute_account_status(db, str(invoice.account_id))


def _create_refund_ledger_entry(
    db: Session,
    payment: Payment,
    refund_amount: Decimal,
    memo: str | None = None,
) -> LedgerEntry:
    """Create a ledger entry for a refund (reverses a payment credit).

    Args:
        db: Database session
        payment: The original payment being refunded
        refund_amount: Amount being refunded
        memo: Optional memo for the entry

    Returns:
        The created ledger entry
    """
    entry = LedgerEntry(
        account_id=payment.account_id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.refund,
        amount=round_money(refund_amount),
        currency=payment.currency or "NGN",
        memo=memo or f"Refund of Payment {payment.id}",
    )
    db.add(entry)
    return entry


def _primary_allocation_invoice_id(payment: Payment) -> str | None:
    if not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _emit_consolidated_payment_events(
    db: Session, payment: Payment, allocations: list[PaymentAllocation]
) -> None:
    """Emit per-subscriber payment.received events plus one aggregate event.

    Per-subscriber events keep existing handlers (notifications, dunning, etc.)
    working without changes. The aggregate event is for handlers that need the
    consolidated view.
    """
    breakdown: list[dict[str, str]] = []
    for allocation in allocations:
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        if invoice is None:
            continue
        breakdown.append(
            {
                "account_id": str(invoice.account_id),
                "invoice_id": str(invoice.id),
                "amount": str(allocation.amount),
            }
        )
        emit_event(
            db,
            EventType.payment_received,
            {
                "payment_id": str(payment.id),
                "amount": str(allocation.amount),
                "currency": payment.currency,
                "invoice_id": str(invoice.id),
                "status": payment.status.value if payment.status else None,
                "billing_account_id": str(payment.billing_account_id),
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )

    emit_event(
        db,
        EventType.billing_account_payment_received,
        {
            "payment_id": str(payment.id),
            "billing_account_id": str(payment.billing_account_id),
            "total": str(payment.amount) if payment.amount else None,
            "currency": payment.currency,
            "status": payment.status.value if payment.status else None,
            "allocations": breakdown,
        },
    )


class Payments(ListResponseMixin):
    @staticmethod
    def _auto_allocate(db: Session, payment: Payment) -> list[PaymentAllocation]:
        """Auto-allocate payment to oldest unpaid invoices.

        For account-scoped payments, only the payer's own invoices are
        candidates. For consolidated (billing-account-scoped) payments,
        candidates span every subscriber under the billing account's reseller.

        Returns:
            List of created allocations
        """
        remaining = round_money(to_decimal(payment.amount))
        if remaining <= 0:
            return []
        invoice_query = (
            db.query(Invoice)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > 0)
        )
        if payment.billing_account_id is not None:
            from app.models.billing import BillingAccount
            from app.models.subscriber import Subscriber

            invoice_query = invoice_query.join(
                Subscriber, Invoice.account_id == Subscriber.id
            ).filter(
                Subscriber.reseller_id
                == db.query(BillingAccount.reseller_id)
                .filter(BillingAccount.id == payment.billing_account_id)
                .scalar_subquery()
            )
        else:
            invoice_query = invoice_query.filter(
                Invoice.account_id == payment.account_id
            )
        invoices = invoice_query.order_by(
            Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc()
        ).all()
        allocations: list[PaymentAllocation] = []
        for invoice in invoices:
            if invoice.currency != payment.currency:
                continue
            amount = min(remaining, round_money(to_decimal(invoice.balance_due)))
            if amount <= 0:
                continue

            allocation, applied_amount = _apply_payment_allocation(
                db,
                payment,
                invoice,
                amount,
            )
            allocations.append(allocation)

            remaining = round_money(remaining - applied_amount)
            if remaining <= 0:
                break

        _record_unallocated_payment_credit(db, payment, remaining)
        apply_prepaid_service_credit(db, payment)

        return allocations

    @staticmethod
    def _create_allocations(
        db: Session,
        payment: Payment,
        allocations: list[PaymentAllocationCreate],
    ) -> list[PaymentAllocation]:
        """Create explicit allocations from payment to invoices.

        Args:
            db: Database session
            payment: The payment to allocate
            allocations: List of allocation specifications

        Returns:
            List of created allocations
        """
        created = []
        remaining = round_money(to_decimal(payment.amount))
        member_reseller_id: str | None = None
        if payment.billing_account_id is not None:
            from app.models.billing import BillingAccount

            ba = get_by_id(db, BillingAccount, payment.billing_account_id)
            if not ba:
                raise HTTPException(status_code=404, detail="Billing account not found")
            member_reseller_id = str(ba.reseller_id)
        for allocation in allocations:
            if allocation.amount > remaining:
                raise HTTPException(
                    status_code=400, detail="Allocation amount exceeds payment amount"
                )
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if payment.billing_account_id is not None:
                from app.models.subscriber import Subscriber

                subscriber = get_by_id(db, Subscriber, invoice.account_id)
                if (
                    subscriber is None
                    or str(subscriber.reseller_id) != member_reseller_id
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Invoice does not belong to a subscriber of this billing account's reseller",
                    )
            elif str(invoice.account_id) != str(payment.account_id):
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            _validate_invoice_currency(invoice, payment.currency)
            _assert_invoice_allocatable(invoice)

            # Cap the allocation at the invoice's outstanding balance so an
            # overpayment cannot over-allocate (allocations summing above the
            # invoice total). The uncapped surplus stays in ``remaining`` and is
            # credited to the account/wallet by _record_unallocated_payment_credit.
            invoice_balance = round_money(
                to_decimal(
                    invoice.balance_due
                    if invoice.balance_due is not None
                    else invoice.total
                )
            )
            if invoice_balance < 0:
                invoice_balance = Decimal("0.00")
            alloc_amount = min(
                round_money(to_decimal(allocation.amount)), invoice_balance
            )
            if alloc_amount <= 0:
                # Invoice already settled; leave the amount in ``remaining`` so
                # it is credited as account/wallet balance below.
                continue

            entry, applied_amount = _apply_payment_allocation(
                db,
                payment,
                invoice,
                alloc_amount,
                memo=allocation.memo,
            )
            created.append(entry)

            remaining = round_money(remaining - applied_amount)

        _record_unallocated_payment_credit(db, payment, remaining)

        return created

    @staticmethod
    def create(
        db: Session,
        payload: PaymentCreate,
        *,
        auto_allocate: bool = True,
        commit: bool = True,
    ):
        """Create a payment.

        When ``auto_allocate`` is False and no explicit allocations are given,
        the payment is NOT spread over open invoices; the full amount is
        recorded as unallocated account credit instead. Default behavior
        (auto-allocate to oldest unpaid invoices) is unchanged.

        ``commit=False`` posts the payment on the caller's transaction and
        flushes instead of committing, so a caller that is already inside a
        SAVEPOINT (the bulk import wizard, which isolates each row so one bad
        row cannot roll back the batch) can still route through this owner
        rather than hand-rolling a Payment row. The caller owns the commit.
        """
        if payload.amount is not None and payload.amount <= 0:
            raise HTTPException(
                status_code=400, detail="Payment amount must be greater than 0"
            )
        # Double-submit guard for manually recorded payments. Gateway payments
        # are deduped by the uq_payments_active_external_id partial index, but a
        # manual/offline payment has no external_id/provider_id, so a
        # double-clicked "record payment" would create two rows and over-credit
        # the account. Reject an identical manual payment recorded in the last
        # minute (mirrors vas_wallet.pay_bill's guard). (#29)
        if (
            payload.external_id is None
            and payload.provider_id is None
            and payload.amount is not None
        ):
            scope_col, scope_val = (
                (Payment.account_id, payload.account_id)
                if payload.account_id is not None
                else (Payment.billing_account_id, payload.billing_account_id)
            )
            if scope_val is not None:
                duplicate = (
                    db.query(Payment.id)
                    .filter(
                        scope_col == scope_val,
                        Payment.amount == payload.amount,
                        Payment.external_id.is_(None),
                        Payment.provider_id.is_(None),
                        Payment.is_active.is_(True),
                        Payment.created_at >= datetime.now(UTC) - timedelta(seconds=60),
                    )
                    .first()
                )
                if duplicate:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "An identical payment was just recorded moments ago. "
                            "Refresh the page to confirm it before recording again."
                        ),
                    )
        data = payload.model_dump(exclude={"allocations"})
        fields_set = payload.model_fields_set
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_payment_status"
            )
            if default_status:
                data["status"] = validate_enum(default_status, PaymentStatus, "status")
        if payload.account_id is not None:
            _validate_payment_linkages(
                db,
                str(payload.account_id),
                None,
                str(payload.payment_method_id) if payload.payment_method_id else None,
            )
        elif payload.billing_account_id is not None:
            from app.services.billing.billing_accounts import BillingAccounts

            BillingAccounts.get(db, str(payload.billing_account_id))
        _validate_payment_provider(
            db, str(payload.provider_id) if payload.provider_id else None
        )
        channel = _resolve_payment_channel(
            db,
            str(payload.payment_channel_id) if payload.payment_channel_id else None,
            str(payload.payment_method_id) if payload.payment_method_id else None,
            str(payload.provider_id) if payload.provider_id else None,
        )
        if channel and not payload.payment_channel_id:
            data["payment_channel_id"] = channel.id
        collection_account = _resolve_collection_account(
            db,
            channel,
            data.get("currency"),
            str(payload.collection_account_id)
            if payload.collection_account_id
            else None,
        )
        if collection_account and not payload.collection_account_id:
            data["collection_account_id"] = collection_account.id
        if payload.collection_account_id and not collection_account:
            _validate_collection_account(
                db, str(payload.collection_account_id), data.get("currency")
            )
        # Validate allocation invoices against payment currency
        if payload.allocations:
            for alloc in payload.allocations:
                invoice = get_by_id(db, Invoice, alloc.invoice_id)
                if invoice:
                    _validate_invoice_currency(invoice, data.get("currency"))
                    _assert_invoice_allocatable(invoice)
        # A payment created already in the succeeded state must carry paid_at.
        # Gateway/top-up reconciliation (e.g. Paystack top-ups via
        # reconcile_topups) creates succeeded payments without an explicit
        # paid_at; without it, billing-enforcement health (which counts recent
        # settlements by paid_at) goes blind and blocks all collections
        # suspensions. Stamp it here so every caller is covered.
        if data.get("status") == PaymentStatus.succeeded and not data.get("paid_at"):
            data["paid_at"] = datetime.now(UTC)
        payment = Payment(**data)
        db.add(payment)
        db.flush()
        allocation_creates: list[PaymentAllocationCreate] = []
        if payload.allocations:
            allocation_creates = [
                PaymentAllocationCreate(
                    payment_id=payment.id,
                    invoice_id=allocation.invoice_id,
                    amount=allocation.amount,
                    memo=allocation.memo,
                )
                for allocation in payload.allocations
            ]
        allocations: list[PaymentAllocation]
        if allocation_creates:
            allocations = Payments._create_allocations(db, payment, allocation_creates)
        elif auto_allocate:
            allocations = Payments._auto_allocate(db, payment)
        else:
            allocations = []
            _record_unallocated_payment_credit(
                db, payment, round_money(to_decimal(payment.amount))
            )

        # Tests run with autoflush disabled; make sure allocations/ledger exist in DB
        # before we query them during invoice recalculation.
        db.flush()

        invoices_to_recalculate = {alloc.invoice_id for alloc in allocations}
        for invoice_id in invoices_to_recalculate:
            invoice = get_by_id(db, Invoice, invoice_id)
            if invoice:
                _finalize_invoice_payment_effects(db, invoice)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(payment)

        # Emit payment.received event(s)
        if payment.billing_account_id is not None:
            _emit_consolidated_payment_events(db, payment, allocations)
        else:
            allocation_invoice_id = _primary_allocation_invoice_id(payment)
            emit_event(
                db,
                EventType.payment_received,
                {
                    "payment_id": str(payment.id),
                    "amount": str(payment.amount) if payment.amount else None,
                    "currency": payment.currency,
                    "invoice_id": allocation_invoice_id,
                    "status": payment.status.value if payment.status else None,
                },
                account_id=payment.account_id,
                invoice_id=allocation_invoice_id,
            )

        # The payment_received handlers (resolve dunning case, restore service)
        # run inline on this session with commit=False. The payment itself is
        # already committed above; commit again so those resolve/restore
        # mutations are durable instead of left pending for the caller to drop.
        if commit:
            db.commit()
        return payment

    @staticmethod
    def allocate_consolidated_balance_to_subscriber(
        db: Session,
        billing_account_id: str,
        subscriber_id: str,
        amount: Decimal | int | float | str | None = None,
    ) -> dict:
        """Allocate a reseller billing account's unallocated balance to one subscriber.

        The credit is consumed from the billing account's existing unallocated
        consolidated payments, oldest first, and applied to the selected
        subscriber's oldest open invoices.
        """
        from app.models.billing import BillingAccount
        from app.models.subscriber import Subscriber
        from app.services.billing.billing_accounts import BillingAccounts

        ba = (
            db.query(BillingAccount)
            .filter(BillingAccount.id == billing_account_id)
            .with_for_update()
            .first()
        )
        if not ba:
            raise HTTPException(status_code=404, detail="Billing account not found")
        available_balance = round_money(to_decimal(ba.balance))
        if available_balance <= 0:
            raise HTTPException(
                status_code=400, detail="No unallocated reseller funds available"
            )
        allocation_limit = available_balance
        if amount is not None:
            allocation_limit = round_money(to_decimal(amount))
            if allocation_limit <= 0:
                raise HTTPException(
                    status_code=400, detail="Allocation amount must be greater than 0"
                )
            if allocation_limit > available_balance:
                raise HTTPException(
                    status_code=400,
                    detail="Allocation exceeds unallocated reseller funds",
                )

        subscriber = get_by_id(db, Subscriber, subscriber_id)
        if subscriber is None or str(subscriber.reseller_id) != str(ba.reseller_id):
            raise HTTPException(status_code=404, detail="Subscriber not found")

        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == subscriber.id)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > 0)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
        if not invoices:
            raise HTTPException(
                status_code=400, detail="Subscriber has no open invoices"
            )

        allocated_sq = (
            db.query(
                PaymentAllocation.payment_id.label("payment_id"),
                func.coalesce(
                    func.sum(PaymentAllocation.amount), Decimal("0.00")
                ).label("allocated"),
            )
            .group_by(PaymentAllocation.payment_id)
            .subquery()
        )
        payment_result_rows = (
            db.query(
                Payment,
                func.coalesce(allocated_sq.c.allocated, Decimal("0.00")).label(
                    "allocated"
                ),
            )
            .outerjoin(allocated_sq, allocated_sq.c.payment_id == Payment.id)
            .filter(Payment.billing_account_id == ba.id)
            .filter(Payment.is_active.is_(True))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(Payment.paid_at.asc().nulls_last(), Payment.created_at.asc())
            .all()
        )
        payment_rows: list[tuple[Payment, Decimal]] = [
            (payment, cast(Decimal, allocated))
            for payment, allocated in payment_result_rows
        ]
        payment_backing_available = round_money(
            sum(
                (
                    round_money(to_decimal(payment.amount) - to_decimal(allocated))
                    for payment, allocated in payment_rows
                    if payment.currency == ba.currency
                ),
                Decimal("0.00"),
            )
        )
        if payment_backing_available < allocation_limit:
            backing_amount = round_money(allocation_limit - payment_backing_available)
            backing_payment = Payment(
                billing_account_id=ba.id,
                amount=backing_amount,
                currency=ba.currency,
                status=PaymentStatus.succeeded,
                memo="Reseller unallocated balance credit",
                paid_at=datetime.now(UTC),
            )
            db.add(backing_payment)
            db.flush()
            payment_rows.append((backing_payment, Decimal("0.00")))

        remaining_balance = allocation_limit
        total_allocated = Decimal("0.00")
        invoice_ids: set = set()
        allocations_by_payment: dict[Payment, list[PaymentAllocation]] = {}
        payment_remaining_by_id = {
            payment.id: round_money(to_decimal(payment.amount) - to_decimal(allocated))
            for payment, allocated in payment_rows
        }

        for invoice in invoices:
            invoice_remaining = round_money(to_decimal(invoice.balance_due))
            if invoice_remaining <= 0:
                continue

            for payment, _already_allocated in payment_rows:
                if remaining_balance <= 0 or invoice_remaining <= 0:
                    break
                if payment.currency != invoice.currency:
                    continue

                payment_available = payment_remaining_by_id.get(
                    payment.id, Decimal("0.00")
                )
                if payment_available <= 0:
                    continue

                amount = min(remaining_balance, invoice_remaining, payment_available)
                allocation, applied_amount = _apply_payment_allocation(
                    db,
                    payment,
                    invoice,
                    amount,
                    memo="Allocated from reseller unallocated funds",
                )
                allocations_by_payment.setdefault(payment, []).append(allocation)
                total_allocated = round_money(total_allocated + applied_amount)
                remaining_balance = round_money(remaining_balance - applied_amount)
                invoice_remaining = round_money(invoice_remaining - applied_amount)
                payment_remaining_by_id[payment.id] = round_money(
                    payment_available - applied_amount
                )
                invoice_ids.add(invoice.id)

            if remaining_balance <= 0:
                break

        if total_allocated <= 0:
            raise HTTPException(
                status_code=400,
                detail="No eligible unallocated reseller funds could be applied",
            )
        if total_allocated > available_balance:
            raise HTTPException(
                status_code=400, detail="Allocation exceeds unallocated reseller funds"
            )

        db.flush()
        for invoice_id in invoice_ids:
            recalculated_invoice = get_by_id(db, Invoice, invoice_id)
            if recalculated_invoice:
                _finalize_invoice_payment_effects(db, recalculated_invoice)

        BillingAccounts.debit_balance(db, str(ba.id), total_allocated)
        db.commit()

        for payment, allocations in allocations_by_payment.items():
            _emit_consolidated_payment_events(db, payment, allocations)

        return {
            "subscriber_id": str(subscriber.id),
            "allocated_total": total_allocated,
            "currency": ba.currency,
            "remaining_unallocated_balance": round_money(
                available_balance - total_allocated
            ),
            "invoice_count": len(invoice_ids),
        }

    @staticmethod
    def get(db: Session, payment_id: str):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return payment

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        invoice_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        *,
        updated_since: datetime | None = None,
    ):
        query = db.query(Payment)
        if account_id:
            query = query.filter(Payment.account_id == account_id)
        if invoice_id:
            query = query.join(
                PaymentAllocation, PaymentAllocation.payment_id == Payment.id
            ).filter(PaymentAllocation.invoice_id == invoice_id)
        if status:
            query = query.filter(
                Payment.status == validate_enum(status, PaymentStatus, "status")
            )
        if is_active is None:
            query = query.filter(Payment.is_active.is_(True))
        else:
            query = query.filter(Payment.is_active == is_active)
        # Incremental-sync watermark (see Invoices.list); backed by
        # ix_payments_is_active_updated_at.
        if updated_since is not None:
            query = query.filter(Payment.updated_at >= updated_since)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Payment.created_at,
                "updated_at": Payment.updated_at,
                "paid_at": Payment.paid_at,
                "status": Payment.status,
            },
        )
        # Stable, keyset-friendly tiebreaker for deterministic forward paging.
        query = query.order_by(Payment.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, payment_id: str, payload: PaymentUpdate):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data or "billing_account_id" in data:
            raise HTTPException(
                status_code=400,
                detail="Payment scope (account_id / billing_account_id) cannot be changed after creation",
            )
        effective_account_id = data.get("account_id", payment.account_id)
        payment_method_id = data.get("payment_method_id", payment.payment_method_id)
        explicit_channel = "payment_channel_id" in data
        payment_channel_id = (
            data.get("payment_channel_id") if explicit_channel else None
        )
        collection_account_id = data.get(
            "collection_account_id", payment.collection_account_id
        )
        provider_id = data.get("provider_id", payment.provider_id)
        if effective_account_id is not None:
            _validate_payment_linkages(
                db,
                str(effective_account_id),
                None,
                str(payment_method_id) if payment_method_id else None,
            )
        _validate_payment_provider(db, str(provider_id) if provider_id else None)
        channel = _resolve_payment_channel(
            db,
            str(payment_channel_id) if payment_channel_id else None,
            str(payment_method_id) if payment_method_id else None,
            str(provider_id) if provider_id else None,
        )
        if channel and not explicit_channel:
            data["payment_channel_id"] = channel.id
        collection_account = _resolve_collection_account(
            db,
            channel,
            data.get("currency", payment.currency),
            str(collection_account_id) if collection_account_id else None,
        )
        if collection_account and not collection_account_id:
            data["collection_account_id"] = collection_account.id
        if collection_account_id and not collection_account:
            _validate_collection_account(
                db, str(collection_account_id), data.get("currency", payment.currency)
            )
        # A status change is a settlement decision, not a field edit. mark_status
        # owns it: it enforces the legal-transition table, stamps paid_at (a
        # succeeded payment with a NULL paid_at is invisible to the enforcement
        # health gate and silently blocks all collections suspensions), resolves
        # dunning cases, applies prepaid service credit, and emits
        # payment_received. Blind-setattr here bypassed every one of those and
        # reopened the production paid_at regression that create() already fixed.
        requested_status = data.pop("status", None)

        for key, value in data.items():
            setattr(payment, key, value)
        invoice_ids = [alloc.invoice_id for alloc in payment.allocations]
        for invoice_id in invoice_ids:
            invoice = get_by_id(db, Invoice, invoice_id)
            if invoice:
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)
        db.commit()

        if requested_status is not None:
            normalized = validate_enum(requested_status, PaymentStatus, "status")
            if normalized and normalized != payment.status:
                Payments.mark_status(db, str(payment.id), normalized)

        db.refresh(payment)
        return payment

    @staticmethod
    def reallocate(db: Session, payment_id: str, invoice_id: str) -> Payment:
        """Move a payment's allocation to a different invoice.

        The canonical owner operation for "admin pointed this payment at the
        wrong invoice". It must be used instead of rewriting ``PaymentAllocation``
        rows directly: the money's effect is spread across the allocation, the
        ledger credit, and the *two* invoices' derived totals, and all of them
        have to move together.

        For each invoice released, the payment ledger credit is deactivated and
        the invoice is recomputed, so a released invoice cannot keep reading as
        ``paid`` with no money behind it. The new allocation is then capped at
        the target's ``balance_due`` — an over-payment becomes account credit
        rather than an allocation larger than the debt it settles.
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if not payment.is_active:
            raise HTTPException(
                status_code=409, detail="Inactive payment cannot be reallocated"
            )
        if payment.status != PaymentStatus.succeeded:
            raise HTTPException(
                status_code=409,
                detail="Only a succeeded payment can be reallocated",
            )
        if payment.account_id is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Consolidated payments require their dedicated allocation "
                    "workflow and cannot be reallocated here"
                ),
            )

        # Account lock first: every subscriber-scoped payment and invoice in
        # this operation shares that serialization key. Then lock the payment
        # and target rows so a concurrent edit cannot change either underneath
        # the release-and-apply sequence.
        lock_account(db, str(payment.account_id))
        payment = (
            db.query(Payment).filter(Payment.id == payment.id).with_for_update().one()
        )
        target = (
            db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
        )
        if not target or not target.is_active:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(target.account_id) != str(payment.account_id):
            raise HTTPException(
                status_code=400,
                detail="Invoice belongs to a different account than the payment",
            )
        _validate_invoice_currency(target, payment.currency)
        _assert_invoice_allocatable(target)
        if target.status == InvoiceStatus.written_off:
            raise HTTPException(
                status_code=400,
                detail="Cannot allocate a payment to a written_off invoice",
            )

        already_on_target = any(
            allocation.is_active and str(allocation.invoice_id) == str(target.id)
            for allocation in payment.allocations
        )
        if not already_on_target and round_money(
            to_decimal(target.balance_due or Decimal("0.00"))
        ) <= Decimal("0.00"):
            raise HTTPException(status_code=400, detail="Invoice has no balance due")

        released: list[Invoice] = []
        for allocation in list(payment.allocations):
            if not allocation.is_active:
                continue
            if str(allocation.invoice_id) == str(target.id):
                # Already where it needs to be; nothing to move.
                continue
            previous = get_by_id(db, Invoice, allocation.invoice_id)
            # Mirror PaymentAllocations.delete: drop the ledger credit with the
            # allocation, never one without the other.
            db.query(LedgerEntry).filter(
                LedgerEntry.payment_id == allocation.payment_id,
                LedgerEntry.invoice_id == allocation.invoice_id,
                LedgerEntry.source == LedgerSource.payment,
            ).update({"is_active": False})
            allocation.is_active = False
            if previous is not None:
                released.append(previous)

        db.flush()
        for previous in released:
            _finalize_invoice_payment_effects(db, previous)

        db.flush()
        db.refresh(target)

        # Only the part of the payment that is not already allocated is free to
        # move. Without this, re-pointing a payment at the invoice it is already
        # allocated to would treat the whole amount as surplus and mint credit
        # the customer never paid.
        allocated = sum(
            (
                round_money(to_decimal(a.amount))
                for a in payment.allocations
                if a.is_active
            ),
            Decimal("0.00"),
        )
        amount = round_money(to_decimal(payment.amount))
        unallocated = amount - allocated
        if unallocated <= 0:
            db.commit()
            db.refresh(payment)
            return payment

        payable = round_money(to_decimal(target.balance_due or Decimal("0.00")))
        applied = min(unallocated, payable) if payable > 0 else Decimal("0.00")

        if applied > 0:
            _apply_payment_allocation(
                db,
                payment,
                target,
                applied,
                memo=f"Reallocated from payment {payment.id}",
            )
            db.flush()
            _finalize_invoice_payment_effects(db, target)

        # Anything the target could not absorb stays with the customer as credit
        # instead of silently inflating the allocation.
        _record_unallocated_payment_credit(db, payment, unallocated - applied)

        db.commit()
        db.refresh(payment)
        return payment

    @staticmethod
    def delete(db: Session, payment_id: str):
        """Soft-delete a payment and remove every effect it had.

        A payment's effect is spread across the payment row, its allocations, and
        the ledger credits those allocations justify. Deactivating only the payment
        row left the ledger credit ACTIVE and unallocated — so the money kept
        counting toward the customer's spendable credit even though the payment
        that created it was gone.

        Allocation and ledger credit drop together, the same way
        ``PaymentAllocations.delete`` and ``Payments.reallocate`` do it. Never one
        without the other.
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")

        invoices = [
            invoice
            for allocation in payment.allocations
            if allocation.is_active
            and (invoice := get_by_id(db, Invoice, allocation.invoice_id))
        ]

        # Drop every ledger entry this payment posted — the invoice-scoped credits
        # AND the unallocated surplus credit, which nothing else would ever undo.
        db.query(LedgerEntry).filter(
            LedgerEntry.payment_id == payment.id,
            LedgerEntry.is_active.is_(True),
        ).update({"is_active": False}, synchronize_session=False)

        for allocation in payment.allocations:
            allocation.is_active = False

        payment.is_active = False
        db.flush()

        for invoice in invoices:
            _finalize_invoice_payment_effects(db, invoice)
        db.commit()

    @staticmethod
    def mark_status(db: Session, payment_id: str, status: PaymentStatus | str):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        previous_status = payment.status
        normalized = validate_enum(status, PaymentStatus, "status")
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid status")
        # Guard against out-of-order / replayed gateway webhooks regressing
        # committed financial state (e.g. success after refunded, late failed
        # after success). An illegal transition is a no-op that still returns
        # the payment so the webhook gets a 200 and stops retrying.
        if (
            previous_status != normalized
            and normalized
            not in _ALLOWED_PAYMENT_TRANSITIONS.get(previous_status, set())
        ):
            logger.warning(
                "Ignoring illegal payment transition %s -> %s for payment %s",
                previous_status.value if previous_status else None,
                normalized.value,
                payment_id,
            )
            return payment

        # A refund is a MONEY MOVEMENT, not a status flip. Setting the status alone
        # posted no refund ledger entry and set no refunded_amount, so the payment
        # still counted at its FULL value: the cash left the business and the
        # customer kept a phantom credit for the same amount. The admin Refund
        # button did exactly this.
        #
        # Delegate to the owner op, which posts the ledger entry, records
        # refunded_amount, releases the allocations with their ledger credits, and
        # recomputes the invoices. Every caller — admin UI, API, provider webhook —
        # gets the same, correct effect.
        if normalized == PaymentStatus.refunded and previous_status in (
            PaymentStatus.succeeded,
            PaymentStatus.partially_refunded,
        ):
            return Refunds.process_refund(
                db, str(payment.id), reason="refund via status change"
            )

        payment.status = normalized
        if normalized == PaymentStatus.succeeded:
            payment.paid_at = datetime.now(UTC)
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)
        if normalized == PaymentStatus.succeeded:
            # Deferred import to avoid circular dependency
            from app.services import collections as collections_service

            collections_service.dunning_workflow.resolve_cases_for_account(
                db,
                str(payment.account_id),
                _primary_allocation_invoice_id(payment),
                commit=False,
            )
            apply_prepaid_service_credit(db, payment)
        db.commit()
        db.refresh(payment)

        # Emit payment event based on status transition
        if previous_status != normalized:
            allocation_invoice_id = _primary_allocation_invoice_id(payment)
            payload = {
                "payment_id": str(payment.id),
                "amount": str(payment.amount) if payment.amount else None,
                "currency": payment.currency,
                "invoice_id": allocation_invoice_id,
                "from_status": previous_status.value if previous_status else None,
                "to_status": normalized.value,
            }
            if normalized == PaymentStatus.succeeded:
                emit_event(
                    db,
                    EventType.payment_received,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            elif normalized == PaymentStatus.failed:
                emit_event(
                    db,
                    EventType.payment_failed,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            elif normalized == PaymentStatus.refunded:
                emit_event(
                    db,
                    EventType.payment_refunded,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            # Persist the inline payment_received handlers' resolve/restore work
            # (run with commit=False); the payment is already committed above.
            db.commit()

        return payment


class PaymentAllocations(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentAllocationCreate):
        payment = (
            db.query(Payment)
            .filter(Payment.id == payload.payment_id)
            .with_for_update()
            .first()
        )
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != str(payment.account_id):
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to account"
            )
        _validate_invoice_currency(invoice, payment.currency)
        _assert_invoice_allocatable(invoice)
        # Idempotency check: return existing allocation for same (payment_id, invoice_id)
        existing = (
            db.query(PaymentAllocation)
            .filter(
                PaymentAllocation.payment_id == payment.id,
                PaymentAllocation.invoice_id == payload.invoice_id,
            )
            .first()
        )
        if existing:
            return existing
        allocated_amount = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .with_entities(func.coalesce(func.sum(PaymentAllocation.amount), 0))
            .scalar()
        )
        if payload.amount + allocated_amount > payment.amount:
            raise HTTPException(
                status_code=400, detail="Allocation exceeds payment amount"
            )
        allocation = PaymentAllocation(**payload.model_dump())
        db.add(allocation)
        db.flush()
        _create_payment_ledger_entry(db, payment, invoice, allocation.amount)
        _finalize_invoice_payment_effects(db, invoice)
        db.commit()
        db.refresh(allocation)
        return allocation

    @staticmethod
    def list(
        db: Session,
        payment_id: str | None,
        invoice_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentAllocation)
        if payment_id:
            query = query.filter(PaymentAllocation.payment_id == payment_id)
        if invoice_id:
            query = query.filter(PaymentAllocation.invoice_id == invoice_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentAllocation.created_at,
                "amount": PaymentAllocation.amount,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def delete(db: Session, allocation_id: str):
        allocation = get_by_id(db, PaymentAllocation, allocation_id)
        if not allocation:
            raise HTTPException(status_code=404, detail="Payment allocation not found")
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        # Soft-delete corresponding ledger entries
        db.query(LedgerEntry).filter(
            LedgerEntry.payment_id == allocation.payment_id,
            LedgerEntry.invoice_id == allocation.invoice_id,
            LedgerEntry.source == LedgerSource.payment,
        ).update({"is_active": False})
        # Soft-delete the allocation to preserve the audit trail
        allocation.is_active = False
        if invoice:
            db.flush()
            _finalize_invoice_payment_effects(db, invoice)
        db.commit()


class CollectionAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CollectionAccountCreate):
        account = CollectionAccount(**payload.model_dump())
        db.add(account)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def get(db: Session, account_id: str):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        return account

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CollectionAccount)
        if is_active is None:
            query = query.filter(CollectionAccount.is_active.is_(True))
        else:
            query = query.filter(CollectionAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CollectionAccount.created_at,
                "name": CollectionAccount.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, account_id: str, payload: CollectionAccountUpdate):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(account, key, value)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def delete(db: Session, account_id: str):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        account.is_active = False
        db.commit()


class PaymentChannels(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentChannelCreate):
        data = payload.model_dump()
        if data.get("default_collection_account_id"):
            _validate_collection_account(
                db, str(data["default_collection_account_id"]), None
            )
        if data.get("is_default"):
            db.query(PaymentChannel).filter(
                PaymentChannel.provider_id == data.get("provider_id"),
                PaymentChannel.is_default.is_(True),
            ).update({"is_default": False})
        channel = PaymentChannel(**data)
        db.add(channel)
        db.commit()
        db.refresh(channel)
        return channel

    @staticmethod
    def get(db: Session, channel_id: str):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        return channel

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentChannel)
        if is_active is None:
            query = query.filter(PaymentChannel.is_active.is_(True))
        else:
            query = query.filter(PaymentChannel.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentChannel.created_at, "name": PaymentChannel.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, channel_id: str, payload: PaymentChannelUpdate):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        data = payload.model_dump(exclude_unset=True)
        if data.get("default_collection_account_id"):
            _validate_collection_account(
                db, str(data["default_collection_account_id"]), None
            )
        if data.get("is_default"):
            provider_id = data.get("provider_id", channel.provider_id)
            db.query(PaymentChannel).filter(
                PaymentChannel.provider_id == provider_id,
                PaymentChannel.id != channel.id,
                PaymentChannel.is_default.is_(True),
            ).update({"is_default": False})
        for key, value in data.items():
            setattr(channel, key, value)
        db.commit()
        db.refresh(channel)
        return channel

    @staticmethod
    def delete(db: Session, channel_id: str):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        channel.is_active = False
        db.commit()


class PaymentChannelAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentChannelAccountCreate):
        channel = _validate_payment_channel(db, str(payload.channel_id))
        if channel is None:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        _validate_collection_account(
            db, str(payload.collection_account_id), payload.currency
        )
        if payload.is_default:
            db.query(PaymentChannelAccount).filter(
                PaymentChannelAccount.channel_id == channel.id,
                PaymentChannelAccount.currency == payload.currency,
                PaymentChannelAccount.is_default.is_(True),
            ).update({"is_default": False})
        mapping = PaymentChannelAccount(**payload.model_dump())
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def get(db: Session, mapping_id: str):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        return mapping

    @staticmethod
    def list(
        db: Session,
        channel_id: str | None,
        collection_account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentChannelAccount)
        if channel_id:
            query = query.filter(PaymentChannelAccount.channel_id == channel_id)
        if collection_account_id:
            query = query.filter(
                PaymentChannelAccount.collection_account_id == collection_account_id
            )
        if is_active is None:
            query = query.filter(PaymentChannelAccount.is_active.is_(True))
        else:
            query = query.filter(PaymentChannelAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentChannelAccount.created_at,
                "priority": PaymentChannelAccount.priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, mapping_id: str, payload: PaymentChannelAccountUpdate):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        data = payload.model_dump(exclude_unset=True)
        channel_id = data.get("channel_id", mapping.channel_id)
        currency = data.get("currency", mapping.currency)
        if "channel_id" in data:
            _validate_payment_channel(db, str(channel_id))
        if "collection_account_id" in data:
            _validate_collection_account(
                db, str(data["collection_account_id"]), currency
            )
        if data.get("is_default"):
            db.query(PaymentChannelAccount).filter(
                PaymentChannelAccount.channel_id == channel_id,
                PaymentChannelAccount.currency == currency,
                PaymentChannelAccount.id != mapping.id,
                PaymentChannelAccount.is_default.is_(True),
            ).update({"is_default": False})
        for key, value in data.items():
            setattr(mapping, key, value)
        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def delete(db: Session, mapping_id: str):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        mapping.is_active = False
        db.commit()


class Refunds:
    """Service for processing payment refunds.

    Refunds create credit ledger entries and update payment status.
    Can optionally create credit notes for partial refunds.
    """

    @staticmethod
    def process_refund(
        db: Session,
        payment_id: str,
        refund_amount: Decimal | None = None,
        reason: str | None = None,
        create_credit_note: bool = False,
    ) -> Payment:
        """Process a refund for a payment.

        Args:
            db: Database session
            payment_id: The payment to refund
            refund_amount: Amount to refund (defaults to full payment amount)
            reason: Reason for the refund
            create_credit_note: Whether to create a credit note

        Returns:
            The updated payment
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        # Lock the payment row so the "sum already-refunded, then add" below is
        # serialized. Without it two concurrent refunds both read the same
        # already_refunded total, both compute the full refundable amount, and
        # both insert a refund — refunding up to N× the captured amount. The
        # lock forces the second caller to re-read the first's committed refund.
        db.refresh(payment, with_for_update=True)

        if payment.status == PaymentStatus.refunded:
            raise HTTPException(
                status_code=400, detail="Payment already fully refunded"
            )

        if payment.status not in (
            PaymentStatus.succeeded,
            PaymentStatus.partially_refunded,
        ):
            raise HTTPException(
                status_code=400,
                detail="Only succeeded or partially refunded payments can be refunded",
            )

        # Calculate already refunded amount from ledger entries
        already_refunded = (
            db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
            .filter(
                LedgerEntry.payment_id == payment.id,
                LedgerEntry.source == LedgerSource.refund,
            )
            .scalar()
        )
        already_refunded = round_money(to_decimal(already_refunded))
        refundable_amount = round_money(to_decimal(payment.amount) - already_refunded)

        # ``refund_amount or ...`` would treat 0 as "refund everything"; be
        # explicit and reject non-positive amounts (incl. a negative that would
        # otherwise pass the upper-bound check and INCREASE the payment).
        if refund_amount is None:
            amount_to_refund = refundable_amount
        else:
            amount_to_refund = round_money(to_decimal(refund_amount))
        if amount_to_refund <= 0:
            raise HTTPException(
                status_code=400, detail="Refund amount must be positive"
            )
        if amount_to_refund > refundable_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Refund amount exceeds refundable balance ({refundable_amount})",
            )

        # Create refund ledger entry
        _create_refund_ledger_entry(
            db, payment, amount_to_refund, reason or f"Refund: {payment_id}"
        )

        # Update payment status - full refund if this refund exhausts remaining balance
        is_full_refund = amount_to_refund == refundable_amount
        if is_full_refund:
            payment.status = PaymentStatus.refunded
        else:
            payment.status = PaymentStatus.partially_refunded

        # Track the running refunded total on the payment (gross `amount` is left
        # unchanged) so consumers can derive net cash without summing the ledger.
        payment.refunded_amount = round_money(already_refunded + amount_to_refund)

        # Recalculate invoice totals for all allocated invoices
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                # A full refund removes the allocation's effect. It used to
                # db.delete() the allocation, which (a) destroyed the audit row,
                # (b) defeated _find_inactive_payment_allocation, which exists to
                # reuse a soft-deleted allocation after a reversal, and (c) left
                # the paired payment ledger CREDIT active and orphaned — money
                # justified by an allocation that no longer existed.
                #
                # Soft-delete the allocation and drop its ledger credit with it,
                # exactly as PaymentAllocations.delete and Payments.reallocate do.
                # Never one without the other.
                if is_full_refund and allocation.is_active:
                    db.query(LedgerEntry).filter(
                        LedgerEntry.payment_id == allocation.payment_id,
                        LedgerEntry.invoice_id == allocation.invoice_id,
                        LedgerEntry.source == LedgerSource.payment,
                    ).update({"is_active": False}, synchronize_session=False)
                    allocation.is_active = False
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)

        # Create credit note if requested
        if create_credit_note and payment.allocations:
            Refunds._create_credit_note(db, payment, amount_to_refund, reason)

        db.commit()
        db.refresh(payment)

        # Emit payment.refunded event
        emit_event(
            db,
            EventType.payment_refunded,
            {
                "payment_id": str(payment.id),
                "refund_amount": str(amount_to_refund),
                "currency": payment.currency,
                "reason": reason,
                "is_full_refund": is_full_refund,
            },
            account_id=payment.account_id,
        )

        return payment

    @staticmethod
    def _create_credit_note(
        db: Session,
        payment: Payment,
        amount: Decimal,
        reason: str | None,
    ) -> CreditNote | None:
        """Create a credit note for a refund using the proper CreditNote model.

        Credit notes have positive amounts and can be applied to future invoices.
        """
        if not payment.allocations:
            return None

        # Get the first allocated invoice as reference for linking
        reference_invoice = get_by_id(db, Invoice, payment.allocations[0].invoice_id)

        credit_note = CreditNote(
            account_id=payment.account_id,
            invoice_id=reference_invoice.id if reference_invoice else None,
            status=CreditNoteStatus.issued,
            currency=payment.currency or "NGN",
            subtotal=round_money(amount),
            tax_total=Decimal("0.00"),
            total=round_money(amount),
            applied_total=Decimal("0.00"),
            memo=reason or f"Credit note for refund of payment {payment.id}",
        )
        db.add(credit_note)
        db.flush()

        # Create credit note line item
        line = CreditNoteLine(
            credit_note_id=credit_note.id,
            description=reason or f"Refund of payment {payment.id}",
            quantity=Decimal("1.000"),
            unit_price=round_money(amount),
            amount=round_money(amount),
        )
        db.add(line)

        # Create credit ledger entry
        entry = LedgerEntry(
            account_id=payment.account_id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.credit_note,
            amount=round_money(amount),
            currency=payment.currency or "NGN",
            memo=f"Credit note {credit_note.id}",
        )
        db.add(entry)

        return credit_note

    @staticmethod
    def reverse_payment(
        db: Session,
        payment_id: str,
        reason: str | None = None,
    ) -> Payment:
        """Reverse a payment entirely (e.g., for chargebacks or bank reversals).

        This marks the payment as failed and recalculates all affected invoices.

        Args:
            db: Database session
            payment_id: The payment to reverse
            reason: Reason for the reversal

        Returns:
            The updated payment
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")

        if payment.status == PaymentStatus.failed:
            raise HTTPException(
                status_code=400, detail="Payment already failed/reversed"
            )

        previous_status = payment.status

        # Create reversal ledger entry if payment was succeeded
        if previous_status == PaymentStatus.succeeded:
            _create_refund_ledger_entry(
                db, payment, payment.amount, reason or f"Payment reversal: {payment_id}"
            )

        # Mark payment as failed
        payment.status = PaymentStatus.failed

        # Recalculate all affected invoices
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)

        db.commit()
        db.refresh(payment)

        # Emit payment.failed event
        emit_event(
            db,
            EventType.payment_failed,
            {
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": payment.currency,
                "reason": reason or "payment_reversed",
                "from_status": previous_status.value if previous_status else None,
            },
            account_id=payment.account_id,
        )

        return payment
