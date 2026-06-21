"""Single source of truth for FUP consumption windows and windowed usage (#21).

One reader, one window definition, multiple backing sources. Enforcement
(`evaluate_fup_rules`), the customer usage summary, and notifications all go
through this module so they can never drift apart. See
docs/designs/FUP_CONSUMPTION_WINDOWS.md.

This file is Phase A1 (window bounds). A2 adds ``get_fup_usage_gb`` (the reader);
B layers durable period buckets behind the same reader.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

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


@dataclass(frozen=True)
class FupUsageWindow:
    """Windowed FUP usage: the single value enforcement, UI, and notifications
    all read. ``source`` records which backing store answered (so A/B drift can
    be observed); ``is_authoritative`` is False when derived from possibly-sparse
    samples — callers should avoid a hard throttle on non-authoritative zero."""

    used_gb: float
    window: FupWindow
    source: str  # "quota_bucket" | "samples" | "fallback"
    is_authoritative: bool


async def _resolve_fup_usage(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime,
    tz: ZoneInfo | None,
) -> FupUsageWindow:
    # Lazy import keeps fup_usage importable without pulling the bandwidth/VM
    # stack at module load, and avoids any import cycle with usage_summary.
    from app.services.usage_summary import (
        _GB_BYTES,
        _current_bucket_used_gb,
        _subscriber_tz,
        windowed_used_bytes,
    )

    p = period_value(period)
    if tz is None:
        tz = _subscriber_tz(db, str(subscription.subscriber_id))
    window = fup_window_bounds(p, now, tz)

    if p == "monthly":
        # Authoritative billing-cycle usage from the rated quota bucket.
        used = _current_bucket_used_gb(db, subscription.id)
        return FupUsageWindow(
            used_gb=float(used or 0.0),
            window=window,
            source="quota_bucket",
            is_authoritative=used is not None,
        )

    total_bytes, authoritative = await windowed_used_bytes(
        db, [subscription.id], window.start, window.end, tz
    )
    return FupUsageWindow(
        used_gb=total_bytes / _GB_BYTES,
        window=window,
        source="samples",
        is_authoritative=authoritative,
    )


async def get_fup_usage_gb_async(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> FupUsageWindow:
    """Windowed FUP usage for one subscription. Await this from async callers
    (e.g. the customer usage-summary endpoint)."""
    return await _resolve_fup_usage(
        db, subscription, period, now or datetime.now(UTC), tz
    )


def get_fup_usage_gb(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> FupUsageWindow:
    """Sync entry for the Celery FUP-evaluation task. Bridges the async reader
    via asyncio.run — do NOT call from within a running event loop (use
    ``get_fup_usage_gb_async`` there)."""
    return asyncio.run(
        _resolve_fup_usage(db, subscription, period, now or datetime.now(UTC), tz)
    )


def build_usage_by_period(
    db: Session,
    subscription,
    offer_id: str,
    now: datetime,
    monthly_used_gb: float,
) -> dict[str, FupUsageWindow]:
    """Usage per consumption period needed by an offer's active FUP rules.

    monthly reuses the already-resolved billing-bucket figure (authoritative, no
    extra query); daily/weekly come from the windowed reader. Keyed by the
    period string so ``evaluate_rules`` can look each rule up by its
    ``consumption_period``. Sync — call only from the Celery evaluation task.
    """
    from app.services.fup import FupPolicies

    policy = FupPolicies.get_by_offer(db, offer_id)
    periods = (
        {period_value(r.consumption_period) for r in policy.rules if r.is_active}
        if policy
        else set()
    )
    out: dict[str, FupUsageWindow] = {}
    for p in periods:
        if p == "monthly":
            out["monthly"] = FupUsageWindow(
                used_gb=float(monthly_used_gb),
                window=fup_window_bounds("monthly", now),  # monthly ignores tz
                source="quota_bucket",
                is_authoritative=True,
            )
        else:
            out[p] = get_fup_usage_gb(db, subscription, p, now=now)
    return out
