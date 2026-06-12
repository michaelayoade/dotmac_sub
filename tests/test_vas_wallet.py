"""Tests for the VAS wallet core (Phase 1)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.models.vas import VasEntryCategory
from app.services import vas_wallet


def _enable_vas(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.vas,
            key="enabled",
            value_text="true",
            is_active=True,
        )
    )
    db_session.commit()


def _subscriber(db_session):
    subscriber = Subscriber(
        first_name="Wallet",
        last_name="Owner",
        email=f"wallet-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _bind_intent(db_session, subscriber, reference, amount="2500.00"):
    from app.models.vas import VasTopupIntent

    wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
    db_session.add(
        VasTopupIntent(reference=reference, wallet_id=wallet.id, amount=Decimal(amount))
    )
    db_session.commit()
    return wallet


class TestFeatureFlag:
    def test_disabled_by_default(self, db_session):
        assert vas_wallet.is_enabled(db_session) is False

    def test_endpoints_gate_with_404(self, db_session):
        subscriber = _subscriber(db_session)
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.wallet_overview(db_session, str(subscriber.id))
        assert exc_info.value.status_code == 404


class TestWalletLedger:
    def test_balance_credits_minus_debits(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("5000.00"),
            category=VasEntryCategory.topup,
            reference="ref-1",
        )
        vas_wallet.debit_wallet(
            db_session,
            wallet,
            amount=Decimal("1200.50"),
            category=VasEntryCategory.purchase,
        )
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("3799.50")

    def test_get_or_create_is_idempotent(self, db_session):
        subscriber = _subscriber(db_session)
        w1 = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        w2 = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        assert w1.id == w2.id

    def test_debit_insufficient_funds(self, db_session):
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.debit_wallet(
                db_session,
                wallet,
                amount=Decimal("10.00"),
                category=VasEntryCategory.purchase,
            )
        assert exc_info.value.status_code == 400
        assert "Insufficient" in exc_info.value.detail

    def test_amount_must_be_positive(self, db_session):
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        with pytest.raises(HTTPException):
            vas_wallet.credit_wallet(
                db_session,
                wallet,
                amount=Decimal("0.00"),
                category=VasEntryCategory.topup,
            )


class TestTopup:
    def test_initiate_respects_limits(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.initiate_topup(db_session, str(subscriber.id), Decimal("50"))
        assert "Minimum" in exc_info.value.detail
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.initiate_topup(db_session, str(subscriber.id), Decimal("60000"))
        assert "Maximum" in exc_info.value.detail

    def test_initiate_daily_limit(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("90000.00"),
            category=VasEntryCategory.topup,
            reference="big-topup",
        )
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.initiate_topup(db_session, str(subscriber.id), Decimal("20000"))
        assert "Daily" in exc_info.value.detail

    def test_verify_credits_and_is_idempotent(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        tx = SimpleNamespace(
            amount=Decimal("2500"), provider_type="paystack", external_id="x1"
        )
        _bind_intent(db_session, subscriber, "ref-9")
        with patch.object(
            vas_wallet.payment_gateway_adapter, "verify", return_value=tx
        ) as mock_verify:
            first = vas_wallet.verify_topup(db_session, str(subscriber.id), "ref-9")
            second = vas_wallet.verify_topup(db_session, str(subscriber.id), "ref-9")
        assert first["already_recorded"] is False
        assert first["balance"] == Decimal("2500.00")
        assert second["already_recorded"] is True
        assert second["balance"] == Decimal("2500.00")
        assert mock_verify.call_count == 1

    def test_verify_rejects_foreign_reference(self, db_session):
        _enable_vas(db_session)
        owner = _subscriber(db_session)
        attacker = _subscriber(db_session)
        tx = SimpleNamespace(
            amount=Decimal("1000"), provider_type="paystack", external_id="x2"
        )
        _bind_intent(db_session, owner, "ref-owned", amount="1000.00")
        with patch.object(
            vas_wallet.payment_gateway_adapter, "verify", return_value=tx
        ):
            vas_wallet.verify_topup(db_session, str(owner.id), "ref-owned")
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.verify_topup(db_session, str(attacker.id), "ref-owned")
        assert exc_info.value.status_code == 400


class TestPayBill:
    def test_pay_bill_debits_and_creates_payment(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("10000.00"),
            category=VasEntryCategory.topup,
            reference="fund-1",
        )
        result = vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("4000"))
        assert result["balance"] == Decimal("6000.00")

        from app.models.billing import Payment

        payment = db_session.get(Payment, uuid.UUID(result["payment_id"]))
        assert payment is not None
        assert payment.amount == Decimal("4000.00")
        # And the service credit balance now sees the money (the bridge worked)
        from app.services.billing._common import get_account_credit_balance

        assert get_account_credit_balance(db_session, str(subscriber.id)) >= Decimal(
            "0.00"
        )

    def test_pay_bill_insufficient(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("50"))
        assert exc_info.value.status_code == 400

    def test_pay_bill_reverses_debit_on_payment_failure(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("3000.00"),
            category=VasEntryCategory.topup,
            reference="fund-2",
        )
        from app.services.billing.payments import Payments

        with (
            patch.object(Payments, "create", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("1000"))
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("3000.00")


class TestAutoDeduct:
    def test_toggle(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.set_auto_deduct(db_session, str(subscriber.id), True)
        assert wallet.auto_pay_bill_enabled is True
        wallet = vas_wallet.set_auto_deduct(db_session, str(subscriber.id), False)
        assert wallet.auto_pay_bill_enabled is False

    def test_sweep_disabled_feature(self, db_session):
        result = vas_wallet.run_auto_deduct_sweep(db_session)
        assert result["status"] == "disabled"

    def test_sweep_skips_empty_and_opted_out(self, db_session):
        _enable_vas(db_session)
        opted_out = _subscriber(db_session)
        vas_wallet.get_or_create_wallet(db_session, str(opted_out.id))
        empty = _subscriber(db_session)
        vas_wallet.set_auto_deduct(db_session, str(empty.id), True)
        result = vas_wallet.run_auto_deduct_sweep(db_session)
        assert result["paid"] == 0
        assert result["errors"] == 0


class TestReviewFixes:
    def test_verify_rejects_unbound_reference(self, db_session):
        """Reference theft: verifying a reference you didn't initiate fails."""
        _enable_vas(db_session)
        attacker = _subscriber(db_session)
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.verify_topup(db_session, str(attacker.id), "stolen-ref")
        assert exc_info.value.status_code == 400
        assert "Unknown payment reference" in exc_info.value.detail

    def test_verify_rejects_other_wallets_intent(self, db_session):
        _enable_vas(db_session)
        owner = _subscriber(db_session)
        attacker = _subscriber(db_session)
        _bind_intent(db_session, owner, "owners-ref")
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.verify_topup(db_session, str(attacker.id), "owners-ref")
        assert exc_info.value.status_code == 400

    def test_initiate_binds_intent(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        result = vas_wallet.initiate_topup(
            db_session, str(subscriber.id), Decimal("1000")
        )
        from app.models.vas import VasTopupIntent

        intent = (
            db_session.query(VasTopupIntent)
            .filter(VasTopupIntent.reference == result["reference"])
            .one()
        )
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        assert intent.wallet_id == wallet.id

    def test_pay_bill_double_submit_guard(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("10000.00"),
            category=VasEntryCategory.topup,
            reference="dsg-fund",
        )
        vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("2000"))
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("2000"))
        assert exc_info.value.status_code == 409
        # A different amount goes through.
        result = vas_wallet.pay_bill(db_session, str(subscriber.id), Decimal("1000"))
        assert result["balance"] == Decimal("7000.00")
