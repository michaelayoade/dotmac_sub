import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import settings_spec
from app.services.billing_settings import resolve_payment_due_days
from app.services.web_system_config import (
    get_billing_config_context,
    save_billing_config,
)


def _setting(key: str, value: str) -> DomainSetting:
    return DomainSetting(
        domain=SettingDomain.billing,
        key=key,
        value_type=SettingValueType.integer,
        value_text=value,
        is_active=True,
    )


def test_resolve_payment_due_days_uses_canonical_key(db_session):
    db_session.add(_setting("payment_due_days", "21"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 21


def test_resolve_payment_due_days_falls_back_to_legacy_invoice_key(db_session):
    db_session.add(_setting("invoice_due_days", "17"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 17


def test_resolve_payment_due_days_falls_back_to_legacy_default_terms_key(db_session):
    db_session.add(_setting("default_payment_terms_days", "30"))
    db_session.commit()

    assert resolve_payment_due_days(db_session) == 30


def test_billing_config_context_backfills_payment_due_days_from_legacy_key(db_session):
    db_session.add(_setting("invoice_due_days", "9"))
    db_session.commit()

    context = get_billing_config_context(db_session)

    assert context["billing"]["payment_due_days"] == "9"


def test_billing_config_context_backfills_notification_defaults(db_session):
    context = get_billing_config_context(db_session)

    assert context["billing"]["suspension_grace_hours"] == "48"
    assert context["billing"]["expiry_reminder_days"] == "7"
    assert context["billing"]["invoice_reminder_days"] == "7,1"
    assert context["billing"]["dunning_escalation_days"] == "3,7,14,30"


def test_save_billing_config_normalizes_money_and_day_lists(db_session):
    save_billing_config(
        db_session,
        {
            "minimum_balance": "12.3",
            "invoice_reminder_days": "7, 1",
            "dunning_escalation_days": "3, 7,14",
            "blocking_period_days": "5",
        },
    )

    settings = {
        row.key: row.value_text
        for row in db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .all()
    }
    assert settings["minimum_balance"] == "12.30"
    assert settings["invoice_reminder_days"] == "7,1"
    assert settings["dunning_escalation_days"] == "3,7,14"
    assert settings["blocking_period_days"] == "5"


def test_save_billing_config_rejects_invalid_money_and_lists(db_session):
    with pytest.raises(ValueError, match="Minimum balance"):
        save_billing_config(db_session, {"minimum_balance": "-5"})

    with pytest.raises(ValueError, match="Invoice reminder days"):
        save_billing_config(db_session, {"invoice_reminder_days": "7,foo"})


def test_billing_policy_settings_have_registered_specs():
    assert settings_spec.get_spec(SettingDomain.billing, "blocking_period_days")
    assert settings_spec.get_spec(SettingDomain.billing, "deactivation_period_days")
    assert settings_spec.get_spec(SettingDomain.billing, "minimum_balance")
    assert settings_spec.get_spec(SettingDomain.billing, "billing_enabled_expected")
    assert settings_spec.get_spec(
        SettingDomain.billing, "autopay_max_consecutive_failures"
    )
    assert settings_spec.get_spec(SettingDomain.billing, "service_extension_max_days")
    assert settings_spec.get_spec(
        SettingDomain.billing, "payment_arrangement_min_installments"
    )
    assert settings_spec.get_spec(
        SettingDomain.billing, "payment_arrangement_max_installments"
    )
    assert settings_spec.get_spec(
        SettingDomain.billing, "payment_arrangement_default_overdue_installments"
    )
    assert settings_spec.get_spec(SettingDomain.billing, "topup_preset_amounts")
