"""Sale → Money shadow phase.

Three things must hold or the phase is worthless as cutover evidence:
settlement is what was *applied*, not what the sale *originated*; every order
lands in exactly one bucket; and the check refuses to repair.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.sales import SalesOrderPaymentStatus, SalesOrderStatus
from app.models.sales_billing_shadow import (
    SalesBillingShadowBucket,
    SalesBillingShadowRun,
)
from app.models.subscriber import Subscriber
from app.schemas.sales_order import SalesOrderCreate, SalesOrderLineCreate
from app.services import crm_api
from app.services import sales_billing_position as shadow
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
    sales_order_service.sales_order_lines.create(
        db,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Install",
            quantity=Decimal("1"),
            unit_price=Decimal(total),
        ),
    )
    db.refresh(order)
    return order


def _invoice(db, account_id, *, total, balance, status=InvoiceStatus.issued):
    invoice = Invoice(
        account_id=account_id,
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


def _link_install_invoice(db, order, invoice_id):
    project = order.project
    assert project is not None
    project.metadata_ = {
        **(project.metadata_ or {}),
        "selfcare_installation_invoice_id": str(invoice_id),
    }
    db.add(project)
    db.commit()
    db.refresh(order)


def _payment(db, account_id, *, amount, external_id=None):
    payment = Payment(
        account_id=account_id,
        amount=Decimal(amount),
        currency="NGN",
        status=PaymentStatus.succeeded,
        external_id=external_id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def _allocate(db, payment, invoice, amount):
    db.add(
        PaymentAllocation(
            payment_id=payment.id, invoice_id=invoice.id, amount=Decimal(amount)
        )
    )
    db.commit()


def _waive(db, order, monkeypatch, *, reason="Goodwill"):
    monkeypatch.setattr(
        crm_api, "create_subscription", lambda db, **kw: {"subscription": None}
    )
    monkeypatch.setattr(
        crm_api,
        "create_installation_invoice",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )
    return sales_order_service.record_waiver(
        db, sales_order_id=order.id, actor_id="staff:folake", reason=reason
    )


# ---------------------------------------------------------------------------
# Settlement is application, not origin
# ---------------------------------------------------------------------------


def test_settlement_counts_allocation_not_payment_origin(db_session):
    """The bug this replaced: summing order-originated payments.

    An order-originated payment auto-allocates across the account's open
    invoices, so it may settle a completely different obligation. Crediting the
    sale with it would overstate the position.
    """
    order = _order(db_session)
    sale_invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_install_invoice(db_session, order, sale_invoice.id)

    other_invoice = _invoice(
        db_session, order.subscriber_id, total="9000.00", balance="0.00"
    )
    payment = _payment(
        db_session,
        order.subscriber_id,
        amount="9000.00",
        external_id=f"crm:sales_order:{order.id}:payment",
    )
    # Originated by this sale, applied somewhere else entirely.
    _allocate(db_session, payment, other_invoice, "9000.00")

    position = shadow.resolve_billing_position(db_session, order)

    assert position.settled == Decimal("0.00")
    assert position.originating_payment_ids == (payment.id,)
    assert position.payment_status == SalesOrderPaymentStatus.pending.value


def test_allocation_to_the_sale_invoice_counts_as_settled(db_session):
    order = _order(db_session)
    invoice = _invoice(
        db_session,
        order.subscriber_id,
        total="40000.00",
        balance="15000.00",
        status=InvoiceStatus.partially_paid,
    )
    _link_install_invoice(db_session, order, invoice.id)
    payment = _payment(db_session, order.subscriber_id, amount="25000.00")
    _allocate(db_session, payment, invoice, "25000.00")

    position = shadow.resolve_billing_position(db_session, order)

    assert position.settled == Decimal("25000.00")
    assert position.payment_status == SalesOrderPaymentStatus.partial.value


def test_fully_settled_invoice_reads_as_paid(db_session):
    order = _order(db_session)
    invoice = _invoice(
        db_session,
        order.subscriber_id,
        total="40000.00",
        balance="0.00",
        status=InvoiceStatus.paid,
    )
    _link_install_invoice(db_session, order, invoice.id)
    payment = _payment(db_session, order.subscriber_id, amount="40000.00")
    _allocate(db_session, payment, invoice, "40000.00")

    position = shadow.resolve_billing_position(db_session, order)
    assert position.payment_status == SalesOrderPaymentStatus.paid.value


# ---------------------------------------------------------------------------
# Buckets — exactly one each
# ---------------------------------------------------------------------------


def _bucket(db, order, shared=frozenset()):
    position = shadow.resolve_billing_position(db, order, shared_invoice_ids=shared)
    bucket, _drifts = shadow.classify(order, position)
    return bucket


def test_waiver_with_evidence_is_excluded(db_session, monkeypatch):
    order = _waive(db_session, _order(db_session), monkeypatch)
    assert _bucket(db_session, order) == SalesBillingShadowBucket.WAIVED_EXCLUDED


def test_waiver_without_canonical_evidence_blocks(db_session):
    """A waived status with no owner-written evidence is not a valid exclusion."""
    order = _order(db_session)
    order.payment_status = SalesOrderPaymentStatus.waived.value
    db_session.commit()

    assert _bucket(db_session, order) == (
        SalesBillingShadowBucket.WAIVED_EVIDENCE_MISSING
    )


def test_draft_order_without_billing_is_expected(db_session):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    assert order.status == SalesOrderStatus.draft.value
    assert _bucket(db_session, order) == SalesBillingShadowBucket.UNLINKED_EXPECTED


def test_billable_order_without_billing_is_unexpected(db_session):
    order = _order(db_session)
    order.status = SalesOrderStatus.confirmed.value
    db_session.commit()
    db_session.refresh(order)

    assert _bucket(db_session, order) == SalesBillingShadowBucket.UNLINKED_UNEXPECTED


def test_malformed_metadata_id_is_invalid(db_session):
    order = _order(db_session)
    _link_install_invoice(db_session, order, "not-a-uuid")
    assert _bucket(db_session, order) == SalesBillingShadowBucket.UNRESOLVED_INVALID


def test_id_pointing_at_no_invoice_is_missing(db_session):
    order = _order(db_session)
    _link_install_invoice(db_session, order, uuid.uuid4())
    assert _bucket(db_session, order) == SalesBillingShadowBucket.UNRESOLVED_MISSING


def test_invoice_shared_across_orders_is_ambiguous(db_session):
    """Invoice reuse across orders is real, and cannot carry the boundary."""
    first = _order(db_session)
    second = _order(db_session)
    invoice = _invoice(
        db_session,
        first.subscriber_id,
        total="40000.00",
        balance="0.00",
        status=InvoiceStatus.paid,
    )
    _link_install_invoice(db_session, first, invoice.id)
    _link_install_invoice(db_session, second, invoice.id)

    shared = shadow._shared_invoice_ids(db_session, [first, second])
    assert invoice.id in shared
    assert _bucket(db_session, first, shared) == (
        SalesBillingShadowBucket.UNRESOLVED_AMBIGUOUS
    )


def test_agreement_and_drift(db_session):
    order = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_install_invoice(db_session, order, invoice.id)

    assert _bucket(db_session, order) == SalesBillingShadowBucket.AGREEING

    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()
    db_session.refresh(order)

    position = shadow.resolve_billing_position(db_session, order)
    bucket, drifts = shadow.classify(order, position)
    assert bucket == SalesBillingShadowBucket.DRIFTING
    assert {d.field for d in drifts} == {"amount_paid", "payment_status"}


def test_unsafe_join_is_classified_before_comparison(db_session):
    """A comparison across a join we distrust is not evidence of anything."""
    order = _order(db_session)
    _link_install_invoice(db_session, order, uuid.uuid4())
    order.amount_paid = Decimal("99999.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()
    db_session.refresh(order)

    position = shadow.resolve_billing_position(db_session, order)
    bucket, drifts = shadow.classify(order, position)
    assert bucket == SalesBillingShadowBucket.UNRESOLVED_MISSING
    assert drifts == []


# ---------------------------------------------------------------------------
# Exhaustiveness, refusal to repair, durable evidence
# ---------------------------------------------------------------------------


def test_every_order_lands_in_exactly_one_bucket(db_session, monkeypatch):
    _order(db_session)
    billable = _order(db_session)
    billable.status = SalesOrderStatus.confirmed.value
    db_session.commit()
    _waive(db_session, _order(db_session), monkeypatch)

    report = shadow.scan_billing_shadow(db_session)

    report.assert_exhaustive()
    assert sum(report.buckets.values()) == report.scanned
    assert report.scanned >= 3


def test_the_check_fails_closed_when_asked_to_repair(db_session):
    with pytest.raises(shadow.ShadowCheckCannotRepair):
        shadow.scan_billing_shadow(db_session, apply=True)


def test_supports_apply_is_declared_false():
    assert shadow.SUPPORTS_APPLY is False


def test_scan_persists_immutable_evidence(db_session):
    order = _order(db_session)
    order.status = SalesOrderStatus.confirmed.value
    db_session.commit()

    report = shadow.scan_billing_shadow(db_session, actor_id="staff:audit")
    db_session.commit()

    run = db_session.query(SalesBillingShadowRun).one()
    assert run.scanned == report.scanned
    assert run.cohort_fingerprint == report.cohort_fingerprint
    assert run.clean is False  # unlinked_unexpected blocks
    assert run.actor_id == "staff:audit"
    assert sum(run.bucket_counts.values()) == run.scanned

    from app.models.sales_billing_shadow import SalesBillingShadowImmutableError

    run.scanned = 0
    with pytest.raises(SalesBillingShadowImmutableError):
        db_session.flush()
    db_session.rollback()


def test_fingerprint_is_stable_for_an_unchanged_cohort(db_session):
    _order(db_session)
    first = shadow.scan_billing_shadow(db_session, persist=False)
    second = shadow.scan_billing_shadow(db_session, persist=False)
    assert first.cohort_fingerprint == second.cohort_fingerprint


def test_fingerprint_changes_when_a_bucket_changes(db_session):
    order = _order(db_session)
    before = shadow.scan_billing_shadow(db_session, persist=False)

    order.status = SalesOrderStatus.confirmed.value
    db_session.commit()

    after = shadow.scan_billing_shadow(db_session, persist=False)
    assert before.cohort_fingerprint != after.cohort_fingerprint


def test_clean_streak_resets_on_a_dirty_run(db_session):
    order = _order(db_session)  # draft -> unlinked_expected -> clean
    shadow.scan_billing_shadow(db_session)
    db_session.commit()
    assert shadow.consecutive_clean_runs(db_session) == 1

    shadow.scan_billing_shadow(db_session)
    db_session.commit()
    assert shadow.consecutive_clean_runs(db_session) == 2

    order.status = SalesOrderStatus.confirmed.value
    db_session.commit()
    shadow.scan_billing_shadow(db_session)
    db_session.commit()
    assert shadow.consecutive_clean_runs(db_session) == 0


@pytest.mark.parametrize("apply", [False, True])
def test_reconciler_observes_without_repairing(db_session, apply):
    from app.services import sales_lifecycle_reconciliation as reconciler

    order = _order(db_session)
    invoice = _invoice(
        db_session, order.subscriber_id, total="40000.00", balance="40000.00"
    )
    _link_install_invoice(db_session, order, invoice.id)
    order.amount_paid = Decimal("40000.00")
    order.payment_status = SalesOrderPaymentStatus.paid.value
    db_session.commit()

    result = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=apply)

    assert result["sales_billing_shadow_drifting"] == 1
    db_session.expire_all()
    db_session.refresh(order)
    assert order.amount_paid == Decimal("40000.00")

    # Evidence survives a detect-mode run, where the repair transaction rolls back.
    assert db_session.query(SalesBillingShadowRun).count() == 1
