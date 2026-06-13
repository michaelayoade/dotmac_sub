"""Tests for VAS Phase 3: rate cards, commission engine, reseller wallets."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscriber import Reseller, Subscriber
from app.models.vas import (
    VasEntryCategory,
    VasPartyType,
    VasRateCard,
    VasService,
    VasTransactionStatus,
)
from app.schemas.vas import VasResellerTransactionRead
from app.services import vas_purchases, vas_wallet


def _enable_vas(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.vas, key="enabled", value_text="true", is_active=True
        )
    )
    db_session.commit()


def _subscriber(db_session):
    subscriber = Subscriber(
        first_name="P3",
        last_name="Buyer",
        email=f"p3-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _reseller(db_session):
    reseller = Reseller(name="P3 Reseller", code=f"P3R{uuid.uuid4().hex[:8].upper()}")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


def _service(db_session, category="airtime"):
    service = VasService(
        category=category,
        service_id=f"svc-{uuid.uuid4().hex[:6]}",
        name="MTN Airtime",
        is_enabled=True,
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)
    return service


def _rates(db_session, *, owner="3.0", reseller=None, category="airtime"):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    db_session.add(
        VasRateCard(
            category=category,
            party_type=VasPartyType.owner,
            rate_pct=Decimal(owner),
            effective_from=yesterday,
        )
    )
    if reseller is not None:
        db_session.add(
            VasRateCard(
                category=category,
                party_type=VasPartyType.reseller,
                rate_pct=Decimal(reseller),
                effective_from=yesterday,
            )
        )
    db_session.commit()


def _ok_float():
    return patch.object(
        vas_purchases.vtpass, "get_balance", return_value=Decimal("1000000")
    )


DELIVERED_BODY = {
    "code": "000",
    "content": {"transactions": {"status": "delivered"}},
}
FAILED_BODY = {"code": "016", "response_description": "TRANSACTION FAILED"}


class TestRateResolution:
    def test_latest_effective_wins(self, db_session):
        now = datetime.now(UTC)
        for days_ago, rate in [(30, "2.0"), (10, "2.5"), (1, "3.0")]:
            db_session.add(
                VasRateCard(
                    category="airtime",
                    party_type=VasPartyType.owner,
                    rate_pct=Decimal(rate),
                    effective_from=now - timedelta(days=days_ago),
                )
            )
        # A future rate must not apply yet.
        db_session.add(
            VasRateCard(
                category="airtime",
                party_type=VasPartyType.owner,
                rate_pct=Decimal("9.9"),
                effective_from=now + timedelta(days=5),
            )
        )
        db_session.commit()
        assert vas_purchases.resolve_rate(
            db_session, "airtime", VasPartyType.owner
        ) == Decimal("3.0")

    def test_missing_returns_none(self, db_session):
        assert (
            vas_purchases.resolve_rate(db_session, "nope", VasPartyType.owner) is None
        )


class TestCommissionEngine:
    def test_customer_direct_snapshots_owner_only(self, db_session):
        _enable_vas(db_session)
        _rates(db_session, owner="3.0")
        subscriber = _subscriber(db_session)
        service = _service(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("5000"),
            category=VasEntryCategory.topup,
            reference=f"f-{uuid.uuid4().hex[:8]}",
        )
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08030000001",
                amount=Decimal("1000"),
            )
        assert txn.vtpass_rate_pct == Decimal("3.0")
        assert txn.reseller_rate_pct is None
        assert txn.owner_net == Decimal("30.00")

    def test_reseller_sale_pays_commission_and_splits_exactly(self, db_session):
        _enable_vas(db_session)
        _rates(db_session, owner="3.0", reseller="2.5")
        reseller = _reseller(db_session)
        service = _service(db_session)
        wallet = vas_wallet.get_or_create_reseller_wallet(db_session, str(reseller.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("5000"),
            category=VasEntryCategory.topup,
            reference=f"f-{uuid.uuid4().hex[:8]}",
        )
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.reseller_purchase(
                db_session,
                reseller_id=str(reseller.id),
                service_id=service.service_id,
                identifier="08030000002",
                amount=Decimal("333"),  # forces kobo rounding
            )
        assert txn.status == VasTransactionStatus.delivered
        assert txn.reseller_id == reseller.id
        gross = Decimal("9.99")  # floor(333 * 3.0%)
        payout = Decimal("8.32")  # floor(333 * 2.5%)
        assert txn.vtpass_rate_pct == Decimal("3.0")
        assert txn.reseller_rate_pct == Decimal("2.5")
        assert txn.owner_net == gross - payout  # splits sum exactly
        # Float wallet: 5000 - 333 (sale) + 8.32 (commission)
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("4675.32")

    def test_no_commission_on_failed_purchase(self, db_session):
        _enable_vas(db_session)
        _rates(db_session, owner="3.0", reseller="2.5")
        reseller = _reseller(db_session)
        service = _service(db_session)
        wallet = vas_wallet.get_or_create_reseller_wallet(db_session, str(reseller.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("5000"),
            category=VasEntryCategory.topup,
            reference=f"f-{uuid.uuid4().hex[:8]}",
        )
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=FAILED_BODY),
        ):
            txn = vas_purchases.reseller_purchase(
                db_session,
                reseller_id=str(reseller.id),
                service_id=service.service_id,
                identifier="08030000003",
                amount=Decimal("1000"),
            )
        assert txn.status == VasTransactionStatus.refunded
        assert txn.owner_net is None
        # Fully refunded: no commission entry, balance restored.
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("5000.00")

    def test_missing_rate_cards_never_break_delivery(self, db_session):
        _enable_vas(db_session)  # no rate cards at all
        subscriber = _subscriber(db_session)
        service = _service(db_session)
        wallet = vas_wallet.get_or_create_wallet(db_session, str(subscriber.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("5000"),
            category=VasEntryCategory.topup,
            reference=f"f-{uuid.uuid4().hex[:8]}",
        )
        with (
            _ok_float(),
            patch.object(vas_purchases.vtpass, "pay", return_value=DELIVERED_BODY),
        ):
            txn = vas_purchases.purchase(
                db_session,
                subscriber_id=str(subscriber.id),
                service_id=service.service_id,
                identifier="08030000004",
                amount=Decimal("1000"),
            )
        assert txn.status == VasTransactionStatus.delivered
        assert txn.owner_net is None


class TestResellerWallet:
    def test_reseller_topup_and_debit(self, db_session):
        _enable_vas(db_session)
        reseller = _reseller(db_session)
        wallet = vas_wallet.get_or_create_reseller_wallet(db_session, str(reseller.id))
        vas_wallet.credit_wallet(
            db_session,
            wallet,
            amount=Decimal("2000"),
            category=VasEntryCategory.topup,
            reference=f"f-{uuid.uuid4().hex[:8]}",
        )
        vas_wallet.debit_wallet(
            db_session,
            wallet,
            amount=Decimal("500"),
            category=VasEntryCategory.purchase,
        )
        assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("1500.00")

    def test_reseller_insufficient_float(self, db_session):
        _enable_vas(db_session)
        reseller = _reseller(db_session)
        wallet = vas_wallet.get_or_create_reseller_wallet(db_session, str(reseller.id))
        with pytest.raises(HTTPException) as exc_info:
            vas_wallet.debit_wallet(
                db_session,
                wallet,
                amount=Decimal("10"),
                category=VasEntryCategory.purchase,
            )
        assert exc_info.value.status_code == 400


class TestOverrideInvisibility:
    def test_reseller_serializer_has_no_owner_fields(self):
        """Build-failing guard: the reseller-facing schema must never carry
        owner-side economics (the override is internal by design)."""
        fields = set(VasResellerTransactionRead.model_fields)
        assert "vtpass_rate_pct" not in fields
        assert "owner_net" not in fields
        assert "commission_rate_pct" in fields
