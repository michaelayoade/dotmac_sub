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
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.vas import (
    VasEntryCategory,
    VasEntryType,
    VasTopupIntent,
    VasWallet,
    VasWalletEntry,
)
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.billing._common import lock_account
from app.services.common import coerce_uuid
from app.services.domain_settings import vas_settings
from app.services.payment_gateway_adapter import payment_gateway_adapter

logger = logging.getLogger(__name__)


# --- Settings ---------------------------------------------------------------


def _setting_int(db: Session, key: str, default: int) -> int:
    value = settings_spec.resolve_value(db, SettingDomain.vas, key)
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _billing_setting(db: Session, key: str, default: str) -> str:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    text = str(value or "").strip()
    return text or default


def currency_code(db: Session) -> str:
    return _billing_setting(db, "default_currency", "NGN").upper()


def currency_symbol(db: Session) -> str:
    # Delegates to the canonical map; NGN/USD/EUR/GBP are identical there and
    # both fall back to the raw code, so VAS output is unchanged.
    from app.services import display_format

    return display_format.currency_symbol(currency_code(db))


def topup_limits(db: Session) -> dict[str, int]:
    return {
        "min_topup": _setting_int(db, "topup_min", 100),
        "max_topup": _setting_int(db, "topup_max_per_txn", 50000),
        "daily_limit": _setting_int(db, "topup_daily_limit", 100000),
        "auth_threshold": _setting_int(db, "auth_threshold", 5000),
    }


def pay_bill_dedupe_window_seconds(db: Session) -> int:
    return max(0, _setting_int(db, "pay_bill_dedupe_window_seconds", 60))


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


def get_or_create_reseller_wallet(db: Session, reseller_id: str) -> VasWallet:
    """The reseller float wallet (commissions credit here; sells debit it)."""
    rid = coerce_uuid(reseller_id)
    wallet = db.scalars(select(VasWallet).where(VasWallet.reseller_id == rid)).first()
    if wallet:
        return wallet
    wallet = VasWallet(reseller_id=rid)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def _lock_wallet_owner(db: Session, wallet: VasWallet) -> None:
    """Serialize read-modify-write per wallet owner (cf. lock_account)."""
    if wallet.subscriber_id is not None:
        lock_account(db, str(wallet.subscriber_id))
        return
    from app.models.subscriber import Reseller

    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.query(Reseller).filter(Reseller.id == wallet.reseller_id).with_for_update(
            of=Reseller
        ).first()


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
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_amount",
                "message": "Amount must be greater than 0",
            },
        )
    entry = VasWalletEntry(
        wallet_id=wallet.id,
        entry_type=entry_type,
        category=category,
        amount=amount,
        currency=currency_code(db),
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
    """Debit under an owner lock — insufficient funds is a 400, never negative."""
    _lock_wallet_owner(db, wallet)
    balance = wallet_balance(db, wallet.id)
    if amount > balance:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "insufficient_balance",
                "message": "Insufficient wallet balance",
            },
        )
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


def initiate_topup(
    db: Session, subscriber_id: str, amount: Decimal, *, provider: str | None = None
) -> dict:
    """Start a wallet top-up: limit checks + gateway checkout context.

    No Payment row is created — wallet top-ups are liabilities, not revenue;
    they live only in vas_wallet_entries (credited at verify).
    """
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)
    return _initiate_topup_for_wallet(db, wallet, amount, provider=provider)


def initiate_reseller_topup(
    db: Session, reseller_id: str, amount: Decimal, *, provider: str | None = None
) -> dict:
    require_enabled(db)
    wallet = get_or_create_reseller_wallet(db, reseller_id)
    return _initiate_topup_for_wallet(db, wallet, amount, provider=provider)


def _initiate_topup_for_wallet(
    db: Session, wallet: VasWallet, amount: Decimal, *, provider: str | None = None
) -> dict:
    amount = Decimal(str(amount))
    limits = topup_limits(db)
    min_amount = Decimal(limits["min_topup"])
    max_amount = Decimal(limits["max_topup"])
    daily_limit = Decimal(limits["daily_limit"])
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
    context = payment_gateway_adapter.build_context(
        db, provider_type=provider or _provider(db)
    )
    # Bind the reference to THIS wallet — verify refuses unknown references,
    # so a leaked/stolen reference can never credit a different wallet.
    db.add(
        VasTopupIntent(reference=context.reference, wallet_id=wallet.id, amount=amount)
    )
    db.commit()
    return {
        "provider_type": context.provider_type,
        "provider_public_key": context.public_key,
        "reference": context.reference,
        "amount": amount,
        "currency": currency_code(db),
    }


def _provider(db: Session) -> str:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_payment_provider"
    )
    return str(value) if value else "paystack"


