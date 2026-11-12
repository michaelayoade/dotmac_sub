from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.project import Project, ProjectTask
from app.models.rbac import Role, SystemUserRole
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Address, AddressType, Subscriber, UserType
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate, WorkOrderHeaderUpdate
from app.schemas.network import InfrastructureWorkOrderIssueRequest
from app.schemas.support import TicketWorkOrderIssueRequest
from app.services import (
    support_ticket_settings,
    ticket_work_order_handoff,
    web_dispatch_work_orders,
)
from app.services.owner_commands import CommandContext
from app.services.topology import outage_work_order_handoff
from app.services.work_order_assignment_contracts import (
    WorkOrderAssignmentEligibilityQuery,
)
from app.services.work_order_commands import WorkOrderCommands
from tests.staff_identity_fixtures import add_bound_staff_user


def _subscriber(db_session, label: str = "Handoff") -> Subscriber:
    row = Subscriber(
        first_name=label,
        last_name="Customer",
        email=f"handoff-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _ticket_with_team(db_session):
    subscriber = _subscriber(db_session)
    actor, person = add_bound_staff_user(db_session)
    actor_id = actor.id
    team = ServiceTeam(
        name="Field Response",
        team_type=ServiceTeamType.field_service.value,
        is_active=True,
    )
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=person.id,
            is_active=True,
        )
    )
    ticket = Ticket(
        number=f"TKT-{uuid4().hex[:6]}",
        title="Loss of signal",
        description="Subscriber has no optical signal",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        service_team_id=team.id,
        status="open",
        priority="high",
    )
    db_session.add(ticket)
    db_session.commit()
    return subscriber, team, actor_id, ticket


def _payload() -> TicketWorkOrderIssueRequest:
    return TicketWorkOrderIssueRequest(
        reason="Optical levels require an onsite trace",
        work_type="repair",
        tags=["fibre"],
    )


def _grant_admin(db_session, actor_id) -> None:
    role = db_session.query(Role).filter(Role.name == "admin").one_or_none()
    if role is None:
        role = Role(name="admin", is_active=True)
        db_session.add(role)
        db_session.flush()
    else:
        role.is_active = True
    db_session.add(SystemUserRole(system_user_id=actor_id, role_id=role.id))
    db_session.commit()


def _issue(
    db_session,
    *,
    ticket_id,
    actor_id,
    idempotency_key: str,
    payload: TicketWorkOrderIssueRequest | None = None,
):
    # Owner commands require a transaction-free adapter session. Test setup
    # commits above, so all identifiers are captured before entering it.
    command = ticket_work_order_handoff.TicketWorkOrderIssueCommand(
        ticket_id=ticket_id,
        request=payload or _payload(),
        actor_id=actor_id,
        actor_type=ticket_work_order_handoff.HandoffActorType.SYSTEM_USER,
        permissions=frozenset({"support:ticket:update", "operations:dispatch:write"}),
        context=CommandContext.system(
            actor=str(actor_id),
            scope=ticket_work_order_handoff.WORK_ORDER_ISSUE_SCOPE,
            reason="test ticket-to-work-order issuance",
            idempotency_key=idempotency_key,
        ),
    )
    return ticket_work_order_handoff.issue_work_order(db_session, command)


