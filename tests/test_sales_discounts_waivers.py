"""Discounts and waivers on sales orders.

Two things a sale needs beyond "the customer paid the full price": a reduced
price, and giving the work away. They are different — a discount reduces what
is owed and the customer still pays the remainder through the normal funding
path; a waiver says nothing is owed at all and is what authorizes provisioning
in place of a payment.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus
from app.models.provisioning import ServiceOrder
from app.models.sales import SalesOrderPaymentStatus, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.schemas.sales import QuoteCreate, QuoteLineItemCreate
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderLineUpdate,
)
from app.services import crm_api
from app.services import sales as sales_service
from app.services import sales_orders as sales_order_service
from app.services.common import net_line_amount


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ngozi",
        last_name="Eze",
        email=f"ngozi-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


@pytest.fixture()
def billing_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_create_subscription(db, **kwargs):
        calls.append(("create_subscription", kwargs))
        return {
            "subscription": SimpleNamespace(id=uuid.uuid4()),
            "invoice": SimpleNamespace(id=uuid.uuid4()),
            "created": True,
        }

    def fake_record_external_payment(db, **kwargs):
        calls.append(("record_external_payment", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crm_api, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(
        crm_api, "record_external_payment", fake_record_external_payment
    )
    return calls


# ---------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------


def test_net_line_amount_applies_and_clamps_the_discount():
    assert net_line_amount(2, "100.00", "10") == Decimal("180.00")
    assert net_line_amount(1, "100.00", "0") == Decimal("100.00")
    assert net_line_amount(1, "100.00", "100") == Decimal("0.00")
    # Out-of-range input is clamped rather than producing negative money.
    assert net_line_amount(1, "100.00", "150") == Decimal("0.00")
    assert net_line_amount(1, "100.00", "-20") == Decimal("100.00")


def test_order_line_amount_is_net_of_its_discount(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal("100000.00"),
            discount_percent=Decimal("20.00"),
        ),
    )
    assert line.amount == Decimal("80000.00")
    db_session.refresh(order)
    assert order.total == Decimal("80000.00")


def test_editing_a_line_does_not_restore_the_gross_price(db_session, billing_calls):
    """The regression: any line edit used to recompute amount as qty x price."""
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal("100000.00"),
            discount_percent=Decimal("20.00"),
        ),
    )

    line = sales_order_service.sales_order_lines.update(
        db_session, str(line.id), SalesOrderLineUpdate(quantity=Decimal("2"))
    )

    assert line.discount_percent == Decimal("20.00")
    assert line.amount == Decimal("160000.00")  # not 200000.00
    db_session.refresh(order)
    assert order.total == Decimal("160000.00")


def test_changing_only_the_discount_reprices_the_line(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal("50000.00"),
        ),
    )
    assert line.amount == Decimal("50000.00")

    line = sales_order_service.sales_order_lines.update(
        db_session,
        str(line.id),
        SalesOrderLineUpdate(discount_percent=Decimal("50.00")),
    )
    assert line.amount == Decimal("25000.00")


def test_quote_discount_survives_conversion_to_a_sales_order(db_session):
    subscriber = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Discounted install",
            quantity=Decimal("1"),
            unit_price=Decimal("100000.00"),
            discount_percent=Decimal("25.00"),
        ),
    )

    order = sales_order_service.sales_orders.create_from_quote(
        db_session, str(quote.id)
    )

    line = order.lines[0]
    assert line.discount_percent == Decimal("25.00")
    assert line.amount == Decimal("75000.00")


# ---------------------------------------------------------------------------
# Waivers
# ---------------------------------------------------------------------------


def _order_with_service_line(db, *, total="30000.00"):
    subscriber = _make_subscriber(db)
    order = sales_order_service.sales_orders.create(
        db, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Monthly plan",
            quantity=Decimal("1"),
            unit_price=Decimal(total),
            metadata_={"sub_offer_id": str(uuid.uuid4())},
        ),
    )
    db.refresh(order)
    return order, line


def test_waiver_requires_actor_and_reason(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.record_waiver(
            db_session, sales_order_id=order.id, actor_id="", reason="why"
        )
    assert exc.value.code == "actor_required"

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.record_waiver(
            db_session, sales_order_id=order.id, actor_id="staff:a", reason="  "
        )
    assert exc.value.code == "reason_required"


def test_waiver_records_its_evidence(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)

    order = sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Goodwill after a failed install",
    )

    waiver = (order.metadata_ or {})["waiver"]
    assert waiver["waived_by"] == "staff:folake"
    assert waiver["reason"] == "Goodwill after a failed install"
    assert waiver["waived_total"] == "30000.00"
    assert order.payment_status == SalesOrderPaymentStatus.waived.value
    assert order.balance_due == Decimal("0.00")


def test_waived_order_provisions(db_session, billing_calls):
    """The dead end: a waived order used to never create service at all."""
    order, line = _order_with_service_line(db_session)

    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Staff account",
    )

    assert [name for name, _ in billing_calls] == ["create_subscription"]
    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id")


def test_waiver_posts_no_money_to_the_ledger(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)

    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Promotional install",
    )

    assert "record_external_payment" not in [name for name, _ in billing_calls]


def test_waived_order_gets_a_settled_zero_invoice(db_session, billing_calls):
    """An accounting document that can never age into collections."""
    order, _line = _order_with_service_line(db_session)

    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Promotional install",
    )

    invoices = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == order.subscriber_id)
        .all()
    )
    assert len(invoices) == 1
    assert invoices[0].total == Decimal("0.00")
    assert invoices[0].status == InvoiceStatus.paid


def test_waiver_replay_is_idempotent(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    kwargs = dict(
        sales_order_id=order.id, actor_id="staff:folake", reason="Staff account"
    )

    first = sales_order_service.record_waiver(db_session, **kwargs)
    original = dict(first.metadata_ or {})["waiver"]
    second = sales_order_service.record_waiver(db_session, **kwargs)

    assert dict(second.metadata_ or {})["waiver"] == original
    assert [name for name, _ in billing_calls].count("create_subscription") == 1


def test_an_order_with_money_on_it_cannot_be_waived(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("5000.00"),
        reference="psk_1",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.record_waiver(
            db_session,
            sales_order_id=order.id,
            actor_id="staff:folake",
            reason="Changed our mind",
        )
    assert exc.value.code == "sales_order_already_funded"


def test_waived_order_can_be_fulfilled(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Staff account",
    )

    changed = sales_order_service.fulfill_from_customer_experience(
        db_session,
        sales_order_id=order.id,
        handoff_id=uuid.uuid4(),
        actor_id="staff:cx",
    )
    db_session.commit()

    assert changed is True
    db_session.refresh(order)
    assert order.status == SalesOrderStatus.fulfilled.value


def test_unfunded_order_still_cannot_be_fulfilled(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.fulfill_from_customer_experience(
            db_session,
            sales_order_id=order.id,
            handoff_id=uuid.uuid4(),
            actor_id="staff:cx",
        )
    assert exc.value.code == "sales_order_not_settled"


def test_waived_order_is_not_reported_as_funding_drift(db_session, billing_calls):
    from app.services import sales_lifecycle_reconciliation as reconciler

    order, _line = _order_with_service_line(db_session)
    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Staff account",
    )

    report = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=False)
    assert report["funded_orders_missing_subscription"] == 0


def test_waiving_a_cancelled_order_is_refused(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    order.status = SalesOrderStatus.cancelled.value
    db_session.commit()

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.record_waiver(
            db_session,
            sales_order_id=order.id,
            actor_id="staff:folake",
            reason="Cancelled anyway",
        )
    assert exc.value.code == "sales_order_canceled"


def test_generic_update_refuses_to_waive(db_session, billing_calls):
    from app.schemas.sales_order import SalesOrderUpdate

    order, _line = _order_with_service_line(db_session)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(payment_status=SalesOrderPaymentStatus.waived),
        )
    assert exc.value.status_code == 409
    db_session.refresh(order)
    assert order.payment_status == SalesOrderPaymentStatus.pending.value


def test_waived_order_still_stages_its_provisioning_order(db_session, catalog_offer):
    """Gate 4's second half: a ServiceOrder, staged draft, for a waived sale."""
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.common import coerce_uuid

    def fake_create_subscription(db, **kwargs):
        subscription = Subscription(
            subscriber_id=coerce_uuid(str(kwargs["subscriber_id"])),
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
        )
        db.add(subscription)
        db.flush()
        return {"subscription": subscription, "invoice": None, "created": True}

    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber service",
            quantity=Decimal("1"),
            unit_price=Decimal("30000.00"),
            metadata_={"sub_offer_id": str(catalog_offer.id)},
        ),
    )

    original = crm_api.create_subscription
    crm_api.create_subscription = fake_create_subscription
    try:
        sales_order_service.record_waiver(
            db_session,
            sales_order_id=order.id,
            actor_id="staff:folake",
            reason="Staff account",
        )
    finally:
        crm_api.create_subscription = original

    assert (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .count()
        == 1
    )
