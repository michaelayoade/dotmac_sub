from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
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
    OfferStatus,
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
    status: OfferStatus = OfferStatus.active,
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
        status=status,
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


def test_change_plan_page_separates_migration_offers(db_session, subscriber):
    from app.services import customer_portal_flow_changes as flow

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    instant_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    migration_offer = _make_offer(
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

    page = flow.get_change_plan_page(
        db_session,
        {"account_id": str(subscriber.id)},
        str(subscription.id),
    )

    assert page is not None
    assert {str(offer.id) for offer in page["available_offers"]} == {
        str(current_offer.id),
        str(instant_offer.id),
    }
    assert {str(offer.id) for offer in page["migration_offers"]} == {
        str(migration_offer.id),
    }
    assert str(migration_offer.id) in page["migration_offer_summaries"]


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


def test_request_plan_migration_includes_requested_offer(
    db_session, subscriber, monkeypatch
):
    from app.services import crm_portal
    from app.services import customer_portal_flow_changes as flow

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

    captured: dict[str, str] = {}

    def fake_ticket_create(
        db, customer, subscriber_lookup, title, description, priority
    ):
        captured["subscriber_lookup"] = subscriber_lookup
        captured["title"] = title
        captured["description"] = description
        captured["priority"] = priority
        return {"success": True, "ticket": {"id": "ticket-123"}}

    monkeypatch.setattr(crm_portal, "handle_ticket_create", fake_ticket_create)

    result = flow.request_plan_migration(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        target_family="dedicated",
        requested_offer_id=str(target_offer.id),
        notes="Please move me.",
    )

    assert result["ticket"]["id"] == "ticket-123"
    assert captured["title"] == "Request Plan Migration"
    assert captured["priority"] == "normal"
    assert f"Subscription: {subscription.id}" in captured["description"]
    assert "Current offer: Unlimited Basic" in captured["description"]
    assert "Requested family: dedicated" in captured["description"]
    assert "Requested offer: Dedicated 1" in captured["description"]
    assert "Customer notes: Please move me." in captured["description"]


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
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
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


def test_available_portal_offers_eager_loads_prices(db_session, subscriber):
    """Prices must be eager-loaded so the per-offer price summary is not an N+1."""
    from sqlalchemy import inspect as sa_inspect

    from app.services.customer_portal_context import get_available_portal_offers

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    db_session.expire_all()  # force a fresh load so the selectinload is exercised
    offers = get_available_portal_offers(db_session, subscription)

    assert offers
    assert all("prices" not in sa_inspect(o).unloaded for o in offers)


# ---------------------------------------------------------------------------
# Reseller availability scoping
# ---------------------------------------------------------------------------


def _reseller(db_session, name):
    from app.models.subscriber import Reseller

    reseller = Reseller(name=name)
    db_session.add(reseller)
    db_session.commit()
    return reseller


def _restrict_to(db_session, offer, reseller):
    from app.models.offer_availability import OfferResellerAvailability

    db_session.add(
        OfferResellerAvailability(offer_id=offer.id, reseller_id=reseller.id)
    )
    db_session.commit()


def test_reseller_restricted_offer_hidden_from_other_resellers_customer(
    db_session, subscriber
):
    reseller_a = _reseller(db_session, "Partner A")
    reseller_b = _reseller(db_session, "Partner B")
    subscriber.reseller_id = reseller_b.id
    db_session.commit()

    current = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    ids = {str(o.id) for o in offers}
    assert str(restricted.id) not in ids
    assert str(current.id) in ids


def test_reseller_restricted_offer_visible_to_member(db_session, subscriber):
    reseller_a = _reseller(db_session, "Partner A2")
    subscriber.reseller_id = reseller_a.id
    db_session.commit()

    current = _make_offer(
        db_session,
        name="Unlimited Basic2",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner2",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert str(restricted.id) in {str(o.id) for o in offers}


def test_restricted_offer_hidden_without_subscriber_context(db_session):
    reseller_a = _reseller(db_session, "Partner A3")
    restricted = _make_offer(
        db_session,
        name="Unlimited Partner3",
        amount=Decimal("80"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, restricted, reseller_a)
    _make_offer(
        db_session,
        name="Unlimited Open3",
        amount=Decimal("90"),
        plan_family="unlimited",
    )

    offers = get_available_portal_offers(db_session)

    names = {o.name for o in offers}
    assert "Unlimited Partner3" not in names
    assert "Unlimited Open3" in names


def test_restrict_mode_reseller_sees_only_assigned_offers(db_session, subscriber):
    # C-2: a reseller flagged restrict_to_assigned_offers sees ONLY offers
    # assigned to it — unrestricted offers are hidden too.
    reseller = _reseller(db_session, "Restricted Partner")
    reseller.restrict_to_assigned_offers = True
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Only",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Open Unrestricted",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    offers = get_available_portal_offers(db_session, subscriber_id=subscriber.id)

    names = {o.name for o in offers}
    assert names == {"Assigned Only"}


def test_non_restrict_reseller_still_sees_unrestricted_offers(db_session, subscriber):
    # Flag explicitly False (open) behaves exactly like today.
    reseller = _reseller(db_session, "Open Partner")
    reseller.restrict_to_assigned_offers = False
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Open Partner",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unrestricted Open Partner",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    offers = get_available_portal_offers(db_session, subscriber_id=subscriber.id)

    names = {o.name for o in offers}
    assert "Assigned Open Partner" in names
    assert "Unrestricted Open Partner" in names


def test_global_default_flip_flows_through_when_flag_null(
    db_session, subscriber, monkeypatch
):
    # Per-reseller flag NULL (inherit): flipping the global default to
    # "not open" puts the reseller into restrict mode.
    import app.services.customer_portal_context as cpc

    reseller = _reseller(db_session, "Inherit Partner")
    assert reseller.restrict_to_assigned_offers is None
    subscriber.reseller_id = reseller.id
    db_session.commit()

    assigned = _make_offer(
        db_session,
        name="Assigned Inherit",
        amount=Decimal("100"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unrestricted Inherit",
        amount=Decimal("90"),
        plan_family="unlimited",
    )
    _restrict_to(db_session, assigned, reseller)

    # Global default open (today's behavior) → unrestricted offer visible.
    monkeypatch.setattr(cpc, "_reseller_default_catalog_open", lambda db: True)
    open_names = {
        o.name
        for o in get_available_portal_offers(db_session, subscriber_id=subscriber.id)
    }
    assert "Unrestricted Inherit" in open_names
    assert "Assigned Inherit" in open_names

    # Flip global default to restrict → only assigned offer visible.
    monkeypatch.setattr(cpc, "_reseller_default_catalog_open", lambda db: False)
    restricted_names = {
        o.name
        for o in get_available_portal_offers(db_session, subscriber_id=subscriber.id)
    }
    assert restricted_names == {"Assigned Inherit"}


def test_archived_status_offer_hidden_even_when_is_active_drifted(
    db_session, subscriber
):
    """status=archived must hide an offer from the portal even if is_active
    drifted to True (the edit form historically set only status)."""
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    _make_offer(
        db_session,
        name="Unlimited Retired",
        amount=Decimal("150.00"),
        plan_family="unlimited",
        status=OfferStatus.archived,
        is_active=True,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert {str(offer.id) for offer in offers} == {str(current_offer.id)}


def test_submit_change_plan_rejects_cross_family_at_create(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow
    from app.services import subscription_changes as change_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    cross_family_offer = _make_offer(
        db_session,
        name="Dedicated 1",
        amount=Decimal("300.00"),
        plan_family="dedicated",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    created = []
    monkeypatch.setattr(
        change_service.subscription_change_requests,
        "create",
        lambda **kwargs: created.append(kwargs),
    )
    customer = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    with pytest.raises(ValueError, match="not available for self-service"):
        flow.submit_change_plan(
            db_session,
            customer,
            str(subscription.id),
            str(cross_family_offer.id),
            "2099-01-01",
        )
    assert created == []


def test_submit_change_plan_accepts_same_family_offer(
    db_session, subscriber, monkeypatch
):
    from app.services import customer_portal_flow_changes as flow
    from app.services import subscription_changes as change_service

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("150.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    created = []
    monkeypatch.setattr(
        change_service.subscription_change_requests,
        "create",
        lambda **kwargs: created.append(kwargs),
    )
    customer = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    result = flow.submit_change_plan(
        db_session,
        customer,
        str(subscription.id),
        str(target_offer.id),
        "2099-01-01",
    )
    assert result == {"success": True}
    assert len(created) == 1
    assert created[0]["new_offer_id"] == str(target_offer.id)


def test_apply_instant_plan_change_rejects_archived_offer(
    db_session, subscriber, monkeypatch
):
    """The instant web path must reject an archived-but-is_active offer — it
    now gates through get_available_portal_offers (status==active), like the
    deferred path, instead of only checking is_active."""
    _stub_plan_change_side_effects(monkeypatch)
    now = datetime(2026, 3, 15, tzinfo=UTC)
    _freeze_subscription_now(monkeypatch, now)

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    # Archived target: still is_active=True, same family/service/billing, but
    # status=archived → must be refused.
    archived = _make_offer(
        db_session,
        name="Unlimited Legacy",
        amount=Decimal("50.00"),
        plan_family="unlimited",
        is_active=True,
        status=OfferStatus.archived,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=now + timedelta(days=20),
        start_at=now - timedelta(days=10),
    )

    with pytest.raises(ValueError, match="not available for self-service change"):
        apply_instant_plan_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(archived.id),
        )

    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id  # unchanged


def test_get_available_portal_offers_excludes_empty_family(db_session, subscriber):
    """Offers with no plan_family are not instant-change eligible.

    Regression: the change-plan page listed empty-family offers as "instant
    changes in your current plan family", but _validate_plan_change rejects any
    change where either family is empty, so applying 400'd. The instant list
    must require a non-empty, matching family on both sides.
    """
    current_offer = _make_offer(
        db_session, name="Unclassified A", amount=Decimal("100.00"), plan_family=None
    )
    _make_offer(
        db_session, name="Unclassified B", amount=Decimal("150.00"), plan_family=None
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    offers = get_available_portal_offers(db_session, subscription)

    assert offers == []


def test_plan_change_refreshes_unit_price(db_session, subscriber, monkeypatch):
    """Changing the offer refreshes subscription.unit_price to the new offer's
    recurring price (#10), so billing summaries don't keep showing the old
    plan's price after a change."""
    from app.schemas.catalog import SubscriptionUpdate
    from app.services import catalog as catalog_service

    _stub_plan_change_side_effects(monkeypatch)

    current_offer = _make_offer(
        db_session,
        name="Unlimited 100",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited 200",
        amount=Decimal("200.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    catalog_service.subscriptions.update(
        db_session,
        str(subscription.id),
        SubscriptionUpdate(offer_id=target_offer.id),
        skip_proration_artifacts=True,
    )
    db_session.refresh(subscription)
    assert subscription.unit_price == Decimal("200.00")


def _add_overdue_invoice(db_session, subscriber, amount: Decimal) -> None:
    from app.models.billing import Invoice, InvoiceStatus

    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.overdue,
            currency="NGN",
            subtotal=amount,
            total=amount,
            balance_due=amount,
            is_active=True,
        )
    )
    db_session.commit()


def test_plan_change_blocked_when_account_in_arrears(
    db_session, subscriber, monkeypatch
):
    """An account with an overdue balance cannot self-service change plans
    (policy: block-until-settled). Covers POSTPAID too — the old gate only
    looked at prepaid wallet credit and never considered debt (account
    100000016 could upgrade while owing)."""
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _add_overdue_invoice(db_session, subscriber, Decimal("5000.00"))

    with pytest.raises(ValueError, match="overdue balance"):
        apply_instant_plan_change(
            db_session,
            {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
            str(subscription.id),
            str(target_offer.id),
        )

    db_session.refresh(subscription)
    assert subscription.offer_id == current_offer.id  # unchanged


def test_postpaid_plan_change_applies_when_no_arrears(
    db_session, subscriber, monkeypatch
):
    """With no overdue balance, a postpaid change still auto-applies."""
    _stub_plan_change_side_effects(monkeypatch)
    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    target_offer = _make_offer(
        db_session,
        name="Unlimited Plus",
        amount=Decimal("200.00"),
        plan_family="unlimited",
        billing_mode=BillingMode.postpaid,
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    result = apply_instant_plan_change(
        db_session,
        {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)},
        str(subscription.id),
        str(target_offer.id),
    )
    db_session.refresh(subscription)
    assert result["success"] is True
    assert subscription.offer_id == target_offer.id


def test_change_plan_page_flags_arrears(db_session, subscriber):
    """The change-plan page context surfaces arrears so the UI can show a
    'settle first' notice and hide the plan picker (#30 follow-up)."""
    from app.services.customer_portal_flow_changes import get_change_plan_page

    current_offer = _make_offer(
        db_session,
        name="Unlimited Basic",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        current_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    cust = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}

    page = get_change_plan_page(db_session, cust, str(subscription.id))
    assert page["in_arrears"] is False
    assert page["arrears_amount"] == 0.0

    _add_overdue_invoice(db_session, subscriber, Decimal("5000.00"))
    page = get_change_plan_page(db_session, cust, str(subscription.id))
    assert page["in_arrears"] is True
    assert page["arrears_amount"] == 5000.0


def test_admin_change_plan_quote_response(db_session, subscriber):
    from app.services.web_catalog_subscription_workflows import (
        change_plan_quote_response,
    )

    now = datetime.now(UTC)
    source = _make_offer(
        db_session, name="Quote Src", amount=Decimal("20000.00"), plan_family="u"
    )
    target = _make_offer(
        db_session, name="Quote Tgt", amount=Decimal("35000.00"), plan_family="u"
    )
    sub = _make_subscription(
        db_session,
        subscriber,
        source,
        start_at=now - timedelta(days=10),
        next_billing_at=now + timedelta(days=20),
    )

    payload = change_plan_quote_response(
        db_session, subscription_id=str(sub.id), target_offer_id=str(target.id)
    )

    quote = payload["quote"]
    assert payload["target_offer_name"] == "Quote Tgt"
    assert quote["days_in_cycle"] > 0
    assert quote["days_remaining"] > 0
    assert quote["charge_amount"] > 0
    # Upgrading mid-cycle must cost something net.
    assert quote["net_amount"] > 0


def test_admin_change_plan_quote_unknown_offer_404(db_session, subscriber):
    from app.services.web_catalog_subscription_workflows import (
        change_plan_quote_response,
    )

    now = datetime.now(UTC)
    source = _make_offer(
        db_session, name="Quote Only", amount=Decimal("20000.00"), plan_family="u"
    )
    sub = _make_subscription(
        db_session,
        subscriber,
        source,
        start_at=now - timedelta(days=1),
        next_billing_at=now + timedelta(days=29),
    )

    with pytest.raises(HTTPException) as exc:
        change_plan_quote_response(
            db_session, subscription_id=str(sub.id), target_offer_id=str(uuid4())
        )
    assert exc.value.status_code == 404
