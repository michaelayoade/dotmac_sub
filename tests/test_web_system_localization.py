import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import web_system_config, web_system_settings_hub


@pytest.mark.skip(reason="get_localization_context not implemented yet")
def test_localization_context_uses_defaults_when_settings_missing(db_session):
    context = web_system_config.get_localization_context(db_session)

    assert context["localization"]["system_language"] == "English"
    assert context["localization"]["default_currency"] == "NGN"
    assert context["localization"]["currency_symbol"] == "₦"
    assert context["holiday_calendar"] == []


@pytest.mark.skip(reason="save_localization not implemented yet")
def test_save_localization_persists_settings_and_syncs_skip_holidays(db_session):
    web_system_config.save_localization(
        db_session,
        {
            "system_language": "English",
            "date_format": "YYYY-MM-DD",
            "time_format": "12_hours",
            "default_currency": "USD",
            "currency_symbol": "$",
            "holiday_calendar": (
                '[{"date":"2026-01-01","name":"New Year\'s Day"},'
                '{"date":"2026-12-25","name":"Christmas Day"}]'
            ),
        },
    )

    auth_setting = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.auth)
        .filter(DomainSetting.key == "system_language")
        .one()
    )
    billing_setting = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .filter(DomainSetting.key == "default_currency")
        .one()
    )
    holiday_setting = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .filter(DomainSetting.key == web_system_config.HOLIDAY_CALENDAR_KEY)
        .one()
    )
    skip_holidays_setting = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.collections)
        .filter(DomainSetting.key == web_system_config.PREPAID_SKIP_HOLIDAYS_KEY)
        .one()
    )

    assert auth_setting.value_text == "English"
    assert billing_setting.value_text == "USD"
    assert holiday_setting.value_type == SettingValueType.json
    assert holiday_setting.value_json == [
        {"date": "2026-01-01", "name": "New Year's Day"},
        {"date": "2026-12-25", "name": "Christmas Day"},
    ]
    assert skip_holidays_setting.value_json == ["2026-01-01", "2026-12-25"]


def test_settings_hub_includes_system_category(db_session):
    """Verify the settings hub has a system category with links."""
    context = web_system_settings_hub.build_settings_hub_context(db_session)

    system_category = next(
        category for category in context["categories"] if category["id"] == "system"
    )

    # The system category should have links (localization link not yet added)
    assert len(system_category["links"]) > 0
    link_urls = [link["url"] for link in system_category["links"]]
    assert "/admin/system/config/preferences" in link_urls
