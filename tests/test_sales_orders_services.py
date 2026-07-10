"""Sales-order service tests (Phase 3 port), including the §2.3 native
financial-side-effect rewiring and the crm#233 account_id fix."""

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.project import Project
from app.models.sales import (
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.sequence import DocumentSequence
from app.models.subscriber import Subscriber
from app.schemas.sales import QuoteCreate, QuoteLineItemCreate
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderUpdate,
)
from app.services import crm_api
from app.services import sales as sales_service
from app.services import sales_orders as sales_order_service


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Bola",
        last_name="Ade",
        email=f"bola-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


@pytest.fixture()
def billing_calls(monkeypatch):
    """Record the §2.3 in-process billing calls instead of hitting the real
    invoice/subscription/payment services."""
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

    def fake_create_installation_invoice(db, **kwargs):
        calls.append(("create_installation_invoice", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crm_api, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(
        crm_api, "record_external_payment", fake_record_external_payment
    )
    monkeypatch.setattr(
        crm_api, "create_installation_invoice", fake_create_installation_invoice
    )
    return calls


# ---------------------------------------------------------------------------
# Numbering (SO-%06d via document_sequences)
# ---------------------------------------------------------------------------


def test_order_number_continues_document_sequence(db_session):
    # The backfill imports the CRM row's next_value under the same key; the
    # generator must continue that sequence (§1.5, risk #10).
    db_session.add(DocumentSequence(key="sales_order_number", next_value=1234))
    db_session.commit()

    subscriber = _make_subscriber(db_session)
    first = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    second = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    assert first.order_number == "SO-001234"
    assert second.order_number == "SO-001235"


# ---------------------------------------------------------------------------
# The crm#233 fix — fixed list shape, no account_id slot to mis-plumb
# ---------------------------------------------------------------------------


def test_list_signature_has_no_account_id_slot():
    params = inspect.signature(sales_order_service.SalesOrders.list).parameters
    assert "account_id" not in params
    assert "subscriber_id" in params
    assert "quote_id" in params


def test_list_filters_by_subscriber_quote_and_status(db_session):
    subscriber = _make_subscriber(db_session)
    other = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    with_quote = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, quote_id=quote.id),
    )
    sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=other.id)
    )

    by_subscriber = sales_order_service.sales_orders.list(
        db_session, subscriber_id=str(subscriber.id)
    )
    assert [so.id for so in by_subscriber] == [with_quote.id]

    by_quote = sales_order_service.sales_orders.list(db_session, quote_id=str(quote.id))
    assert [so.id for so in by_quote] == [with_quote.id]

    by_status = sales_order_service.sales_orders.list(
        db_session, status=SalesOrderStatus.draft.value
    )
    assert {so.id for so in by_status} >= {with_quote.id}


def test_second_sales_order_for_quote_rejected(db_session):
    subscriber = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id, quote_id=quote.id)
    )
    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.create(
            db_session,
            SalesOrderCreate(subscriber_id=subscriber.id, quote_id=quote.id),
        )
    assert exc.value.status_code == 400


def test_create_from_quote_is_idempotent(db_session):
    subscriber = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    first = sales_order_service.sales_orders.create_from_quote(
        db_session, str(quote.id)
    )
    second = sales_order_service.sales_orders.create_from_quote(
        db_session, str(quote.id)
    )
    assert first.id == second.id


# ---------------------------------------------------------------------------
# Payment-field state machine
# ---------------------------------------------------------------------------


def test_payment_field_transitions(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, total=Decimal("100.00")),
    )
    assert order.payment_status == SalesOrderPaymentStatus.pending.value
    assert order.balance_due == Decimal("100.00")

    order = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(amount_paid=Decimal("40.00"))
    )
    assert order.payment_status == SalesOrderPaymentStatus.partial.value
    assert order.balance_due == Decimal("60.00")

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    assert order.amount_paid == Decimal("100.00")
    assert order.balance_due == Decimal("0.00")
    assert order.status == SalesOrderStatus.paid.value
    assert order.paid_at is not None


def test_waived_payment_confirms_draft_order(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    assert order.status == SalesOrderStatus.draft.value
    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(payment_status=SalesOrderPaymentStatus.waived),
    )
    assert order.payment_status == SalesOrderPaymentStatus.waived.value
    assert order.status == SalesOrderStatus.confirmed.value


