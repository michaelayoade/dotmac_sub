from __future__ import annotations

from uuid import uuid4

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
