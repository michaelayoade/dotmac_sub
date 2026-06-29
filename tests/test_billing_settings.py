from pathlib import Path

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
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


def test_save_billing_config_normalizes_valid_policy_values(db_session):
    save_billing_config(
        db_session,
        {
            "billing_enabled": "TRUE",
            "payment_period": "Monthly",
            "billing_day": "05",
            "use_creation_date": "false",
            "payment_due_days": "14",
            "auto_suspend_on_overdue": "true",
            "suspension_grace_hours": "48",
            "expiry_reminder_days": "7",
            "invoice_reminder_days": "7, 1",
            "dunning_escalation_days": "3, 7, 14, 30",
            "blocking_period_days": "0",
            "deactivation_period_days": "30",
            "minimum_balance": "10.50",
            "send_billing_notifications": "false",
            "proforma_enabled": "false",
            "zero_total_invoices": "false",
            "invoice_caching": "true",
        },
    )

    context = get_billing_config_context(db_session)["billing"]

    assert context["billing_enabled"] == "true"
    assert context["payment_period"] == "monthly"
    assert context["billing_day"] == "5"
    assert context["invoice_reminder_days"] == "7,1"
    assert context["dunning_escalation_days"] == "3,7,14,30"
    assert context["minimum_balance"] == "10.50"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("billing_day", "29", "Billing Day must be between 1 and 28."),
        ("minimum_balance", "-1", "Minimum Balance must be at least 0."),
        (
            "invoice_reminder_days",
            "7,today",
            "Invoice Reminder Days must be a comma-separated list of day numbers.",
        ),
    ],
)
def test_save_billing_config_rejects_invalid_policy_values(
    db_session, field, value, message
):
    payload = {
        "billing_enabled": "true",
        "payment_period": "monthly",
        "billing_day": "1",
        "minimum_balance": "0",
        "invoice_reminder_days": "7,1",
    }
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        save_billing_config(db_session, payload)


def test_billing_settings_template_confirms_and_bounds_policy_save():
    template = Path("templates/admin/system/config/billing.html").read_text()

    assert "Save fleet-wide billing settings?" in template
    assert "button[type=submit]').disabled = true" in template
    assert "{% if error %}" in template
    assert 'name="payment_due_days"' in template
    assert 'name="minimum_balance"' in template
    assert 'min="0"' in template
    assert 'max="3650"' in template
