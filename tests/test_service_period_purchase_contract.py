from app.models.billing import ServiceEntitlement
from app.models.domain_settings import SettingDomain
from app.models.service_period_purchase import (
    OutageCompensationDecision,
    OutageCompensationDecisionInterval,
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchasePeriod,
)
from app.services.settings_spec import get_spec


def test_purchase_contract_has_structural_uniqueness_and_month_cap() -> None:
    table = PrepaidPeriodPurchase.__table__
    constraint_names = {item.name for item in table.constraints}
    assert "uq_prepaid_period_purchase_key" in constraint_names
    assert "uq_prepaid_period_purchase_intent" in constraint_names
    assert "ck_prepaid_period_purchase_count" in constraint_names

    period_names = {
        item.name for item in PrepaidPeriodPurchasePeriod.__table__.constraints
    }
    assert "uq_prepaid_purchase_period_ordinal" in period_names
    assert "uq_prepaid_purchase_period_invoice" in period_names
    assert "uq_prepaid_purchase_period_entitlement" in period_names


def test_outage_interval_can_be_consumed_only_once() -> None:
    decision_names = {
        item.name for item in OutageCompensationDecision.__table__.constraints
    }
    interval_names = {
        item.name for item in OutageCompensationDecisionInterval.__table__.constraints
    }
    entitlement_indexes = {item.name for item in ServiceEntitlement.__table__.indexes}
    assert "uq_outage_compensation_decision_key" in decision_names
    assert "uq_outage_compensation_consumed_interval" in interval_names
    assert "uq_service_entitlements_active_outage_compensation" in entitlement_indexes


def test_feature_flags_default_off_and_policy_limits_are_bounded() -> None:
    purchase = get_spec(SettingDomain.billing, "prepaid_period_purchase_enabled")
    maximum = get_spec(SettingDomain.billing, "prepaid_period_purchase_max_months")
    outage = get_spec(SettingDomain.billing, "outage_compensation_enabled")
    threshold = get_spec(SettingDomain.billing, "outage_compensation_min_hours")

    assert purchase is not None and purchase.default is False
    assert maximum is not None and maximum.default == 12 and maximum.max_value == 12
    assert outage is not None and outage.default is False
    assert threshold is not None and threshold.default == 6 and threshold.min_value == 1