def test_manual_infrastructure_ticket_issues_work_order_without_subscriber(db_session):
    _subscriber, team, _member_id, _customer_ticket = _ticket_with_team(db_session)
    dispatcher, _person = add_bound_staff_user(db_session)
    _grant_admin(db_session, dispatcher.id)
    ticket = Ticket(
        number=f"TKT-{uuid4().hex[:6]}",
        title="Core link down",
        description="Inspect the link",
        ticket_type="Core Link Disconnection",
        service_team_id=team.id,
        status="open",
        priority="high",
        metadata_={
            "infrastructure_selection": {
                "mode": "manual",
                "name": "Core link A-B",
                "ticket_type": "Core Link Disconnection",
            }
        },
    )
    db_session.add(ticket)
    db_session.commit()

    def issue():
        return outage_work_order_handoff.issue_from_ticket(
            db_session,
            outage_work_order_handoff.InfrastructureTicketWorkOrderIssueCommand(
                ticket_id=ticket.id,
                request=InfrastructureWorkOrderIssueRequest(
                    title="Restore core link A-B", reason="Link is down"
                ),
                actor_id=dispatcher.id,
                actor_type=outage_work_order_handoff.HandoffActorType.SYSTEM_USER,
                permissions=frozenset(
                    {
                        "support:ticket:update",
                        "monitoring:write",
                        "operations:dispatch:write",
                    }
                ),
                context=CommandContext.system(
                    actor=str(dispatcher.id),
                    scope=outage_work_order_handoff.ISSUE_SCOPE,
                    reason="Link is down",
                    idempotency_key="manual-scope-one",
                ),
                manual_target_name="Core link A-B",
            ),
        )

    first = issue()
    replay = issue()
    assert first.work_order.work_order_kind == "infrastructure"
    assert first.work_order.subscriber_id is None
    assert first.work_order.origin_ticket_id == ticket.id
    assert first.work_order.metadata_["infrastructure_target_name"] == "Core link A-B"
    assert replay.work_order.id == first.work_order.id
    assert replay.replayed is True
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.action == "infrastructure_ticket.work_order_issued",
            AuditEvent.entity_id == str(ticket.id),
        )
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "infrastructure_ticket.work_order_issued")
        .count()
        == 1
    )
    assert any(
        item["work_order"].id == first.work_order.id
        for item in web_dispatch_work_orders.list_page(
            db_session, q=first.work_order.public_id
        )["items"]
    )
    assert WorkOrderCommands.assignment_eligibility(
        db_session,
        query=WorkOrderAssignmentEligibilityQuery(
            work_order_public_id=first.work_order.public_id
        ),
    ).allowed


def test_legacy_customerless_infrastructure_ticket_can_issue_field_work(db_session):
    _subscriber, team, _member_id, _customer_ticket = _ticket_with_team(db_session)
    dispatcher, _person = add_bound_staff_user(db_session)
    _grant_admin(db_session, dispatcher.id)
    ticket = Ticket(
        number=f"TKT-{uuid4().hex[:6]}",
        title="Cabinet link issue",
        description="Inspect FDH 12",
        ticket_type="Cabinet Disconnection",
        service_team_id=team.id,
        status="open",
        priority="high",
        metadata_={"base_station_details": "FDH 12"},
    )
    db_session.add(ticket)
    db_session.commit()

    surface = outage_work_order_handoff.ticket_work_order_surface(
        db_session, ticket, actor_id=dispatcher.id
    )
    assert surface is not None
    assert surface.target_label == "FDH 12"
    assert surface.manual_target_name == "FDH 12"
    assert surface.action.allowed
    result = outage_work_order_handoff.issue_from_ticket(
        db_session,
        outage_work_order_handoff.InfrastructureTicketWorkOrderIssueCommand(
            ticket_id=ticket.id,
            request=InfrastructureWorkOrderIssueRequest(
                title="Restore FDH 12", reason="Cabinet link is down"
            ),
            actor_id=dispatcher.id,
            actor_type=outage_work_order_handoff.HandoffActorType.SYSTEM_USER,
            permissions=frozenset(
                {
                    "support:ticket:update",
                    "monitoring:write",
                    "operations:dispatch:write",
                }
            ),
            context=CommandContext.system(
                actor=str(dispatcher.id),
                scope=outage_work_order_handoff.ISSUE_SCOPE,
                reason="Cabinet link is down",
                idempotency_key="legacy-infrastructure-ticket",
            ),
            manual_target_name="FDH 12",
        ),
    )
    assert result.work_order.origin_ticket_id == ticket.id
    assert result.work_order.subscriber_id is None


