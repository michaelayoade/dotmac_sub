"""Real postpaid healing, unambiguous cases only.

`unwall_account` had no Celery task and no beat entry; its only caller was a
one-off script, and the scheduled detector hard-coded `apply=False`. So an
account that owed nothing could stay behind the wall until a human happened to
run a script.

The healing pass now applies for real, but only when a locked recomputation
proves zero overdue receivable. Exact arithmetic stands: a fifty-kobo residue
correctly blocks the automated restore — and becomes an operator exception with
durable evidence instead of silently staying invisible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.admin_alert import AdminAlert
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.enforcement_lock import EnforcementReason
from app.models.network_monitoring import AlertStatus
from app.models.subscriber import SubscriberStatus
from app.services.account_lifecycle import suspend_subscription
from app.services.billing.unwall_paid_accounts import (
    UNWALL_EXCEPTION_PREFIX,
    UnwallDisposition,
    decide_unwall,
    heal_walled_account,
    run_scheduled_walled_account_healing,
)

RESIDUE = Decimal("0.50")


def _overdue_invoice(db, account, *, amount: Decimal) -> Invoice:
    now = datetime.now(UTC)
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-HEAL-{uuid4().hex[:8]}",
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=amount,
        tax_total=Decimal("0.00"),
        total=amount,
        balance_due=amount,
        issued_at=now - timedelta(days=40),
        due_at=now - timedelta(days=10),
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.commit()
    return invoice


def _wall(db, subscription) -> None:
    # Postpaid: this is the cohort the scheduled detector never healed.
    subscription.billing_mode = BillingMode.postpaid
    db.flush()
    suspend_subscription(
        db,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    db.commit()


def _exception_alert(db, account_id) -> AdminAlert | None:
    return (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{UNWALL_EXCEPTION_PREFIX}{account_id}")
        .one_or_none()
    )


def test_decision_recomputes_the_exact_overdue_receivable(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    decision = decide_unwall(db_session, str(subscriber.id))

    assert decision.walled is True
    assert decision.overdue_receivable_total == RESIDUE
    assert len(decision.overdue_receivable_invoice_ids) == 1
    assert decision.unambiguous is False


def test_scheduled_healing_restores_an_account_that_owes_nothing(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)

    result = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )

    assert result.disposition is UnwallDisposition.restored
    assert result.restored is True
    db_session.refresh(subscriber)
    assert subscriber.status is SubscriberStatus.active
    assert _exception_alert(db_session, subscriber.id) is None


def test_a_fifty_kobo_residue_blocks_scheduled_healing(
    db_session, subscriber, subscription
):
    """The money rule: no tolerance, epsilon, or de-minimis threshold."""
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    result = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="service:walled_account_healing",
        reason="scheduled healing",
    )

    assert result.disposition is UnwallDisposition.blocked_overdue_receivable
    assert result.restored is False
    db_session.refresh(subscriber)
    assert subscriber.status is not SubscriberStatus.active


def test_a_blocked_row_becomes_a_durable_operator_exception(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="service:walled_account_healing",
        reason="scheduled healing",
    )

    alert = _exception_alert(db_session, subscriber.id)
    assert alert is not None
    assert alert.status is AlertStatus.open
    assert alert.details["overdue_receivable_total"] == "0.50"
    assert alert.details["disposition"] == "blocked_overdue_receivable"
    assert len(alert.details["overdue_receivable_invoice_ids"]) == 1


def test_the_operator_exception_is_deduplicated_and_idempotent(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    for _ in range(3):
        heal_walled_account(
            db_session,
            str(subscriber.id),
            require_zero_overdue_receivable=True,
            actor="service:walled_account_healing",
            reason="scheduled healing",
        )

    assert (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like(f"{UNWALL_EXCEPTION_PREFIX}%"))
        .count()
        == 1
    )


def test_healing_is_idempotent_once_the_account_is_active(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    first = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )
    second = heal_walled_account(
        db_session,
        str(subscriber.id),
        require_zero_overdue_receivable=True,
        actor="pytest",
        reason="pytest healing",
    )

    assert first.disposition is UnwallDisposition.restored
    assert second.disposition is UnwallDisposition.not_walled
    assert second.restored is False


def test_detect_only_pass_records_the_exception_without_restoring(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)
    _overdue_invoice(db_session, subscriber, amount=RESIDUE)

    stats = run_scheduled_walled_account_healing(db_session, apply=False)

    assert stats["applied"] == 0
    assert stats["blocked_overdue_receivable"] == 1
    assert stats["exceptions"] == 1
    assert stats["restored"] == 0
    db_session.refresh(subscriber)
    assert subscriber.status is not SubscriberStatus.active
    assert _exception_alert(db_session, subscriber.id) is not None


def test_applying_pass_heals_only_the_unambiguous_account(
    db_session, subscriber, subscription
):
    _wall(db_session, subscription)

    stats = run_scheduled_walled_account_healing(db_session, apply=True)

    assert stats["applied"] == 1
    assert stats["restored"] == 1
    assert stats["exceptions"] == 0
    db_session.refresh(subscriber)
    assert subscriber.status is SubscriberStatus.active
