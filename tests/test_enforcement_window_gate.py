"""Tests for the permanent daily enforcement time window."""

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
    tz="UTC",
):
    def fake(db, domain, key):
        if domain == SettingDomain.scheduler and key == "timezone":
            return tz
        if domain == SettingDomain.collections:
            return {
                "enforcement_window_start": start,
                "enforcement_window_end": end,
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


def test_weekend_uses_the_same_window(monkeypatch):
    _patch(monkeypatch, start="09:00", end="17:00")
    assert ew.within_enforcement_window(object(), SAT_NOON) is True


def test_outside_window_always_defers(monkeypatch):
    _patch(monkeypatch, start="09:00", end="17:00")

    decision = ew.resolve_enforcement_window_decision(object(), MON_8PM)

    assert decision.inside_window is False
    assert decision.block_reason == "outside_window"
    assert decision.should_defer is True
