"""The live assignment-rule check that gates Automation Center publication."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.ticket_workflow import TicketAssignmentRule
from app.services import automation_rules
from app.services.ticket_assignment import rules as assignment_rules


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(
        name=f"Conflict Team {uuid4()}", team_type=ServiceTeamType.support.value
    )
    db_session.add(team)
    db_session.flush()
    return team


def _rule(db_session, *, active: bool = True, match_config=None):
    team = _team(db_session)
    rule = TicketAssignmentRule(
        name="Route tickets",
        priority=10,
        is_active=active,
        strategy="round_robin",
        team_id=team.id,
        match_config=match_config
        or {"entity_types": ["ticket"], "priorities": ["urgent"]},
    )
    db_session.add(rule)
    db_session.commit()
    return rule


def _publication(priority: str = "urgent"):
    rule = SimpleNamespace(trigger_key="support.ticket.created")
    version = SimpleNamespace(
        actions=[{"action_key": "support.ticket.assign_service_team"}],
        conditions=[{"field_key": "priority", "operator": "equals", "value": priority}],
    )
    return rule, version


def test_active_urgent_assignment_rule_blocks_publication(db_session):
    rule = _rule(db_session)
    publication, version = _publication("urgent")

    conflicts = automation_rules._live_legacy_rule_conflicts(
        db_session, rule=publication, version=version
    )

    assert [item.rule_id for item in conflicts] == [rule.id]
    assert conflicts[0].surface_key == "support.ticket_assignment_rules"


def test_inactive_rule_or_non_urgent_priority_does_not_block(db_session):
    _rule(db_session, active=False)
    publication, version = _publication("urgent")
    assert (
        automation_rules._live_legacy_rule_conflicts(
            db_session, rule=publication, version=version
        )
        == ()
    )

    _rule(db_session)
    publication, version = _publication("high")
    assert (
        automation_rules._live_legacy_rule_conflicts(
            db_session, rule=publication, version=version
        )
        == ()
    )


def test_rule_restricted_to_other_priorities_does_not_overlap(db_session):
    _rule(db_session, match_config={"entity_types": ["ticket"], "priorities": ["low"]})
    assert (
        assignment_rules.list_automation_center_legacy_conflicts(
            db_session, priority="urgent"
        )
        == ()
    )
