from __future__ import annotations

from uuid import UUID, uuid4

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.ticket_workflow import TicketAssignmentRule
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import web_support_tickets as web_support_tickets_service


def test_ticket_settings_defaults_loaded_without_db_rows(db_session):
    assert support_ticket_settings_service.list_status_options(db_session)
    assert support_ticket_settings_service.list_priority_options(db_session)
    assert support_ticket_settings_service.list_ticket_type_options(db_session)


def test_ticket_settings_drive_support_ticket_form_context(db_session):
    team_id = str(uuid4())
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "needs_vendor"],
        priorities=["normal", "critical"],
        ticket_types=["incident", "network audit"],
        regions=["lagos", "abuja"],
        service_team_ids=[team_id],
        service_team_labels=["Field Ops"],
        status_color_statuses=["open", "needs_vendor"],
        status_color_values=["emerald", "violet"],
    )

    context = web_support_tickets_service.build_ticket_form_context(db_session)

    assert context["all_statuses"] == ["open", "needs_vendor"]
    assert context["all_priorities"] == ["normal", "critical"]
    assert context["ticket_type_options"] == ["incident", "network audit"]
    assert context["region_options"] == ["abuja", "lagos"]
    assert context["service_team_options"] == [{"id": team_id, "label": "Field Ops"}]
    assert context["prefill"]["status"] == "open"
    assert context["prefill"]["priority"] == "normal"


def test_ticket_settings_persist_routing_sla_and_status_colors(db_session):
    team_id = str(uuid4())
    tech_id = str(uuid4())
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "blocked"],
        priorities=["normal"],
        ticket_types=["incident"],
        regions=["north"],
        service_team_ids=[team_id],
        service_team_labels=["Core Network"],
        auto_assign=True,
        routing_regions=["north"],
        routing_technician_person_ids=[tech_id],
        routing_service_team_ids=[team_id],
        sla_priorities=["normal"],
        sla_response_hours=["2"],
        sla_resolution_hours=["12"],
        sla_aging_hours=["6"],
        status_color_statuses=["open", "blocked"],
        status_color_values=["emerald", "red"],
    )

    assert support_ticket_settings_service.auto_assign_enabled(db_session) is True
    assert support_ticket_settings_service.region_assignment_rules(db_session) == {
        "north": {
            "ticket_manager_person_id": None,
            "site_coordinator_person_id": None,
            "technician_person_id": tech_id,
            "service_team_id": team_id,
            "assignee_person_ids": [],
        }
    }
    assert support_ticket_settings_service.sla_policy(db_session)["normal"] == {
        "response_hours": 2,
        "resolution_hours": 12,
        "aging_hours": 6,
    }
    assert support_ticket_settings_service.status_color_options(db_session) == {
        "open": "emerald",
        "blocked": "red",
    }


def test_ticket_settings_sync_service_teams_to_assignment_tables(db_session):
    team_id = str(uuid4())
    member_id = str(uuid4())

    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open"],
        priorities=["normal"],
        ticket_types=["incident"],
        service_team_ids=[team_id],
        service_team_labels=["Field Operations"],
        team_member_team_ids=[team_id],
        team_member_person_ids=[member_id],
    )

    team = db_session.get(ServiceTeam, UUID(team_id))
    assert team is not None
    assert team.name == "Field Operations"
    assert team.is_active is True
    assert (
        db_session.query(ServiceTeamMember)
        .filter(
            ServiceTeamMember.team_id == team.id,
            ServiceTeamMember.person_id == UUID(member_id),
            ServiceTeamMember.is_active.is_(True),
        )
        .count()
        == 1
    )


def test_assignment_rule_create_and_delete(db_session):
    team_id = str(uuid4())
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open"],
        priorities=["normal"],
        ticket_types=["incident"],
        service_team_ids=[team_id],
        service_team_labels=["Support"],
    )

    rule = support_ticket_settings_service.create_assignment_rule(
        db_session,
        name="North incidents",
        priority="50",
        strategy="least_loaded",
        team_id=team_id,
        ticket_types=["incident"],
        regions=["North"],
        assignee_person_id=None,
        assignment_target="technician",
        is_active=True,
    )

    rows = support_ticket_settings_service.list_assignment_rules(db_session)
    assert rows == [
        {
            "id": str(rule.id),
            "name": "North incidents",
            "priority": 50,
            "is_active": True,
            "strategy": "least_loaded",
            "team_id": team_id,
            "team_label": "Support",
            "assignment_target": "technician",
            "assignee_person_id": "",
            "ticket_types": ["incident"],
            "regions": ["north"],
        }
    ]

    support_ticket_settings_service.delete_assignment_rule(db_session, str(rule.id))
    assert db_session.query(TicketAssignmentRule).count() == 0
