from pathlib import Path
from types import SimpleNamespace

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import web_system_config as web_system_config_service
from app.web.admin import system as admin_system


def test_portal_config_saves_only_consumed_domain_routing_keys(db_session):
    web_system_config_service.save_portal_config(
        db_session,
        {
            "selfcare_domain": "selfcare.example.test",
            "selfcare_redirect_root": "/portal/auth/login",
            "admin_domain": "admin.example.test",
            "reseller_domain": "reseller.example.test",
            "portal_language": "fr",
            "show_payment_due": "false",
            "mobile_app_google_play_id": "com.example.app",
        },
    )

    rows = {
        row.key: row.value_text
        for row in db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .all()
    }

    assert rows["selfcare_domain"] == "selfcare.example.test"
    assert rows["selfcare_redirect_root"] == "/portal/auth/login"
    assert rows["admin_domain"] == "admin.example.test"
    assert rows["reseller_domain"] == "reseller.example.test"
    assert "portal_language" not in rows
    assert "show_payment_due" not in rows
    assert "mobile_app_google_play_id" not in rows


def test_portal_config_template_exposes_only_domain_routing_controls():
    template = Path("templates/admin/system/config/portal.html").read_text()

    assert 'name="selfcare_domain"' in template
    assert 'name="selfcare_redirect_root"' in template
    assert 'name="admin_domain"' in template
    assert 'name="reseller_domain"' in template
    assert 'name="portal_language"' not in template
    assert 'name="portal_auth_field"' not in template
    assert 'name="show_payment_due"' not in template
    assert 'name="mobile_app_google_play_id"' not in template


def test_preferences_config_saves_only_consumed_force_2fa(db_session):
    web_system_config_service.save_preferences(
        db_session,
        {
            "force_2fa": "true",
            "default_landing_page": "reseller",
            "admin_portal_title": "Unused Title",
            "search_debounce_ms": "750",
        },
    )

    rows = {
        row.key: row.value_text
        for row in db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .all()
    }

    assert rows["force_2fa"] == "true"
    assert "default_landing_page" not in rows
    assert "admin_portal_title" not in rows
    assert "search_debounce_ms" not in rows


def test_preferences_template_exposes_only_force_2fa_control():
    template = Path("templates/admin/system/config/preferences.html").read_text()

    assert 'name="force_2fa"' in template
    assert 'name="default_landing_page"' not in template
    assert 'name="admin_portal_title"' not in template
    assert 'name="search_debounce_ms"' not in template


def test_monitoring_config_save_redirects_with_error_on_invalid_value(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        admin_system,
        "parse_form_data_sync",
        lambda request: {"server_health_mem_warn_pct": "150"},
    )

    response = admin_system.config_monitoring_save(SimpleNamespace(), db_session)

    assert response.status_code == 303
    assert "error=Server+Health+Memory+Warning" in response.headers["location"]


def test_monitoring_config_template_shows_save_feedback():
    template = Path("templates/admin/system/config/monitoring.html").read_text()

    assert "{% if error %}" in template
    assert "{% elif saved %}" in template
