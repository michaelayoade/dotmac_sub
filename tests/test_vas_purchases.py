"""Tests for the VAS purchase engine (Phase 2 state machine)."""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Subscriber
from app.models.subscription_engine import SettingValueType
from app.models.vas import (
    VasEntryCategory,
    VasService,
    VasServiceVariation,
    VasTransactionStatus,
)
from app.schemas.settings import DomainSettingUpdate
from app.services import vas_purchases, vas_wallet
from app.services.domain_settings import vas_settings


def _enable_vas(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.vas, key="enabled", value_text="true", is_active=True
        )
    )
    db_session.commit()


def _subscriber(db_session):
    subscriber = Subscriber(
        first_name="Buyer",
        last_name="One",
        email=f"buyer-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _airtime_service(db_session, *, enabled=True, category="airtime"):
    service = VasService(
        category=category,
        service_id=f"mtn-{uuid.uuid4().hex[:6]}",
        name="MTN Airtime",
        is_enabled=enabled,
        min_amount=Decimal("50"),
        max_amount=Decimal("50000"),
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)
    return service


def _funded_wallet(db_session, subscriber, amount="10000.00"):
    wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
    vas_wallet.credit_wallet(
        db_session,
        wallet,
        amount=Decimal(amount),
        category=VasEntryCategory.topup,
        reference=f"fund-{uuid.uuid4().hex[:8]}",
    )
    return wallet


def _ok_float():
    return patch.object(
        vas_purchases.vtpass, "get_balance", return_value=Decimal("1000000")
    )


DELIVERED_BODY = {
    "code": "000",
    "content": {"transactions": {"status": "delivered"}},
    "purchased_code": "Token: 1234-5678-9012",
}
PROCESSING_BODY = {"code": "099", "response_description": "TRANSACTION PROCESSING"}
FAILED_BODY = {"code": "016", "response_description": "TRANSACTION FAILED"}


class TestCatalog:
    def test_customer_catalog_filters_disabled(self, db_session):
        _enable_vas(db_session)
        _airtime_service(db_session, enabled=True)
        _airtime_service(db_session, enabled=False)
        _airtime_service(db_session, enabled=True, category="electricity-bill")
        catalog = vas_purchases.customer_catalog(db_session)
        assert len(catalog) == 1  # electricity not in enabled_categories default
        assert catalog[0]["category"] == "airtime"
        assert len(catalog[0]["services"]) == 1


class TestPurchaseStateMachine:
    def test_delivered_immediately(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("1000"),
            )
        assert txn.status == VasTransactionStatus.delivered
        assert txn.delivered_at is not None
        assert vas_purchases.transaction_token(txn) == "Token: 1234-5678-9012"
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9000.00")

    def test_processing_stays_submitted(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=PROCESSING_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert txn.status == VasTransactionStatus.submitted
        # Money stays debited while ambiguous — no refund yet.
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9500.00")

    def test_failed_refunds_instantly(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=FAILED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert txn.status == VasTransactionStatus.refunded
        assert txn.refunded_at is not None
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")

    def test_transport_error_keeps_debit_for_requery(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(
                vas_purchases.vtpass,
                "pay",
                side_effect=HTTPException(status_code=502, detail="down"),
            ),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        # Ambiguous: may have reached the provider — requery decides, no refund.
        assert txn.status == VasTransactionStatus.submitted
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9500.00")

    def test_insufficient_wallet_blocks_before_provider(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay") as mock_pay,
            pytest.raises(HTTPException) as exc_info,
        ):
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert exc_info.value.status_code == 400
        mock_pay.assert_not_called()

    def test_float_gate_blocks_before_debit(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            patch.object(
                vas_purchases.vtpass, "get_balance", return_value=Decimal("5000")
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert exc_info.value.status_code == 503
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")

    def test_disabled_service_404(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session, enabled=False)
        with pytest.raises(HTTPException) as exc_info:
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert exc_info.value.status_code == 404

    def test_variation_fixed_price_wins(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session, category="data")
        db_session.add(
            DomainSetting(
                domain=SettingDomain.vas,
                key="enabled_categories",
                value_text="airtime,data",
                is_active=True,
            )
        )
        variation = VasServiceVariation(
            service_pk=service.id,
            code="mtn-1gb",
            name="1GB - 30 days",
            amount=Decimal("350.00"),
        )
        db_session.add(variation)
        db_session.commit()
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(
                vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY
            ) as mock_pay,
        ):
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                variation_code="mtn-1gb",
                amount=Decimal("9999"),  # ignored — variation price wins
            )
        assert mock_pay.call_args.kwargs["amount"] == Decimal("350.00")
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9650.00")


class TestRequerySweep:
    def _submitted_txn(self, db_session, subscriber, service):
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=PROCESSING_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        return wallet, txn

    def test_requery_delivers(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet, txn = self._submitted_txn(db_session, subscriber, service)
        with patch.object(vas_purchases.vtpass, "requery", return_value=DELIVERED_BODY):
            stats = vas_purchases.run_requery_sweep(db_session)
        db_session.refresh(txn)
        assert stats["delivered"] == 1
        assert txn.status == VasTransactionStatus.delivered
        assert vas_purchases.transaction_token(txn) is not None

    def test_requery_failure_refunds(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet, txn = self._submitted_txn(db_session, subscriber, service)
        with patch.object(vas_purchases.vtpass, "requery", return_value=FAILED_BODY):
            stats = vas_purchases.run_requery_sweep(db_session)
        db_session.refresh(txn)
        assert stats["refunded"] == 1
        assert txn.status == VasTransactionStatus.refunded
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")

    def test_requery_exhaustion_goes_to_review_not_refund(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet, txn = self._submitted_txn(db_session, subscriber, service)
        txn.requery_attempts = vas_purchases.REQUERY_MAX_ATTEMPTS - 1
        db_session.commit()
        with patch.object(
            vas_purchases.vtpass, "requery", return_value=PROCESSING_BODY
        ):
            stats = vas_purchases.run_requery_sweep(db_session)
        db_session.refresh(txn)
        assert stats["review"] == 1
        assert txn.status == VasTransactionStatus.review
        # Crucially: NOT refunded — money stays held for manual resolution.
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9500.00")

    def test_requery_exhaustion_uses_configured_cap(self, db_session):
        _enable_vas(db_session)
        vas_settings.upsert_by_key(
            db_session,
            "requery_max_attempts",
            DomainSettingUpdate(value_type=SettingValueType.integer, value_text="1"),
        )
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        _, txn = self._submitted_txn(db_session, subscriber, service)
        with patch.object(
            vas_purchases.vtpass, "requery", return_value=PROCESSING_BODY
        ):
            stats = vas_purchases.run_requery_sweep(db_session)
        db_session.refresh(txn)
        assert stats["review"] == 1
        assert txn.status == VasTransactionStatus.review

    def test_provider_unreachable_leaves_submitted(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet, txn = self._submitted_txn(db_session, subscriber, service)
        with patch.object(
            vas_purchases.vtpass,
            "requery",
            side_effect=HTTPException(status_code=502, detail="down"),
        ):
            vas_purchases.run_requery_sweep(db_session)
        db_session.refresh(txn)
        assert txn.status == VasTransactionStatus.submitted


class TestOwnership:
    def test_get_transaction_is_owner_scoped(self, db_session):
        _enable_vas(db_session)
        owner = _subscriber(db_session)
        attacker = _subscriber(db_session)
        service = _airtime_service(db_session)
        _funded_wallet(db_session, owner)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(owner.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        with pytest.raises(HTTPException) as exc_info:
            vas_purchases.get_transaction(db_session, str(attacker.id), str(txn.id))
        assert exc_info.value.status_code == 404


class TestHardening:
    def test_duplicate_intent_blocked_then_confirmable(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
            with pytest.raises(HTTPException) as exc_info:
                vas_purchases.purchase(
                    db_session,
                    subscriber_id=str(subscriber.id),
                    service_id=service.service_id,
                    identifier="08031234567",
                    amount=Decimal("500"),
                )
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["code"] == "duplicate_purchase"
            assert exc_info.value.detail["confirm_required"] is True
            # Explicit confirmation goes through.
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
                confirm_duplicate=True,
            )
            assert txn.status == VasTransactionStatus.delivered

    def test_refunded_duplicate_not_blocked(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        _funded_wallet(db_session, subscriber)
        with _ok_float():
            with patch.object(vas_purchases.vtpass, "pay", return_value=FAILED_BODY):
                vas_purchases.purchase(
                    db_session,
                    subscriber_id=str(subscriber.id),
                    service_id=service.service_id,
                    identifier="08031234567",
                    amount=Decimal("500"),
                )
            # Failed/refunded — an immediate retry is legitimate, no guard.
            with patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY):
                txn = vas_purchases.purchase(
                    db_session,
                    subscriber_id=str(subscriber.id),
                    service_id=service.service_id,
                    identifier="08031234567",
                    amount=Decimal("500"),
                )
        assert txn.status == VasTransactionStatus.delivered

    def test_purchase_dedupe_window_can_be_disabled(self, db_session):
        _enable_vas(db_session)
        vas_settings.upsert_by_key(
            db_session,
            "purchase_dedupe_window_seconds",
            DomainSettingUpdate(value_type=SettingValueType.integer, value_text="0"),
        )
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            first = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
            second = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert first.status == VasTransactionStatus.delivered
        assert second.status == VasTransactionStatus.delivered

    def test_live_price_overrides_stale_cache(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session, category="data")
        db_session.add(
            DomainSetting(
                domain=SettingDomain.vas,
                key="enabled_categories",
                value_text="airtime,data",
                is_active=True,
            )
        )
        variation = VasServiceVariation(
            service_pk=service.id,
            code="mtn-1gb",
            name="1GB - 30 days",
            amount=Decimal("350.00"),  # stale cached price
        )
        db_session.add(variation)
        db_session.commit()
        wallet = _funded_wallet(db_session, subscriber)
        live_variations = {
            "variations": [{"variation_code": "mtn-1gb", "variation_amount": "400.00"}]
        }
        with (
            _ok_float(),
            patch.object(
                vas_purchases.vtpass, "get_variations", return_value=live_variations
            ),
            patch.object(
                vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY
            ) as mock_pay,
        ):
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                variation_code="mtn-1gb",
            )
        # Debited and paid at the LIVE price, cache updated.
        assert mock_pay.call_args.kwargs["amount"] == Decimal("400.00")
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("9600.00")
        db_session.refresh(variation)
        assert variation.amount == Decimal("400.00")

    def test_review_requery_closes_parked(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        wallet = _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=PROCESSING_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        txn.status = vas_purchases.VasTransactionStatus.review
        db_session.commit()
        with patch.object(vas_purchases.vtpass, "requery", return_value=FAILED_BODY):
            stats = vas_purchases.run_review_requery(db_session)
        db_session.refresh(txn)
        assert stats["refunded"] == 1
        assert txn.status == vas_purchases.VasTransactionStatus.refunded
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("10000.00")

    def test_reseller_id_stamped_null_for_customer(self, db_session):
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08031234567",
                amount=Decimal("500"),
            )
        assert txn.reseller_id is None


class TestReviewFixes:
    def test_failed_debit_marks_txn_failed_and_allows_retry(self, db_session):
        """An insufficient-funds attempt must not 409-block the retry."""
        _enable_vas(db_session)
        subscriber = _subscriber(db_session)
        service = _airtime_service(db_session)
        with _ok_float(), pytest.raises(HTTPException) as exc_info:
            vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08039999999",
                amount=Decimal("500"),
            )
        # Insufficient-balance now surfaces a structured {code, message} detail.
        detail = exc_info.value.detail
        assert detail["code"] == "insufficient_balance"
        assert "Insufficient" in detail["message"]
        from app.models.vas import VasTransaction

        orphan = db_session.query(VasTransaction).one()
        assert orphan.status == VasTransactionStatus.failed

        # Fund and retry — must NOT trip the duplicate guard.
        _funded_wallet(db_session, subscriber)
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08039999999",
                amount=Decimal("500"),
            )
        assert txn.status == VasTransactionStatus.delivered
