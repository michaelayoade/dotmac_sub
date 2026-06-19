"""Tests for collections and dunning service."""

from datetime import UTC, datetime

from app.models.collections import DunningAction, DunningCase, DunningCaseStatus
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningCaseCreate,
    DunningCaseUpdate,
)
from app.services import collections as collections_service


def test_create_dunning_case(db_session, subscriber_account):
    """Test creating a dunning case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
            current_step=1,
        ),
    )
    assert case.account_id == subscriber_account.id
    assert case.status == DunningCaseStatus.open
    assert case.current_step == 1


def test_dunning_case_status_transitions(db_session, subscriber_account):
    """Test dunning case status transitions."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    assert case.status == DunningCaseStatus.open

    # Pause the case
    updated = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(status=DunningCaseStatus.paused),
    )
    assert updated.status == DunningCaseStatus.paused

    # Resolve the case
    resolved = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(
            status=DunningCaseStatus.resolved,
            resolved_at=datetime.now(UTC),
        ),
    )
    assert resolved.status == DunningCaseStatus.resolved
    assert resolved.resolved_at is not None


def test_dunning_action_log_creation(db_session, subscriber_account):
    """Test creating dunning action logs."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    log = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent successfully",
        ),
    )
    assert log.case_id == case.id
    assert log.action == DunningAction.notify
    assert log.outcome == "Email sent successfully"


def test_list_dunning_cases_by_account(db_session, subscriber_account):
    """Test listing dunning cases by account."""
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )

    cases = collections_service.dunning_cases.list(
        db_session,
        account_id=subscriber_account.id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(cases) >= 1
    assert all(c.account_id == subscriber_account.id for c in cases)


def test_list_dunning_cases_by_status(db_session, subscriber_account):
    """Test listing dunning cases by status."""
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.closed,
        ),
    )

    open_cases = collections_service.dunning_cases.list(
        db_session,
        account_id=None,
        status=DunningCaseStatus.open,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert all(c.status == DunningCaseStatus.open for c in open_cases)


def test_list_action_logs_by_case(db_session, subscriber_account):
    """Test listing action logs by case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
        ),
    )
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.throttle,
            step_day=7,
        ),
    )

    logs = collections_service.dunning_action_logs.list(
        db_session,
        case_id=case.id,
        invoice_id=None,
        payment_id=None,
        order_by="executed_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(logs) >= 2
    assert all(l.case_id == case.id for l in logs)


def test_dunning_case_resolution(db_session, subscriber_account):
    """Test full dunning case resolution flow."""
    # Create case
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
            current_step=1,
            notes="Payment overdue",
        ),
    )

    # Log first action
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            step_day=1,
            outcome="Email sent",
        ),
    )

    # Update step
    updated = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(current_step=2),
    )
    assert updated.current_step == 2

    # Log second action
    collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.suspend,
            step_day=7,
            outcome="Payment successful",
        ),
    )

    # Resolve case
    resolved = collections_service.dunning_cases.update(
        db_session,
        str(case.id),
        DunningCaseUpdate(
            status=DunningCaseStatus.resolved,
            resolved_at=datetime.now(UTC),
            notes="Payment received",
        ),
    )
    assert resolved.status == DunningCaseStatus.resolved


def test_delete_dunning_case(db_session, subscriber_account):
    """Test deleting a dunning case."""
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=subscriber_account.id,
            status=DunningCaseStatus.open,
        ),
    )
    collections_service.dunning_cases.delete(db_session, str(case.id))
    assert db_session.get(DunningCase, case.id) is None


# =============================================================================
# DunningWorkflow.run — functional tests (paused cases must be skipped)
# =============================================================================


