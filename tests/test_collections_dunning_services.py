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
