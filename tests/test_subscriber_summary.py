"""Unit tests for the shared subscriber_summary projection owner.

The composed reads are monkeypatched so the test pins THIS module's mapping
(field names, tone extraction, plan/balance/connection assembly, graceful
None handling) rather than the behaviour of the underlying services.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.models.catalog import SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services import subscriber_summary as ss


def _wire(monkeypatch, *, subscriber, subscriptions=(), position=None, sessions=None):
    monkeypatch.setattr(
        ss.subscriber_service.subscribers, "get", lambda **_: subscriber
    )
    monkeypatch.setattr(
        ss.catalog_service.subscriptions, "list", lambda **_: list(subscriptions)
    )
    monkeypatch.setattr(
        ss, "get_customer_financial_position", lambda _db, _aid: position
    )
    monkeypatch.setattr(
        ss,
        "latest_open_accounting_sessions_by_subscription",
        lambda _db, _ids: dict(sessions or {}),
    )


def _subscriber(**over):
    base = dict(
        id=uuid4(),
        status=SubscriberStatus.active,
        is_active=True,
        is_business=False,
        name="Amaka Okafor",
        display_name="Amaka Okafor",
        company_name=None,
        first_name="Amaka",
        last_name="Okafor",
        email="amaka@example.com",
        phone="+2348035550147",
        account_number="ACC-1",
        subscriber_number="SUB-40122",
        account_start_date=None,
        created_at=datetime(2024, 3, 14, 9, 0),
        address_line1="12 Garki Close",
        city="Abuja",
        region="FCT",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_summary_maps_identity_plan_balance_and_connection(monkeypatch):
    sub = _subscriber()
    sub_id = uuid4()
    subscription = SimpleNamespace(
        id=sub_id,
        status=SubscriptionStatus.active,
        offer=SimpleNamespace(name="Fibre 100"),
        unit_price=Decimal("18500"),
        next_billing_at=None,
    )
    position = SimpleNamespace(
        open_invoice_balance=Decimal("0"),
        overdue_debt_balance=Decimal("0"),
        overdue_invoice_count=0,
        prepaid_available_balance=Decimal("0"),
        currency="NGN",
        days_overdue=0,
    )
    session = SimpleNamespace(
        last_update_at=datetime(2026, 7, 19, 6, 0),
        framed_ip_address="102.89.34.117",
    )
    _wire(
        monkeypatch,
        subscriber=sub,
        subscriptions=[subscription],
        position=position,
        sessions={sub_id: session},
    )

    summary = ss.subscriber_summary(object(), str(sub.id))

    assert summary is not None
    assert summary["id"] == str(sub.id)
    assert summary["url"] == f"/admin/customers/person/{sub.id}"
    assert summary["name"] == "Amaka Okafor"
    assert summary["account_number"] == "ACC-1"
    # status tone comes from the server-owned presentation, not the template
    assert summary["status"]["label"]
    assert summary["status"]["tone"]
    assert summary["plan"]["name"] == "Fibre 100"
    assert summary["plan"]["price"] == Decimal("18500")
    assert summary["active_plan_count"] == 1
    assert summary["balance"]["currency"] == "NGN"
    assert summary["connection"]["online"] is True
    assert summary["connection"]["ip"] == "102.89.34.117"
    assert summary["address"]["city"] == "Abuja"


def test_business_subscriber_gets_business_url(monkeypatch):
    sub = _subscriber(is_business=True, company_name="Dotmac Ltd", name="Dotmac Ltd")
    _wire(monkeypatch, subscriber=sub, subscriptions=[], position=None, sessions={})

    summary = ss.subscriber_summary(object(), str(sub.id))

    assert summary["is_business"] is True
    assert summary["url"] == f"/admin/customers/business/{sub.id}"
    assert summary["plan"] is None
    assert summary["connection"] is None  # no active subscriptions -> no lookup


def test_missing_or_unknown_subscriber_returns_none(monkeypatch):
    assert ss.subscriber_summary(object(), None) is None
    monkeypatch.setattr(ss.subscriber_service.subscribers, "get", lambda **_: None)
    assert ss.subscriber_summary(object(), str(uuid4())) is None
