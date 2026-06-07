from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.services.billing._common import get_account_credit_balance
from app.services.customer_portal_context import get_available_portal_offers
from app.services.customer_portal_flow_changes import apply_instant_plan_change


def _make_offer(
    db_session,
    *,
    name: str,
    amount: Decimal,
    plan_family: str | None,
    billing_mode: BillingMode = BillingMode.prepaid,
    service_type: ServiceType = ServiceType.residential,
    show_on_customer_portal: bool = True,
    is_active: bool = True,
) -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        code=name.lower().replace(" ", "-"),
        service_type=service_type,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=billing_mode,
        plan_family=plan_family,
        show_on_customer_portal=show_on_customer_portal,
        is_active=is_active,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=amount,
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()
    db_session.refresh(offer)
    return offer


def _make_subscription(
    db_session,
    subscriber,
    offer: CatalogOffer,
    *,
    next_billing_at: datetime,
    start_at: datetime,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=start_at,
        next_billing_at=next_billing_at,
    )
    db_session.add(subscription)
    db_session.commit()
    db_session.refresh(subscription)
    return subscription


def _freeze_subscription_now(monkeypatch, frozen: datetime) -> None:
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    subscriptions_service = importlib.import_module(
        "app.services.catalog.subscriptions"
    )

    monkeypatch.setattr(subscriptions_service, "datetime", _FrozenDateTime)


def _stub_plan_change_side_effects(
    monkeypatch, event_types: list | None = None
) -> None:
    import app.services.enforcement as enforcement_service
    import app.services.radius as radius_service

    subscriptions_service = importlib.import_module(
        "app.services.catalog.subscriptions"
    )

    monkeypatch.setattr(
        subscriptions_service,
        "_sync_credentials_to_radius",
        lambda db, subscriber_id: None,
    )
    monkeypatch.setattr(
        enforcement_service,
        "update_subscription_sessions",
        lambda db, subscription_id, reason=None: None,
        raising=False,
    )
    monkeypatch.setattr(
        radius_service,
        "reconcile_subscription_connectivity",
        lambda db, subscription_id: None,
        raising=False,
    )
    monkeypatch.setattr(
        subscriptions_service,
        "emit_event",
        (
            lambda db, event_type, payload, **kwargs: (
                event_types.append(event_type) if event_types is not None else None
            )
        ),
    )


def test_get_available_portal_offers_only_returns_same_family_compatible_offers(
    db_session, subscriber
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    allowed_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    _make_offer(
        db_session,
        name="Unlimited Postpaid",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert {str(offer.id) for offer in offers} == {
        str(current_offer.id),
        str(allowed_offer.id),
    }


def test_validate_plan_change_rejects_cross_family_change(db_session, subscriber):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    with pytest.raises(HTTPException) as exc:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(offer_id=target_offer.id),
        )

    assert exc.value.status_code == 400
    assert "same plan family" in exc.value.detail.lower()


def test_prepaid_upgrade_returns_insufficient_balance_without_mutation(
    db_session, subscriber, monkeypatch
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    import app.services.subscription_changes as change_service

    create_mock = Mock()
    apply_mock = Mock()
    monkeypatch.setattr(
        change_service.subscription_change_requests, "create", create_mock
    )
    monkeypatch.setattr(
        change_service.subscription_change_requests, "apply", apply_mock
    )

    result = apply_instant_plan_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
    )

    db_session.refresh(subscription)
    assert result["success"] is False
    assert result["reason"] == "insufficient_balance"
    assert result["required_amount"] == Decimal("50.00")
    assert result["current_balance"] == Decimal("0.00")
    assert result["shortfall"] == Decimal("50.00")
    assert subscription.offer_id == current_offer.id
    assert db_session.query(LedgerEntry).count() == 0
    create_mock.assert_not_called()
    apply_mock.assert_not_called()


def test_proration_uses_exact_cycle_seconds(db_session, subscriber, monkeypatch):
    from app.services.catalog.subscriptions import _calculate_proration

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    proration = _calculate_proration(db_session, subscription, str(target_offer.id))

    assert proration["remaining_ratio"] == Decimal("0.5")
    assert proration["total_cycle_seconds"] == 2678400
    assert proration["remaining_cycle_seconds"] == 1339200
    assert proration["credit_amount"] == Decimal("50.00")
    assert proration["charge_amount"] == Decimal("100.00")
    assert proration["net_amount"] == Decimal("50.00")


def test_prepaid_upgrade_with_exact_balance_preserves_anniversary_and_uses_wallet(
    db_session, subscriber, monkeypatch
):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    next_billing_at = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=next_billing_at,
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    _stub_plan_change_side_effects(monkeypatch)

    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Wallet top-up",
        )
    )
    db_session.commit()

    result = apply_instant_plan_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
    )

    db_session.refresh(subscription)
    debits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .all()
    )

    assert result["success"] is True
    assert subscription.offer_id == target_offer.id
    assert subscription.next_billing_at.replace(tzinfo=UTC) == next_billing_at
    assert len(debits) == 1
    assert debits[0].amount == Decimal("50.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")
    assert db_session.query(Invoice).count() == 0


