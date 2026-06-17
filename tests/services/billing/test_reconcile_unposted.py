"""Tests for the cutover unposted-payment reconcile engine.

Covers the native credit→invoice settler (the novel money logic), the cohort
finder, the per-subscriber reconcile, and the forward-fix that carries
billing_account_id through webhook ingest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import PaymentCreate, PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.billing.reconcile_unposted import (
    find_cohort_account_ids,
    project_settlement,
    reconcile_subscriber,
    settle_open_invoices_from_credit,
)

CUTOVER = datetime(2026, 6, 13, tzinfo=UTC)


def _native_subscriber(db_session, *, suffix: str) -> Subscriber:
    sub = Subscriber(
        first_name="Native",
        last_name=suffix,
        email=f"native-{suffix.lower()}@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _sitting_credit_payment(db_session, sub, amount: Decimal):
    """Create a succeeded payment that parks as unallocated credit.

    With no open invoice present, auto-allocate finds nothing and the full
    amount is recorded as account credit — the exact 'money captured before the
    invoice issued' state the cutover left behind.
    """
    return billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=sub.id,
            amount=amount,
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )


def _open_invoice(db_session, sub, balance: Decimal) -> Invoice:
    inv = Invoice(
        account_id=sub.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=balance,
        balance_due=balance,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


def test_credit_fully_settles_open_invoice(db_session):
    sub = _native_subscriber(db_session, suffix="Full")
    _sitting_credit_payment(db_session, sub, Decimal("19300.00"))
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("19300.00")
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    result = settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    assert result.applied == Decimal("19300.00")
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")
    # No double count: the credit pool is fully consumed.
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("0.00")


def test_partial_credit_leaves_invoice_partially_paid(db_session):
    sub = _native_subscriber(db_session, suffix="Partial")
    _sitting_credit_payment(db_session, sub, Decimal("5000.00"))
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    result = settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    assert result.applied == Decimal("5000.00")
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.partially_paid
    assert inv.balance_due == Decimal("14300.00")
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("0.00")


def test_overpayment_settles_invoice_and_keeps_surplus(db_session):
    sub = _native_subscriber(db_session, suffix="Over")
    _sitting_credit_payment(db_session, sub, Decimal("30000.00"))
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    result = settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    assert result.applied == Decimal("19300.00")
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    # Surplus stays as available credit.
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("10700.00")


def test_oldest_invoice_settled_first(db_session):
    sub = _native_subscriber(db_session, suffix="Order")
    _sitting_credit_payment(db_session, sub, Decimal("25000.00"))
    older = _open_invoice(db_session, sub, Decimal("19300.00"))
    older.due_at = datetime.now(UTC) - timedelta(days=10)
    newer = _open_invoice(db_session, sub, Decimal("10000.00"))
    newer.due_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.status == InvoiceStatus.paid
    assert newer.status == InvoiceStatus.partially_paid
    assert newer.balance_due == Decimal("4300.00")  # 10000 - (25000 - 19300)
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("0.00")


def test_settle_is_idempotent(db_session):
    sub = _native_subscriber(db_session, suffix="Idem")
    _sitting_credit_payment(db_session, sub, Decimal("19300.00"))
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()
    allocations_after_first = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == inv.id)
        .count()
    )

    second = settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    assert second.applied == Decimal("0.00")
    allocations_after_second = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == inv.id)
        .count()
    )
    assert allocations_after_first == allocations_after_second
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("0.00")


def test_no_credit_is_noop(db_session):
    sub = _native_subscriber(db_session, suffix="NoCredit")
    _open_invoice(db_session, sub, Decimal("19300.00"))

    result = settle_open_invoices_from_credit(db_session, str(sub.id))
    db_session.commit()

    assert result.applied == Decimal("0.00")
    assert result.invoices_touched == []


def test_project_settlement_writes_nothing(db_session):
    sub = _native_subscriber(db_session, suffix="Proj")
    _sitting_credit_payment(db_session, sub, Decimal("19300.00"))
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    projected = project_settlement(db_session, str(sub.id))

    assert projected.applied == Decimal("19300.00")
    assert str(inv.id) in projected.invoices_settled
    # Dry-run must not mutate: invoice still open, credit still parked.
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.issued
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("19300.00")


def test_cohort_finder_selects_credit_with_open_debt(db_session):
    # In-cohort: paid (sitting credit) AND open invoice.
    in_cohort = _native_subscriber(db_session, suffix="InCohort")
    pay = _sitting_credit_payment(db_session, in_cohort, Decimal("19300.00"))
    pay.created_at = CUTOVER + timedelta(hours=1)
    _open_invoice(db_session, in_cohort, Decimal("19300.00"))
    # Out: has credit but no open invoice.
    no_debt = _native_subscriber(db_session, suffix="NoDebt")
    p2 = _sitting_credit_payment(db_session, no_debt, Decimal("5000.00"))
    p2.created_at = CUTOVER + timedelta(hours=1)
    db_session.commit()

    ids = find_cohort_account_ids(db_session, since=CUTOVER)

    assert str(in_cohort.id) in ids
    assert str(no_debt.id) not in ids


def test_reconcile_subscriber_apply_settles_and_commits(db_session):
    sub = _native_subscriber(db_session, suffix="Engine")
    _sitting_credit_payment(db_session, sub, Decimal("19300.00"))
    inv = _open_invoice(db_session, sub, Decimal("19300.00"))

    result = reconcile_subscriber(db_session, str(sub.id), dry_run=False)

    assert result.error is None
    assert result.settle.applied == Decimal("19300.00")
    # Committed — visible on a fresh read.
    db_session.expire_all()
    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid


def test_ingest_carries_billing_account_id_for_consolidated_payment(db_session):
    """Forward fix: a consolidated webhook event must post against the billing
    account (crediting its balance), not land with billing_account_id NULL."""
    reseller = Reseller(name="PartnerCo")
    db_session.add(reseller)
    db_session.commit()
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    provider = PaymentProvider(
        name="Paystack", provider_type=PaymentProviderType.paystack
    )
    db_session.add(provider)
    db_session.commit()

    event = billing_service.payment_provider_events.ingest(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            billing_account_id=ba.id,
            amount=Decimal("50000.00"),
            currency="NGN",
            event_type="charge.success",
            external_id="cutover-consolidated-1",
            status_hint=PaymentStatus.succeeded,
        ),
    )

    payment = db_session.get(Payment, event.payment_id)
    assert payment is not None
    assert str(payment.billing_account_id) == str(ba.id)
    assert payment.account_id is None
    assert payment.status == PaymentStatus.succeeded
    # Surplus credited to the billing account balance (auto_allocate=False path).
    db_session.refresh(ba)
    assert ba.balance == Decimal("50000.00")


def test_notification_handler_skips_inside_suppression_scope(db_session):
    """The backfill runs notifications-off: a notification-spec'd event queues
    nothing while inside suppress_notifications()."""
    from app.models.notification import Notification
    from app.services.events.handlers.notification import NotificationHandler
    from app.services.events.types import Event, EventType
    from app.services.notification_suppression import suppress_notifications

    before = db_session.query(Notification).count()
    event = Event(
        event_type=EventType.subscription_resumed,
        payload={"subscription_id": "00000000-0000-0000-0000-000000000000"},
    )
    with suppress_notifications():
        NotificationHandler().handle(db_session, event)

    assert db_session.query(Notification).count() == before
