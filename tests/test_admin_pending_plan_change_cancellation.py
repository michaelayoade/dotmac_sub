from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.owner_commands import CommandContext
from app.services.subscription_change_execution import (
    CancelPendingPlanChangeCommand,
    PendingPlanChangeCancellationError,
    cancel_pending_plan_change,
)
from tests.test_admin_change_plan_scheduling import _same_family_offers
from tests.test_customer_plan_change_prepaid import _make_subscription


def _pending_request(db_session, subscriber):
    current, target = _same_family_offers(db_session)
    subscription = _make_subscription(
        db_session,
        subscriber,
        current,
        next_billing_at=datetime.now(UTC) + timedelta(days=15),
        start_at=datetime.now(UTC) - timedelta(days=15),
    )
    request = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=current.id,
        requested_offer_id=target.id,
        status=SubscriptionChangeStatus.pending,
        effective_date=subscription.next_billing_at.date(),
    )
    db_session.add(request)
    db_session.commit()
    return subscription, request


def _command(request, subscription, *, reason="Stale request confirmed by support"):
    command_id = uuid4()
    return CancelPendingPlanChangeCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="admin:test-person",
            scope="catalog:write",
            reason=reason,
            idempotency_key=f"cancel-pending-plan-change:{request.id}",
        ),
        request_id=request.id,
        subscription_id=subscription.id,
    )


def test_admin_can_cancel_exact_pending_plan_change(db_session, subscriber):
    subscription, request = _pending_request(db_session, subscriber)

    outcome = cancel_pending_plan_change(db_session, _command(request, subscription))

    assert outcome.status == SubscriptionChangeStatus.canceled
    assert outcome.previous_status == SubscriptionChangeStatus.pending
    assert outcome.replayed is False
    db_session.refresh(request)
    assert request.status == SubscriptionChangeStatus.canceled
    assert "Stale request confirmed by support" in (request.notes or "")


def test_cancel_pending_plan_change_is_idempotent(db_session, subscriber):
    subscription, request = _pending_request(db_session, subscriber)
    command = _command(request, subscription)
    cancel_pending_plan_change(db_session, command)

    replay = cancel_pending_plan_change(db_session, command)

    assert replay.status == SubscriptionChangeStatus.canceled
    assert replay.replayed is True


def test_admin_cannot_cancel_request_from_another_subscription(db_session, subscriber):
    subscription, request = _pending_request(db_session, subscriber)
    command = _command(request, subscription)
    wrong_scope = CancelPendingPlanChangeCommand(
        context=command.context,
        request_id=request.id,
        subscription_id=uuid4(),
    )

    with pytest.raises(PendingPlanChangeCancellationError) as exc:
        cancel_pending_plan_change(db_session, wrong_scope)

    assert exc.value.code.endswith("service_change_scope_mismatch")
    db_session.refresh(request)
    assert request.status == SubscriptionChangeStatus.pending


def test_admin_cannot_cancel_approved_plan_change(db_session, subscriber):
    subscription, request = _pending_request(db_session, subscriber)
    request.status = SubscriptionChangeStatus.approved
    db_session.commit()

    with pytest.raises(PendingPlanChangeCancellationError) as exc:
        cancel_pending_plan_change(db_session, _command(request, subscription))

    assert exc.value.code.endswith("service_change_not_pending")


def test_admin_subscription_page_exposes_only_pending_cancel_action():
    template = Path("templates/admin/catalog/subscription_detail.html").read_text(
        encoding="utf-8"
    )
    routes = Path("app/web/admin/catalog.py").read_text(encoding="utf-8")

    assert "Cancel pending request" in template
    assert "can_cancel_pending_plan_change" in template
    assert "pending-change/{request_id}/cancel" in routes
    assert "revoke" not in template.lower()
