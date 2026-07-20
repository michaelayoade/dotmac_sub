"""Sub-monthly FUP safeguards (#21): admin gate + no-data propagation.

Daily/weekly rules are gated behind an explicit opt-in (samples-derived usage
isn't billing-grade), and a window we couldn't measure ("no_data") flows through
evaluation so enforcement skips it instead of acting on a blind zero.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.services.fup import evaluate_rules, fup_policies
from app.services.fup_usage import FupUsageWindow, fup_window_bounds
from app.services.web_fup import _guard_submonthly_period

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def test_monthly_is_never_gated(db_session):
    _guard_submonthly_period(db_session, "monthly")  # no raise


@pytest.mark.parametrize("period", ["daily", "weekly"])
def test_submonthly_gated_by_default(db_session, period):
    with pytest.raises(HTTPException) as ei:
        _guard_submonthly_period(db_session, period)
    assert ei.value.status_code == 400


def test_submonthly_allowed_when_flag_enabled(db_session, monkeypatch):
    # The gate now reads the flag through the control registry
    # (usage.fup_submonthly_rules) rather than settings resolve_value.
    monkeypatch.setattr(
        "app.services.web_fup.control_registry.is_enabled", lambda *a, **k: True
    )
    _guard_submonthly_period(db_session, "daily")  # no raise


def test_no_data_window_propagates_and_does_not_trigger(db_session, catalog_offer):
    policy = fup_policies.get_or_create(db_session, str(catalog_offer.id))
    fup_policies.add_rule(
        db_session,
        str(policy.id),
        name="daily-5",
        consumption_period="daily",
        direction="down",
        threshold_amount=5,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
    )
    db_session.commit()

    no_data = FupUsageWindow(
        used_gb=0.0,
        window=fup_window_bounds("daily", NOW),
        source="no_data",
        is_authoritative=False,
    )
    results = evaluate_rules(
        db_session,
        str(catalog_offer.id),
        current_usage_gb=0.0,
        current_time=NOW,
        usage_by_period={"daily": no_data},
    )
    rule = next(r for r in results if r["name"] == "daily-5")
    assert rule["usage_source"] == "no_data"  # the task skips enforcing on this
    assert rule["triggered"] is False
