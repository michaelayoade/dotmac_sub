"""Wallet money must never leave the wallet and reach nothing.

F25. ``pay_bill`` committed the wallet debit (``_write_entry`` committed) and only
then called ``Payments.create``. A process death in that window destroyed the
customer's money with no payment to show for it — and nothing could recover it:
the compensating ``credit_wallet`` only ever ran for a *raised exception*, never
for a crash.

The debit and the payment it funds are now one transaction, so a failure anywhere
rolls the debit back with it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.billing import Payment
from app.models.subscriber import Subscriber
from app.models.vas import VasEntryCategory, VasWalletEntry
from app.services import vas_wallet


@pytest.fixture(autouse=True)
def _wallet_enabled(monkeypatch):
    monkeypatch.setattr(vas_wallet, "is_enabled", lambda db: True)


def _account(db) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _funded_wallet(db, account, amount: str):
    wallet = vas_wallet.get_or_create_wallet(db, str(account.id))
    vas_wallet.credit_wallet(
        db,
        wallet,
        amount=Decimal(amount),
        category=VasEntryCategory.topup,
        memo="test funding",
    )
    db.commit()
    return wallet


def test_a_crash_between_debit_and_payment_loses_no_money(db_session, monkeypatch):
    """The exact failure the old compensating credit could not cover.

    BaseException (not Exception) stands in for a process death: it bypasses the
    ``except Exception`` compensation entirely, which is precisely why the old
    code lost the money.
    """
    account = _account(db_session)
    wallet = _funded_wallet(db_session, account, "10000.00")
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")

    class Crash(BaseException):
        """Not an Exception — the compensation cannot catch this."""

    def _die(*args, **kwargs):
        raise Crash("process died mid-flight")

    from app.services.billing.payments import Payments as PaymentsOwner

    monkeypatch.setattr(PaymentsOwner, "create", _die)

    with pytest.raises(BaseException, match="process died"):
        vas_wallet.pay_bill(db_session, str(account.id), Decimal("4000.00"))

    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00"), (
        "the wallet debit survived a crash that never produced a payment — the "
        "customer's money is gone"
    )
    debits = (
        db_session.query(VasWalletEntry)
        .filter(VasWalletEntry.wallet_id == wallet.id)
        .filter(VasWalletEntry.category == VasEntryCategory.bill_payment)
        .all()
    )
    assert not debits, "an orphaned bill-payment debit was left behind"


def test_a_failed_payment_leaves_the_wallet_whole(db_session, monkeypatch):
    """The ordinary exception path must also lose nothing."""
    account = _account(db_session)
    wallet = _funded_wallet(db_session, account, "10000.00")

    def _boom(*args, **kwargs):
        raise RuntimeError("gateway exploded")

    from app.services.billing.payments import Payments as PaymentsOwner

    monkeypatch.setattr(PaymentsOwner, "create", _boom)

    with pytest.raises(RuntimeError):
        vas_wallet.pay_bill(db_session, str(account.id), Decimal("4000.00"))

    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")


def test_a_successful_pay_bill_debits_once_and_posts_the_payment(db_session):
    account = _account(db_session)
    wallet = _funded_wallet(db_session, account, "10000.00")

    result = vas_wallet.pay_bill(db_session, str(account.id), Decimal("4000.00"))
    db_session.commit()

    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("6000.00")
    payment = db_session.get(Payment, uuid.UUID(result["payment_id"]))
    assert payment is not None
    assert payment.amount == Decimal("4000.00")

    # The debit is linked to the payment it funded — one fact, not two.
    debit = (
        db_session.query(VasWalletEntry)
        .filter(VasWalletEntry.wallet_id == wallet.id)
        .filter(VasWalletEntry.category == VasEntryCategory.bill_payment)
        .one()
    )
    assert debit.payment_id == payment.id


def test_insufficient_balance_still_refuses_and_keeps_the_wallet_whole(db_session):
    account = _account(db_session)
    wallet = _funded_wallet(db_session, account, "1000.00")

    with pytest.raises(Exception):
        vas_wallet.pay_bill(db_session, str(account.id), Decimal("4000.00"))

    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("1000.00")
