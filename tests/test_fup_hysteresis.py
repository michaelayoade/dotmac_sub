"""FUP transition-driven enforcement / hysteresis (review task #12).

A subscription that is ALREADY throttled must not re-emit usage_exhausted (and
thus re-apply the RADIUS profile / SSH address-list) on every sweep tick. The
event fires only on entering the state, or after cooldown_minutes elapses.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4


def _run(prior_action_status):
    sub = MagicMock(id=uuid4(), offer_id=uuid4(), subscriber_id=uuid4())
    lock_session = MagicMock()
    lock_session.bind.dialect.name = "sqlite"
    session = MagicMock()
    query = session.query.return_value
    joined = query.join.return_value.outerjoin.return_value
    joined.filter.return_value.filter.return_value.all.return_value = [sub]

    state = None
    if prior_action_status is not None:
        state = MagicMock()
        state.action_status.value = prior_action_status
        state.cap_resets_at = None  # no period reset
        state.last_evaluated_at = None
    fup_state_mock = MagicMock()
    fup_state_mock.get.return_value = state

    bucket = MagicMock(used_gb=50, period_end=None)
    rule_result = {
        "triggered": True,
        "action": "reduce_speed",
        "rule_id": str(uuid4()),
        "threshold_gb": 10.0,
        "name": "r",
        "cooldown_minutes": 0,
    }
    emitted = []

    with (
        patch("app.tasks.usage.SessionLocal", side_effect=[lock_session, session]),
        patch("app.services.fup_state.fup_state", fup_state_mock),
        patch(
            "app.services.usage._resolve_or_create_quota_bucket",
            return_value=bucket,
        ),
        patch("app.services.fup.evaluate_rules", return_value=[rule_result]),
        patch("app.services.events.emit_event", lambda *a, **k: emitted.append(a)),
        patch("app.services.settings_spec.resolve_value", return_value=None),
        patch("app.tasks.usage._maybe_queue_repeat_upsell", lambda *a, **k: None),
    ):
        from app.tasks.usage import evaluate_fup_rules

        result = evaluate_fup_rules()
    return result, emitted


def test_enforces_on_transition_into_throttled():
    result, emitted = _run(prior_action_status=None)
    assert result["enforced"] == 1
    assert len(emitted) == 1
    assert emitted[0][2]["action"] == "reduce_speed"


def test_does_not_re_enforce_when_already_throttled():
    result, emitted = _run(prior_action_status="throttled")
    # Already throttled, cooldown 0 → no re-emit, no re-enforce this tick.
    assert result["enforced"] == 0
    assert emitted == []
