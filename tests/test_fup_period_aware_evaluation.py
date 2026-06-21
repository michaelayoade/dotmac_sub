"""Period-aware FUP evaluation (#21, A3/A6).

Each rule is measured over its own consumption window: a daily rule must
trigger on daily usage and ignore high monthly usage, and vice versa. The
legacy (no usage_by_period) path still compares every rule to one figure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.fup import evaluate_rules, fup_policies
from app.services.fup_usage import FupUsageWindow, fup_window_bounds

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _window(period: str, used_gb: float) -> FupUsageWindow:
    return FupUsageWindow(
        used_gb=used_gb,
        window=fup_window_bounds(period, NOW),
        source="test",
        is_authoritative=True,
    )


def _policy_daily_and_monthly(db, offer):
    policy = fup_policies.get_or_create(db, str(offer.id))
    fup_policies.add_rule(
        db,
        str(policy.id),
        name="daily-5",
        consumption_period="daily",
        direction="down",
        threshold_amount=5,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
        sort_order=1,
    )
    fup_policies.add_rule(
        db,
        str(policy.id),
        name="monthly-100",
        consumption_period="monthly",
        direction="down",
        threshold_amount=100,
        threshold_unit="gb",
        action="block",
        sort_order=2,
    )
    db.commit()
    return policy


def test_each_rule_uses_its_own_period_window(db_session, catalog_offer):
    _policy_daily_and_monthly(db_session, catalog_offer)
    # daily usage over its 5 GB cap; monthly usage well under its 100 GB cap.
    usage = {"daily": _window("daily", 6.0), "monthly": _window("monthly", 40.0)}
    results = evaluate_rules(
        db_session,
        str(catalog_offer.id),
        current_usage_gb=40.0,
        current_time=NOW,
        usage_by_period=usage,
    )
    by_name = {r["name"]: r for r in results}
    assert by_name["daily-5"]["triggered"] is True  # 6 >= 5 over the daily window
    assert by_name["monthly-100"]["triggered"] is False  # 40 < 100 monthly
    assert by_name["daily-5"]["current_usage_gb"] == 6.0
    assert (
        by_name["daily-5"]["window_end"]
        == fup_window_bounds("daily", NOW).end.isoformat()
    )


def test_daily_rule_ignores_high_monthly_usage(db_session, catalog_offer):
    _policy_daily_and_monthly(db_session, catalog_offer)
    # The key proof: daily usage LOW, monthly usage HIGH.
    usage = {"daily": _window("daily", 2.0), "monthly": _window("monthly", 120.0)}
    results = evaluate_rules(
        db_session,
        str(catalog_offer.id),
        current_usage_gb=120.0,
        current_time=NOW,
        usage_by_period=usage,
    )
    by_name = {r["name"]: r for r in results}
    # Daily rule does NOT fire despite 120 GB this month — it only sees its day.
    assert by_name["daily-5"]["triggered"] is False  # 2 < 5
    assert by_name["monthly-100"]["triggered"] is True  # 120 >= 100


def test_legacy_path_without_usage_by_period_uses_single_figure(
    db_session, catalog_offer
):
    _policy_daily_and_monthly(db_session, catalog_offer)
    results = evaluate_rules(
        db_session,
        str(catalog_offer.id),
        current_usage_gb=8.0,
        current_time=NOW,
    )
    by_name = {r["name"]: r for r in results}
    assert by_name["daily-5"]["triggered"] is True  # 8 >= 5
    assert by_name["monthly-100"]["triggered"] is False  # 8 < 100
    assert by_name["daily-5"]["window_end"] is None  # no window in legacy path
