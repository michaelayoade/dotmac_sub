"""§6.1 billing-path coverage: every active sub maps to an enabled path."""

from __future__ import annotations

from decimal import Decimal

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import billing_health
from app.services.billing_health import BillingHealthSnapshot


def test_prepaid_monthly_flag_defaults_false(db_session):
    assert billing_health._prepaid_monthly_enabled(db_session) is False


def test_prepaid_monthly_flag_reads_setting(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="prepaid_monthly_invoicing_enabled",
            value_text="true",
            value_type=SettingValueType.boolean,
        )
    )
    db_session.commit()
    assert billing_health._prepaid_monthly_enabled(db_session) is True


def test_coverage_sql_executes_both_branches(db_session, monkeypatch):
    # flag OFF branch (default) — SQL must execute on SQLite, empty -> (0, 0)
    monkeypatch.setattr(billing_health, "_prepaid_monthly_enabled", lambda db: False)
    assert billing_health.billing_path_coverage(db_session) == (0, 0)
    # flag ON branch (joins catalog_offers) — also executes, empty -> (0, 0)
    monkeypatch.setattr(billing_health, "_prepaid_monthly_enabled", lambda db: True)
    assert billing_health.billing_path_coverage(db_session) == (0, 0)


def _snap(**kw) -> BillingHealthSnapshot:
    base = dict(
        paid_with_balance_count=0,
        paid_with_balance_total=Decimal("0"),
        last_scanned=100,
        eligible_active_subs=100,
        scan_ratio=1.0,
        payments_24h=10,
        payments_7d_daily_avg=10.0,
        payment_volume_ratio=1.0,
        payment_volume_collapsed=False,
        runners=(),
        covered_but_locked=0,
        unbilled_no_path=0,
        active_subs_on_terminal_account=0,
    )
    base.update(kw)
    return BillingHealthSnapshot(**base)


def test_unbilled_no_path_is_an_anomaly():
    assert "active_subs_without_billing_path" in _snap(unbilled_no_path=5).anomalies


def test_terminal_account_alone_does_not_page():
    # lifecycle drift is exported as a metric but is not a paging anomaly
    assert _snap(active_subs_on_terminal_account=2).anomalies == []
