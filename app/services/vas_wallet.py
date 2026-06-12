"""VAS wallet service: customer-liability wallet, separate from billing.

Design: docs/designs/VTU_BILL_PAYMENTS.md. Key invariants enforced here:
- No code path reads or writes the service credit balance (the only bridge
  is ``pay_bill``, which creates an ordinary ``Payment``).
- Every debit holds ``lock_account`` (serialized read-modify-write).
- Debits are immediate; refunds are explicit credits — balance == spendable.
- Top-up verification is idempotent on the gateway reference.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.vas import VasEntryCategory, VasEntryType, VasWallet, VasWalletEntry
from app.services import settings_spec
from app.services.billing._common import lock_account
from app.services.common import coerce_uuid
from app.services.payment_gateway_adapter import payment_gateway_adapter

logger = logging.getLogger(__name__)


# --- Settings ---------------------------------------------------------------


def _setting_int(db: Session, key: str, default: int) -> int:
    value = settings_spec.resolve_value(db, SettingDomain.vas, key)
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def is_enabled(db: Session) -> bool:
    value = settings_spec.resolve_value(db, SettingDomain.vas, "enabled")
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def require_enabled(db: Session) -> None:
    """404 (not 403) when the feature is off — invisible, not forbidden."""
    if not is_enabled(db):
        raise HTTPException(status_code=404, detail="Not found")


# --- Wallet primitives ------------------------------------------------------


def get_or_create_wallet(db: Session, subscriber_id: str) -> VasWallet:
    sid = coerce_uuid(subscriber_id)
    wallet = db.scalars(select(VasWallet).where(VasWallet.subscriber_id == sid)).first()
    if wallet:
        return wallet
    wallet = VasWallet(subscriber_id=sid)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def wallet_balance(db: Session, wallet_id) -> Decimal:
    credit = db.query(
        func.coalesce(func.sum(VasWalletEntry.amount), Decimal("0.00"))
    ).filter(
        VasWalletEntry.wallet_id == wallet_id,
        VasWalletEntry.entry_type == VasEntryType.credit,
    ).scalar() or Decimal("0.00")
    debit = db.query(
        func.coalesce(func.sum(VasWalletEntry.amount), Decimal("0.00"))
    ).filter(
        VasWalletEntry.wallet_id == wallet_id,
        VasWalletEntry.entry_type == VasEntryType.debit,
    ).scalar() or Decimal("0.00")
    return Decimal(str(credit)) - Decimal(str(debit))


def _write_entry(
    db: Session,
    wallet: VasWallet,
    *,
    entry_type: VasEntryType,
    category: VasEntryCategory,
    amount: Decimal,
    reference: str | None = None,
    payment_id=None,
    memo: str | None = None,
) -> VasWalletEntry:
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    entry = VasWalletEntry(
        wallet_id=wallet.id,
        entry_type=entry_type,
        category=category,
        amount=amount,
        reference=reference,
        payment_id=payment_id,
        memo=memo,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def credit_wallet(
    db: Session,
    wallet: VasWallet,
    *,
    amount: Decimal,
    category: VasEntryCategory,
    reference: str | None = None,
    payment_id=None,
    memo: str | None = None,
) -> VasWalletEntry:
    return _write_entry(
        db,
        wallet,
        entry_type=VasEntryType.credit,
        category=category,
        amount=amount,
        reference=reference,
        payment_id=payment_id,
        memo=memo,
    )


def debit_wallet(
    db: Session,
    wallet: VasWallet,
    *,
    amount: Decimal,
    category: VasEntryCategory,
    reference: str | None = None,
    payment_id=None,
    memo: str | None = None,
) -> VasWalletEntry:
    """Debit under lock_account — insufficient funds is a 400, never negative."""
    if wallet.subscriber_id is None:
        raise HTTPException(status_code=400, detail="Unsupported wallet owner")
    lock_account(db, str(wallet.subscriber_id))
    balance = wallet_balance(db, wallet.id)
    if amount > balance:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")
    return _write_entry(
        db,
        wallet,
        entry_type=VasEntryType.debit,
        category=category,
        amount=amount,
        reference=reference,
        payment_id=payment_id,
        memo=memo,
    )


# --- Funding (gateway top-up) -----------------------------------------------


def _topup_credited_today(db: Session, wallet_id) -> Decimal:
    day_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    total = db.query(
        func.coalesce(func.sum(VasWalletEntry.amount), Decimal("0.00"))
    ).filter(
        VasWalletEntry.wallet_id == wallet_id,
        VasWalletEntry.entry_type == VasEntryType.credit,
        VasWalletEntry.category == VasEntryCategory.topup,
        VasWalletEntry.created_at >= day_start,
    ).scalar() or Decimal("0.00")
    return Decimal(str(total))


def initiate_topup(db: Session, subscriber_id: str, amount: Decimal) -> dict:
    """Start a wallet top-up: limit checks + gateway checkout context.

    No Payment row is created — wallet top-ups are liabilities, not revenue;
    they live only in vas_wallet_entries (credited at verify).
    """
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)
    amount = Decimal(str(amount))
    min_amount = Decimal(_setting_int(db, "topup_min", 100))
    max_amount = Decimal(_setting_int(db, "topup_max_per_txn", 50000))
    daily_limit = Decimal(_setting_int(db, "topup_daily_limit", 100000))
    if amount < min_amount:
        raise HTTPException(
            status_code=400, detail=f"Minimum top-up is {min_amount:.0f}"
        )
    if amount > max_amount:
        raise HTTPException(
            status_code=400, detail=f"Maximum top-up is {max_amount:.0f}"
        )
    if _topup_credited_today(db, wallet.id) + amount > daily_limit:
        raise HTTPException(
            status_code=400, detail="Daily wallet funding limit reached"
        )
    context = payment_gateway_adapter.build_context(db, provider_type=_provider(db))
    return {
        "provider_type": context.provider_type,
        "provider_public_key": context.public_key,
        "reference": context.reference,
        "amount": amount,
        "currency": "NGN",
    }


def _provider(db: Session) -> str:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_payment_provider"
    )
    return str(value) if value else "paystack"


def verify_topup(
    db: Session, subscriber_id: str, reference: str, *, provider: str | None = None
) -> dict:
    """Verify a gateway top-up and credit the wallet (idempotent on reference)."""
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)

    existing = db.scalars(
        select(VasWalletEntry).where(VasWalletEntry.reference == reference)
    ).first()
    if existing:
        if existing.wallet_id != wallet.id:
            raise HTTPException(
                status_code=400,
                detail="Payment reference is already linked to a different wallet",
            )
        return {
            "amount": existing.amount,
            "already_recorded": True,
            "balance": wallet_balance(db, wallet.id),
        }

    tx = payment_gateway_adapter.verify(
        db, provider_type=provider or _provider(db), reference=reference
    )
    amount = Decimal(str(tx.amount)).quantize(Decimal("0.01"))
    entry = credit_wallet(
        db,
        wallet,
        amount=amount,
        category=VasEntryCategory.topup,
        reference=reference,
        memo=f"Wallet top-up via {tx.provider_type}",
    )
    logger.info(
        "vas_wallet_topup credited wallet=%s amount=%s ref=%s",
        wallet.id,
        amount,
        reference,
    )
    return {
        "amount": entry.amount,
        "already_recorded": False,
        "balance": wallet_balance(db, wallet.id),
    }


# --- DotMac-as-biller -------------------------------------------------------


def pay_bill(db: Session, subscriber_id: str, amount: Decimal) -> dict:
    """Pay the DotMac bill from the wallet.

    The ONLY bridge between the wallet and billing: debits the wallet and
    creates an ordinary Payment (auto-allocated to oldest unpaid invoices,
    remainder to service credit) — identical in effect to a gateway payment.
    """
    require_enabled(db)
    from app.schemas.billing import PaymentCreate
    from app.services.billing.payments import Payments

    wallet = get_or_create_wallet(db, subscriber_id)
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))

    entry = debit_wallet(
        db,
        wallet,
        amount=amount,
        category=VasEntryCategory.bill_payment,
        memo="DotMac bill payment from wallet",
    )
    try:
        payment = Payments.create(
            db,
            PaymentCreate(
                account_id=wallet.subscriber_id,
                amount=amount,
                external_id=f"vaswallet-{entry.id}",
                memo="Paid from wallet",
            ),
            auto_allocate=True,
        )
    except Exception:
        # Roll the debit back symmetrically — the customer must never lose
        # wallet money to a failed bill payment.
        credit_wallet(
            db,
            wallet,
            amount=amount,
            category=VasEntryCategory.adjustment,
            reference=f"reversal-{entry.id}",
            memo="Reversal: bill payment failed",
        )
        raise
    entry.payment_id = payment.id
    db.commit()
    return {
        "payment_id": str(payment.id),
        "amount": amount,
        "balance": wallet_balance(db, wallet.id),
    }


def set_auto_deduct(db: Session, subscriber_id: str, enabled: bool) -> VasWallet:
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)
    wallet.auto_pay_bill_enabled = bool(enabled)
    db.commit()
    db.refresh(wallet)
    return wallet


def wallet_overview(db: Session, subscriber_id: str, *, limit: int = 20) -> dict:
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)
    entries = (
        db.query(VasWalletEntry)
        .filter(VasWalletEntry.wallet_id == wallet.id)
        .order_by(VasWalletEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "balance": wallet_balance(db, wallet.id),
        "auto_pay_bill_enabled": wallet.auto_pay_bill_enabled,
        "currency": "NGN",
        "min_topup": _setting_int(db, "topup_min", 100),
        "max_topup": _setting_int(db, "topup_max_per_txn", 50000),
        "auth_threshold": _setting_int(db, "auth_threshold", 5000),
        "entries": entries,
    }


# --- Auto-deduct sweep (Celery) ----------------------------------------------


def _open_invoice_balance(db: Session, subscriber_id) -> Decimal:
    from app.models.billing import Invoice, InvoiceStatus

    today = datetime.now(UTC)
    total = db.query(
        func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00"))
    ).filter(
        Invoice.account_id == subscriber_id,
        Invoice.is_active.is_(True),
        Invoice.status.in_(
            [InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue]
        ),
        Invoice.due_at <= today,
    ).scalar() or Decimal("0.00")
    return Decimal(str(total))


def run_auto_deduct_sweep(db: Session) -> dict:
    """Pay due/overdue invoices from wallets that opted in.

    Pays min(wallet balance, due balance) per wallet; settlement idempotency
    and allocation are the Payments service's responsibility.
    """
    if not is_enabled(db):
        return {"status": "disabled", "paid": 0}
    paid = 0
    errors = 0
    swept_total = Decimal("0.00")
    wallets = (
        db.query(VasWallet)
        .filter(
            VasWallet.auto_pay_bill_enabled.is_(True),
            VasWallet.is_active.is_(True),
            VasWallet.subscriber_id.isnot(None),
        )
        .all()
    )
    for wallet in wallets:
        try:
            balance = wallet_balance(db, wallet.id)
            if balance <= 0:
                continue
            due = _open_invoice_balance(db, wallet.subscriber_id)
            if due <= 0:
                continue
            amount = min(balance, due)
            pay_bill(db, str(wallet.subscriber_id), amount)
            paid += 1
            swept_total += amount
        except Exception as exc:
            errors += 1
            logger.warning("vas auto-deduct failed for wallet %s: %s", wallet.id, exc)
    return {
        "status": "ok",
        "paid": paid,
        "errors": errors,
        "swept_total": str(swept_total),
    }
