"""Properties of the SLA interval algebra (OUTAGE_SLA_SPINE §4).

Period scoring is almost entirely this arithmetic, and the defects that matter
live in the INTERACTIONS between operations, not in any one of them. So these
are properties checked over many generated inputs rather than hand-picked
examples: hand-picked cases pass exactly the interactions their author already
thought of.

Generation is deterministic (a fixed seed, whole-minute boundaries in a bounded
window) so a failure is reproducible from the printed case alone. Hypothesis
would give shrinking and would be the better tool; it is not a dependency of
this repository, and adding one mid-slice is a separate decision.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.sla_interval_algebra import (
    Span,
    clamp,
    intersect,
    normalize,
    span_or_none,
    subtract,
    total_seconds,
    union,
)

ORIGIN = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = Span(ORIGIN, ORIGIN + timedelta(days=31))
CASES = 400


def _spans(rng: random.Random, *, count: int | None = None) -> list[Span]:
    """A random set of spans that deliberately includes the boundary cases.

    Purely random minutes almost never produce an EXACT abuttal, and abuttal
    is precisely where a half-open convention can go wrong. A generator that
    never emits one lets a broken `normalize` pass its own disjointness
    property — verified by mutation. So a third of the draws are anchored to a
    previous span's edge, and duplicates are emitted too.
    """
    out: list[Span] = []
    for _ in range(count if count is not None else rng.randint(0, 6)):
        choice = rng.random()
        if out and choice < 0.2:
            # exact abuttal: starts precisely where an existing span ends
            anchor = rng.choice(out)
            start = anchor.end
            length = rng.randint(1, 2_880)
            out.append(Span(start, start + timedelta(minutes=length)))
            continue
        if out and choice < 0.3:
            out.append(rng.choice(out))  # exact duplicate
            continue
        start_min = rng.randint(0, 44_640)  # minutes in 31 days
        length = rng.randint(1, 2_880)
        out.append(
            Span(
                ORIGIN + timedelta(minutes=start_min),
                ORIGIN + timedelta(minutes=start_min + length),
            )
        )
    return out


def _cases(seed: int = 20260804):
    # noqa S311: deterministic test-data generation, not cryptography.
    rng = random.Random(seed)  # noqa: S311
    for _ in range(CASES):
        yield rng, _spans(rng), _spans(rng)


def _is_disjoint_and_sorted(spans) -> bool:
    for a, b in zip(spans, spans[1:], strict=False):
        if a.end >= b.start:  # abutting must have merged, so >= not >
            return False
    return True


# --- canonical form ---------------------------------------------------------


def test_normalize_yields_a_disjoint_sorted_set():
    for _rng, a, _b in _cases():
        result = normalize(a)
        assert _is_disjoint_and_sorted(result), (a, result)


def test_normalize_is_idempotent():
    for _rng, a, _b in _cases():
        once = normalize(a)
        assert normalize(once) == once, a


def test_normalize_preserves_covered_instants():
    """Merging must not gain or lose coverage, only re-express it."""
    for _rng, a, _b in _cases():
        merged = normalize(a)
        for span in a:
            # every original instant is still covered somewhere
            assert any(m.start <= span.start and span.end <= m.end for m in merged), (
                a,
                merged,
            )


# --- union ------------------------------------------------------------------


def test_union_is_order_independent():
    for rng, a, b in _cases():
        shuffled_a = a[:]
        shuffled_b = b[:]
        rng.shuffle(shuffled_a)
        rng.shuffle(shuffled_b)
        assert union(a, b) == union(shuffled_b, shuffled_a), (a, b)


def test_union_is_idempotent():
    for _rng, a, _b in _cases():
        assert union(a, a) == normalize(a), a


def test_union_never_exceeds_the_sum_of_its_parts():
    """Overlap must be counted once — this is the "two concurrent incidents
    became twice the downtime" defect."""
    for _rng, a, b in _cases():
        assert total_seconds(union(a, b)) <= total_seconds(a) + total_seconds(b)


# --- intersect / subtract ---------------------------------------------------


def test_intersect_is_commutative_and_bounded():
    for _rng, a, b in _cases():
        both = intersect(a, b)
        assert both == intersect(b, a), (a, b)
        assert total_seconds(both) <= min(total_seconds(a), total_seconds(b))
        assert _is_disjoint_and_sorted(both)


def test_intersect_with_self_is_the_canonical_form():
    for _rng, a, _b in _cases():
        assert intersect(a, a) == normalize(a), a


def test_subtracting_self_leaves_nothing():
    for _rng, a, _b in _cases():
        assert subtract(a, a) == (), a


def test_subtract_and_intersect_partition_exactly():
    """a == (a ∩ b) ⊎ (a − b), with no gap and no double count."""
    for _rng, a, b in _cases():
        inside = intersect(a, b)
        outside = subtract(a, b)
        assert intersect(inside, outside) == (), (a, b)
        assert union(inside, outside) == normalize(a), (a, b)
        assert total_seconds(inside) + total_seconds(outside) == total_seconds(a)


# --- clamping to a reporting period -----------------------------------------


def test_clamp_never_escapes_the_window():
    for _rng, a, _b in _cases():
        for span in clamp(a, WINDOW):
            assert WINDOW.start <= span.start < span.end <= WINDOW.end


def test_clamp_is_idempotent():
    for _rng, a, _b in _cases():
        once = clamp(a, WINDOW)
        assert clamp(once, WINDOW) == once, a


# --- the scoring identity ---------------------------------------------------


def test_downtime_and_unknown_can_never_exceed_eligible_time():
    """The invariant the score contract depends on: uptime is what remains,
    and it can never be negative."""
    for _rng, confirmed, monitored in _cases():
        eligible = clamp(_spans(random.Random(1), count=2), WINDOW)  # noqa: S311
        if not eligible:
            continue
        downtime = intersect(confirmed, eligible)
        unknown = subtract(eligible, union(monitored, downtime))
        uptime = subtract(eligible, union(downtime, unknown))

        eligible_s = total_seconds(eligible)
        assert total_seconds(downtime) + total_seconds(unknown) <= eligible_s
        assert (
            total_seconds(downtime) + total_seconds(unknown) + total_seconds(uptime)
            == eligible_s
        ), "downtime, unknown and uptime must partition eligible time exactly"


def test_unknown_is_the_absence_of_monitoring_not_uptime():
    """Eligible time with no monitoring evidence is unknown, never uptime."""
    eligible = (Span(ORIGIN, ORIGIN + timedelta(days=10)),)
    monitored = (Span(ORIGIN, ORIGIN + timedelta(days=4)),)
    downtime = ()

    unknown = subtract(eligible, union(monitored, downtime))

    assert total_seconds(unknown) == int(timedelta(days=6).total_seconds())


# --- construction guards ----------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (ORIGIN, ORIGIN),  # empty
        (ORIGIN + timedelta(hours=1), ORIGIN),  # inverted
    ],
)
def test_degenerate_spans_are_rejected(start, end):
    with pytest.raises(ValueError):
        Span(start, end)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        Span(datetime(2026, 8, 1), datetime(2026, 8, 2))  # noqa: DTZ001


def test_span_or_none_coerces_naive_bounds_and_rejects_empty():
    assert span_or_none(None, ORIGIN) is None
    assert span_or_none(ORIGIN, ORIGIN) is None
    assert span_or_none(ORIGIN, ORIGIN - timedelta(hours=1)) is None
    coerced = span_or_none(datetime(2026, 8, 1), datetime(2026, 8, 2))  # noqa: DTZ001
    assert coerced == Span(ORIGIN, ORIGIN + timedelta(days=1))