def topup_payment_options(db: Session) -> list[dict[str, str]]:
    """Online-gateway options for VAS float top-up checkout.

    Reuses the consolidated-billing active-provider selection so VAS honours the
    same rule: Paystack is the baseline, Flutterwave appears only when an active
    Flutterwave ``PaymentProvider`` row exists, and the configured default is
    surfaced first. Bank transfer is billing-only and is not offered here.
    """
    from app.services.customer_portal_flow_payments import (
        online_gateway_payment_options,
    )

    return online_gateway_payment_options(db, _provider(db))


def verify_topup(
    db: Session, subscriber_id: str, reference: str, *, provider: str | None = None
) -> dict:
    """Verify a gateway top-up and credit the wallet (idempotent on reference)."""
    require_enabled(db)
    wallet = get_or_create_wallet(db, subscriber_id)
    return _verify_topup_for_wallet(db, wallet, reference, provider=provider)


def verify_reseller_topup(
    db: Session, reseller_id: str, reference: str, *, provider: str | None = None
) -> dict:
    require_enabled(db)
    wallet = get_or_create_reseller_wallet(db, reseller_id)
    return _verify_topup_for_wallet(db, wallet, reference, provider=provider)


def _verify_topup_for_wallet(
    db: Session, wallet: VasWallet, reference: str, *, provider: str | None = None
) -> dict:
    intent = db.scalars(
        select(VasTopupIntent).where(VasTopupIntent.reference == reference)
    ).first()
    if not intent or intent.wallet_id != wallet.id:
        raise HTTPException(status_code=400, detail="Unknown payment reference")

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


_PAY_BILL_IDEMPOTENCY_SCOPE = "wallet_pay_bill"


def _pay_bill_replay(db: Session, wallet: VasWallet, ref_id: str | None) -> dict | None:
    """Return the prior pay_bill result for a replayed idempotency key."""
    from app.models.billing import Payment

    payment = db.get(Payment, coerce_uuid(ref_id)) if ref_id else None
    if payment is None:
        return None
    return {
        "payment_id": str(payment.id),
        "amount": Decimal(str(payment.amount)).quantize(Decimal("0.01")),
        "balance": wallet_balance(db, wallet.id),
        "replayed": True,
    }


def pay_bill(
    db: Session,
    subscriber_id: str,
    amount: Decimal,
    *,
    idempotency_key: str | None = None,
) -> dict:
    """Pay the DotMac bill from the wallet.

    The ONLY bridge between the wallet and billing: debits the wallet and
    creates an ordinary Payment (auto-allocated to oldest unpaid invoices,
    remainder to service credit) — identical in effect to a gateway payment.

    Idempotent on ``idempotency_key`` (e.g. an ``Idempotency-Key`` header): a
    replay returns the original payment instead of debiting the wallet again.
    """
    require_enabled(db)
    from app.models.idempotency import IdempotencyKey
    from app.schemas.billing import PaymentCreate
    from app.services.billing.payments import Payments

    wallet = get_or_create_wallet(db, subscriber_id)
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))

    idem_key = (idempotency_key or "").strip() or None
    reservation: IdempotencyKey | None = None
    if idem_key:
        prior = db.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _PAY_BILL_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == idem_key,
            )
        ).first()
        if prior is not None:
            if str(prior.account_id) != str(wallet.subscriber_id):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_key_conflict",
                        "message": "Idempotency key already used.",
                    },
                )
            replayed = _pay_bill_replay(db, wallet, prior.ref_id)
            if replayed is not None:
                return replayed
            # Key reserved but never linked to a payment (a prior attempt died
            # mid-flight) — drop the orphan and retry below.
            db.delete(prior)
            db.commit()
        # Reserve the key BEFORE any money moves so a concurrent same-key
        # request fails the unique constraint here and never double-debits.
        reservation = IdempotencyKey(
            scope=_PAY_BILL_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=wallet.subscriber_id,
            ref_id=None,
        )
        db.add(reservation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            prior = db.scalars(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == _PAY_BILL_IDEMPOTENCY_SCOPE,
                    IdempotencyKey.key == idem_key,
                )
            ).first()
            replayed = _pay_bill_replay(db, wallet, prior.ref_id) if prior else None
            if replayed is not None:
                return replayed
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_payment",
                    "message": "A payment with this key is already in progress.",
                },
            )

    # Double-submit guard: an identical bill payment within the configured
    # window is almost certainly a double-click, not intent.
    from datetime import timedelta

    dedupe_window = pay_bill_dedupe_window_seconds(db)
    recent = (
        db.query(VasWalletEntry)
        .filter(
            VasWalletEntry.wallet_id == wallet.id,
            VasWalletEntry.entry_type == VasEntryType.debit,
            VasWalletEntry.category == VasEntryCategory.bill_payment,
            VasWalletEntry.amount == amount,
            VasWalletEntry.created_at
            >= datetime.now(UTC) - timedelta(seconds=dedupe_window),
        )
        .first()
        if dedupe_window > 0
        else None
    )
    if recent:
        if reservation is not None:
            db.delete(reservation)
            db.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_payment",
                "message": "An identical bill payment was made moments ago — wait "
                "a minute if you really mean to pay again.",
            },
        )

    try:
        entry = debit_wallet(
            db,
            wallet,
            amount=amount,
            category=VasEntryCategory.bill_payment,
            memo="DotMac bill payment from wallet",
        )
    except Exception:
        # Insufficient funds (or any debit failure) must release the key so the
        # customer can retry once they top up.
        if reservation is not None:
            db.delete(reservation)
            db.commit()
        raise
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
        if reservation is not None:
            db.delete(reservation)
            db.commit()
        raise
    entry.payment_id = payment.id
    if reservation is not None:
        reservation.ref_id = str(payment.id)
        db.add(reservation)
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
    limits = topup_limits(db)
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
        "currency": currency_code(db),
        "currency_symbol": currency_symbol(db),
        "min_topup": limits["min_topup"],
        "max_topup": limits["max_topup"],
        "daily_limit": limits["daily_limit"],
        "auth_threshold": limits["auth_threshold"],
        "entries": entries,
        "payment_options": topup_payment_options(db),
    }


