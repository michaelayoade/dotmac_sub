from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.billing_settings import resolve_payment_due_days
from app.services.web_system_config import get_billing_config_context


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
