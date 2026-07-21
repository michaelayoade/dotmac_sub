from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.network import IPAssignment, IPVersion, IPv4Address
from app.models.project import Project, ProjectTask, ProjectTaskStatus
from app.models.provisioning import (
    ProvisioningReadinessDecisionStatus,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.services.owner_commands import CommandContext
from app.services.provisioning_lifecycle import (
    ConfirmActivationCommand,
    EvaluateReadinessCommand,
    ProvisioningLifecycleError,
    confirm_activation,
    evaluate_readiness,
)


def _context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="pytest:provisioning_lifecycle",
        scope="pytest:service_order",
        reason=reason,
    )


def _graph(db, subscriber, subscription, *, task_status: str):
    subscription.status = SubscriptionStatus.pending
    project = Project(
        name="Customer installation",
        project_type="fiber_optics_installation",
        subscriber_id=subscriber.id,
        status="active",
    )
    db.add(project)
    db.flush()
    task = ProjectTask(
        project_id=project.id,
        title="Power Direction, Splicing & Customer Activation",
        status=task_status,
        metadata_={"fiber_stage_key": "power_splicing_activation"},
    )
    db.add(task)
    workflow = ProvisioningWorkflow(name=f"workflow-{uuid4()}")
    db.add(workflow)
    db.flush()
    order = ServiceOrder(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        project_id=project.id,
        activation_project_task_id=task.id,
        order_type=ServiceOrderType.new_install,
        status=ServiceOrderStatus.provisioning,
    )
    db.add(order)
    db.flush()
    run = ProvisioningRun(
        workflow_id=workflow.id,
        service_order_id=order.id,
        subscription_id=subscription.id,
        status=ProvisioningRunStatus.success,
    )
    address = IPv4Address(address=f"10.253.{uuid4().int % 200}.10")
    db.add(address)
    db.flush()
    db.add(
        IPAssignment(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
    )
    db.add(run)
    db.commit()
    return order, run


def test_incomplete_activation_task_blocks_without_activating(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    monkeypatch.setattr("app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None)

    outcome = evaluate_readiness(
        db_session,
        EvaluateReadinessCommand(
            context=_context("successful run with incomplete field scope"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert outcome.status == ProvisioningReadinessDecisionStatus.blocked
    assert outcome.reason_code == "activation_task_incomplete"
    assert db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    assert subscription.status == SubscriptionStatus.pending


def test_ready_order_requests_activation_then_requires_projection_confirmation(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    emitted = []
    monkeypatch.setattr(
        "app.services.provisioning_lifecycle.emit_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    requested = evaluate_readiness(
        db_session,
        EvaluateReadinessCommand(
            context=_context("all readiness facts established"),
            service_order_id=order.id,
            provisioning_run_id=run.id,
        ),
    )

    assert requested.status == ProvisioningReadinessDecisionStatus.activation_requested
    assert subscription.status == SubscriptionStatus.active
    assert db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.provisioning
    assert emitted[0][1]["service_order_id"] == order.id

    # Release the read transaction before entering the next public owner command.
    db_session.commit()
    confirmed = confirm_activation(
        db_session,
        ConfirmActivationCommand(
            context=_context("connectivity projections succeeded"),
            service_order_id=order.id,
            subscription_id=subscription.id,
        ),
    )

    assert confirmed.status == ProvisioningReadinessDecisionStatus.activated
    assert db_session.get(ServiceOrder, order.id).status == ServiceOrderStatus.active
    assert len(emitted) == 2


def test_readiness_command_id_is_an_idempotent_replay(
    db_session, subscriber, subscription, monkeypatch
):
    order, run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.in_progress.value,
    )
    monkeypatch.setattr("app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None)
    context = _context("retryable terminal run event")
    command = EvaluateReadinessCommand(
        context=context,
        service_order_id=order.id,
        provisioning_run_id=run.id,
    )

    first = evaluate_readiness(db_session, command)
    second = evaluate_readiness(db_session, command)

    assert second.decision_id == first.decision_id
    assert second.command_id == context.command_id


def test_confirmation_rejects_activation_without_readiness_request(
    db_session, subscriber, subscription, monkeypatch
):
    order, _run = _graph(
        db_session,
        subscriber,
        subscription,
        task_status=ProjectTaskStatus.done.value,
    )
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    monkeypatch.setattr("app.services.provisioning_lifecycle.emit_event", lambda *a, **k: None)

    with pytest.raises(ProvisioningLifecycleError) as exc:
        confirm_activation(
            db_session,
            ConfirmActivationCommand(
                context=_context("unsolicited confirmation"),
                service_order_id=order.id,
                subscription_id=subscription.id,
            ),
        )

    assert exc.value.code == "operations.provisioning_lifecycle.activation_not_requested"
