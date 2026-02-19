"""Tests for provisioning service."""

from datetime import UTC, datetime, timedelta

from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningStepType,
    ServiceOrderStatus,
    ServiceState,
    TaskStatus,
)
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    ProvisioningRunCreate,
    ProvisioningStepCreate,
    ProvisioningTaskCreate,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ServiceOrderCreate,
    ServiceOrderUpdate,
    ServiceStateTransitionCreate,
)
from app.services import provisioning as provisioning_service


def test_create_service_order(db_session, subscriber_account, subscription):
    """Test creating a service order."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            notes="New fiber installation",
        ),
    )
    assert order.account_id == subscriber_account.id
    assert order.subscription_id == subscription.id
    assert order.status == ServiceOrderStatus.draft


def test_list_service_orders_by_account(db_session, subscriber_account, subscription):
    """Test listing service orders by account."""
    provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )

    orders = provisioning_service.service_orders.list(
        db_session,
        account_id=str(subscriber_account.id),
        subscription_id=None,
        status=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(orders) >= 2
    assert all(o.account_id == subscriber_account.id for o in orders)


def test_update_service_order_status(db_session, subscriber_account, subscription):
    """Test updating service order status."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            status=ServiceOrderStatus.draft,
        ),
    )
    updated = provisioning_service.service_orders.update(
        db_session,
        str(order.id),
        ServiceOrderUpdate(status=ServiceOrderStatus.submitted),
    )
    assert updated.status == ServiceOrderStatus.submitted


def test_delete_service_order(db_session, subscriber_account, subscription):
    """Test deleting a service order."""
    import pytest
    from fastapi import HTTPException

    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    order_id = order.id
    provisioning_service.service_orders.delete(db_session, str(order_id))

    # Verify order is deleted
    with pytest.raises(HTTPException) as exc_info:
        provisioning_service.service_orders.get(db_session, str(order_id))
    assert exc_info.value.status_code == 404


def test_create_install_appointment(db_session, subscriber_account, subscription):
    """Test creating an install appointment."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    appointment = provisioning_service.install_appointments.create(
        db_session,
        InstallAppointmentCreate(
            service_order_id=order.id,
            scheduled_start=datetime.now(UTC) + timedelta(days=1),
            scheduled_end=datetime.now(UTC) + timedelta(days=1, hours=2),
        ),
    )
    assert appointment.service_order_id == order.id
    assert appointment.status == AppointmentStatus.proposed


def test_list_appointments_by_service_order(db_session, subscriber_account, subscription):
    """Test listing appointments by service order."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    provisioning_service.install_appointments.create(
        db_session,
        InstallAppointmentCreate(
            service_order_id=order.id,
            scheduled_start=datetime.now(UTC) + timedelta(days=1),
            scheduled_end=datetime.now(UTC) + timedelta(days=1, hours=2),
        ),
    )

    appointments = provisioning_service.install_appointments.list(
        db_session,
        service_order_id=str(order.id),
        status=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(appointments) >= 1
    assert all(a.service_order_id == order.id for a in appointments)


def test_create_provisioning_task(db_session, subscriber_account, subscription):
    """Test creating a provisioning task."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    task = provisioning_service.provisioning_tasks.create(
        db_session,
        ProvisioningTaskCreate(
            service_order_id=order.id,
            name="Configure ONT",
            notes="Configure customer ONT device",
        ),
    )
    assert task.service_order_id == order.id
    assert task.name == "Configure ONT"
    assert task.status == TaskStatus.pending


def test_update_provisioning_task(db_session, subscriber_account, subscription):
    """Test updating a provisioning task."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    task = provisioning_service.provisioning_tasks.create(
        db_session,
        ProvisioningTaskCreate(
            service_order_id=order.id,
            name="Original Task",
            status=TaskStatus.pending,
        ),
    )
    updated = provisioning_service.provisioning_tasks.update(
        db_session,
        str(task.id),
        ProvisioningTaskUpdate(status=TaskStatus.completed),
    )
    assert updated.status == TaskStatus.completed


def test_create_provisioning_workflow(db_session):
    """Test creating a provisioning workflow."""
    workflow = provisioning_service.provisioning_workflows.create(
        db_session,
        ProvisioningWorkflowCreate(
            name="Standard Install",
            description="Standard fiber installation workflow",
        ),
    )
    assert workflow.name == "Standard Install"


