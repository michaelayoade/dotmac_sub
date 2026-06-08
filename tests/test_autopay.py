"""Autopay engine — mandate + auto-charge of due invoices.

Paystack is mocked (no live keys); the charge path runs through the real billing
adapter so a recorded payment actually reduces the invoice balance.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.services.paystack as paystack
from app.models.billing import InvoiceStatus, Payment
from app.schemas.billing import InvoiceCreate, PaymentMethodCreate
from app.services import autopay
from app.services import billing as billing_service


def _card(db_session, account_id, *, default=True):
    return billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=account_id,
            label="Visa •••• 4081",
            token="AUTH_test",
            last4="4081",
            brand="visa",
            is_default=default,
        ),
    )


def _open_invoice(db_session, account_id, amount: Decimal):
    return billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=amount,
            total=amount,
            balance_due=amount,
        ),
    )


def _mock_charge(monkeypatch, *, status="success"):
    def fake(db, **kwargs):
        return {"status": status, "reference": kwargs.get("reference")}

    monkeypatch.setattr(paystack, "charge_authorization", fake)


# --- mandate management ----------------------------------------------------


def test_enable_requires_a_saved_card(db_session, subscriber):
    with pytest.raises(ValueError, match="saved card"):
        autopay.enable(db_session, str(subscriber.id))


def test_enable_then_status_then_disable(db_session, subscriber):
    card = _card(db_session, subscriber.id)
    autopay.enable(db_session, str(subscriber.id))

    status = autopay.get_status(db_session, str(subscriber.id))
    assert status["enabled"] is True
    assert status["payment_method_id"] == str(card.id)

    assert autopay.disable(db_session, str(subscriber.id)) is True
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is False


# --- the charge engine -----------------------------------------------------


def test_run_charges_open_invoice_and_records_payment(
    db_session, subscriber, monkeypatch
):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    assert result["failed"] == 0

    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("0.00")
    payments = (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).all()
    )
    assert len(payments) == 1
    assert Decimal(str(payments[0].amount)) == Decimal("5000.00")


def test_run_does_not_record_when_charge_fails(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="failed")

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 0
    assert result["failed"] == 1

    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("5000.00")
    assert (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).count()
        == 0
    )


def test_run_noop_when_not_enabled(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("1000.00"))
    _mock_charge(monkeypatch)
    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result.get("skipped") == "not_enabled"


def test_removing_the_card_disables_autopay(db_session, subscriber):
    from app.services import customer_portal_flow_payment_methods as cards

    card = _card(db_session, subscriber.id)
    autopay.enable(db_session, str(subscriber.id))
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is True

    cards.remove(db_session, str(subscriber.id), str(card.id))
    assert autopay.get_status(db_session, str(subscriber.id))["enabled"] is False


def test_run_all_due_iterates_active_mandates(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("2500.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    summary = autopay.run_all_due(db_session)
    assert summary["accounts"] == 1
    assert summary["charged"] == 1


def test_rerun_does_not_double_charge(db_session, subscriber, monkeypatch):
    _card(db_session, subscriber.id)
    _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))
    _mock_charge(monkeypatch, status="success")

    first = autopay.run_account_autopay(db_session, str(subscriber.id))
    second = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert first["charged"] == 1
    assert second["charged"] == 0  # invoice already settled — not re-charged
    assert (
        db_session.query(Payment).filter(Payment.account_id == subscriber.id).count()
        == 1
    )


def test_recovers_capture_when_charge_errors(db_session, subscriber, monkeypatch):
    # The charge attempt errors (e.g. a duplicate-reference error after the card
    # was already captured); recovery via verify_transaction records it instead
    # of re-charging.
    _card(db_session, subscriber.id)
    invoice = _open_invoice(db_session, subscriber.id, Decimal("5000.00"))
    autopay.enable(db_session, str(subscriber.id))

    def boom(db, **kwargs):
        raise RuntimeError("Duplicate Transaction Reference")

    monkeypatch.setattr(paystack, "charge_authorization", boom)
    monkeypatch.setattr(
        paystack,
        "verify_transaction",
        lambda db, ref: {"status": "success", "reference": ref},
    )

    result = autopay.run_account_autopay(db_session, str(subscriber.id))
    assert result["charged"] == 1
    db_session.refresh(invoice)
    assert Decimal(str(invoice.balance_due)) == Decimal("0.00")
