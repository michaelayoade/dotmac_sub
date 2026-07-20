from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import (
    SlaBreach,
    SlaClockStatus,
    SlaPolicy,
    WorkflowEntityType,
)
from app.services.sla_assignment import (
    check_sla_breaches,
    create_sla_clock_for_ticket,
    latest_ticket_sla_clocks,
    priority_sla_target_minutes,
    resolve_ticket_sla_target_minutes,
    ticket_sla_status,
    ticket_type_sla_target_minutes,
    update_sla_clocks_for_status_change,
)


def _policy(db_session) -> SlaPolicy:
    policy = SlaPolicy(
        name="Ticket Resolution SLA",
        entity_type=WorkflowEntityType.ticket.value,
        description="Default CRM ticket SLA policy",
        is_active=True,
    )
    db_session.add(policy)
    db_session.flush()
    return policy


def test_ticket_type_sla_targets_match_crm_windows():
    assert ticket_type_sla_target_minutes("Customer Link Disconnection") == 24 * 60
    assert ticket_type_sla_target_minutes("Core Link Disconnection") == 48 * 60
    assert ticket_type_sla_target_minutes("Billing Request") is None


def test_priority_sla_target_uses_sub_support_policy(db_session):
    assert priority_sla_target_minutes(db_session, "urgent") == 8 * 60
    assert priority_sla_target_minutes(db_session, "normal") == 72 * 60
    assert priority_sla_target_minutes(db_session, "unknown") is None


def test_ticket_type_sla_target_wins_over_priority_policy(db_session):
    assert (
        resolve_ticket_sla_target_minutes(
            db_session,
            priority="urgent",
            ticket_type="Core Link Disconnection",
        )
        == 48 * 60
    )


def test_create_sla_clock_for_known_ticket_type(db_session):
    _policy(db_session)
    created_at = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
    ticket = Ticket(
        title="Cabinet down",
        status=TicketStatus.open.value,
        priority="urgent",
        ticket_type="Cabinet Disconnection",
        created_at=created_at,
    )
    db_session.add(ticket)
    db_session.commit()

    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.commit()

    assert clock is not None
    assert clock.status == SlaClockStatus.running.value
    due_at = clock.due_at if clock.due_at.tzinfo else clock.due_at.replace(tzinfo=UTC)
    assert due_at == created_at + timedelta(hours=24)


def test_create_sla_clock_for_priority_policy_ticket(db_session):
    _policy(db_session)
    created_at = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
    ticket = Ticket(
        title="General support request",
        status=TicketStatus.open.value,
        priority="normal",
        ticket_type="Billing Request",
        created_at=created_at,
        due_at=created_at + timedelta(hours=72),
    )
    db_session.add(ticket)
    db_session.commit()

    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.commit()

    assert clock is not None
    due_at = clock.due_at if clock.due_at.tzinfo else clock.due_at.replace(tzinfo=UTC)
    ticket_due_at = (
        ticket.due_at if ticket.due_at.tzinfo else ticket.due_at.replace(tzinfo=UTC)
    )
    assert due_at == ticket_due_at


def test_status_change_completes_open_sla_clock(db_session):
    _policy(db_session)
    ticket = Ticket(
        title="Core down",
        status=TicketStatus.open.value,
        ticket_type="Core Link Disconnection",
    )
    db_session.add(ticket)
    db_session.commit()
    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.flush()

    ticket.status = TicketStatus.closed.value
    update_sla_clocks_for_status_change(
        db_session, ticket, TicketStatus.open.value, TicketStatus.closed.value
    )
    db_session.commit()

    assert clock is not None
    assert clock.status == SlaClockStatus.completed.value
    assert clock.completed_at is not None


def test_status_change_completes_resolution_confirmation_clock(db_session):
    _policy(db_session)
    ticket = Ticket(
        title="Waiting for confirmation",
        status=TicketStatus.open.value,
        priority="normal",
    )
    db_session.add(ticket)
    db_session.commit()
    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.flush()

    ticket.status = TicketStatus.pending_confirmation.value
    update_sla_clocks_for_status_change(
        db_session,
        ticket,
        TicketStatus.open.value,
        TicketStatus.pending_confirmation.value,
    )
    db_session.commit()

    assert clock is not None
    assert clock.status == SlaClockStatus.completed.value
    assert clock.completed_at is not None


def test_check_sla_breaches_records_open_breach(db_session):
    _policy(db_session)
    ticket = Ticket(
        title="Expired cabinet SLA",
        status=TicketStatus.open.value,
        ticket_type="Cabinet Disconnection",
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    db_session.add(ticket)
    db_session.commit()
    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.commit()

    breached = check_sla_breaches(db_session, ticket.id)
    db_session.commit()

    assert breached == [clock]
    assert clock.status == SlaClockStatus.breached.value
    assert (
        db_session.query(SlaBreach).filter(SlaBreach.clock_id == clock.id).count() == 1
    )


def test_latest_ticket_sla_clocks_and_status_read_model(db_session):
    _policy(db_session)
    created_at = datetime.now(UTC) - timedelta(days=4)
    ticket = Ticket(
        title="Expired normal ticket",
        status=TicketStatus.open.value,
        priority="normal",
        created_at=created_at,
        due_at=created_at + timedelta(hours=72),
    )
    db_session.add(ticket)
    db_session.commit()
    clock = create_sla_clock_for_ticket(db_session, ticket)
    db_session.commit()

    clocks = latest_ticket_sla_clocks(db_session, [ticket.id])
    state = ticket_sla_status(
        ticket,
        clock=clocks[str(ticket.id)],
        now=created_at + timedelta(hours=73),
    )

    assert clock is not None
    assert clocks[str(ticket.id)] == clock
    assert state["breached"] is True
    assert state["minutes_remaining"] == -60
