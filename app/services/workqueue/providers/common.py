"""Helpers shared by providers."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ItemKind, Urgency

#: Legacy numeric priority (lower = more important) kept on the item for
#: API back-compat. Ranking uses ``score``, not this.
_LEGACY_PRIORITY = {
    "urgent": 10,
    "high": 20,
    "normal": 40,
    "medium": 40,
    "low": 70,
    "lower": 80,
}


def legacy_priority(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    return _LEGACY_PRIORITY.get(str(value or "").lower(), 50)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat stored times as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def seconds_until(due: datetime | None, now: datetime) -> float | None:
    due_utc = as_utc(due)
    if due_utc is None:
        return None
    return (due_utc - now).total_seconds()


def best_reason(candidates: list[tuple[int, str]]) -> tuple[int, str]:
    """Highest-scoring reason wins; ties break on the reason name for stability."""
    return max(candidates, key=lambda pair: (pair[0], pair[1]))


def score_item(
    candidates: list[tuple[int, str]],
    config: WorkqueueScoringConfig,
) -> tuple[int, str, Urgency]:
    score, reason = best_reason(candidates)
    return score, reason, config.urgency_for_score(score)


def kind_sort_key(kind: ItemKind, config: WorkqueueScoringConfig) -> int:
    return config.kind_rank(kind)
