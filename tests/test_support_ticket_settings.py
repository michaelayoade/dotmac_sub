from __future__ import annotations

from app.services import support_ticket_settings as support_ticket_settings_service
from app.services import web_support_tickets as web_support_tickets_service


def test_ticket_settings_defaults_loaded_without_db_rows(db_session):
    assert support_ticket_settings_service.list_status_options(db_session)
    assert support_ticket_settings_service.list_priority_options(db_session)
    assert support_ticket_settings_service.list_ticket_type_options(db_session)


def test_ticket_settings_drive_support_ticket_form_context(db_session):
    support_ticket_settings_service.update_options(
        db_session,
        statuses=["open", "needs_vendor"],
        priorities=["normal", "critical"],
        ticket_types=["incident", "network audit"],
    )

    context = web_support_tickets_service.build_ticket_form_context(db_session)

    assert context["all_statuses"] == ["open", "needs_vendor"]
    assert context["all_priorities"] == ["normal", "critical"]
    assert context["ticket_type_options"] == ["incident", "network audit"]
    assert context["prefill"]["status"] == "open"
    assert context["prefill"]["priority"] == "normal"
