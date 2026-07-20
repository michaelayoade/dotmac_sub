"""Billing health threshold setting registration and resolution."""

from decimal import Decimal

from app.models.domain_settings import SettingDomain
from app.services import billing_health
from app.services.billing_health import BillingHealthSnapshot
from app.services.settings_spec import get_spec


def _snap(**overrides) -> BillingHealthSnapshot:
    values = {
        "paid_with_balance_count": 0,
        "paid_with_balance_total": Decimal("0"),
        "last_scanned": 100,
        "eligible_active_subs": 100,
        "scan_ratio": 1.0,
        "payments_24h": 10,
        "payments_7d_daily_avg": 10.0,
        "payment_volume_ratio": 1.0,
        "payment_volume_collapsed": False,
    }
    values.update(overrides)
    return BillingHealthSnapshot(**values)


def test_billing_health_threshold_settings_registered_and_resolved(monkeypatch):
    expected = {
        "billing_health_scan_min_ratio": "0.5",
        "billing_health_payment_volume_min_ratio": "0.4",
        "billing_health_payment_baseline_min_daily": "5.0",
    }
    for key, default in expected.items():
        spec = get_spec(SettingDomain.billing, key)
        assert spec is not None
        assert spec.default == default

    def fake_resolve_value(_db, _domain, key):
        return {
            "billing_health_scan_min_ratio": "0.7",
            "billing_health_payment_volume_min_ratio": "0.9",
            "billing_health_payment_baseline_min_daily": "3.0",
        }[key]

    monkeypatch.setattr(
        billing_health.settings_spec, "resolve_value", fake_resolve_value
    )

    thresholds = billing_health._health_thresholds(object())

    assert thresholds == (0.7, 0.9, 3.0)
    assert (
        "invoice_scan_count_low"
        in _snap(scan_ratio=0.6, scan_min_ratio=thresholds[0]).anomalies
    )
