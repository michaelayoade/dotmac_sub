from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.collections import (
    DunningAction,
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import SubscriberStatus
from app.services import billing_automation
from app.services.billing_prepaid_overlap_repair import (
    find_prepaid_overlap_candidates,
    repair_prepaid_overlapping_invoices,
)


def _paid_invoice(db, account, subscription, start, end):
    invoice = Invoice(
        account_id=account.id,
        invoice_number="PAID-COVERAGE",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=start,
        billing_period_end=end,
        issued_at=start,
        due_at=start,
        paid_at=start,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Paid prepaid service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_active=True,
        )
    )
    return invoice


def _bad_invoice(db, account, subscription, start, end, *, status=InvoiceStatus.issued):
    invoice = Invoice(
        account_id=account.id,
        invoice_number="BAD-OVERLAP",
        status=status,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        billing_period_start=start,
        billing_period_end=end,
        issued_at=start,
        due_at=start - timedelta(days=1),
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Bad overlapping prepaid service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_active=True,
        )
    )
    return invoice


def _prepare_prepaid(subscription, account, *, next_billing_at):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = next_billing_at
    account.status = SubscriberStatus.active
    account.is_active = True


def test_finds_overlapping_prepaid_invoice_and_reports_paid_coverage(
    db_session, subscriber_account, subscription
):
    paid_start = datetime(2026, 6, 4, tzinfo=UTC)
    paid_end = datetime(2026, 7, 3, tzinfo=UTC)
    bad_start = datetime(2026, 6, 20, tzinfo=UTC)
    bad_end = datetime(2026, 7, 20, tzinfo=UTC)
    _prepare_prepaid(subscription, subscriber_account, next_billing_at=bad_start)
    paid = _paid_invoice(db_session, subscriber_account, subscription, paid_start, paid_end)
    bad = _bad_invoice(db_session, subscriber_account, subscription, bad_start, bad_end)
    db_session.commit()

    rows = find_prepaid_overlap_candidates(db_session)

    assert len(rows) == 1
    row = rows[0]
    assert row.bad_invoice_id == str(bad.id)
    assert row.valid_paid_invoice_id == str(paid.id)
    assert row.corrected_next_billing_at == paid_end.isoformat()
    assert row.action == "void_unpaid_invoice"


def test_mark_overdue_freezes_unflagged_overlapping_prepaid_invoice(
    db_session, subscriber_account, subscription, monkeypatch
):
    paid_start = datetime(2026, 6, 4, tzinfo=UTC)
    paid_end = datetime(2026, 7, 3, tzinfo=UTC)
    bad_start = datetime(2026, 6, 20, tzinfo=UTC)
    bad_end = datetime(2026, 7, 20, tzinfo=UTC)
    _prepare_prepaid(subscription, subscriber_account, next_billing_at=bad_start)
    _paid_invoice(db_session, subscriber_account, subscription, paid_start, paid_end)
    bad = _bad_invoice(db_session, subscriber_account, subscription, bad_start, bad_end)
    db_session.commit()
    emitted = []
    monkeypatch.setattr(
        billing_automation,
        "emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    result = billing_automation.mark_overdue_invoices(db_session)

    db_session.refresh(bad)
    assert result["skipped_on_hold"] == 1
    assert result["marked_overdue"] == 0
    assert bad.status == InvoiceStatus.issued
    assert (bad.metadata_ or {}).get("reconciliation_hold") is True
    assert emitted == []


def test_apply_repair_voids_bad_invoice_corrects_anchor_and_restores_bad_lock(
    db_session, subscriber_account, subscription
):
    paid_start = datetime(2026, 6, 4, tzinfo=UTC)
    paid_end = datetime(2026, 7, 3, tzinfo=UTC)
    bad_start = datetime(2026, 6, 20, tzinfo=UTC)
    bad_end = datetime(2026, 7, 20, tzinfo=UTC)
    _prepare_prepaid(subscription, subscriber_account, next_billing_at=bad_start)
    _paid_invoice(db_session, subscriber_account, subscription, paid_start, paid_end)
    bad = _bad_invoice(
        db_session,
        subscriber_account,
        subscription,
        bad_start,
        bad_end,
        status=InvoiceStatus.overdue,
    )
    case = DunningCase(
        account_id=subscriber_account.id,
        status=DunningCaseStatus.open,
        current_step=3,
        started_at=bad_start,
    )
    db_session.add(case)
    db_session.flush()
    db_session.add(
        DunningActionLog(
            case_id=case.id,
            invoice_id=bad.id,
            action=DunningAction.suspend,
            step_day=3,
            outcome="suspended",
        )
    )
    subscription.status = SubscriptionStatus.suspended
    subscriber_account.status = SubscriberStatus.suspended
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber_account.id,
            reason=EnforcementReason.overdue,
            source=f"dunning_case:{case.id}",
            is_active=True,
        )
    )
    db_session.commit()

    result = repair_prepaid_overlapping_invoices(db_session, apply=True)

    db_session.refresh(bad)
    db_session.refresh(subscription)
    db_session.refresh(subscriber_account)
    db_session.refresh(case)
    assert result["voided"] == 1
    assert result["dunning_cases_resolved"] == 1
    assert result["subscriptions_restored"] == 1
    assert bad.status == InvoiceStatus.void
    assert bad.balance_due == Decimal("0.00")
    assert subscription.next_billing_at == paid_end
    assert subscription.status == SubscriptionStatus.active
    assert subscriber_account.status == SubscriberStatus.active
    assert case.status == DunningCaseStatus.resolved
