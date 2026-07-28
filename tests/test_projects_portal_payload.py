"""Native customer-experience lifecycle projection contracts."""

from __future__ import annotations

from uuid import UUID, uuid4

from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.schemas.portal import (
    CustomerActionKey,
    CustomerExperienceState,
    MyProjectsResponse,
    ProjectStageState,
)
from app.services import customer_experience_lifecycle


def _subscriber(db_session, label: str) -> Subscriber:
    row = Subscriber(
        first_name=label,
        last_name="Customer",
        email=f"{label.lower()}-{uuid4().hex}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _lifecycle(db_session, subscriber: Subscriber):
    project = Project(
        name="Fiber install — Wuse II",
        project_type="fiber_optics_installation",
        status="active",
        subscriber_id=subscriber.id,
        customer_address="12 Aminu Kano Crescent",
        region="Abuja",
    )
    ticket = Ticket(
        title="Activation light level remains low",
        subscriber_id=subscriber.id,
        status="pending_confirmation",
    )
    db_session.add_all([project, ticket])
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        title="Project Plan",
        status="done",
        ticket_id=ticket.id,
        metadata_={"fiber_stage_key": "project_plan"},
    )
    db_session.add(task)
    db_session.flush()
    visit = WorkOrder(
        public_id="sub-field-visit-1",
        subscriber_id=subscriber.id,
        project_id=project.id,
        project_task_id=task.id,
        origin_ticket_id=ticket.id,
        title="Inspect and re-splice customer drop",
        status="completed",
    )
    db_session.add(visit)
    db_session.commit()
    return project, task, visit, ticket


def test_project_projection_traces_task_field_visit_ticket_and_actions(
    db_session, subscriber
):
    project, task, visit, ticket = _lifecycle(db_session, subscriber)

    response = customer_experience_lifecycle.projects_for_subscriber(
        db_session, str(subscriber.id)
    )

    assert isinstance(response, MyProjectsResponse)
    assert response.total == 1
    item = response.projects[0]
    assert item.id == project.id
    assert isinstance(item.id, UUID)
    assert item.experience_state == CustomerExperienceState.waiting_on_customer
    assert item.progress_pct == 17
    stage = item.stages[0]
    assert stage.task_id == task.id
    assert stage.status == ProjectStageState.done
    assert stage.ticket is not None
    assert stage.ticket.id == ticket.id
    assert {action.key for action in stage.ticket.actions} >= {
        CustomerActionKey.confirm_resolution,
        CustomerActionKey.dispute_resolution,
    }
    assert [row.id for row in stage.work_orders] == [visit.id]
    assert stage.work_orders[0].public_id == visit.public_id
    assert stage.work_orders[0].project_task_id == task.id


def test_work_order_projection_uses_native_identity_and_relationships(
    db_session, subscriber
):
    project, task, visit, ticket = _lifecycle(db_session, subscriber)

    response = customer_experience_lifecycle.work_orders_for_subscriber(
        db_session, str(subscriber.id)
    )

    assert response.total == 1
    item = response.work_orders[0]
    assert item.id == visit.id
    assert item.public_id == "sub-field-visit-1"
    assert item.project_id == project.id
    assert item.project_task_id == task.id
    assert item.origin_ticket_id == ticket.id
    assert item.origin_ticket is not None
    assert item.origin_ticket.id == ticket.id


def test_projection_is_subscriber_scoped_and_excludes_inactive_projects(
    db_session, subscriber
):
    project, *_ = _lifecycle(db_session, subscriber)
    other = _subscriber(db_session, "Other")
    db_session.add(
        Project(
            name="Other install",
            project_type="fiber_optics_installation",
            status="active",
            subscriber_id=other.id,
        )
    )
    project.is_active = False
    db_session.commit()

    response = customer_experience_lifecycle.projects_for_subscriber(
        db_session, str(subscriber.id)
    )

    assert response.projects == []
    assert response.total == 0
