"""Every active subscription maps to the permanent canonical billing path."""

from decimal import Decimal

from app.services import billing_health
from app.services.billing_health import BillingHealthSnapshot


def test_coverage_sql_executes_without_runtime_control(db_session):
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
    assert _snap(active_subs_on_terminal_account=2).anomalies == []
