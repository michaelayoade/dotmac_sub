"""Behavior contracts for the event-driven access-policy owner."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.domain_settings import SettingDomain
from app.services import enforcement_event_policy as policy


def _resolver(values):
    def resolve(_db, domain, key):
        return values[(domain, key)]

    return resolve


def test_fup_throttle_decision_uses_canonical_settings(monkeypatch) -> None:
    profile_id = uuid4()
    monkeypatch.setattr(
        policy.settings_spec,
        "resolve_value",
        _resolver(
            {
                (SettingDomain.usage, "fup_action"): "throttle",
                (
                    SettingDomain.usage,
                    "fup_throttle_radius_profile_id",
                ): str(profile_id),
                (
                    SettingDomain.radius,
                    "refresh_sessions_on_profile_change",
                ): True,
            }
        ),
    )

    decision = policy.resolve_fup_event_policy(
        MagicMock(), policy.ResolveFupEventPolicy()
    )

    assert decision.action is policy.FupEnforcementAction.THROTTLE
    assert decision.required_throttle_profile_id() == profile_id
    assert decision.refresh_sessions is True


def test_typed_event_action_overrides_global_policy_without_unrelated_reads(
    monkeypatch,
) -> None:
    resolve = MagicMock(side_effect=AssertionError("settings must not be read"))
    monkeypatch.setattr(policy.settings_spec, "resolve_value", resolve)

    decision = policy.resolve_fup_event_policy(
        MagicMock(),
        policy.ResolveFupEventPolicy(
            requested_action=policy.FupEnforcementAction.SUSPEND
        ),
    )

    assert decision == policy.FupEventPolicyDecision(
        action=policy.FupEnforcementAction.SUSPEND,
        throttle_profile_id=None,
        refresh_sessions=False,
    )
    resolve.assert_not_called()


def test_reduce_speed_alias_is_validated_at_the_owner_boundary() -> None:
    assert (
        policy.parse_fup_action_override("reduce_speed")
        is policy.FupEnforcementAction.THROTTLE
    )

    with pytest.raises(policy.AccessEventPolicyError) as captured:
        policy.parse_fup_action_override({"unexpected": "shape"})

    assert captured.value.code == "access.event_policy.invalid_requested_fup_action"


def test_missing_throttle_profile_fails_visibly(monkeypatch) -> None:
    monkeypatch.setattr(
        policy.settings_spec,
        "resolve_value",
        _resolver(
            {
                (SettingDomain.usage, "fup_action"): "throttle",
                (SettingDomain.usage, "fup_throttle_radius_profile_id"): None,
            }
        ),
    )

    with pytest.raises(policy.AccessEventPolicyError) as captured:
        policy.resolve_fup_event_policy(MagicMock(), policy.ResolveFupEventPolicy())

    assert captured.value.code == "access.event_policy.throttle_profile_required"


def test_boolean_policy_has_no_parallel_default(monkeypatch) -> None:
    monkeypatch.setattr(policy.settings_spec, "resolve_value", lambda *_args: None)

    with pytest.raises(policy.AccessEventPolicyError) as captured:
        policy.resolve_group_routing_policy(MagicMock())

    assert captured.value.code == "access.event_policy.invalid_boolean_setting"