# --- Auto-deduct sweep (Celery) ----------------------------------------------


def _open_invoice_balance(db: Session, subscriber_id) -> Decimal:
    from app.services.customer_financial_position import get_customer_financial_position

    return get_customer_financial_position(
        db,
        subscriber_id,
        include_prepaid_balance=False,
    ).due_invoice_balance


def run_auto_deduct_sweep(db: Session) -> dict:
    """Pay due/overdue invoices from wallets that opted in.

    Pays min(wallet balance, due balance) per wallet; settlement idempotency
    and allocation are the Payments service's responsibility.
    """
    if not is_enabled(db):
        result = {"status": "disabled", "paid": 0}
        record_auto_deduct_result(db, result)
        return result
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
    result = {
        "status": "ok",
        "paid": paid,
        "errors": errors,
        "swept_total": str(swept_total),
    }
    record_auto_deduct_result(db, result)
    return result


def record_auto_deduct_result(db: Session, result: dict[str, Any]) -> None:
    payload = dict(result)
    payload["finished_at"] = datetime.now(UTC).isoformat()
    vas_settings.upsert_by_key(
        db,
        "auto_deduct_last_result",
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=payload,
        ),
    )


def last_auto_deduct_result(db: Session) -> dict[str, Any] | None:
    try:
        setting = vas_settings.get_by_key(db, "auto_deduct_last_result")
    except Exception:
        return None
    return setting.value_json if isinstance(setting.value_json, dict) else None


def wallet_entries(db: Session, wallet_id, *, limit: int = 20) -> list[VasWalletEntry]:
    return (
        db.query(VasWalletEntry)
        .filter(VasWalletEntry.wallet_id == wallet_id)
        .order_by(VasWalletEntry.created_at.desc())
        .limit(limit)
        .all()
    )


def commission_summary(db: Session, wallet_id) -> dict:
    """Total + recent commission entries for a (reseller) wallet."""
    total = db.query(
        func.coalesce(func.sum(VasWalletEntry.amount), Decimal("0.00"))
    ).filter(
        VasWalletEntry.wallet_id == wallet_id,
        VasWalletEntry.entry_type == VasEntryType.credit,
        VasWalletEntry.category == VasEntryCategory.commission,
    ).scalar() or Decimal("0.00")
    entries = (
        db.query(VasWalletEntry)
        .filter(
            VasWalletEntry.wallet_id == wallet_id,
            VasWalletEntry.category == VasEntryCategory.commission,
        )
        .order_by(VasWalletEntry.created_at.desc())
        .limit(50)
        .all()
    )
    return {"total": Decimal(str(total)), "entries": entries}


def topup_entry(db: Session, entry_id: str) -> VasWalletEntry | None:
    return db.get(VasWalletEntry, entry_id)


def wallet_by_id(db: Session, wallet_id) -> VasWallet | None:
    return db.get(VasWallet, wallet_id)


def refund_reference_exists(db: Session, entry_id: str) -> bool:
    return (
        db.query(VasWalletEntry)
        .filter(VasWalletEntry.reference == f"rts-{entry_id}")
        .first()
        is not None
    )


def funding_provider_for_entry(db: Session, entry: VasWalletEntry) -> str:
    memo = str(entry.memo or "").strip().lower()
    prefix = "wallet top-up via "
    if memo.startswith(prefix):
        provider = memo.removeprefix(prefix).strip()
        if provider:
            return provider
    return _provider(db)
