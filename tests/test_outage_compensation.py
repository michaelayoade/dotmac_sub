from datetime import UTC, datetime, timedelta

from app.services.outage_compensation import (
    TimeInterval,
    intersect_seconds,
    interval_seconds,
    merge_intervals,
)


def _at(hours: int) -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=hours)


def test_overlapping_outages_are_unioned_without_double_counting() -> None:
    merged = merge_intervals(
        [
            TimeInterval(_at(0), _at(8)),
            TimeInterval(_at(4), _at(10)),
            TimeInterval(_at(10), _at(12)),
        ]
    )
    assert merged == (TimeInterval(_at(0), _at(12)),)
    assert interval_seconds(merged) == 12 * 3600


def test_compensation_is_capped_to_funded_service_overlap() -> None:
    outage = merge_intervals([TimeInterval(_at(0), _at(12))])
    funded = merge_intervals(
        [TimeInterval(_at(2), _at(5)), TimeInterval(_at(8), _at(10))]
    )
    assert intersect_seconds(outage, funded) == 5 * 3600


def test_disjoint_outages_remain_separate_decisions() -> None:
    merged = merge_intervals(
        [TimeInterval(_at(0), _at(6)), TimeInterval(_at(7), _at(13))]
    )
    assert merged == (
        TimeInterval(_at(0), _at(6)),
        TimeInterval(_at(7), _at(13)),
    )
