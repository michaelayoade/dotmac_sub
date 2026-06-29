from fastapi.routing import APIRoute

from app.services import web_system_settings_hub
from app.web.admin import system as admin_system


def test_settings_hub_includes_branding_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    branding_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/branding"
    )

    assert branding_link["name"] == "Branding & Assets"


def test_settings_hub_includes_canonical_email_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    email_link = next(
        link for link in system_category["links"] if link["name"] == "Email / SMTP"
    )

    assert email_link["url"] == "/admin/system/email"


def test_settings_hub_does_not_include_removed_data_retention_page(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )

    assert all(
        link["url"] != "/admin/system/config/data-retention"
        for link in system_category["links"]
    )


def test_system_router_does_not_register_removed_data_retention_page():
    registered_paths = {
        route.path for route in admin_system.router.routes if isinstance(route, APIRoute)
    }

    assert "/config/data-retention" not in registered_paths


def test_settings_hub_categories_can_build_query_links(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    category_ids = [category["id"] for category in context["categories"]]

    assert category_ids
    assert all(category_id.strip() for category_id in category_ids)


def test_settings_hub_includes_whats_new_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    whats_new_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/whats-new"
    )

    assert whats_new_link["name"] == "What's New"


def test_settings_hub_includes_ticket_settings_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )
    ticket_settings_link = next(
        link
        for link in system_category["links"]
        if link["url"] == "/admin/system/ticket-settings"
    )

    assert ticket_settings_link["name"] == "Ticket Settings"


def test_settings_hub_includes_bulk_notification_setup_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    notifications_category = next(
        category
        for category in context["categories"]
        if category["id"] == "notifications"
    )
    bulk_setup_link = next(
        link
        for link in notifications_category["links"]
        if link["url"] == "/admin/notifications/setup"
    )

    assert bulk_setup_link["name"] == "Bulk Notification Setup"
