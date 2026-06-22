"""Shared time-of-day gating for billing/dunning enforcement and notifications.

Generalizes the wall-clock window already used by ``PrepaidEnforcement.run``
(``app/services/collections/_core.py``) so the same logic can gate other
customer-impacting paths (postpaid suspension, dunning comms) without each
re-implementing timezone math.

Pure decision helper (``window_block_reason``) + thin settings resolvers. The
celery cadence must fire often enough (hourly) for a window to be effective — a
daily 01:00-UTC run with a 09:00 window would never act. See
``docs/designs/BILLING_ENFORCEMENT_WINDOW.md``.

Timezone note: celery beat fires on the celery *app* timezone (UTC today); the
window comparisons here use the ``scheduler.timezone`` setting (local TZ).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec


def resolve_timezone_name(db: Session) -> str:
    """The configured local timezone for enforcement/notification decisions."""
    value = settings_spec.resolve_value(db, SettingDomain.scheduler, "timezone")
    return str(value) if value else "UTC"


def to_local(db: Session, run_at: datetime) -> datetime:
    """Convert ``run_at`` to the configured local timezone (falls back to as-is)."""
    try:
        return run_at.astimezone(ZoneInfo(resolve_timezone_name(db)))
    except Exception:
        return run_at


def parse_time(value: str | None) -> time | None:
    """Parse an ``HH:MM`` / ``HH:MM:SS`` setting value into a ``time``."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def window_block_reason(
    local_run_at: datetime,
    *,
    start_time: time | None = None,
    end_time: time | None = None,
    skip_weekends: bool = False,
    skip_holidays: list[str] | None = None,
) -> str | None:
    """Why an action should be skipped at ``local_run_at``, or ``None`` to proceed.

    ``local_run_at`` must already be in the target local timezone (see
    ``to_local``). Window semantics:

    * ``start_time`` + ``end_time``, start <= end: act only when
      ``start_time <= now < end_time`` (e.g. 09:00–18:00).
    * ``start_time`` + ``end_time``, start > end: window wraps midnight, act when
      ``now >= start_time`` OR ``now < end_time`` (e.g. 22:00–06:00).
    * ``start_time`` only: act only at/after it (matches the legacy prepaid
      ``blocking_time`` gate).
    * neither: no time gate.

    Weekend/holiday skips apply on top of the time gate. ``skip_holidays`` is a
    list of ISO date strings (``YYYY-MM-DD``).
    """
    now_t = local_run_at.time()
    if start_time is not None and end_time is not None:
        if start_time <= end_time:
            if not (start_time <= now_t < end_time):
                return "outside_window"
        else:  # wraps midnight
            if not (now_t >= start_time or now_t < end_time):
                return "outside_window"
    elif start_time is not None:
        if now_t < start_time:
            return "before_window"

    if skip_weekends and local_run_at.weekday() >= 5:
        return "weekend"
    if skip_holidays and local_run_at.date().isoformat() in skip_holidays:
        return "holiday"
    return None
