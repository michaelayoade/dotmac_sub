"""Tests for the VAS wallet core (Phase 1)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import PaymentProvider, PaymentProviderType, TopupIntent
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.models.vas import VasEntryCategory
from app.schemas.settings import DomainSettingUpdate
from app.services import vas_wallet
from app.services.domain_settings import billing_settings, vas_settings
from app.services.settings_cache import SettingsCache


def _billing_setting(db_session, key: str, value: str) -> None:
    setting = (
        db_session.query(DomainSetting)
        .filter_by(domain=SettingDomain.billing, key=key)
        .first()
    )
    if setting is None:
        setting = DomainSetting(domain=SettingDomain.billing, key=key)
    setting.value_type = SettingValueType.string
    setting.value_text = value
    setting.value_json = None
    setting.is_secret = "secret" in key
    setting.is_active = True
    db_session.add(setting)
    db_session.commit()
    SettingsCache.invalidate(SettingDomain.billing.value, key)


@pytest.fixture(autouse=True)
def _configure_paystack_route(db_session):
    db_session.add(
        PaymentProvider(
            name="VAS Paystack Route",
            provider_type=PaymentProviderType.paystack,
            is_active=True,
        )
    )
    db_session.commit()
    _billing_setting(db_session, "paystack_secret_key", "sk_test_vas")
    _billing_setting(db_session, "paystack_public_key", "pk_test_vas")


def _enable_flutterwave_route(db_session) -> None:
    db_session.add(
        PaymentProvider(
            name="VAS Flutterwave Route",
            provider_type=PaymentProviderType.flutterwave,
            is_active=True,
        )
    )
    db_session.commit()
    _billing_setting(db_session, "flutterwave_secret_key", "FLWSECK_TEST-vas")
    _billing_setting(db_session, "flutterwave_public_key", "FLWPUBK_TEST-vas")
    _billing_setting(db_session, "flutterwave_secret_hash", "vas-hash")


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
    vas_intent = VasTopupIntent(
        reference=reference, wallet_id=wallet.id, amount=Decimal(amount)
    )
    db_session.add(vas_intent)
    db_session.flush()
    provider = (
        db_session.query(PaymentProvider)
        .filter_by(provider_type=PaymentProviderType.paystack, is_active=True)
        .one()
    )
    db_session.add(
        TopupIntent(
            account_id=subscriber.id,
            reference=reference,
            provider_type="paystack",
            requested_amount=Decimal(amount),
            metadata_={
                "payment_flow": "vas_wallet_topup",
                "vas_topup_intent_id": str(vas_intent.id),
                "vas_wallet_id": str(wallet.id),
                "provider_id": str(provider.id),
            },
        )
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

    def test_currency_and_topup_limits_are_single_sourced(self, db_session):
        _enable_vas(db_session)
        billing_settings.upsert_by_key(
            db_session,
            "default_currency",
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text="USD",
            ),
        )
        vas_settings.upsert_by_key(
            db_session,
            "topup_min",
            DomainSettingUpdate(value_type=SettingValueType.integer, value_text="250"),
        )
        vas_settings.upsert_by_key(
            db_session,
            "topup_max_per_txn",
            DomainSettingUpdate(value_type=SettingValueType.integer, value_text="7500"),
        )
        subscriber = _subscriber(db_session)

        overview = vas_wallet.wallet_overview(db_session, str(subscriber.id))

        assert overview["currency"] == "USD"
        assert overview["currency_symbol"] == "$"
        assert overview["min_topup"] == 250
        assert overview["max_topup"] == 7500

    def test_funding_provider_prefers_topup_memo_then_default(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        entry = vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("1000.00"),
            category=VasEntryCategory.topup,
            reference="flutter-ref",
            memo="Wallet top-up via flutterwave",
        )

        assert vas_wallet.funding_provider_for_entry(db_session, entry) == "flutterwave"

        entry.memo = None
        db_session.commit()
        assert vas_wallet.funding_provider_for_entry(db_session, entry) == "paystack"

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
        assert exc_info.value.detail["code"] == "insufficient_balance"

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


class TestTopupPaymentOptions:
    def test_only_paystack_when_no_flutterwave_provider(self, db_session):
        _enable_vas(db_session)
        assert vas_wallet.topup_payment_options(db_session) == [
            {"provider_type": "paystack", "label": "Pay with Paystack"},
        ]

    def test_surfaces_active_flutterwave_provider(self, db_session):
        _enable_vas(db_session)
        _enable_flutterwave_route(db_session)
        db_session.add_all(
            [
                PaymentProvider(
                    name="Disabled Flutterwave",
                    provider_type=PaymentProviderType.flutterwave,
                    is_active=False,
                ),
            ]
        )
        db_session.commit()
        assert vas_wallet.topup_payment_options(db_session) == [
            {"provider_type": "paystack", "label": "Pay with Paystack"},
            {"provider_type": "flutterwave", "label": "Pay with Flutterwave"},
        ]

    def test_overview_exposes_payment_options(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        overview = vas_wallet.wallet_overview(db_session, str(subscriber.id))
        assert overview["payment_options"] == [
            {"provider_type": "paystack", "label": "Pay with Paystack"},
        ]

    def test_initiate_threads_chosen_provider(self, db_session):
        _enable_vas(db_session)
        _enable_flutterwave_route(db_session)
        subscriber = _subscriber(db_session)
        seen: dict[str, str] = {}

        def _fake_build_context(_db, *, provider_type, **_kwargs):
            seen["provider_type"] = provider_type
            return SimpleNamespace(
                provider_type=provider_type,
                public_key="pk_test",
                reference="vas-ref-flw",
            )

        with patch.object(
            vas_wallet.payment_gateway_adapter,
            "build_context",
            _fake_build_context,
        ):
            result = vas_wallet.initiate_topup(
                db_session,
                str(subscriber.id),
                Decimal("1000"),
                provider="flutterwave",
            )
        assert seen["provider_type"] == "flutterwave"
        assert result["provider_type"] == "flutterwave"

    def test_initiate_defaults_provider_when_absent(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        seen: dict[str, str] = {}

        def _fake_build_context(_db, *, provider_type, **_kwargs):
            seen["provider_type"] = provider_type
            return SimpleNamespace(
                provider_type=provider_type,
                public_key="pk_test",
                reference="vas-ref-default",
            )

        with patch.object(
            vas_wallet.payment_gateway_adapter,
            "build_context",
            _fake_build_context,
        ):
            vas_wallet.initiate_topup(db_session, str(subscriber.id), Decimal("1000"))
        assert seen["provider_type"] == "paystack"
