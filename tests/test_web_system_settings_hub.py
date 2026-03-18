from app.services import web_system_settings_hub


def test_settings_hub_includes_branding_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(category for category in context["categories"] if category["id"] == "system")
    branding_link = next(link for link in system_category["links"] if link["url"] == "/admin/system/branding")

    assert branding_link["name"] == "Branding & Assets"


def test_settings_hub_includes_canonical_email_link(db_session):
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(category for category in context["categories"] if category["id"] == "system")
    email_link = next(link for link in system_category["links"] if link["name"] == "Email / SMTP")

    assert email_link["url"] == "/admin/system/email"
