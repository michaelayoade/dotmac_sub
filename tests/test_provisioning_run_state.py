"""Provisioning run/order state-machine fixes (review tasks #7, #8, #9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_provisioning_completed_advances_order_to_active(
    db_session, subscriber_account, subscription
):
    """Run success advances a stuck 'provisioning' order to active (#7)."""
    order = _order(
        db_session,
        subscriber_account,
        subscription,
        ServiceOrderStatus.provisioning,
    )
    event = Event(
        event_type=EventType.provisioning_completed,
        payload={"service_order_id": str(order.id), "provisioning_run_id": "x"},
        service_order_id=order.id,
    )
    ProvisioningHandler().handle(db_session, event)
    db_session.refresh(order)
    assert order.status == ServiceOrderStatus.active


def test_provisioning_failed_advances_order_to_failed(
    db_session, subscriber_account, subscription
):
    """Run failure advances the order to failed, not stuck provisioning (#7)."""
    order = _order(
        db_session,
        subscriber_account,
        subscription,
        ServiceOrderStatus.provisioning,
    )
    event = Event(
        event_type=EventType.provisioning_failed,
        payload={"service_order_id": str(order.id)},
        service_order_id=order.id,
    )
    ProvisioningHandler().handle(db_session, event)
    db_session.refresh(order)
    assert order.status == ServiceOrderStatus.failed


def test_run_advance_does_not_override_terminal_order(
    db_session, subscriber_account, subscription
):
    """A completed run must not resurrect a canceled order (#7)."""
    order = _order(
        db_session, subscriber_account, subscription, ServiceOrderStatus.canceled
    )
    event = Event(
        event_type=EventType.provisioning_completed,
        payload={"service_order_id": str(order.id)},
        service_order_id=order.id,
    )
    ProvisioningHandler().handle(db_session, event)
    db_session.refresh(order)
    assert order.status == ServiceOrderStatus.canceled


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
