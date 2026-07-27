"""Sale -> Money shadow phase: read the position, report drift, write nothing.

The point of these tests is that the shadow phase is *honest* — it must not
manufacture drift out of legitimate states (waivers, unlinked drafts) and must
not repair anything, in either reconciler mode.
"""

import uuid
from decimal import Decimal

import pytest

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.sales import SalesOrderPaymentStatus
from app.models.subscriber import Subscriber
from app.schemas.sales_order import SalesOrderCreate, SalesOrderLineCreate
from app.services import sales_billing_position as position_service
from app.services import sales_orders as sales_order_service


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Tunde",
        last_name="Bello",
        email=f"tunde-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _order(db, *, total="40000.00"):
    subscriber = _make_subscriber(db)
    order = sales_order_service.sales_orders.create(
        db, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal(total),
        ),
    )
    db.refresh(order)
    return order, line


def _invoice(db, subscriber_id, *, total, balance, status=InvoiceStatus.issued):
    invoice = Invoice(
        account_id=subscriber_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:10]}",
        currency="NGN",
        total=Decimal(total),
        balance_due=Decimal(balance),
        status=status,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def _link_installation_invoice(db, order, invoice):
    project = order.project
    assert project is not None
    metadata = dict(project.metadata_ or {})
    metadata["selfcare_installation_invoice_id"] = str(invoice.id)
    project.metadata_ = metadata
    db.add(project)
    db.commit()


def _payment(db, subscriber_id, order_id, amount):
    payment = Payment(
        account_id=subscriber_id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=f"crm:sales_order:{order_id}:payment",
    )
    db.add(payment)
    db.commit()
    return payment


# ---------------------------------------------------------------------------
# Reading the position
# ---------------------------------------------------------------------------


def test_settled_order_reads_as_paid_from_the_ledger(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session,
        order.subscriber_id,
        total="40000.00",
        balance="0.00",
        status=InvoiceStatus.paid,
    )
    _link_installation_invoice(db_session, order, invoice)
    _payment(db_session, order.subscriber_id, order.id, "40000.00")
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)

    assert position.invoiced == Decimal("40000.00")
    assert position.settled == Decimal("40000.00")
    assert position.open_balance == Decimal("0.00")
    assert position.payment_status == SalesOrderPaymentStatus.paid.value
    assert position.invoice_ids == (invoice.id,)


def test_open_invoice_reads_as_pending(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_installation_invoice(db_session, order, invoice)
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)

    assert position.open_balance == Decimal("40000.00")
    assert position.payment_status == SalesOrderPaymentStatus.pending.value


def test_part_settled_invoice_reads_as_partial(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session,
        order.subscriber_id,
        total="40000.00",
        balance="15000.00",
        status=InvoiceStatus.partially_paid,
    )
    _link_installation_invoice(db_session, order, invoice)
    _payment(db_session, order.subscriber_id, order.id, "25000.00")
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)

    assert position.settled == Decimal("25000.00")
    assert position.payment_status == SalesOrderPaymentStatus.partial.value


def test_a_metadata_id_pointing_nowhere_is_reported_not_ignored(db_session):
    """An unsafe join is the argument for the structural phase — surface it."""
    order, _line = _order(db_session)
    missing = uuid.uuid4()
    project = order.project
    project.metadata_ = {
        **(project.metadata_ or {}),
        "selfcare_installation_invoice_id": str(missing),
    }
    db_session.add(project)
    db_session.commit()
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)

    assert position.invoice_ids == ()
    assert any("invoice_missing" in entry for entry in position.unresolved)


# ---------------------------------------------------------------------------
# Drift comparison — must not manufacture signal
# ---------------------------------------------------------------------------


def test_agreement_reports_no_drift(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session,
        order.subscriber_id,
        total="40000.00",
        balance="0.00",
        status=InvoiceStatus.paid,
    )
    _link_installation_invoice(db_session, order, invoice)
    _payment(db_session, order.subscriber_id, order.id, "40000.00")
    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)
    assert position_service.compare_with_stored(order, position) == []


def test_disagreement_is_reported_per_field(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_installation_invoice(db_session, order, invoice)
    # The stored column claims money the ledger has never seen.
    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)
    drifts = position_service.compare_with_stored(order, position)

    fields = {drift.field for drift in drifts}
    assert fields == {"amount_paid", "payment_status"}
    paid_drift = next(d for d in drifts if d.field == "amount_paid")
    assert paid_drift.stored == "40000.00"
    assert paid_drift.billing == "0.00"


def test_a_waived_order_is_never_reported_as_drift(db_session, monkeypatch):
    """A waiver is settled by decision; the ledger correctly shows nothing."""
    from types import SimpleNamespace

    from app.services import crm_api

    monkeypatch.setattr(
        crm_api, "create_subscription", lambda db, **kw: {"subscription": None}
    )
    monkeypatch.setattr(
        crm_api,
        "create_installation_invoice",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )

    order, _line = _order(db_session)
    order = sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Goodwill install",
    )

    position = position_service.resolve_billing_position(db_session, order)
    assert position_service.compare_with_stored(order, position) == []


def test_an_unlinked_order_is_not_counted_as_drift(db_session):
    """No billing artifacts at all is a join gap, not a disagreement."""
    order, _line = _order(db_session)
    db_session.refresh(order)

    position = position_service.resolve_billing_position(db_session, order)
    assert position.invoice_ids == ()
    assert position_service.compare_with_stored(order, position) == []

    report = position_service.scan_billing_shadow(db_session)
    assert report.unlinked >= 1
    assert report.drifting == 0


# ---------------------------------------------------------------------------
# The scan writes nothing
# ---------------------------------------------------------------------------


def test_the_shadow_scan_does_not_touch_stored_columns(db_session):
    order, _line = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_installation_invoice(db_session, order, invoice)
    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()

    report = position_service.scan_billing_shadow(db_session)
    assert report.drifting == 1

    db_session.expire_all()
    db_session.refresh(order)
    assert order.amount_paid == Decimal("40000.00")
    assert order.payment_status == SalesOrderPaymentStatus.paid.value


@pytest.mark.parametrize("apply", [False, True])
def test_reconciler_reports_shadow_drift_without_repairing_it(db_session, apply):
    """Detect-only in BOTH modes: money repair needs finance, not a sweep."""
    from app.services import sales_lifecycle_reconciliation as reconciler

    order, _line = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_installation_invoice(db_session, order, invoice)
    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()

    result = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=apply)

    assert result["sales_orders_drifting_from_billing"] == 1
    assert result["sales_orders_scanned"] >= 1

    db_session.expire_all()
    db_session.refresh(order)
    assert order.amount_paid == Decimal("40000.00")
