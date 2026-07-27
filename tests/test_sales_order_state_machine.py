"""The SalesOrder lifecycle is a declared machine, not scattered conditionals.

Billing has carried ``ALLOWED_INVOICE_TRANSITIONS`` all along; sales carried
its edges as loose ``if`` guards across several functions, which is how a waived
order came to be permanently stranded at ``confirmed`` — nobody could see the
whole machine at once.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.sales import SalesOrderPaymentStatus, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.schemas.sales_order import SalesOrderCreate, SalesOrderUpdate
from app.services import crm_api
from app.services import sales_orders as sales_order_service


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Amaka",
        last_name="Okoro",
        email=f"amaka-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _order(db, *, status=None):
    order = sales_order_service.sales_orders.create(
        db, SalesOrderCreate(subscriber_id=_make_subscriber(db).id)
    )
    if status is not None:
        order.status = status
        db.commit()
        db.refresh(order)
    return order


@pytest.fixture()
def quiet_billing(monkeypatch):
    monkeypatch.setattr(
        crm_api, "create_subscription", lambda db, **kw: {"subscription": None}
    )
    monkeypatch.setattr(
        crm_api,
        "record_external_payment",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )
    monkeypatch.setattr(
        crm_api,
        "create_installation_invoice",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_terminal_states_have_no_way_out():
    table = sales_order_service.ALLOWED_SALES_ORDER_TRANSITIONS
    assert table[SalesOrderStatus.fulfilled.value] == frozenset()
    assert table[SalesOrderStatus.cancelled.value] == frozenset()


def test_every_status_is_declared():
    """A status missing from the table would silently permit nothing."""
    table = sales_order_service.ALLOWED_SALES_ORDER_TRANSITIONS
    assert set(table) == {status.value for status in SalesOrderStatus}


def test_a_same_state_transition_is_a_no_op():
    sales_order_service.assert_legal_sales_order_transition("paid", "paid")


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("draft", "confirmed"),
        ("draft", "paid"),
        ("confirmed", "paid"),
        ("confirmed", "fulfilled"),  # the waived path
        ("paid", "fulfilled"),
    ],
)
def test_legal_edges_are_permitted(from_status, to_status):
    sales_order_service.assert_legal_sales_order_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("paid", "draft"),
        ("paid", "confirmed"),
        ("fulfilled", "paid"),
        ("fulfilled", "draft"),
        ("cancelled", "confirmed"),
        ("confirmed", "draft"),
    ],
)
def test_backwards_and_terminal_edges_are_refused(from_status, to_status):
    with pytest.raises(HTTPException) as exc:
        sales_order_service.assert_legal_sales_order_transition(from_status, to_status)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Cancellation has no owner, so a status write may not perform it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("from_status", ["paid", "fulfilled"])
def test_a_committed_order_cannot_be_cancelled_by_status(from_status):
    with pytest.raises(HTTPException) as exc:
        sales_order_service.assert_legal_sales_order_transition(
            from_status, SalesOrderStatus.cancelled.value
        )
    assert exc.value.status_code == 409
    assert "refund obligation" in exc.value.detail


@pytest.mark.parametrize("from_status", ["draft", "confirmed"])
def test_an_uncommitted_order_may_still_be_cancelled(from_status):
    sales_order_service.assert_legal_sales_order_transition(
        from_status, SalesOrderStatus.cancelled.value
    )


# ---------------------------------------------------------------------------
# Enforced at the boundary
# ---------------------------------------------------------------------------


def test_update_refuses_an_illegal_status(db_session, quiet_billing):
    order = _order(db_session, status=SalesOrderStatus.fulfilled.value)

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(status=SalesOrderStatus.draft),
        )
    assert exc.value.status_code == 409
    db_session.refresh(order)
    assert order.status == SalesOrderStatus.fulfilled.value


def test_update_permits_a_legal_status(db_session, quiet_billing):
    order = _order(db_session)
    assert order.status == SalesOrderStatus.draft.value

    order = sales_order_service.sales_orders.update(
        db_session, str(order.id), SalesOrderUpdate(status=SalesOrderStatus.confirmed)
    )
    assert order.status == SalesOrderStatus.confirmed.value


def test_a_full_payment_still_promotes_a_draft_order(db_session, quiet_billing):
    """The derived promotion is a legal edge, not an exception to the table."""
    order = _order(db_session)

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(total=Decimal("500.00"), amount_paid=Decimal("500.00")),
    )
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    assert order.status == SalesOrderStatus.paid.value


def test_fulfilment_asserts_its_own_edge(db_session, quiet_billing):
    """CX acceptance goes through the same table as everything else."""
    order = _order(db_session)
    order.payment_status = SalesOrderPaymentStatus.paid.value
    order.status = SalesOrderStatus.fulfilled.value
    db_session.commit()

    with pytest.raises(sales_order_service.SalesOrderLifecycleError):
        # Already fulfilled by different evidence — refused before the machine
        # is consulted, so the two guards agree rather than conflict.
        sales_order_service.fulfill_from_customer_experience(
            db_session,
            sales_order_id=order.id,
            handoff_id=uuid.uuid4(),
            actor_id="staff:cx",
        )
