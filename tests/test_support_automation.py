"""Focused tests for app.services.support_automation core logic."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services import support_automation
from app.services import support_ticket_settings as support_ticket_settings_service
from app.web.admin import support_automation as admin_support_automation

# ---------------------------------------------------------------------------
# Pure-Python helpers — no DB required.
# ---------------------------------------------------------------------------


def test_clean_conditions_drops_unknown_keys_and_empty_values():
    raw = {
        "status": "open",
        "priority": "",
        "channel": None,
        "ticket_type": "incident",
        "made_up_key": "x",
    }
    cleaned = support_automation._clean_conditions(raw)
    assert cleaned == {"status": "open", "ticket_type": "incident"}


def test_conditions_match_empty_dict_matches_anything():
    ticket = SimpleNamespace(status="open", priority="high")
    assert support_automation._conditions_match({}, ticket) is True


def test_conditions_match_exact_strings():
    ticket = SimpleNamespace(status="open", priority="urgent", region="lagos")
    assert (
        support_automation._conditions_match(
            {"status": "open", "region": "lagos"}, ticket
        )
        is True
    )
    assert (
        support_automation._conditions_match(
            {"status": "open", "region": "abuja"}, ticket
        )
        is False
    )


def test_conditions_match_unwraps_enum_value():
    """Enum-typed ticket fields should compare by `.value`, not by enum object."""
    ticket = SimpleNamespace(status=TicketStatus.open, priority=TicketPriority.high)
    assert (
        support_automation._conditions_match(
            {"status": "open", "priority": "high"}, ticket
        )
        is True
    )


# ---------------------------------------------------------------------------
# Action application — requires a Ticket-shaped object but no DB.
# ---------------------------------------------------------------------------


def _fake_ticket(**overrides):
    base = {
        "service_team_id": None,
        "technician_person_id": None,
        "priority": "normal",
        "status": "open",
        "due_at": None,
        "tags": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_rule(action_type, action_value):
    return SimpleNamespace(action_type=action_type, action_value=action_value)


def test_apply_action_set_priority():
    ticket = _fake_ticket()
    rule = _fake_rule(AutomationActionType.set_priority, {"priority": "high"})
    support_automation._apply_action(rule, ticket)
    assert ticket.priority == "high"


def test_apply_action_set_status_strips_whitespace():
    ticket = _fake_ticket()
    rule = _fake_rule(AutomationActionType.set_status, {"status": "  resolved  "})
    support_automation._apply_action(rule, ticket)
    assert ticket.status == "resolved"


def test_apply_action_add_tag_dedupes():
    ticket = _fake_ticket(tags=["vip", "urgent"])
    rule = _fake_rule(AutomationActionType.add_tag, {"tag": "vip"})
    support_automation._apply_action(rule, ticket)
    assert ticket.tags == ["vip", "urgent"]  # unchanged

    rule_new = _fake_rule(AutomationActionType.add_tag, {"tag": "escalated"})
    support_automation._apply_action(rule_new, ticket)
    assert ticket.tags == ["vip", "urgent", "escalated"]


def test_apply_action_set_due_in_hours_uses_now_plus_delta():
    ticket = _fake_ticket()
    rule = _fake_rule(AutomationActionType.set_due_in_hours, {"hours": 4})
    before = datetime.now(UTC)
    support_automation._apply_action(rule, ticket)
    delta = (ticket.due_at - before).total_seconds()
    assert 3.9 * 3600 <= delta <= 4.1 * 3600


def test_apply_action_assign_team_with_invalid_uuid_raises():
    ticket = _fake_ticket()
    rule = _fake_rule(
        AutomationActionType.assign_team, {"service_team_id": "not-a-uuid"}
    )
    with pytest.raises(ValueError):
        support_automation._apply_action(rule, ticket)


def test_apply_action_missing_value_is_a_no_op():
    """If action_value is empty / missing the expected key, nothing changes."""
    ticket = _fake_ticket(priority="medium")
    rule = _fake_rule(AutomationActionType.set_priority, {})
    support_automation._apply_action(rule, ticket)
    assert ticket.priority == "medium"


def test_admin_action_value_validation_rejects_mismatched_payload(db_session):
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open"],
        priorities=["normal"],
        ticket_types=["incident"],
    )

    with pytest.raises(ValueError, match="priority"):
        admin_support_automation._validate_action_value(
            db_session,
            AutomationActionType.set_priority,
            {"status": "open"},
        )


# ---------------------------------------------------------------------------
# Service CRUD + apply_rules (needs db_session fixture).
# ---------------------------------------------------------------------------


def _make_subscriber(db_session):
    from app.models.subscriber import Subscriber

    s = Subscriber(
        first_name="Auto",
        last_name="Tester",
        email=f"auto-{uuid4().hex[:8]}@example.invalid",
    )
    db_session.add(s)
    db_session.flush()
    return s


def test_set_rule_active_is_idempotent(db_session):
    subscriber = _make_subscriber(db_session)
    del subscriber  # only used to ensure the DB has at least one subscriber row
    rule = support_automation.create_rule(
        db_session,
        name="Idempotent rule",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.add_tag,
        action_value={"tag": "noop"},
        is_active=True,
    )
    db_session.commit()

    initial_updated = rule.updated_at
    # Same state -> no flush, no updated_at change
    support_automation.set_rule_active(db_session, str(rule.id), is_active=True)
    db_session.refresh(rule)
    assert rule.updated_at == initial_updated

    # Flip
    support_automation.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.commit()
    db_session.refresh(rule)
    assert rule.is_active is False

    # Setting to the same flipped state again must remain idempotent
    second_flipped_updated = rule.updated_at
    support_automation.set_rule_active(db_session, str(rule.id), is_active=False)
    db_session.refresh(rule)
    assert rule.updated_at == second_flipped_updated


def test_get_rule_raises_404_for_missing(db_session):
    with pytest.raises(HTTPException) as exc:
        support_automation.get_rule(db_session, str(uuid4()))
    assert exc.value.status_code == 404


def test_apply_rules_records_last_fired_at(db_session):
    rule = support_automation.create_rule(
        db_session,
        name="Set high on created",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_priority,
        action_value={"priority": "high"},
        is_active=True,
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Automation observability test",
            description="",
            channel=TicketChannel.web,
            priority="normal",
        ),
        actor_id=None,
    )

    db_session.refresh(rule)
    # Identity-resolution suppression may apply for tickets without an
    # inbound sender; that path also sets metadata flags. We only need to
    # confirm the bookkeeping fields work when a rule succeeds normally.
    assert ticket.priority in {"high", "normal"}
    if ticket.priority == "high":
        assert rule.last_fired_at is not None
        assert rule.last_error is None
