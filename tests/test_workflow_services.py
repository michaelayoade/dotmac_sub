"""Tests for workflow service."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models.projects import TaskStatus
from app.models.tickets import TicketStatus
from app.models.workforce import WorkOrderStatus
from app.models.workflow import (
    ProjectTaskStatusTransition,
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    SlaTarget,
    TicketStatusTransition,
    WorkflowEntityType,
    WorkOrderStatusTransition,
)
from app.schemas.workflow import (
    ProjectTaskStatusTransitionCreate,
    ProjectTaskStatusTransitionUpdate,
    SlaBreachCreate,
    SlaBreachUpdate,
    SlaClockCreate,
    SlaClockUpdate,
    SlaPolicyCreate,
    SlaPolicyUpdate,
    SlaTargetCreate,
    SlaTargetUpdate,
    StatusTransitionRequest,
    TicketStatusTransitionCreate,
    TicketStatusTransitionUpdate,
    WorkOrderStatusTransitionCreate,
    WorkOrderStatusTransitionUpdate,
)
from app.services import workflow as workflow_service
from app.services.common import apply_ordering, apply_pagination, validate_enum


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering helper."""

    def test_orders_ascending(self, db_session):
        """Test orders ascending."""
        query = db_session.query(SlaPolicy)
        allowed = {"created_at": SlaPolicy.created_at}
        result = apply_ordering(query, "created_at", "asc", allowed)
        assert result is not None

    def test_orders_descending(self, db_session):
        """Test orders descending."""
        query = db_session.query(SlaPolicy)
        allowed = {"created_at": SlaPolicy.created_at}
        result = apply_ordering(query, "created_at", "desc", allowed)
        assert result is not None

    def test_raises_for_invalid_column(self, db_session):
        """Test raises HTTPException for invalid column."""
        query = db_session.query(SlaPolicy)
        allowed = {"created_at": SlaPolicy.created_at}
        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid", "asc", allowed)
        assert exc_info.value.status_code == 400


class TestValidateEnum:
    """Tests for _validate_enum helper."""

    def test_returns_none_for_none(self):
        """Test returns None for None value."""
        result = validate_enum(None, TicketStatus, "status")
        assert result is None

    def test_returns_enum_for_valid(self):
        """Test returns enum for valid value."""
        result = validate_enum("new", TicketStatus, "status")
        assert result == TicketStatus.new

    def test_raises_for_invalid(self):
        """Test raises HTTPException for invalid value."""
        with pytest.raises(HTTPException) as exc_info:
            validate_enum("invalid", TicketStatus, "status")
        assert exc_info.value.status_code == 400


