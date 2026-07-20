"""Shared UI projection contracts (State, KPI, Action)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.schemas.status_presentation import StatusIcon, StatusTone
from app.services.ui_contracts import Action, Kpi, StateKind, StateValue


def test_state_value_present_and_stale_are_renderable():
    now = datetime(2026, 7, 18, tzinfo=UTC)
    present = StateValue.present(42, as_of=now)
    assert present.kind is StateKind.present
    assert present.is_present is True
    assert present.is_stale is False
    assert present.value == 42
    assert present.as_of == now
    assert present.placeholder == ""

    stale = StateValue.stale(41, as_of=now)
    assert stale.is_present is True
    assert stale.is_stale is True


def test_state_value_absent_kinds_are_distinct_and_not_zero():
    unknown = StateValue.unknown()
    unavailable = StateValue.unavailable()
    na = StateValue.not_applicable()

    for sv in (unknown, unavailable, na):
        assert sv.is_present is False
        assert sv.value is None  # never a zero standing in for the unknown

    # The three absent states stay distinct.
    assert unknown.kind is StateKind.unknown
    assert unavailable.kind is StateKind.unavailable
    assert na.kind is StateKind.not_applicable
    assert unknown.placeholder == "Unknown"
    assert unavailable.placeholder == "Unavailable"
    assert na.placeholder == "—"


def test_kpi_carries_value_state_and_cohort_url():
    kpi = Kpi(
        label="Overdue",
        value=StateValue.present(3),
        cohort_url="/admin/billing?status=overdue",
        tone=StatusTone.warning,
        icon=StatusIcon.alert,
        unit="invoices",
    )
    assert kpi.value.is_present
    assert kpi.cohort_url == "/admin/billing?status=overdue"
    assert kpi.tone is StatusTone.warning
    # An unknown KPI defaults to neutral tone and renders unknown, not zero.
    blank = Kpi(
        label="Balance",
        value=StateValue.unavailable(),
        cohort_url="/admin/billing",
    )
    assert blank.tone is StatusTone.neutral
    assert blank.value.placeholder == "Unavailable"


def test_action_eligibility_and_confirmation_policy_are_distinct():
    allowed = Action(
        key="restore",
        label="Restore",
        allowed=True,
        permission="operations:dispatch:write",
        affected=2,
    )
    assert allowed.allowed is True
    assert allowed.requires_confirmation is False
    assert allowed.tone is StatusTone.neutral
    assert allowed.reason is None

    blocked = Action(
        key="disable",
        label="Disable",
        allowed=False,
        reason="Account already disabled",
        preview_url="/reseller/accounts/1/disable/preview",
        tone=StatusTone.negative,
        requires_confirmation=True,
    )
    assert blocked.allowed is False
    assert blocked.reason == "Account already disabled"
    assert blocked.tone is StatusTone.negative
    assert blocked.requires_confirmation is True


def test_contracts_reject_contradictory_state_and_action_shapes():
    with pytest.raises(ValueError, match="cannot carry"):
        StateValue(StateKind.unknown, value=0)
    with pytest.raises(ValueError, match="requires a value"):
        StateValue.present(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        StateValue.present(1, as_of=datetime(2026, 7, 18))
    with pytest.raises(ValueError, match="cohort URL"):
        Kpi(label="Orphan", value=StateValue.present(1), cohort_url="")
    with pytest.raises(ValueError, match="Blocked action requires"):
        Action(key="disable", label="Disable", allowed=False)
    with pytest.raises(ValueError, match="declared together"):
        Action(
            key="refund",
            label="Refund",
            allowed=True,
            requires_confirmation=True,
        )