def _setup_overdue_postpaid_account(
    db_session, subscriber, subscription, catalog_offer
):
    """Overdue postpaid account with a configured day-1 notify dunning step."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.models.billing import Invoice, InvoiceStatus
    from app.models.catalog import (
        BillingMode,
        PolicyDunningStep,
        PolicySet,
        SubscriptionStatus,
    )
    from app.models.catalog import (
        DunningAction as CatalogDunningAction,
    )

    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active

    policy_set = PolicySet(name="Dunning Test Policy")
    db_session.add(policy_set)
    db_session.flush()
    db_session.add(
        PolicyDunningStep(
            policy_set_id=policy_set.id,
            day_offset=1,
            action=CatalogDunningAction.notify,
            note="day-1 reminder",
        )
    )
    catalog_offer.policy_set_id = policy_set.id
    # The subscription may resolve its policy via the offer version first.
    if subscription.offer_version is not None:
        subscription.offer_version.policy_set_id = policy_set.id

    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DUN-RUN-1",
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=5),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_dunning_run_skips_paused_case(
    db_session, subscriber, subscription, catalog_offer
):
    """A paused case must not execute escalation steps."""
    from datetime import UTC, datetime

    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.paused,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(case)
    assert case.status == DunningCaseStatus.paused
    assert case.current_step is None
    assert response.actions_created == 0
    assert response.skipped >= 1
    logs = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .all()
    )
    assert logs == []


def test_payment_resolves_open_but_not_paused_cases(
    db_session, subscriber, subscription, catalog_offer
):
    """An incoming payment auto-resolves OPEN dunning cases but must leave a
    PAUSED case (operator hold) untouched (#A7 paused-policy)."""
    open_case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    paused_case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.paused,
        started_at=datetime.now(UTC),
    )
    db_session.add_all([open_case, paused_case])
    db_session.commit()

    collections_service.dunning_workflow.resolve_cases_for_account(
        db_session, str(subscriber.id), None, commit=False
    )
    db_session.flush()

    db_session.refresh(open_case)
    db_session.refresh(paused_case)
    assert open_case.status == DunningCaseStatus.resolved
    assert paused_case.status == DunningCaseStatus.paused  # operator hold kept


def test_suspend_proceeds_when_overdue_and_unshielded(
    db_session, subscriber, subscription, catalog_offer
):
    """Control: an overdue, unshielded account is actually suspended."""
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "suspended"
    assert get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_skipped_when_balance_cleared_mid_run(
    db_session, subscriber, subscription, catalog_offer
):
    """The dunning-after-payment race: if the balance cleared since the run's
    snapshot, the re-read under lock cancels the suspend (#A1)."""
    from app.models.billing import Invoice, InvoiceStatus
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    # Simulate the payment landing after the run snapshotted balances.
    inv = db_session.query(Invoice).filter(Invoice.account_id == subscriber.id).one()
    inv.balance_due = 0
    inv.status = InvoiceStatus.paid
    db_session.commit()

    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "balance_cleared"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_suspend_shielded_by_active_arrangement(
    db_session, subscriber, subscription, catalog_offer
):
    """A customer with an active payment arrangement must not be dunned (#A2)."""
    from datetime import date
    from decimal import Decimal

    from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
    from app.services.account_lifecycle import get_active_locks
    from app.services.collections._core import _execute_dunning_action

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    db_session.add(
        PaymentArrangement(
            subscriber_id=subscriber.id,
            status=ArrangementStatus.active,
            is_active=True,
            total_amount=Decimal("100.00"),
            installment_amount=Decimal("50.00"),
            installments_total=2,
            start_date=date(2026, 1, 1),
        )
    )
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    outcome = _execute_dunning_action(
        db_session, case, DunningAction.suspend, day_offset=7, note=None
    )
    assert outcome == "shielded"
    assert not get_active_locks(db_session, subscription_id=str(subscription.id))


def test_dunning_run_executes_step_for_active_case(
    db_session, subscriber, subscription, catalog_offer
):
    """An open case with an overdue invoice executes the configured step."""
    from datetime import UTC, datetime

    from app.models.collections import DunningActionLog
    from app.schemas.collections import DunningRunRequest

    _setup_overdue_postpaid_account(db_session, subscriber, subscription, catalog_offer)
    case = DunningCase(
        account_id=subscriber.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    response = collections_service.dunning_workflow.run(db_session, DunningRunRequest())

    db_session.refresh(case)
    assert case.status == DunningCaseStatus.open
    assert case.current_step == 1
    assert response.actions_created == 1
    logs = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id == case.id)
        .all()
    )
    assert len(logs) == 1
    assert logs[0].action == DunningAction.notify
    assert logs[0].outcome == "notification_sent"
