"""Stateless interval algebra used by the outage-compensation owner.

This module decides no eligibility, writes no records, and reads no configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TimeInterval:
    starts_at: datetime
    ends_at: datetime


def merge_intervals(intervals: list[TimeInterval]) -> tuple[TimeInterval, ...]:
    ordered = sorted(
        (
            TimeInterval(_utc(item.starts_at), _utc(item.ends_at))
            for item in intervals
            if item.ends_at > item.starts_at
        ),
        key=lambda item: (item.starts_at, item.ends_at),
    )
    merged: list[TimeInterval] = []
    for item in ordered:
        if not merged or item.starts_at > merged[-1].ends_at:
            merged.append(item)
            continue
        previous = merged[-1]
        merged[-1] = TimeInterval(
            previous.starts_at, max(previous.ends_at, item.ends_at)
        )
    return tuple(merged)


def interval_seconds(intervals: tuple[TimeInterval, ...]) -> int:
    return sum(
        int((item.ends_at - item.starts_at).total_seconds()) for item in intervals
    )


def intersect_seconds(
    left: tuple[TimeInterval, ...], right: tuple[TimeInterval, ...]
) -> int:
    total = 0
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        start = max(left[left_index].starts_at, right[right_index].starts_at)
        end = min(left[left_index].ends_at, right[right_index].ends_at)
        if end > start:
            total += int((end - start).total_seconds())
        if left[left_index].ends_at <= right[right_index].ends_at:
            left_index += 1
        else:
            right_index += 1
    return total


def intersect_intervals(
    left: tuple[TimeInterval, ...], right: tuple[TimeInterval, ...]
) -> tuple[TimeInterval, ...]:
    return merge_intervals(
        [
            TimeInterval(max(a.starts_at, b.starts_at), min(a.ends_at, b.ends_at))
            for a in left
            for b in right
            if min(a.ends_at, b.ends_at) > max(a.starts_at, b.starts_at)
        ]
    )


def subtract_intervals(
    source: tuple[TimeInterval, ...], removed: tuple[TimeInterval, ...]
) -> tuple[TimeInterval, ...]:
    remaining: list[TimeInterval] = []
    for item in source:
        cursor = item.starts_at
        for cut in removed:
            if cut.ends_at <= cursor or cut.starts_at >= item.ends_at:
                continue
            if cut.starts_at > cursor:
                remaining.append(TimeInterval(cursor, cut.starts_at))
            cursor = max(cursor, cut.ends_at)
            if cursor >= item.ends_at:
                break
        if cursor < item.ends_at:
            remaining.append(TimeInterval(cursor, item.ends_at))
    return tuple(remaining)