def test_manual_infrastructure_work_order_requires_target_and_authorized_issuer(
    db_session,
):
    _subscriber, team, _member_id, _customer_ticket = _ticket_with_team(db_session)
    outsider, _person = add_bound_staff_user(db_session)
    ticket = Ticket(
        title="Core link issue",
        ticket_type="Core Link Disconnection",
        service_team_id=team.id,
        status="open",
        is_active=True,
    )
    db_session.add(ticket)
    db_session.commit()

    def command(actor_id, *, target="Core link A-B", key="manual-issuer-check"):
        return outage_work_order_handoff.InfrastructureTicketWorkOrderIssueCommand(
            ticket_id=ticket.id,
            request=InfrastructureWorkOrderIssueRequest(
                title="Restore core link", reason="Link is down"
            ),
            actor_id=actor_id,
            actor_type=outage_work_order_handoff.HandoffActorType.SYSTEM_USER,
            permissions=frozenset(
                {
                    "support:ticket:update",
                    "monitoring:write",
                    "operations:dispatch:write",
                }
            ),
            context=CommandContext.system(
                actor=str(actor_id),
                scope=outage_work_order_handoff.ISSUE_SCOPE,
                reason="Link is down",
                idempotency_key=key,
            ),
            manual_target_name=target,
        )

    surface = outage_work_order_handoff.ticket_work_order_surface(
        db_session, ticket, actor_id=outsider.id
    )
    assert surface is not None
    assert surface.manual_target_name == ""
    assert not surface.action.allowed
    with pytest.raises(outage_work_order_handoff.OutageWorkOrderHandoffError) as denied:
        outage_work_order_handoff.issue_from_ticket(db_session, command(outsider.id))
    assert denied.value.code == "assigned_team_membership_required"

    _grant_admin(db_session, outsider.id)
    with pytest.raises(
        outage_work_order_handoff.OutageWorkOrderHandoffError
    ) as missing:
        outage_work_order_handoff.issue_from_ticket(
            db_session, command(outsider.id, target="", key="manual-missing-target")
        )
    assert missing.value.code == "manual_target_required"

    outsider.user_type = UserType.vendor
    db_session.commit()
    with pytest.raises(outage_work_order_handoff.OutageWorkOrderHandoffError) as vendor:
        outage_work_order_handoff.issue_from_ticket(
            db_session, command(outsider.id, key="manual-vendor-check")
        )
    assert vendor.value.code == "staff_required"
    assert db_session.query(WorkOrder).count() == 0


def test_customer_ticket_with_infrastructure_category_keeps_customer_work_order_path(
    db_session,
):
    _subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    ticket.ticket_type = "Cabinet Disconnection"
    db_session.commit()

    assert (
        outage_work_order_handoff.ticket_work_order_surface(
            db_session, ticket, actor_id=actor_id
        )
        is None
    )
    assert ticket_work_order_handoff.issue_action(
        db_session, ticket, actor_id=actor_id
    ).allowed


def test_removing_manual_policy_stops_legacy_fallback_but_preserves_explicit_origin(
    db_session,
):
    _subscriber, team, actor_id, _customer_ticket = _ticket_with_team(db_session)
    ticket = Ticket(
        title="Core link issue",
        ticket_type="Core Link Disconnection",
        service_team_id=team.id,
        status="open",
        is_active=True,
    )
    explicit_ticket = Ticket(
        title="Another core link issue",
        ticket_type="Core Link Disconnection",
        service_team_id=team.id,
        status="open",
        is_active=True,
        metadata_={
            "infrastructure_selection": {
                "mode": "manual",
                "name": "Core link C-D",
                "ticket_type": "Core Link Disconnection",
            }
        },
    )
    db_session.add_all([ticket, explicit_ticket])
    db_session.commit()
    support_ticket_settings.update_ticket_configuration(
        db_session,
        support_ticket_settings.TicketConfigurationUpdate(
            statuses=("open", "pending"),
            priorities=("normal",),
            ticket_types=("Core Link Disconnection",),
            infrastructure_types=(),
        ),
    )
    assert (
        outage_work_order_handoff.ticket_work_order_surface(
            db_session, ticket, actor_id=actor_id
        )
        is None
    )
    explicit_surface = outage_work_order_handoff.ticket_work_order_surface(
        db_session, explicit_ticket, actor_id=actor_id
    )
    assert explicit_surface is not None
    assert explicit_surface.manual_target_name == "Core link C-D"
    assert explicit_surface.action.allowed


