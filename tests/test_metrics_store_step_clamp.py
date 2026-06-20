"""VictoriaMetrics step clamp (#49).

A query_range whose point count (range/step) exceeds maxPointsPerTimeseries
(default 30000) 422s. _clamp_step_to_points coarsens the step so wide windows
(e.g. 24h at a 1s step = 86,400 points) stay under the cap.
"""

from datetime import UTC, datetime, timedelta

from app.services.metrics_store import (
    _VM_MAX_POINTS,
    _clamp_step_to_points,
    _parse_step_seconds,
)


def _range(hours):
    end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    return end - timedelta(hours=hours), end


def test_parse_step_units():
    assert _parse_step_seconds("1s") == 1
    assert _parse_step_seconds("30s") == 30
    assert _parse_step_seconds("1m") == 60
    assert _parse_step_seconds("5m") == 300
    assert _parse_step_seconds("1h") == 3600
    assert _parse_step_seconds("15") == 15  # bare seconds
    assert _parse_step_seconds("garbage") == 60  # safe default


def test_24h_at_1s_is_coarsened_under_cap():
    start, end = _range(24)  # 86,400 seconds
    clamped = _clamp_step_to_points(start, end, "1s")
    step_s = _parse_step_seconds(clamped)
    assert step_s > 1
    points = (end - start).total_seconds() // step_s
    assert points <= _VM_MAX_POINTS


def test_narrow_range_left_unchanged():
    start, end = _range(1)  # 3600s at 1m = 60 points, well under cap
    assert _clamp_step_to_points(start, end, "1m") == "1m"


def test_never_finer_than_requested():
    start, end = _range(24)
    # a 5m step over 24h is already only 288 points — must stay 5m
    assert _clamp_step_to_points(start, end, "5m") == "5m"
