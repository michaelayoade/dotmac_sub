"""Tests for within_enforcement_window (phase 6 audit gate)."""

from datetime import UTC, datetime

from app.models.domain_settings import SettingDomain
from app.services import enforcement_window as ew

# 2026-01-05 Monday, 2026-01-10 Saturday
MON_NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
MON_8PM = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
SAT_NOON = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _patch(
    monkeypatch,
    *,
    start=None,
    end=None,
    skip_weekends=False,
    skip_holidays=None,
    tz="UTC",
):
    def fake(db, domain, key):
        if domain == SettingDomain.scheduler and key == "timezone":
            return tz
        if domain == SettingDomain.collections:
            return {
                "enforcement_window_start": start,
                "enforcement_window_end": end,
                "enforcement_skip_weekends": skip_weekends,
                "enforcement_skip_holidays": skip_holidays,
            }.get(key)
        return None

    monkeypatch.setattr(ew.settings_spec, "resolve_value", fake)


def test_unconfigured_is_open(monkeypatch):
    _patch(monkeypatch)  # nothing set
    assert ew.within_enforcement_window(object(), MON_8PM) is True


def test_bounded_window(monkeypatch):
    _patch(monkeypatch, start="09:00", end="17:00")
    assert ew.within_enforcement_window(object(), MON_NOON) is True
    assert ew.within_enforcement_window(object(), MON_8PM) is False


def test_window_respects_timezone(monkeypatch):
    # 09:00-17:00 Africa/Lagos (UTC+1) => 08:00-16:00 UTC
    _patch(monkeypatch, start="09:00", end="17:00", tz="Africa/Lagos")
    # 12:00 UTC = 13:00 WAT -> inside
    assert ew.within_enforcement_window(object(), MON_NOON) is True
    # 16:30 UTC = 17:30 WAT -> outside
    assert (
        ew.within_enforcement_window(object(), datetime(2026, 1, 5, 16, 30, tzinfo=UTC))
        is False
    )


def test_skip_weekends_only(monkeypatch):
    _patch(monkeypatch, skip_weekends=True)
    assert ew.within_enforcement_window(object(), SAT_NOON) is False
    assert ew.within_enforcement_window(object(), MON_NOON) is True


def test_skip_holidays(monkeypatch):
    _patch(monkeypatch, skip_holidays=["2026-01-05"])
    assert ew.within_enforcement_window(object(), MON_NOON) is False
    assert ew.within_enforcement_window(object(), SAT_NOON) is True