class TestEnsureEntity:
    """Tests for _ensure_entity helper."""

    def test_returns_ticket(self, db_session, ticket):
        """Test returns ticket for ticket entity type."""
        result = workflow_service._ensure_entity(
            db_session, WorkflowEntityType.ticket, str(ticket.id)
        )
        assert result.id == ticket.id

    def test_raises_for_invalid_ticket(self, db_session):
        """Test raises for invalid ticket id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service._ensure_entity(
                db_session, WorkflowEntityType.ticket, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404
        assert "Ticket not found" in exc_info.value.detail

    def test_returns_work_order(self, db_session, work_order):
        """Test returns work order for work_order entity type."""
        result = workflow_service._ensure_entity(
            db_session, WorkflowEntityType.work_order, str(work_order.id)
        )
        assert result.id == work_order.id

    def test_raises_for_invalid_work_order(self, db_session):
        """Test raises for invalid work order id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service._ensure_entity(
                db_session, WorkflowEntityType.work_order, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404
        assert "Work order not found" in exc_info.value.detail

    def test_returns_project_task(self, db_session, project_task):
        """Test returns project task for project_task entity type."""
        result = workflow_service._ensure_entity(
            db_session, WorkflowEntityType.project_task, str(project_task.id)
        )
        assert result.id == project_task.id

    def test_raises_for_invalid_project_task(self, db_session):
        """Test raises for invalid project task id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service._ensure_entity(
                db_session, WorkflowEntityType.project_task, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404
        assert "Project task not found" in exc_info.value.detail


# ============================================================================
# TicketTransitions Tests
# ============================================================================


class TestTicketTransitionsCreate:
    """Tests for TicketTransitions.create."""

    def test_creates_transition(self, db_session):
        """Test creates transition."""
        payload = TicketStatusTransitionCreate(
            from_status="new",
            to_status="open",
        )
        result = workflow_service.ticket_transitions.create(db_session, payload)
        assert result.id is not None
        assert result.from_status == "new"
        assert result.to_status == "open"

    def test_validates_from_status(self, db_session):
        """Test validates from_status enum."""
        payload = TicketStatusTransitionCreate(
            from_status="invalid",
            to_status="open",
        )
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.create(db_session, payload)
        assert exc_info.value.status_code == 400

    def test_validates_to_status(self, db_session):
        """Test validates to_status enum."""
        payload = TicketStatusTransitionCreate(
            from_status="new",
            to_status="invalid",
        )
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.create(db_session, payload)
        assert exc_info.value.status_code == 400


class TestTicketTransitionsGet:
    """Tests for TicketTransitions.get."""

    def test_gets_transition(self, db_session):
        """Test gets transition by id."""
        transition = TicketStatusTransition(from_status="new", to_status="open")
        db_session.add(transition)
        db_session.commit()

        result = workflow_service.ticket_transitions.get(db_session, str(transition.id))
        assert result.id == transition.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestTicketTransitionsList:
    """Tests for TicketTransitions.list."""

    def test_lists_active_by_default(self, db_session):
        """Test lists active transitions by default."""
        active = TicketStatusTransition(
            from_status="new", to_status="open", is_active=True
        )
        inactive = TicketStatusTransition(
            from_status="open", to_status="closed", is_active=False
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        result = workflow_service.ticket_transitions.list(
            db=db_session,
            from_status=None,
            to_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert all(t.is_active for t in result)

    def test_filters_by_from_status(self, db_session):
        """Test filters by from_status."""
        t1 = TicketStatusTransition(from_status="new", to_status="open")
        t2 = TicketStatusTransition(from_status="open", to_status="in_progress")
        db_session.add_all([t1, t2])
        db_session.commit()

        result = workflow_service.ticket_transitions.list(
            db=db_session,
            from_status="new",
            to_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(t.from_status == "new" for t in result)

    def test_filters_by_to_status(self, db_session):
        """Test filters by to_status."""
        t1 = TicketStatusTransition(from_status="new", to_status="open")
        t2 = TicketStatusTransition(from_status="new", to_status="closed")
        db_session.add_all([t1, t2])
        db_session.commit()

        result = workflow_service.ticket_transitions.list(
            db=db_session,
            from_status=None,
            to_status="open",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(t.to_status == "open" for t in result)

    def test_lists_inactive_when_specified(self, db_session):
        """Test lists inactive when specified."""
        inactive = TicketStatusTransition(
            from_status="new", to_status="closed", is_active=False
        )
        db_session.add(inactive)
        db_session.commit()

        result = workflow_service.ticket_transitions.list(
            db=db_session,
            from_status=None,
            to_status=None,
            is_active=False,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(not t.is_active for t in result)


class TestTicketTransitionsUpdate:
    """Tests for TicketTransitions.update."""

    def test_updates_transition(self, db_session):
        """Test updates transition."""
        transition = TicketStatusTransition(from_status="new", to_status="open")
        db_session.add(transition)
        db_session.commit()

        payload = TicketStatusTransitionUpdate(requires_note=True)
        result = workflow_service.ticket_transitions.update(
            db_session, str(transition.id), payload
        )
        assert result.requires_note is True

    def test_validates_status_on_update(self, db_session):
        """Test validates status on update."""
        transition = TicketStatusTransition(from_status="new", to_status="open")
        db_session.add(transition)
        db_session.commit()

        payload = TicketStatusTransitionUpdate(from_status="invalid")
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.update(
                db_session, str(transition.id), payload
            )
        assert exc_info.value.status_code == 400

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = TicketStatusTransitionUpdate(requires_note=True)
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestTicketTransitionsDelete:
    """Tests for TicketTransitions.delete (soft delete)."""

    def test_soft_deletes_transition(self, db_session):
        """Test soft deletes transition."""
        transition = TicketStatusTransition(
            from_status="new", to_status="open", is_active=True
        )
        db_session.add(transition)
        db_session.commit()

        workflow_service.ticket_transitions.delete(db_session, str(transition.id))

        db_session.refresh(transition)
        assert transition.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.ticket_transitions.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# WorkOrderTransitions Tests
# ============================================================================


class TestWorkOrderTransitionsCreate:
    """Tests for WorkOrderTransitions.create."""

    def test_creates_transition(self, db_session):
        """Test creates transition."""
        payload = WorkOrderStatusTransitionCreate(
            from_status="draft",
            to_status="scheduled",
        )
        result = workflow_service.work_order_transitions.create(db_session, payload)
        assert result.id is not None


class TestWorkOrderTransitionsGet:
    """Tests for WorkOrderTransitions.get."""

    def test_gets_transition(self, db_session):
        """Test gets transition."""
        transition = WorkOrderStatusTransition(
            from_status="draft", to_status="scheduled"
        )
        db_session.add(transition)
        db_session.commit()

        result = workflow_service.work_order_transitions.get(
            db_session, str(transition.id)
        )
        assert result.id == transition.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.work_order_transitions.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestWorkOrderTransitionsList:
    """Tests for WorkOrderTransitions.list."""

    def test_lists_transitions(self, db_session):
        """Test lists transitions."""
        transition = WorkOrderStatusTransition(
            from_status="draft", to_status="scheduled"
        )
        db_session.add(transition)
        db_session.commit()

        result = workflow_service.work_order_transitions.list(
            db=db_session,
            from_status=None,
            to_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1


class TestWorkOrderTransitionsUpdate:
    """Tests for WorkOrderTransitions.update."""

    def test_updates_transition(self, db_session):
        """Test updates transition."""
        transition = WorkOrderStatusTransition(
            from_status="draft", to_status="scheduled"
        )
        db_session.add(transition)
        db_session.commit()

        payload = WorkOrderStatusTransitionUpdate(requires_note=True)
        result = workflow_service.work_order_transitions.update(
            db_session, str(transition.id), payload
        )
        assert result.requires_note is True

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = WorkOrderStatusTransitionUpdate(requires_note=True)
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.work_order_transitions.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestWorkOrderTransitionsDelete:
    """Tests for WorkOrderTransitions.delete (soft delete)."""

    def test_soft_deletes_transition(self, db_session):
        """Test soft deletes transition."""
        transition = WorkOrderStatusTransition(
            from_status="draft", to_status="scheduled", is_active=True
        )
        db_session.add(transition)
        db_session.commit()

        workflow_service.work_order_transitions.delete(db_session, str(transition.id))

        db_session.refresh(transition)
        assert transition.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.work_order_transitions.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# ProjectTaskTransitions Tests
# ============================================================================


class TestProjectTaskTransitionsCreate:
    """Tests for ProjectTaskTransitions.create."""

    def test_creates_transition(self, db_session):
        """Test creates transition."""
        payload = ProjectTaskStatusTransitionCreate(
            from_status="backlog",
            to_status="todo",
        )
        result = workflow_service.project_task_transitions.create(db_session, payload)
        assert result.id is not None


class TestProjectTaskTransitionsGet:
    """Tests for ProjectTaskTransitions.get."""

    def test_gets_transition(self, db_session):
        """Test gets transition."""
        transition = ProjectTaskStatusTransition(from_status="backlog", to_status="todo")
        db_session.add(transition)
        db_session.commit()

        result = workflow_service.project_task_transitions.get(
            db_session, str(transition.id)
        )
        assert result.id == transition.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.project_task_transitions.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestProjectTaskTransitionsList:
    """Tests for ProjectTaskTransitions.list."""

    def test_lists_transitions(self, db_session):
        """Test lists transitions."""
        transition = ProjectTaskStatusTransition(from_status="backlog", to_status="todo")
        db_session.add(transition)
        db_session.commit()

        result = workflow_service.project_task_transitions.list(
            db=db_session,
            from_status=None,
            to_status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1


class TestProjectTaskTransitionsUpdate:
    """Tests for ProjectTaskTransitions.update."""

    def test_updates_transition(self, db_session):
        """Test updates transition."""
        transition = ProjectTaskStatusTransition(from_status="backlog", to_status="todo")
        db_session.add(transition)
        db_session.commit()

        payload = ProjectTaskStatusTransitionUpdate(requires_note=True)
        result = workflow_service.project_task_transitions.update(
            db_session, str(transition.id), payload
        )
        assert result.requires_note is True

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = ProjectTaskStatusTransitionUpdate(requires_note=True)
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.project_task_transitions.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestProjectTaskTransitionsDelete:
    """Tests for ProjectTaskTransitions.delete (soft delete)."""

    def test_soft_deletes_transition(self, db_session):
        """Test soft deletes transition."""
        transition = ProjectTaskStatusTransition(
            from_status="backlog", to_status="todo", is_active=True
        )
        db_session.add(transition)
        db_session.commit()

        workflow_service.project_task_transitions.delete(db_session, str(transition.id))

        db_session.refresh(transition)
        assert transition.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.project_task_transitions.delete(
                db_session, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404


# ============================================================================
# SlaPolicies Tests
# ============================================================================


class TestSlaPoliciesCreate:
    """Tests for SlaPolicies.create."""

    def test_creates_policy(self, db_session):
        """Test creates policy."""
        payload = SlaPolicyCreate(
            name="Ticket Response SLA",
            entity_type=WorkflowEntityType.ticket,
        )
        result = workflow_service.sla_policies.create(db_session, payload)
        assert result.id is not None
        assert result.name == "Ticket Response SLA"


class TestSlaPoliciesGet:
    """Tests for SlaPolicies.get."""

    def test_gets_policy(self, db_session):
        """Test gets policy."""
        policy = SlaPolicy(
            name="Test SLA", entity_type=WorkflowEntityType.ticket
        )
        db_session.add(policy)
        db_session.commit()

        result = workflow_service.sla_policies.get(db_session, str(policy.id))
        assert result.id == policy.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_policies.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestSlaPoliciesList:
    """Tests for SlaPolicies.list."""

    def test_lists_active_by_default(self, db_session):
        """Test lists active by default."""
        active = SlaPolicy(
            name="Active", entity_type=WorkflowEntityType.ticket, is_active=True
        )
        inactive = SlaPolicy(
            name="Inactive", entity_type=WorkflowEntityType.ticket, is_active=False
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        result = workflow_service.sla_policies.list(
            db=db_session,
            entity_type=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert all(p.is_active for p in result)

    def test_filters_by_entity_type(self, db_session):
        """Test filters by entity_type."""
        ticket_policy = SlaPolicy(
            name="Ticket SLA", entity_type=WorkflowEntityType.ticket
        )
        wo_policy = SlaPolicy(
            name="WO SLA", entity_type=WorkflowEntityType.work_order
        )
        db_session.add_all([ticket_policy, wo_policy])
        db_session.commit()

        result = workflow_service.sla_policies.list(
            db=db_session,
            entity_type="ticket",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(p.entity_type == WorkflowEntityType.ticket for p in result)


class TestSlaPoliciesUpdate:
    """Tests for SlaPolicies.update."""

    def test_updates_policy(self, db_session):
        """Test updates policy."""
        policy = SlaPolicy(
            name="Original", entity_type=WorkflowEntityType.ticket
        )
        db_session.add(policy)
        db_session.commit()

        payload = SlaPolicyUpdate(name="Updated")
        result = workflow_service.sla_policies.update(db_session, str(policy.id), payload)
        assert result.name == "Updated"

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = SlaPolicyUpdate(name="Test")
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_policies.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestSlaPoliciesDelete:
    """Tests for SlaPolicies.delete (soft delete)."""

    def test_soft_deletes_policy(self, db_session):
        """Test soft deletes policy."""
        policy = SlaPolicy(
            name="Test", entity_type=WorkflowEntityType.ticket, is_active=True
        )
        db_session.add(policy)
        db_session.commit()

        workflow_service.sla_policies.delete(db_session, str(policy.id))

        db_session.refresh(policy)
        assert policy.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_policies.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# SlaTargets Tests
# ============================================================================


class TestSlaTargetsCreate:
    """Tests for SlaTargets.create."""

    def test_creates_target(self, db_session):
        """Test creates target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        payload = SlaTargetCreate(
            policy_id=policy.id,
            target_minutes=60,
        )
        result = workflow_service.sla_targets.create(db_session, payload)
        assert result.id is not None
        assert result.target_minutes == 60

    def test_raises_for_invalid_policy(self, db_session):
        """Test raises for invalid policy_id."""
        payload = SlaTargetCreate(
            policy_id=uuid.uuid4(),
            target_minutes=60,
        )
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_targets.create(db_session, payload)
        assert exc_info.value.status_code == 404


class TestSlaTargetsGet:
    """Tests for SlaTargets.get."""

    def test_gets_target(self, db_session):
        """Test gets target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30)
        db_session.add(target)
        db_session.commit()

        result = workflow_service.sla_targets.get(db_session, str(target.id))
        assert result.id == target.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_targets.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestSlaTargetsList:
    """Tests for SlaTargets.list."""

    def test_lists_targets(self, db_session):
        """Test lists targets."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30)
        db_session.add(target)
        db_session.commit()

        result = workflow_service.sla_targets.list(
            db=db_session,
            policy_id=None,
            priority=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1

    def test_filters_by_policy_id(self, db_session):
        """Test filters by policy_id."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30)
        db_session.add(target)
        db_session.commit()

        result = workflow_service.sla_targets.list(
            db=db_session,
            policy_id=str(policy.id),
            priority=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(t.policy_id == policy.id for t in result)

    def test_filters_by_priority(self, db_session):
        """Test filters by priority."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        high = SlaTarget(policy_id=policy.id, target_minutes=30, priority="high")
        low = SlaTarget(policy_id=policy.id, target_minutes=60, priority="low")
        db_session.add_all([high, low])
        db_session.commit()

        result = workflow_service.sla_targets.list(
            db=db_session,
            policy_id=None,
            priority="high",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(t.priority == "high" for t in result)


class TestSlaTargetsUpdate:
    """Tests for SlaTargets.update."""

    def test_updates_target(self, db_session):
        """Test updates target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30)
        db_session.add(target)
        db_session.commit()

        payload = SlaTargetUpdate(target_minutes=60)
        result = workflow_service.sla_targets.update(db_session, str(target.id), payload)
        assert result.target_minutes == 60

    def test_validates_policy_on_update(self, db_session):
        """Test validates policy_id on update."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30)
        db_session.add(target)
        db_session.commit()

        payload = SlaTargetUpdate(policy_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_targets.update(db_session, str(target.id), payload)
        assert exc_info.value.status_code == 404

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = SlaTargetUpdate(target_minutes=60)
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_targets.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestSlaTargetsDelete:
    """Tests for SlaTargets.delete (soft delete)."""

    def test_soft_deletes_target(self, db_session):
        """Test soft deletes target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=30, is_active=True)
        db_session.add(target)
        db_session.commit()

        workflow_service.sla_targets.delete(db_session, str(target.id))

        db_session.refresh(target)
        assert target.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_targets.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# SlaClocks Tests
# ============================================================================


class TestSlaClocksCreate:
    """Tests for SlaClocks.create."""

    def test_creates_clock(self, db_session, ticket):
        """Test creates clock."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=60)
        db_session.add(target)
        db_session.commit()

        now = datetime.now(timezone.utc)
        payload = SlaClockCreate(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
        )
        result = workflow_service.sla_clocks.create(db_session, payload)
        assert result.id is not None
        # Compare naive datetimes since SQLite doesn't preserve timezone
        expected_due = now.replace(tzinfo=None) + timedelta(minutes=60)
        assert result.due_at == expected_due

    def test_raises_for_invalid_policy(self, db_session, ticket):
        """Test raises for invalid policy_id."""
        payload = SlaClockCreate(
            policy_id=uuid.uuid4(),
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
        )
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_clocks.create(db_session, payload)
        assert exc_info.value.status_code == 404

    def test_raises_for_invalid_entity(self, db_session):
        """Test raises for invalid entity_id."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        target = SlaTarget(policy_id=policy.id, target_minutes=60)
        db_session.add(target)
        db_session.commit()

        payload = SlaClockCreate(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=uuid.uuid4(),
        )
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_clocks.create(db_session, payload)
        assert exc_info.value.status_code == 404


class TestSlaClocksGet:
    """Tests for SlaClocks.get."""

    def test_gets_clock(self, db_session, ticket):
        """Test gets clock."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        result = workflow_service.sla_clocks.get(db_session, str(clock.id))
        assert result.id == clock.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_clocks.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestSlaClocksList:
    """Tests for SlaClocks.list."""

    def test_lists_clocks(self, db_session, ticket):
        """Test lists clocks."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        result = workflow_service.sla_clocks.list(
            db=db_session,
            policy_id=None,
            entity_type=None,
            entity_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1

    def test_filters_by_status(self, db_session, ticket):
        """Test filters by status."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        running = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
            status=SlaClockStatus.running,
        )
        db_session.add(running)
        db_session.commit()

        result = workflow_service.sla_clocks.list(
            db=db_session,
            policy_id=None,
            entity_type=None,
            entity_id=None,
            status="running",
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(c.status == SlaClockStatus.running for c in result)


class TestSlaClocksUpdate:
    """Tests for SlaClocks.update."""

    def test_updates_clock(self, db_session, ticket):
        """Test updates clock."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        payload = SlaClockUpdate(status=SlaClockStatus.paused)
        result = workflow_service.sla_clocks.update(db_session, str(clock.id), payload)
        assert result.status == SlaClockStatus.paused

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = SlaClockUpdate(status=SlaClockStatus.paused)
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_clocks.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestSlaClocksDelete:
    """Tests for SlaClocks.delete (hard delete)."""

    def test_deletes_clock(self, db_session, ticket):
        """Test hard deletes clock."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()
        clock_id = clock.id

        workflow_service.sla_clocks.delete(db_session, str(clock_id))

        assert db_session.get(SlaClock, clock_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_clocks.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# SlaBreaches Tests
# ============================================================================


class TestSlaBreachesCreate:
    """Tests for SlaBreaches.create."""

    def test_creates_breach(self, db_session, ticket):
        """Test creates breach."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        payload = SlaBreachCreate(clock_id=clock.id, notes="Late response")
        result = workflow_service.sla_breaches.create(db_session, payload)
        assert result.id is not None

        # Clock should be marked as breached
        db_session.refresh(clock)
        assert clock.status == SlaClockStatus.breached

    def test_raises_for_invalid_clock(self, db_session):
        """Test raises for invalid clock_id."""
        payload = SlaBreachCreate(clock_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_breaches.create(db_session, payload)
        assert exc_info.value.status_code == 404


class TestSlaBreachesGet:
    """Tests for SlaBreaches.get."""

    def test_gets_breach(self, db_session, ticket):
        """Test gets breach."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        breach = SlaBreach(clock_id=clock.id, breached_at=now)
        db_session.add(breach)
        db_session.commit()

        result = workflow_service.sla_breaches.get(db_session, str(breach.id))
        assert result.id == breach.id

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_breaches.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestSlaBreachesList:
    """Tests for SlaBreaches.list."""

    def test_lists_breaches(self, db_session, ticket):
        """Test lists breaches."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        breach = SlaBreach(clock_id=clock.id, breached_at=now)
        db_session.add(breach)
        db_session.commit()

        result = workflow_service.sla_breaches.list(
            db=db_session,
            clock_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1

    def test_filters_by_status(self, db_session, ticket):
        """Test filters by status."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        breach = SlaBreach(
            clock_id=clock.id, breached_at=now, status=SlaBreachStatus.open
        )
        db_session.add(breach)
        db_session.commit()

        result = workflow_service.sla_breaches.list(
            db=db_session,
            clock_id=None,
            status="open",
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(b.status == SlaBreachStatus.open for b in result)


class TestSlaBreachesUpdate:
    """Tests for SlaBreaches.update."""

    def test_updates_breach(self, db_session, ticket):
        """Test updates breach."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        breach = SlaBreach(clock_id=clock.id, breached_at=now)
        db_session.add(breach)
        db_session.commit()

        payload = SlaBreachUpdate(status=SlaBreachStatus.acknowledged)
        result = workflow_service.sla_breaches.update(db_session, str(breach.id), payload)
        assert result.status == SlaBreachStatus.acknowledged

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        payload = SlaBreachUpdate(notes="Test")
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_breaches.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestSlaBreachesDelete:
    """Tests for SlaBreaches.delete (hard delete)."""

    def test_deletes_breach(self, db_session, ticket):
        """Test hard deletes breach."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        now = datetime.now(timezone.utc)
        clock = SlaClock(
            policy_id=policy.id,
            entity_type=WorkflowEntityType.ticket,
            entity_id=ticket.id,
            started_at=now,
            due_at=now + timedelta(hours=1),
        )
        db_session.add(clock)
        db_session.commit()

        breach = SlaBreach(clock_id=clock.id, breached_at=now)
        db_session.add(breach)
        db_session.commit()
        breach_id = breach.id

        workflow_service.sla_breaches.delete(db_session, str(breach_id))

        assert db_session.get(SlaBreach, breach_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises for not found."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.sla_breaches.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Transition Functions Tests
# ============================================================================


class TestResolveSlaTarget:
    """Tests for _resolve_sla_target helper."""

    def test_returns_priority_target(self, db_session):
        """Test returns priority-specific target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        high = SlaTarget(policy_id=policy.id, target_minutes=30, priority="high")
        fallback = SlaTarget(policy_id=policy.id, target_minutes=120, priority=None)
        db_session.add_all([high, fallback])
        db_session.commit()

        result = workflow_service._resolve_sla_target(
            db_session, str(policy.id), "high"
        )
        assert result.target_minutes == 30

    def test_returns_fallback_target(self, db_session):
        """Test returns fallback (no priority) target."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        fallback = SlaTarget(policy_id=policy.id, target_minutes=120, priority=None)
        db_session.add(fallback)
        db_session.commit()

        result = workflow_service._resolve_sla_target(
            db_session, str(policy.id), "low"
        )
        assert result.target_minutes == 120

    def test_raises_when_no_target(self, db_session):
        """Test raises when no target found."""
        policy = SlaPolicy(name="Test", entity_type=WorkflowEntityType.ticket)
        db_session.add(policy)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            workflow_service._resolve_sla_target(db_session, str(policy.id), "high")
        assert exc_info.value.status_code == 404


class TestRequiresTransition:
    """Tests for _requires_transition helper."""

    def test_returns_none_when_no_rules(self, db_session):
        """Test returns None when no transition rules exist."""
        result = workflow_service._requires_transition(
            db_session, TicketStatusTransition, "new", "open"
        )
        assert result is None

    def test_raises_when_transition_not_allowed(self, db_session):
        """Test raises when transition not allowed."""
        # Create a rule that doesn't match
        rule = TicketStatusTransition(from_status="new", to_status="open")
        db_session.add(rule)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            workflow_service._requires_transition(
                db_session, TicketStatusTransition, "new", "closed"
            )
        assert exc_info.value.status_code == 400
        assert "Transition not allowed" in exc_info.value.detail

    def test_returns_matching_rule(self, db_session):
        """Test returns matching transition rule."""
        rule = TicketStatusTransition(
            from_status="new", to_status="open", requires_note=True
        )
        db_session.add(rule)
        db_session.commit()

        result = workflow_service._requires_transition(
            db_session, TicketStatusTransition, "new", "open"
        )
        assert result is not None
        assert result.requires_note is True


class TestTransitionTicket:
    """Tests for transition_ticket function."""

    def test_transitions_ticket(self, db_session, ticket):
        """Test transitions ticket status."""
        result = workflow_service.transition_ticket(
            db_session, str(ticket.id), StatusTransitionRequest(to_status="open")
        )
        assert result.status == TicketStatus.open

    def test_sets_resolved_at(self, db_session, ticket):
        """Test sets resolved_at when transitioning to resolved."""
        result = workflow_service.transition_ticket(
            db_session, str(ticket.id), StatusTransitionRequest(to_status="resolved")
        )
        assert result.resolved_at is not None

    def test_sets_closed_at(self, db_session, ticket):
        """Test sets closed_at when transitioning to closed."""
        result = workflow_service.transition_ticket(
            db_session, str(ticket.id), StatusTransitionRequest(to_status="closed")
        )
        assert result.closed_at is not None

    def test_raises_for_invalid_ticket(self, db_session):
        """Test raises for invalid ticket_id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.transition_ticket(
                db_session, str(uuid.uuid4()), StatusTransitionRequest(to_status="open")
            )
        assert exc_info.value.status_code == 404

    def test_requires_note_when_rule_requires_it(self, db_session, ticket):
        """Test requires note when rule requires it."""
        rule = TicketStatusTransition(
            from_status="new", to_status="open", requires_note=True
        )
        db_session.add(rule)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            workflow_service.transition_ticket(
                db_session, str(ticket.id), StatusTransitionRequest(to_status="open")
            )
        assert exc_info.value.status_code == 400
        assert "Transition note required" in exc_info.value.detail


class TestTransitionWorkOrder:
    """Tests for transition_work_order function."""

    def test_transitions_work_order(self, db_session, work_order):
        """Test transitions work order status."""
        result = workflow_service.transition_work_order(
            db_session,
            str(work_order.id),
            StatusTransitionRequest(to_status="scheduled"),
        )
        assert result.status == WorkOrderStatus.scheduled

    def test_sets_started_at(self, db_session, work_order):
        """Test sets started_at when transitioning to in_progress."""
        result = workflow_service.transition_work_order(
            db_session,
            str(work_order.id),
            StatusTransitionRequest(to_status="in_progress"),
        )
        assert result.started_at is not None

    def test_sets_completed_at(self, db_session, work_order):
        """Test sets completed_at when transitioning to completed."""
        result = workflow_service.transition_work_order(
            db_session,
            str(work_order.id),
            StatusTransitionRequest(to_status="completed"),
        )
        assert result.completed_at is not None

    def test_raises_for_invalid_work_order(self, db_session):
        """Test raises for invalid work_order_id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.transition_work_order(
                db_session,
                str(uuid.uuid4()),
                StatusTransitionRequest(to_status="scheduled"),
            )
        assert exc_info.value.status_code == 404


class TestTransitionProjectTask:
    """Tests for transition_project_task function."""

    def test_transitions_task(self, db_session, project_task):
        """Test transitions task status."""
        result = workflow_service.transition_project_task(
            db_session,
            str(project_task.id),
            StatusTransitionRequest(to_status="in_progress"),
        )
        assert result.status == TaskStatus.in_progress

    def test_sets_completed_at(self, db_session, project_task):
        """Test sets completed_at when transitioning to done."""
        result = workflow_service.transition_project_task(
            db_session,
            str(project_task.id),
            StatusTransitionRequest(to_status="done"),
        )
        assert result.completed_at is not None

    def test_raises_for_invalid_task(self, db_session):
        """Test raises for invalid task_id."""
        with pytest.raises(HTTPException) as exc_info:
            workflow_service.transition_project_task(
                db_session,
                str(uuid.uuid4()),
                StatusTransitionRequest(to_status="done"),
            )
        assert exc_info.value.status_code == 404
