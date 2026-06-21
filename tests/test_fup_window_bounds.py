"""FUP consumption window bounds (#21, A1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.fup import FupConsumptionPeriod
from app.services.fup_usage import fup_window_bounds, period_value

LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1, no DST


def test_daily_aligns_to_local_midnight():
    now = datetime(2026, 6, 21, 15, 30, tzinfo=UTC)  # 16:30 Lagos
    w = fup_window_bounds("daily", now, LAGOS)
    # Lagos midnight 2026-06-21 00:00 == 2026-06-20 23:00 UTC
    assert w.start == datetime(2026, 6, 20, 23, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 6, 21, 23, 0, tzinfo=UTC)
    assert w.end - w.start == timedelta(days=1)
    assert w.period_key == "2026-06-21"
    assert w.period == "daily"
    assert w.start <= now < w.end


def test_weekly_aligns_to_monday():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)  # a Sunday
    w = fup_window_bounds("weekly", now, UTC)
    assert w.start == datetime(2026, 6, 15, 0, 0, tzinfo=UTC)  # Monday
    assert w.end == datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    assert w.end - w.start == timedelta(days=7)
    assert w.period_key.startswith("2026-W")
    assert w.start <= now < w.end


def test_monthly_is_utc_calendar_month():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    w = fup_window_bounds(FupConsumptionPeriod.monthly, now, LAGOS)  # tz ignored
    assert w.start == datetime(2026, 6, 1, tzinfo=UTC)
    assert w.end == datetime(2026, 7, 1, tzinfo=UTC)
    assert w.period_key == "2026-06"
    assert w.timezone == "UTC"


def test_monthly_december_rolls_year():
    w = fup_window_bounds("monthly", datetime(2026, 12, 15, tzinfo=UTC), UTC)
    assert w.end == datetime(2027, 1, 1, tzinfo=UTC)


def test_period_value_normalizes():
    assert period_value(FupConsumptionPeriod.daily) == "daily"
    assert period_value("weekly") == "weekly"
    assert period_value(None) == "monthly"
    assert period_value("bogus") == "monthly"


def test_naive_now_treated_as_utc():
    w = fup_window_bounds("monthly", datetime(2026, 6, 21, 12, 0), UTC)
    assert w.start == datetime(2026, 6, 1, tzinfo=UTC)