def test_create_provisioning_step(db_session):
    """Test creating a provisioning step."""
    workflow = provisioning_service.provisioning_workflows.create(
        db_session,
        ProvisioningWorkflowCreate(name="Test Workflow"),
    )
    step = provisioning_service.provisioning_steps.create(
        db_session,
        ProvisioningStepCreate(
            workflow_id=workflow.id,
            name="Assign ONT",
            step_type=ProvisioningStepType.assign_ont,
            order_index=1,
        ),
    )
    assert step.workflow_id == workflow.id
    assert step.order_index == 1


def test_provisioning_step_ordering(db_session):
    """Test provisioning step ordering."""
    workflow = provisioning_service.provisioning_workflows.create(
        db_session,
        ProvisioningWorkflowCreate(name="Ordered Workflow"),
    )
    provisioning_service.provisioning_steps.create(
        db_session,
        ProvisioningStepCreate(
            workflow_id=workflow.id,
            name="Step 1",
            step_type=ProvisioningStepType.assign_ont,
            order_index=1,
        ),
    )
    provisioning_service.provisioning_steps.create(
        db_session,
        ProvisioningStepCreate(
            workflow_id=workflow.id,
            name="Step 2",
            step_type=ProvisioningStepType.push_config,
            order_index=2,
        ),
    )
    provisioning_service.provisioning_steps.create(
        db_session,
        ProvisioningStepCreate(
            workflow_id=workflow.id,
            name="Step 3",
            step_type=ProvisioningStepType.confirm_up,
            order_index=3,
        ),
    )

    steps = provisioning_service.provisioning_steps.list(
        db_session,
        workflow_id=str(workflow.id),
        step_type=None,
        is_active=None,
        order_by="order_index",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(steps) >= 3
    # Verify ordering
    ordered_steps = [s for s in steps if s.workflow_id == workflow.id]
    for i in range(len(ordered_steps) - 1):
        assert ordered_steps[i].order_index <= ordered_steps[i + 1].order_index


def test_create_provisioning_run(db_session, subscriber_account, subscription):
    """Test creating a provisioning run."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    workflow = provisioning_service.provisioning_workflows.create(
        db_session,
        ProvisioningWorkflowCreate(name="Run Workflow"),
    )
    run = provisioning_service.provisioning_runs.create(
        db_session,
        ProvisioningRunCreate(
            service_order_id=order.id,
            workflow_id=workflow.id,
        ),
    )
    assert run.service_order_id == order.id
    assert run.workflow_id == workflow.id


def test_create_service_state_transition(db_session, subscriber_account, subscription):
    """Test creating a service state transition."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    transition = provisioning_service.service_state_transitions.create(
        db_session,
        ServiceStateTransitionCreate(
            service_order_id=order.id,
            from_state=ServiceState.pending,
            to_state=ServiceState.provisioning,
            reason="Started processing",
        ),
    )
    assert transition.service_order_id == order.id
    assert transition.from_state == ServiceState.pending
    assert transition.to_state == ServiceState.provisioning


def test_list_transitions_by_service_order(db_session, subscriber_account, subscription):
    """Test listing state transitions by service order."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
        ),
    )
    provisioning_service.service_state_transitions.create(
        db_session,
        ServiceStateTransitionCreate(
            service_order_id=order.id,
            from_state=ServiceState.pending,
            to_state=ServiceState.provisioning,
        ),
    )
    provisioning_service.service_state_transitions.create(
        db_session,
        ServiceStateTransitionCreate(
            service_order_id=order.id,
            from_state=ServiceState.provisioning,
            to_state=ServiceState.active,
        ),
    )

    transitions = provisioning_service.service_state_transitions.list(
        db_session,
        service_order_id=str(order.id),
        order_by="changed_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(transitions) >= 2
    assert all(t.service_order_id == order.id for t in transitions)


def test_get_service_order(db_session, subscriber_account, subscription):
    """Test getting a service order by ID."""
    order = provisioning_service.service_orders.create(
        db_session,
        ServiceOrderCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            notes="Test order",
        ),
    )
    fetched = provisioning_service.service_orders.get(db_session, str(order.id))
    assert fetched is not None
    assert fetched.id == order.id
    assert fetched.notes == "Test order"