def test_update_from_input_parses_strings(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    updated = sales_order_service.sales_orders.update_from_input(
        db_session,
        str(order.id),
        payment_status="paid",
        total="250.00",
        notes="  settled in cash  ",
    )
    assert updated.total == Decimal("250.00")
    assert updated.payment_status == SalesOrderPaymentStatus.paid.value
    assert updated.notes == "settled in cash"


# ---------------------------------------------------------------------------
# §2.3 — native financial side-effects
# ---------------------------------------------------------------------------


def test_paid_order_pushes_subscription_then_payment(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    offer_id = str(uuid.uuid4())
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Monthly plan",
            quantity=Decimal("1"),
            unit_price=Decimal("25000.00"),
            metadata_={"sub_offer_id": offer_id},
        ),
    )
    assert billing_calls == []  # pending order: no financial side-effects yet

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )

    names = [name for name, _ in billing_calls]
    # Subscription (plus its first invoice) BEFORE the payment, so a single
    # account-level payment settles everything (§2.3).
    assert names == ["create_subscription", "record_external_payment"]

    sub_kwargs = billing_calls[0][1]
    assert sub_kwargs["subscriber_id"] == str(subscriber.id)
    assert sub_kwargs["offer_ref"] == offer_id
    # The idempotency keys are byte-identical to the HTTP era.
    assert sub_kwargs["external_ref"] == (
        f"sales_order:{order.id}:subscription:{line.id}"
    )

    pay_kwargs = billing_calls[1][1]
    assert pay_kwargs["subscriber_id"] == str(subscriber.id)
    assert pay_kwargs["external_ref"] == f"sales_order:{order.id}:payment"
    assert Decimal(str(pay_kwargs["amount"])) == order.amount_paid

    # The resolved ids are written back onto the line metadata (§1.5 keys).
    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id")
    assert (line.metadata_ or {}).get("selfcare_subscription_invoice_id")

    # Re-running the sync skips the already-tagged line but re-records the
    # payment (idempotent server-side on external_ref).
    billing_calls.clear()
    sales_order_service._sync_sales_order_financials(db_session, order)
    names = [name for name, _ in billing_calls]
    assert "create_subscription" not in names
    assert names == ["record_external_payment"]


def test_lines_without_offer_tags_push_no_subscription(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Installation fee",
            quantity=Decimal("1"),
            unit_price=Decimal("50000.00"),
        ),
    )
    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid,
            paid_at=datetime.now(UTC),
        ),
    )
    names = [name for name, _ in billing_calls]
    assert "create_subscription" not in names
    assert "record_external_payment" in names


def test_installation_invoice_created_once_for_project(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    # A sales-order line create triggers the invoice hook, but without a
    # project row there is nothing to invoice against yet.
    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Fiber installation",
            quantity=Decimal("1"),
            unit_price=Decimal("80000.00"),
        ),
    )
    assert billing_calls == []

    project = Project(
        name="Fiber Optics Installation - Bola Ade",
        subscriber_id=subscriber.id,
        metadata_={"sales_order_id": str(order.id)},
    )
    db_session.add(project)
    db_session.commit()

    sales_order_service.ensure_installation_invoice_for_sales_order(
        db_session, order.id
    )
    assert len(billing_calls) == 1
    name, kwargs = billing_calls[0]
    assert name == "create_installation_invoice"
    assert kwargs["subscriber_id"] == str(subscriber.id)
    assert kwargs["amount"] == Decimal("80000.00")
    # The idempotency key keeps its HTTP-era shape (§2.3).
    assert kwargs["external_ref"] == f"project:{project.id}"

    db_session.refresh(project)
    metadata = project.metadata_ or {}
    assert metadata.get("selfcare_installation_invoice_id")
    assert metadata.get("selfcare_installation_invoice_amount") == "80000.00"

    # Second trigger is a no-op — the stored metadata dedups it.
    billing_calls.clear()
    sales_order_service.ensure_installation_invoice_for_sales_order(
        db_session, order.id
    )
    assert billing_calls == []


def test_installation_amount_falls_back_to_quote_lines(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    quote = sales_service.quotes.create(
        db_session, QuoteCreate(subscriber_id=subscriber.id)
    )
    sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation (survey inclusive)",
            quantity=Decimal("1"),
            unit_price=Decimal("60000.00"),
        ),
    )
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    project = Project(
        name="Install",
        subscriber_id=subscriber.id,
        metadata_={
            "sales_order_id": str(order.id),
            "quote_id": str(quote.id),
        },
    )
    db_session.add(project)
    db_session.commit()

    billing_calls.clear()
    sales_order_service.ensure_installation_invoice_for_sales_order(
        db_session, order.id
    )
    assert len(billing_calls) == 1
    assert billing_calls[0][1]["amount"] == Decimal("60000.00")
