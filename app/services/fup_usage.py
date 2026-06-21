"""Single source of truth for FUP consumption windows and windowed usage (#21).

One reader, one window definition, multiple backing sources. Enforcement
(`evaluate_fup_rules`), the customer usage summary, and notifications all go
through this module so they can never drift apart. See
docs/designs/FUP_CONSUMPTION_WINDOWS.md.

This file is Phase A1 (window bounds). A2 adds ``get_fup_usage_gb`` (the reader);
B layers durable period buckets behind the same reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.fup import FupConsumptionPeriod


@dataclass(frozen=True)
class FupWindow:
    """An aligned [start, end) consumption window for a FUP rule."""

    period: str  # "daily" | "weekly" | "monthly"
    start: datetime  # UTC instant, inclusive
    end: datetime  # UTC instant, exclusive
    period_key: str  # "2026-06-21" | "2026-W25" | "2026-06"
    timezone: str  # tz the window was aligned to


def period_value(period: FupConsumptionPeriod | str | None) -> str:
    """Normalize a consumption_period (enum or str) to its string value."""
    if isinstance(period, FupConsumptionPeriod):
        return period.value
    value = str(period or "monthly").lower()
    return value if value in {"daily", "weekly", "monthly"} else "monthly"


def fup_window_bounds(
    period: FupConsumptionPeriod | str | None,
    now: datetime,
    tz: ZoneInfo | None = None,
) -> FupWindow:
    """Aligned consumption window for ``period`` containing ``now``.

    - daily: subscriber-local midnight to next local midnight.
    - weekly: subscriber-local Monday 00:00 to the next Monday.
    - monthly: UTC calendar month — matches the billing QuotaBucket so existing
      monthly rules are unchanged (avoids an accidental tz-shifted month).

    daily/weekly align to ``tz`` (subscriber timezone, app-tz fallback) so a
    "per day" cap resets at the customer's local midnight, not UTC midnight.
    """
    p = period_value(period)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_tz = tz or UTC

    if p == "daily":
        local = now.astimezone(local_tz)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start.astimezone(UTC)
        end = (day_start + timedelta(days=1)).astimezone(UTC)
        key = day_start.strftime("%Y-%m-%d")
        tzname = _tz_name(local_tz)
    elif p == "weekly":
        local = now.astimezone(local_tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = midnight - timedelta(days=midnight.weekday())  # Monday
        start = week_start.astimezone(UTC)
        end = (week_start + timedelta(days=7)).astimezone(UTC)
        iso = week_start.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        tzname = _tz_name(local_tz)
    else:  # monthly — UTC calendar month, matches QuotaBucket
        u = now.astimezone(UTC)
        start = datetime(u.year, u.month, 1, tzinfo=UTC)
        end = (
            datetime(u.year + 1, 1, 1, tzinfo=UTC)
            if u.month == 12
            else datetime(u.year, u.month + 1, 1, tzinfo=UTC)
        )
        key = start.strftime("%Y-%m")
        tzname = "UTC"

    return FupWindow(period=p, start=start, end=end, period_key=key, timezone=tzname)


def _tz_name(tz) -> str:
    return getattr(tz, "key", None) or str(tz)
