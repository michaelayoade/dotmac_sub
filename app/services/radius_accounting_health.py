"""Single source of truth for RADIUS accounting feed freshness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

DEFAULT_STALE_SECONDS = 3600
MIN_STALE_SECONDS = 300


class RadiusAccountingSourceState(StrEnum):
    fresh = "fresh"
    stale = "stale"
    unavailable = "unavailable"
    clock_skew = "clock_skew"


@dataclass(frozen=True)
class RadiusAccountingFreshness:
    state: RadiusAccountingSourceState
    observed_at: datetime | None
    checked_at: datetime
    stale_after_seconds: int
    age_seconds: int | None

    @property
    def fresh(self) -> bool:
        return self.state == RadiusAccountingSourceState.fresh


def stale_after_seconds(db: Session) -> int:
    raw = settings_spec.resolve_value(
        db,
        SettingDomain.usage,
        "radius_accounting_source_stale_seconds",
    )
    try:
        value = int(str(raw)) if raw is not None else DEFAULT_STALE_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_STALE_SECONDS
    return max(value, MIN_STALE_SECONDS)


def assess_freshness(
    observed_at: datetime | None,
    *,
    checked_at: datetime | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> RadiusAccountingFreshness:
    checked = (checked_at or datetime.now(UTC)).astimezone(UTC)
    threshold = max(int(stale_seconds), MIN_STALE_SECONDS)
    if observed_at is None:
        return RadiusAccountingFreshness(
            state=RadiusAccountingSourceState.unavailable,
            observed_at=None,
            checked_at=checked,
            stale_after_seconds=threshold,
            age_seconds=None,
        )
    observed = (
        observed_at.replace(tzinfo=UTC)
        if observed_at.tzinfo is None
        else observed_at.astimezone(UTC)
    )
    age_seconds = int((checked - observed).total_seconds())
    if age_seconds < 0:
        state = RadiusAccountingSourceState.clock_skew
    elif age_seconds > threshold:
        state = RadiusAccountingSourceState.stale
    else:
        state = RadiusAccountingSourceState.fresh
    return RadiusAccountingFreshness(
        state=state,
        observed_at=observed,
        checked_at=checked,
        stale_after_seconds=threshold,
        age_seconds=age_seconds,
    )