def test_apply_instant_plan_change_emits_single_upgrade_event(
    db_session, subscriber, monkeypatch
):
    from app.services.events.types import EventType

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    emitted: list = []
    _stub_plan_change_side_effects(monkeypatch, emitted)

    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("50.00"),
            currency="NGN",
            memo="Wallet top-up",
        )
    )
    db_session.commit()

    apply_instant_plan_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
    )

    assert emitted.count(EventType.subscription_upgraded) == 1


def test_no_provisioning_before_payment_coverage(db_session, subscriber, monkeypatch):
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    import app.services.subscription_changes as change_service

    apply_mock = Mock()
    monkeypatch.setattr(
        change_service.subscription_change_requests, "apply", apply_mock
    )

    result = apply_instant_plan_change(
        db_session,
        {
            "account_id": str(subscriber.id),
            "subscriber_id": str(subscriber.id),
        },
        str(subscription.id),
        str(target_offer.id),
    )

    assert result["success"] is False
    apply_mock.assert_not_called()


def test_change_plan_page_does_not_price_catalog_upfront(
    db_session, subscriber, monkeypatch
):
    """The page must not compute a proration quote per offer (it timed out)."""
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session, name="Unlimited Basic", amount=Decimal("100.00"), plan_family="unlimited"
    )
    _make_offer(
        db_session, name="Unlimited Plus", amount=Decimal("200.00"), plan_family="unlimited"
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    calls: list = []
    monkeypatch.setattr(
        flow, "_build_plan_change_quote", lambda *a, **k: calls.append(1)
    )
    page = flow.get_change_plan_page(
        db_session, {"account_id": str(subscriber.id)}, str(subscription.id)
    )

    assert page is not None
    assert page["available_offer_change_quotes"] == {}
    assert calls == []  # no upfront per-offer quote computation


def test_get_plan_change_quote_returns_single_quote(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session, name="Unlimited Basic", amount=Decimal("100.00"), plan_family="unlimited"
    )
    target_offer = _make_offer(
        db_session, name="Unlimited Plus", amount=Decimal("200.00"), plan_family="unlimited"
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    _freeze_subscription_now(monkeypatch, datetime(2026, 5, 16, 12, 0, tzinfo=UTC))

    quote = flow.get_plan_change_quote(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
    )

    assert quote is not None and quote != {}
    assert "required_amount" in quote


def test_get_plan_change_quote_enforces_ownership(db_session, subscriber):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session, name="Unlimited Basic", amount=Decimal("100.00"), plan_family="unlimited"
    )
    target_offer = _make_offer(
        db_session, name="Unlimited Plus", amount=Decimal("200.00"), plan_family="unlimited"
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    # A different account must not get a quote for this subscription.
    quote = flow.get_plan_change_quote(
        db_session,
        {"account_id": str(uuid4())},
        str(subscription.id),
        str(target_offer.id),
    )
    assert quote is None
