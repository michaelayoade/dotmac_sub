from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.crm.team import CrmAgent, CrmAgentTeam
from app.models.projects import ProjectTask, TaskStatus
from app.models.tickets import Ticket, TicketStatus
from app.models.provisioning import AppointmentStatus, InstallAppointment, ServiceOrder, ServiceOrderStatus
from app.models.workforce import WorkOrder, WorkOrderPriority, WorkOrderStatus, WorkOrderType
from app.models.workflow import (
    ProjectTaskStatusTransition,
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    SlaTarget,
    TicketStatusTransition,
    WorkOrderStatusTransition,
    WorkflowEntityType,
)
from app.models.domain_settings import SettingDomain
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
from app.services.common import validate_enum, apply_pagination, apply_ordering, coerce_uuid
from app.services.response import ListResponseMixin
from app.services import settings_spec


def _get_by_id(db: Session, model, value):
    return db.get(model, coerce_uuid(value))


def _ensure_entity(db: Session, entity_type: WorkflowEntityType, entity_id: str):
    entity_id = coerce_uuid(entity_id)
    if entity_type == WorkflowEntityType.ticket:
        entity = _get_by_id(db, Ticket, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return entity
    if entity_type == WorkflowEntityType.work_order:
        entity = _get_by_id(db, WorkOrder, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Work order not found")
        return entity
    if entity_type == WorkflowEntityType.project_task:
        entity = _get_by_id(db, ProjectTask, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Project task not found")
        return entity
    raise HTTPException(status_code=400, detail="Invalid entity type")


def _resolve_sla_target(db: Session, policy_id: str, priority: str | None) -> SlaTarget:
    query = (
        db.query(SlaTarget)
        .filter(SlaTarget.policy_id == coerce_uuid(policy_id))
        .filter(SlaTarget.is_active.is_(True))
    )
    if priority:
        match = query.filter(SlaTarget.priority == priority).first()
        if match:
            return match
    fallback = query.filter(SlaTarget.priority.is_(None)).first()
    if not fallback:
        raise HTTPException(status_code=404, detail="SLA target not found")
    return fallback


class TicketTransitions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: TicketStatusTransitionCreate):
        validate_enum(payload.from_status, TicketStatus, "from_status")
        validate_enum(payload.to_status, TicketStatus, "to_status")
        transition = TicketStatusTransition(**payload.model_dump())
        db.add(transition)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def list(
        db: Session,
        from_status: str | None,
        to_status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(TicketStatusTransition)
        if from_status:
            query = query.filter(
                TicketStatusTransition.from_status
                == validate_enum(from_status, TicketStatus, "from_status").value
            )
        if to_status:
            query = query.filter(
                TicketStatusTransition.to_status
                == validate_enum(to_status, TicketStatus, "to_status").value
            )
        if is_active is None:
            query = query.filter(TicketStatusTransition.is_active.is_(True))
        else:
            query = query.filter(TicketStatusTransition.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": TicketStatusTransition.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, transition_id: str):
        transition = _get_by_id(db, TicketStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Ticket transition not found")
        return transition

    @staticmethod
    def update(db: Session, transition_id: str, payload: TicketStatusTransitionUpdate):
        transition = _get_by_id(db, TicketStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Ticket transition not found")
        data = payload.model_dump(exclude_unset=True)
        if "from_status" in data:
            validate_enum(data["from_status"], TicketStatus, "from_status")
        if "to_status" in data:
            validate_enum(data["to_status"], TicketStatus, "to_status")
        for key, value in data.items():
            setattr(transition, key, value)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def delete(db: Session, transition_id: str):
        transition = _get_by_id(db, TicketStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Ticket transition not found")
        transition.is_active = False
        db.commit()


class WorkOrderTransitions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: WorkOrderStatusTransitionCreate):
        validate_enum(payload.from_status, WorkOrderStatus, "from_status")
        validate_enum(payload.to_status, WorkOrderStatus, "to_status")
        transition = WorkOrderStatusTransition(**payload.model_dump())
        db.add(transition)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def list(
        db: Session,
        from_status: str | None,
        to_status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(WorkOrderStatusTransition)
        if from_status:
            query = query.filter(
                WorkOrderStatusTransition.from_status
                == validate_enum(from_status, WorkOrderStatus, "from_status").value
            )
        if to_status:
            query = query.filter(
                WorkOrderStatusTransition.to_status
                == validate_enum(to_status, WorkOrderStatus, "to_status").value
            )
        if is_active is None:
            query = query.filter(WorkOrderStatusTransition.is_active.is_(True))
        else:
            query = query.filter(WorkOrderStatusTransition.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": WorkOrderStatusTransition.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, transition_id: str):
        transition = _get_by_id(db, WorkOrderStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Work order transition not found")
        return transition

    @staticmethod
    def update(db: Session, transition_id: str, payload: WorkOrderStatusTransitionUpdate):
        transition = _get_by_id(db, WorkOrderStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Work order transition not found")
        data = payload.model_dump(exclude_unset=True)
        if "from_status" in data:
            validate_enum(data["from_status"], WorkOrderStatus, "from_status")
        if "to_status" in data:
            validate_enum(data["to_status"], WorkOrderStatus, "to_status")
        for key, value in data.items():
            setattr(transition, key, value)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def delete(db: Session, transition_id: str):
        transition = _get_by_id(db, WorkOrderStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Work order transition not found")
        transition.is_active = False
        db.commit()


class ProjectTaskTransitions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ProjectTaskStatusTransitionCreate):
        validate_enum(payload.from_status, TaskStatus, "from_status")
        validate_enum(payload.to_status, TaskStatus, "to_status")
        transition = ProjectTaskStatusTransition(**payload.model_dump())
        db.add(transition)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def list(
        db: Session,
        from_status: str | None,
        to_status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ProjectTaskStatusTransition)
        if from_status:
            query = query.filter(
                ProjectTaskStatusTransition.from_status
                == validate_enum(from_status, TaskStatus, "from_status").value
            )
        if to_status:
            query = query.filter(
                ProjectTaskStatusTransition.to_status
                == validate_enum(to_status, TaskStatus, "to_status").value
            )
        if is_active is None:
            query = query.filter(ProjectTaskStatusTransition.is_active.is_(True))
        else:
            query = query.filter(ProjectTaskStatusTransition.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ProjectTaskStatusTransition.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def get(db: Session, transition_id: str):
        transition = _get_by_id(db, ProjectTaskStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Project task transition not found")
        return transition

    @staticmethod
    def update(
        db: Session, transition_id: str, payload: ProjectTaskStatusTransitionUpdate
    ):
        transition = _get_by_id(db, ProjectTaskStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Project task transition not found")
        data = payload.model_dump(exclude_unset=True)
        if "from_status" in data:
            validate_enum(data["from_status"], TaskStatus, "from_status")
        if "to_status" in data:
            validate_enum(data["to_status"], TaskStatus, "to_status")
        for key, value in data.items():
            setattr(transition, key, value)
        db.commit()
        db.refresh(transition)
        return transition

    @staticmethod
    def delete(db: Session, transition_id: str):
        transition = _get_by_id(db, ProjectTaskStatusTransition, transition_id)
        if not transition:
            raise HTTPException(status_code=404, detail="Project task transition not found")
        transition.is_active = False
        db.commit()


class SlaPolicies(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaPolicyCreate):
        policy = SlaPolicy(**payload.model_dump())
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def get(db: Session, policy_id: str):
        policy = _get_by_id(db, SlaPolicy, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")
        return policy

    @staticmethod
    def list(
        db: Session,
        entity_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaPolicy)
        if entity_type:
            query = query.filter(
                SlaPolicy.entity_type
                == validate_enum(entity_type, WorkflowEntityType, "entity_type")
            )
        if is_active is None:
            query = query.filter(SlaPolicy.is_active.is_(True))
        else:
            query = query.filter(SlaPolicy.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaPolicy.created_at, "name": SlaPolicy.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, policy_id: str, payload: SlaPolicyUpdate):
        policy = _get_by_id(db, SlaPolicy, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(policy, key, value)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def delete(db: Session, policy_id: str):
        policy = _get_by_id(db, SlaPolicy, policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")
        policy.is_active = False
        db.commit()


class SlaTargets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaTargetCreate):
        policy = _get_by_id(db, SlaPolicy, payload.policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")
        target = SlaTarget(**payload.model_dump())
        db.add(target)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def get(db: Session, target_id: str):
        target = _get_by_id(db, SlaTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SLA target not found")
        return target

    @staticmethod
    def list(
        db: Session,
        policy_id: str | None,
        priority: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaTarget)
        if policy_id:
            query = query.filter(SlaTarget.policy_id == policy_id)
        if priority:
            query = query.filter(SlaTarget.priority == priority)
        if is_active is None:
            query = query.filter(SlaTarget.is_active.is_(True))
        else:
            query = query.filter(SlaTarget.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaTarget.created_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, target_id: str, payload: SlaTargetUpdate):
        target = _get_by_id(db, SlaTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SLA target not found")
        data = payload.model_dump(exclude_unset=True)
        if "policy_id" in data:
            policy = _get_by_id(db, SlaPolicy, data["policy_id"])
            if not policy:
                raise HTTPException(status_code=404, detail="SLA policy not found")
        for key, value in data.items():
            setattr(target, key, value)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def delete(db: Session, target_id: str):
        target = _get_by_id(db, SlaTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SLA target not found")
        target.is_active = False
        db.commit()


class SlaClocks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaClockCreate):
        policy = _get_by_id(db, SlaPolicy, payload.policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="SLA policy not found")
        _ensure_entity(db, payload.entity_type, str(payload.entity_id))
        target = _resolve_sla_target(db, str(payload.policy_id), payload.priority)
        started_at = payload.started_at or datetime.now(timezone.utc)
        due_at = started_at + timedelta(minutes=target.target_minutes)
        default_status = settings_spec.resolve_value(
            db, SettingDomain.workflow, "default_sla_clock_status"
        )
        status_value = (
            validate_enum(default_status, SlaClockStatus, "status")
            if default_status
            else SlaClockStatus.running
        )
        clock = SlaClock(
            policy_id=payload.policy_id,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            priority=payload.priority,
            status=status_value,
            started_at=started_at,
            due_at=due_at,
        )
        db.add(clock)
        db.commit()
        db.refresh(clock)
        return clock

    @staticmethod
    def get(db: Session, clock_id: str):
        clock = _get_by_id(db, SlaClock, clock_id)
        if not clock:
            raise HTTPException(status_code=404, detail="SLA clock not found")
        return clock

    @staticmethod
    def list(
        db: Session,
        policy_id: str | None,
        entity_type: str | None,
        entity_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaClock)
        if policy_id:
            query = query.filter(SlaClock.policy_id == policy_id)
        if entity_type:
            query = query.filter(
                SlaClock.entity_type
                == validate_enum(entity_type, WorkflowEntityType, "entity_type")
            )
        if entity_id:
            query = query.filter(SlaClock.entity_id == entity_id)
        if status:
            query = query.filter(
                SlaClock.status == validate_enum(status, SlaClockStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaClock.created_at, "due_at": SlaClock.due_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, clock_id: str, payload: SlaClockUpdate):
        clock = _get_by_id(db, SlaClock, clock_id)
        if not clock:
            raise HTTPException(status_code=404, detail="SLA clock not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(clock, key, value)
        db.commit()
        db.refresh(clock)
        return clock

    @staticmethod
    def delete(db: Session, clock_id: str):
        clock = _get_by_id(db, SlaClock, clock_id)
        if not clock:
            raise HTTPException(status_code=404, detail="SLA clock not found")
        db.delete(clock)
        db.commit()


class SlaBreaches(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaBreachCreate):
        clock = _get_by_id(db, SlaClock, payload.clock_id)
        if not clock:
            raise HTTPException(status_code=404, detail="SLA clock not found")
        breached_at = payload.breached_at or datetime.now(timezone.utc)
        default_status = settings_spec.resolve_value(
            db, SettingDomain.workflow, "default_sla_breach_status"
        )
        status_value = (
            validate_enum(default_status, SlaBreachStatus, "status")
            if default_status
            else SlaBreachStatus.open
        )
        breach = SlaBreach(
            clock_id=payload.clock_id,
            breached_at=breached_at,
            status=status_value,
            notes=payload.notes,
        )
        clock.status = SlaClockStatus.breached
        clock.breached_at = breached_at
        db.add(breach)
        db.commit()
        db.refresh(breach)
        return breach

    @staticmethod
    def get(db: Session, breach_id: str):
        breach = _get_by_id(db, SlaBreach, breach_id)
        if not breach:
            raise HTTPException(status_code=404, detail="SLA breach not found")
        return breach

    @staticmethod
    def list(
        db: Session,
        clock_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaBreach)
        if clock_id:
            query = query.filter(SlaBreach.clock_id == clock_id)
        if status:
            query = query.filter(
                SlaBreach.status == validate_enum(status, SlaBreachStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaBreach.created_at, "breached_at": SlaBreach.breached_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, breach_id: str, payload: SlaBreachUpdate):
        breach = _get_by_id(db, SlaBreach, breach_id)
        if not breach:
            raise HTTPException(status_code=404, detail="SLA breach not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(breach, key, value)
        db.commit()
        db.refresh(breach)
        return breach

    @staticmethod
    def delete(db: Session, breach_id: str):
        breach = _get_by_id(db, SlaBreach, breach_id)
        if not breach:
            raise HTTPException(status_code=404, detail="SLA breach not found")
        db.delete(breach)
        db.commit()


def _requires_transition(
    db: Session,
    transition_model,
    from_status: str,
    to_status: str,
) -> TicketStatusTransition | WorkOrderStatusTransition | ProjectTaskStatusTransition | None:
    transitions = (
        db.query(transition_model)
        .filter(transition_model.is_active.is_(True))
        .all()
    )
    if not transitions:
        return None
    match = (
        db.query(transition_model)
        .filter(transition_model.is_active.is_(True))
        .filter(transition_model.from_status == from_status)
        .filter(transition_model.to_status == to_status)
        .first()
    )
    if not match:
        raise HTTPException(status_code=400, detail="Transition not allowed")
    return match


def transition_ticket(db: Session, ticket_id: str, payload: StatusTransitionRequest):
    ticket = _get_by_id(db, Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    to_status = validate_enum(payload.to_status, TicketStatus, "to_status")
    from_status = ticket.status.value
    rule = _requires_transition(db, TicketStatusTransition, from_status, to_status.value)
    if rule and rule.requires_note and not payload.note:
        raise HTTPException(status_code=400, detail="Transition note required")
    ticket.status = to_status
    now = datetime.now(timezone.utc)
    if to_status == TicketStatus.resolved:
        ticket.resolved_at = ticket.resolved_at or now
    if to_status == TicketStatus.closed:
        ticket.closed_at = ticket.closed_at or now
    db.commit()
    db.refresh(ticket)
    return ticket


def transition_work_order(
    db: Session, work_order_id: str, payload: StatusTransitionRequest
):
    work_order = _get_by_id(db, WorkOrder, work_order_id)
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")
    to_status = validate_enum(payload.to_status, WorkOrderStatus, "to_status")
    from_status = work_order.status.value
    rule = _requires_transition(
        db, WorkOrderStatusTransition, from_status, to_status.value
    )
    if rule and rule.requires_note and not payload.note:
        raise HTTPException(status_code=400, detail="Transition note required")
    work_order.status = to_status
    appointment = None
    if work_order.metadata_ and work_order.metadata_.get("install_appointment_id"):
        appointment = _get_by_id(
            db, InstallAppointment, work_order.metadata_.get("install_appointment_id")
        )
    if not appointment and work_order.service_order_id:
        appointment = (
            db.query(InstallAppointment)
            .filter(InstallAppointment.service_order_id == work_order.service_order_id)
            .order_by(InstallAppointment.scheduled_start.desc())
            .first()
        )
    now = datetime.now(timezone.utc)
    if to_status == WorkOrderStatus.in_progress:
        work_order.started_at = work_order.started_at or now
        if appointment and appointment.status not in (
            AppointmentStatus.completed,
            AppointmentStatus.canceled,
        ):
            appointment.status = AppointmentStatus.confirmed
    if to_status == WorkOrderStatus.dispatched:
        if appointment and appointment.status not in (
            AppointmentStatus.completed,
            AppointmentStatus.canceled,
        ):
            appointment.status = AppointmentStatus.confirmed
    if to_status == WorkOrderStatus.completed:
        work_order.completed_at = work_order.completed_at or now
        if appointment:
            appointment.status = AppointmentStatus.completed
        if work_order.service_order_id:
            service_order = _get_by_id(db, ServiceOrder, work_order.service_order_id)
            if service_order:
                service_order.status = ServiceOrderStatus.active
    if to_status == WorkOrderStatus.canceled:
        if appointment:
            appointment.status = AppointmentStatus.canceled
        if work_order.service_order_id:
            service_order = _get_by_id(db, ServiceOrder, work_order.service_order_id)
            if service_order:
                service_order.status = ServiceOrderStatus.canceled
    db.commit()
    db.refresh(work_order)
    return work_order


def transition_project_task(
    db: Session, task_id: str, payload: StatusTransitionRequest
):
    task = _get_by_id(db, ProjectTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project task not found")
    to_status = validate_enum(payload.to_status, TaskStatus, "to_status")
    from_status = task.status.value
    rule = _requires_transition(
        db, ProjectTaskStatusTransition, from_status, to_status.value
    )
    if rule and rule.requires_note and not payload.note:
        raise HTTPException(status_code=400, detail="Transition note required")
    task.status = to_status
    now = datetime.now(timezone.utc)
    if to_status == TaskStatus.done:
        task.completed_at = task.completed_at or now
    db.commit()
    db.refresh(task)
    return task


class TicketAssignments:
    """Service for auto-assigning tickets to available agents using round-robin."""

    @staticmethod
    def get_available_agents(
        db: Session,
        team_id: str | None = None,
        is_active: bool = True,
    ) -> list[CrmAgent]:
        """
        Get list of available agents, optionally filtered by team.

        Args:
            db: Database session
            team_id: Optional team ID to filter agents
            is_active: Whether to filter by active status

        Returns:
            List of available CrmAgent records
        """
        query = db.query(CrmAgent).filter(CrmAgent.is_active == is_active)

        if team_id:
            query = query.join(CrmAgentTeam).filter(
                CrmAgentTeam.team_id == coerce_uuid(team_id),
                CrmAgentTeam.is_active.is_(True),
            )

        return query.order_by(CrmAgent.created_at).all()

    @staticmethod
    def get_agent_assignment_count(
        db: Session,
        agent_id: str,
        status_filter: list[TicketStatus] | None = None,
    ) -> int:
        """
        Get the count of tickets currently assigned to an agent.

        Args:
            db: Database session
            agent_id: Agent ID to check
            status_filter: Optional list of statuses to filter (default: open tickets)

        Returns:
            Count of assigned tickets
        """
        agent = db.get(CrmAgent, coerce_uuid(agent_id))
        if not agent:
            return 0

        query = db.query(func.count(Ticket.id)).filter(
            Ticket.assigned_to_person_id == agent.person_id,
            Ticket.is_active.is_(True),
        )

        if status_filter:
            query = query.filter(Ticket.status.in_(status_filter))
        else:
            # Default to open tickets (not resolved/closed/canceled)
            query = query.filter(
                Ticket.status.in_([
                    TicketStatus.new,
                    TicketStatus.open,
                    TicketStatus.pending,
                    TicketStatus.on_hold,
                ])
            )

        return query.scalar() or 0

    @staticmethod
    def find_next_agent_round_robin(
        db: Session,
        team_id: str | None = None,
    ) -> CrmAgent | None:
        """
        Find the next available agent using round-robin assignment.

        Uses the agent with the fewest currently assigned open tickets
        to distribute workload evenly.

        Args:
            db: Database session
            team_id: Optional team ID to filter agents

        Returns:
            The next agent to assign, or None if no agents available
        """
        # Build subquery for open ticket counts per person
        open_statuses = [
            TicketStatus.new,
            TicketStatus.open,
            TicketStatus.pending,
            TicketStatus.on_hold,
        ]

        ticket_count_subquery = (
            db.query(
                Ticket.assigned_to_person_id,
                func.count(Ticket.id).label("ticket_count"),
            )
            .filter(Ticket.is_active.is_(True))
            .filter(Ticket.status.in_(open_statuses))
            .group_by(Ticket.assigned_to_person_id)
            .subquery()
        )

        # Build main query for agents with optional team filter
        query = db.query(CrmAgent).filter(CrmAgent.is_active.is_(True))

        if team_id:
            query = query.join(CrmAgentTeam).filter(
                CrmAgentTeam.team_id == coerce_uuid(team_id),
                CrmAgentTeam.is_active.is_(True),
            )

        # Left join with ticket counts and order by count (nulls first = 0 tickets)
        query = (
            query.outerjoin(
                ticket_count_subquery,
                CrmAgent.person_id == ticket_count_subquery.c.assigned_to_person_id,
            )
            .order_by(
                func.coalesce(ticket_count_subquery.c.ticket_count, 0).asc(),
                CrmAgent.created_at.asc(),  # Tie-breaker: oldest agent first
            )
        )

        return query.first()

    @staticmethod
    def assign_ticket(
        db: Session,
        ticket_id: str,
        team_id: str | None = None,
    ) -> Ticket:
        """
        Auto-assign a ticket to the next available agent.

        Uses round-robin assignment based on current workload.

        Args:
            db: Database session
            ticket_id: Ticket ID to assign
            team_id: Optional team ID to restrict agent selection

        Returns:
            Updated Ticket with assignment

        Raises:
            HTTPException: If ticket not found or no agents available
        """
        ticket = _get_by_id(db, Ticket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # Don't reassign if already assigned
        if ticket.assigned_to_person_id:
            return ticket

        agent = TicketAssignments.find_next_agent_round_robin(db, team_id=team_id)
        if not agent:
            raise HTTPException(
                status_code=400,
                detail="No available agents for assignment",
            )

        ticket.assigned_to_person_id = agent.person_id
        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def reassign_ticket(
        db: Session,
        ticket_id: str,
        agent_id: str | None = None,
        team_id: str | None = None,
    ) -> Ticket:
        """
        Reassign a ticket to a specific agent or find next available.

        Args:
            db: Database session
            ticket_id: Ticket ID to reassign
            agent_id: Optional specific agent ID to assign to
            team_id: Optional team ID for round-robin selection

        Returns:
            Updated Ticket with new assignment

        Raises:
            HTTPException: If ticket or agent not found, or agent is inactive
        """
        ticket = _get_by_id(db, Ticket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        if agent_id:
            # Assign to specific agent
            agent = _get_by_id(db, CrmAgent, agent_id)
            if not agent:
                raise HTTPException(status_code=404, detail="Agent not found")
            if not agent.is_active:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot assign ticket to inactive agent",
                )
            ticket.assigned_to_person_id = agent.person_id
        else:
            # Find next agent via round-robin
            agent = TicketAssignments.find_next_agent_round_robin(db, team_id=team_id)
            if not agent:
                raise HTTPException(
                    status_code=400,
                    detail="No available agents for assignment",
                )
            ticket.assigned_to_person_id = agent.person_id

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def unassign_ticket(db: Session, ticket_id: str) -> Ticket:
        """
        Remove assignment from a ticket.

        Args:
            db: Database session
            ticket_id: Ticket ID to unassign

        Returns:
            Updated Ticket with assignment removed

        Raises:
            HTTPException: If ticket not found
        """
        ticket = _get_by_id(db, Ticket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        ticket.assigned_to_person_id = None
        db.commit()
        db.refresh(ticket)
        return ticket


def _auto_create_work_order_for_service_order(db: Session, service_order: ServiceOrder) -> WorkOrder | None:
    """Auto-create a work order when a service order is scheduled.

    Returns the existing work order if one already exists, or creates a new one.
    """
    # Check if work order already exists for this service order
    existing = (
        db.query(WorkOrder)
        .filter(WorkOrder.service_order_id == service_order.id)
        .filter(WorkOrder.is_active.is_(True))
        .first()
    )
    if existing:
        return existing

    # Get account name for title
    account_name = "Customer"
    if service_order.account and service_order.account.subscriber:
        if service_order.account.subscriber.person:
            account_name = service_order.account.subscriber.person.name or "Customer"

    work_order = WorkOrder(
        title=f"Installation - {account_name}",
        work_type=WorkOrderType.install,
        status=WorkOrderStatus.scheduled,
        priority=WorkOrderPriority.normal,
        account_id=service_order.account_id,
        subscription_id=service_order.subscription_id,
        service_order_id=service_order.id,
    )
    db.add(work_order)
    return work_order


def transition_service_order(
    db: Session,
    service_order_id: str,
    payload: StatusTransitionRequest,
    skip_contract_check: bool = False,
) -> ServiceOrder:
    """Transition a service order to a new status.

    Auto-creates a work order when transitioning to 'scheduled'.
    Blocks transition to 'provisioning' or 'active' if contract not signed.

    Args:
        db: Database session
        service_order_id: Service order ID
        payload: Status transition request
        skip_contract_check: If True, skip the contract signature check

    Raises:
        HTTPException: If service order not found or contract not signed
    """
    from app.services.contracts import contract_signatures

    service_order = _get_by_id(db, ServiceOrder, service_order_id)
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")

    to_status = validate_enum(payload.to_status, ServiceOrderStatus, "to_status")

    # Block transition to fulfillment stages if contract not signed
    fulfillment_stages = {ServiceOrderStatus.provisioning, ServiceOrderStatus.active}
    if to_status in fulfillment_stages and not skip_contract_check:
        if not contract_signatures.is_signed(db, service_order_id):
            raise HTTPException(
                status_code=400,
                detail="Contract must be signed before fulfillment. "
                f"Please sign at: /portal/service-orders/{service_order_id}/contract",
            )

    # Auto-create work order when scheduled
    if to_status == ServiceOrderStatus.scheduled:
        _auto_create_work_order_for_service_order(db, service_order)

    service_order.status = to_status
    db.commit()
    db.refresh(service_order)
    return service_order


ticket_transitions = TicketTransitions()
work_order_transitions = WorkOrderTransitions()
project_task_transitions = ProjectTaskTransitions()
sla_policies = SlaPolicies()
sla_targets = SlaTargets()
sla_clocks = SlaClocks()
sla_breaches = SlaBreaches()
ticket_assignments = TicketAssignments()