def test_assigned_team_member_issues_many_idempotent_work_orders(db_session):
    subscriber, team, actor_id, ticket = _ticket_with_team(db_session)
    ticket_id = ticket.id
    first = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        idempotency_key="scope-one",
    )
    replay = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        idempotency_key="scope-one",
    )
    second = _issue(
        db_session,
        ticket_id=ticket_id,
        actor_id=actor_id,
        payload=TicketWorkOrderIssueRequest(
            reason="A second feeder trace is required",
            title="Trace secondary feeder",
            work_type="survey",
        ),
        idempotency_key="scope-two",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.work_order.id == first.work_order.id
    assert second.work_order.id != first.work_order.id
    linked = ticket_work_order_handoff.list_for_ticket(db_session, ticket.id)
    assert [row.id for row in linked] == [first.work_order.id, second.work_order.id]
    assert all(row.origin_ticket_id == ticket.id for row in linked)
    assert all(row.subscriber_id == subscriber.id for row in linked)
    assert all(row.crm_ticket_id is None for row in linked)
    assert "work_order_id" not in (ticket.metadata_ or {})
    assert "ticket_id" not in (first.work_order.metadata_ or {})
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "ticket.work_order_issued")
        .filter(AuditEvent.entity_id == str(ticket.id))
        .count()
        == 2
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.created")
        .count()
        == 2
    )
    assert team.id == ticket.service_team_id


def test_ticket_work_order_defaults_to_subscriber_service_address(db_session):
    subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    db_session.add(
        Address(
            subscriber_id=subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
            address_line1="8 Customer Avenue",
            city="Abuja",
        )
    )
    db_session.commit()

    result = _issue(
        db_session,
        ticket_id=ticket.id,
        actor_id=actor_id,
        idempotency_key="service-address-default",
    )

    assert result.work_order.address == "8 Customer Avenue, Abuja"


def test_handoff_rejects_non_member_and_terminal_ticket(db_session):
    _subscriber_row, _team, actor_id, ticket = _ticket_with_team(db_session)

    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as forbidden:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=uuid4(),
            idempotency_key="outsider",
        )
    assert forbidden.value.code == "assigned_team_membership_required"
    assert db_session.query(WorkOrder).count() == 0

    ticket.status = "closed"
    db_session.commit()
    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as terminal:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=actor_id,
            idempotency_key="terminal",
        )
    assert terminal.value.code == "ticket_terminal"


def test_handoff_scopes_field_visit_to_ticket_linked_project_task(db_session):
    subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    project = Project(name="Repair project", subscriber_id=subscriber.id)
    db_session.add(project)
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=ticket.id,
        title="Trace feeder",
        status="todo",
    )
    db_session.add(task)
    db_session.commit()

    result = _issue(
        db_session,
        ticket_id=ticket.id,
        actor_id=actor_id,
        payload=TicketWorkOrderIssueRequest(
            reason="Field trace required",
            project_task_id=task.id,
        ),
        idempotency_key="project-task-scope",
    )

    assert result.work_order.project_id == project.id
    assert result.work_order.project_task_id == task.id
    assert result.work_order.origin_ticket_id == ticket.id


def test_handoff_rejects_project_task_linked_to_another_ticket(db_session):
    subscriber, _team, actor_id, ticket = _ticket_with_team(db_session)
    other_ticket = Ticket(title="Other incident", subscriber_id=subscriber.id)
    project = Project(name="Repair project", subscriber_id=subscriber.id)
    db_session.add_all([project, other_ticket])
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=other_ticket.id,
        title="Other scope",
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(
        ticket_work_order_handoff.TicketWorkOrderHandoffError
    ) as mismatch:
        _issue(
            db_session,
            ticket_id=ticket.id,
            actor_id=actor_id,
            payload=TicketWorkOrderIssueRequest(
                reason="Wrong task",
                project_task_id=task.id,
            ),
            idempotency_key="wrong-task",
        )
    assert mismatch.value.code == "project_task_ticket_mismatch"


def test_generic_work_order_writes_cannot_accept_origin_ticket_id():
    assert "origin_ticket_id" not in WorkOrderHeaderCreate.model_fields
    assert "origin_ticket_id" not in WorkOrderHeaderUpdate.model_fields
