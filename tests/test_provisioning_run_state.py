"""Provisioning run/order state-machine fixes (review tasks #7, #8, #9)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.provisioning import (
    ProvisioningRun,
    ProvisioningRunStatus,
    ServiceOrder,
    ServiceOrderStatus,
)
from app.services.events.handlers.provisioning import ProvisioningHandler
from app.services.events.types import Event, EventType


def _order(db, subscriber_account, subscription, status):
    order = ServiceOrder(
        subscriber_id=subscriber_account.id,
        subscription_id=subscription.id,
        status=status,
    )
    db.add(order)
    db.flush()
    return order


@pytest.mark.parametrize(
    "event_type",
    [EventType.provisioning_completed, EventType.provisioning_failed],
)
def test_terminal_run_event_delegates_to_lifecycle_owner(
    db_session, subscriber_account, subscription, monkeypatch, event_type
):
    """Terminal runs are observations; the lifecycle owner decides the order state."""
    order = _order(
        db_session,
        subscriber_account,
        subscription,
        ServiceOrderStatus.provisioning,
    )
    run_id = uuid4()
    owner_db = object()
    captured = []

    @contextmanager
    def owner_command_session():
        yield owner_db

    monkeypatch.setattr(
        "app.services.events.handlers.provisioning.db_session_adapter.owner_command_session",
        owner_command_session,
    )
    monkeypatch.setattr(
        "app.services.events.handlers.provisioning.evaluate_readiness",
        lambda db, command: captured.append((db, command))
        or SimpleNamespace(status=SimpleNamespace(value="blocked")),
    )
    event = Event(
        event_type=event_type,
        payload={
            "service_order_id": str(order.id),
            "provisioning_run_id": str(run_id),
        },
        service_order_id=order.id,
    )

    ProvisioningHandler().handle(db_session, event)

    assert captured[0][0] is owner_db
    assert captured[0][1].service_order_id == order.id
    assert captured[0][1].provisioning_run_id == run_id
    assert order.status == ServiceOrderStatus.provisioning


def test_reaper_fails_stale_running_runs(db_session, subscriber_account, subscription):
    """A run stuck in 'running' past the timeout is reaped to failed (#8)."""
    from app.models.provisioning import ProvisioningWorkflow
    from app.services.provisioning_managers import ProvisioningRuns

    wf = ProvisioningWorkflow(name="wf")
    db_session.add(wf)
    db_session.flush()

    fresh = ProvisioningRun(
        workflow_id=wf.id,
        status=ProvisioningRunStatus.running,
        started_at=datetime.now(UTC),
    )
    stale = ProvisioningRun(
        workflow_id=wf.id,
        status=ProvisioningRunStatus.running,
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add_all([fresh, stale])
    db_session.flush()

    reaped = ProvisioningRuns.reap_stale_runs(db_session, older_than_minutes=30)
    assert reaped == 1
    db_session.refresh(fresh)
    db_session.refresh(stale)
    assert fresh.status == ProvisioningRunStatus.running
    assert stale.status == ProvisioningRunStatus.failed
    assert stale.error_message is not None


def test_step_returning_failed_marks_run_failed(db_session):
    """A step that returns status='failed' (without raising) must fail the run,
    not be recorded as success (#9). UnsupportedProvisioner (vendor=other)
    returns a failed ProvisioningResult without raising."""
    from app.models.provisioning import (
        ProvisioningStep,
        ProvisioningStepType,
        ProvisioningVendor,
        ProvisioningWorkflow,
    )
    from app.services.provisioning_managers import ProvisioningRuns

    wf = ProvisioningWorkflow(name="wf-unsupported", vendor=ProvisioningVendor.other)
    db_session.add(wf)
    db_session.flush()
    db_session.add(
        ProvisioningStep(
            workflow_id=wf.id,
            name="push",
            step_type=ProvisioningStepType.push_config,
            order_index=0,
            is_active=True,
        )
    )
    db_session.flush()

    run = ProvisioningRuns.run(db_session, str(wf.id))
    assert run.status == ProvisioningRunStatus.failed
    assert run.error_message is not None
