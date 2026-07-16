"""The operator planner cannot substitute a parallel funding source."""

from __future__ import annotations

from datetime import UTC

import pytest

from scripts.one_off.plan_prepaid_balance_sweep import _parse_datetime


def test_activation_timestamp_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _parse_datetime("2026-07-20T08:00:00", field="activation_at")


def test_activation_timestamp_is_preserved() -> None:
    parsed = _parse_datetime("2026-07-20T08:00:00+01:00", field="activation_at")

    assert parsed.astimezone(UTC).isoformat() == "2026-07-20T07:00:00+00:00"
