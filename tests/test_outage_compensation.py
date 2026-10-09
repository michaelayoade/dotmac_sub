from datetime import UTC, datetime, timedelta

from app.services.outage_interval_algebra import (
    TimeInterval,
    intersect_intervals,
    intersect_seconds,
    interval_seconds,
    merge_intervals,
    subtract_intervals,
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


def test_later_finalization_awards_only_previously_uncredited_clock_time() -> None:
    previously_credited = merge_intervals([TimeInterval(_at(0), _at(8))])
    all_evidence = merge_intervals(
        [
            TimeInterval(_at(0), _at(8)),
            TimeInterval(_at(4), _at(12)),
        ]
    )
    funded = merge_intervals([TimeInterval(_at(0), _at(24))])
    delta = subtract_intervals(
        intersect_intervals(all_evidence, funded), previously_credited
    )
    assert delta == (TimeInterval(_at(8), _at(12)),)
    assert interval_seconds(previously_credited) + interval_seconds(delta) == 12 * 3600


def test_range_subtraction_preserves_holes_and_exact_seconds() -> None:
    end = _at(12) + timedelta(seconds=19)
    delta = subtract_intervals(
        (TimeInterval(_at(0), end),),
        merge_intervals([TimeInterval(_at(2), _at(4)), TimeInterval(_at(7), _at(9))]),
    )
    assert delta == (
        TimeInterval(_at(0), _at(2)),
        TimeInterval(_at(4), _at(7)),
        TimeInterval(_at(9), end),
    )
    assert interval_seconds(delta) == 8 * 3600 + 19


def test_range_algebra_matches_discrete_reference_for_all_small_intervals() -> None:
    from itertools import combinations

    candidates = [
        TimeInterval(_at(start), _at(end)) for start, end in combinations(range(6), 2)
    ]
    for first in candidates:
        for second in candidates:
            source = (first,)
            removed = (second,)
            expected = {
                hour
                for hour in range(5)
                if first.starts_at <= _at(hour) < first.ends_at
                and not second.starts_at <= _at(hour) < second.ends_at
            }
            assert (
                interval_seconds(subtract_intervals(source, removed))
                == len(expected) * 3600
            )
