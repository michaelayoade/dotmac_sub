"""FUP enforcement hardening:

- reduce_speed with no throttle profile configured must NOT tell the customer
  their speed was reduced (the handler silently no-ops); surface it instead.
- a queue-independent safety-net lifts FUP enforcement past its reset boundary
  even when the billing queue (where evaluate_fup_rules runs) is stalled.

Driven with mocks (the established pattern for tasks in test_celery_tasks.py /
test_fup_evaluate_commits.py): the tasks use the production ``SessionLocal`` +
``commit()``, which the rollback-isolated ``db_session`` fixture can't host.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4


def _sub():
    return MagicMock(id=uuid4(), offer_id=uuid4(), subscriber_id=uuid4())


def _settings_side_effect(*, throttle_profile):
    def _resolve(session, domain, key):
        if key == "fup_throttle_radius_profile_id":
            return throttle_profile
        if key == "usage_warning_thresholds":
            return "0.8"
        return None

    return _resolve


def _triggered_reduce_speed():
    return {
        "triggered": True,
        "action": "reduce_speed",
        "rule_id": str(uuid4()),
        "name": "Monthly cap",
        "threshold_gb": 100,
        "consumption_period": "monthly",
        "usage_source": "rated",
        "is_authoritative": True,
        "current_usage_gb": 500,
        "window_end": None,
    }


def _run_evaluate(*, throttle_profile, should_enforce=True):
    sub = _sub()
    session = MagicMock()
    query = session.query.return_value
    joined = query.join.return_value.outerjoin.return_value
    joined.filter.return_value.filter.return_value.all.return_value = [sub]
    fup_state_mock = MagicMock()
    fup_state_mock.get.return_value = None  # prior_status "none"
    bucket = MagicMock(used_gb=500, period_end=None)
    emit_mock = MagicMock()
    notif_mock = MagicMock(return_value=0)

    with (
        patch("app.tasks.usage.SessionLocal", return_value=session),
        patch("app.services.fup_state.fup_state", fup_state_mock),
        patch(
            "app.services.usage._resolve_or_create_quota_bucket", return_value=bucket
        ),
        patch(
            "app.services.fup.evaluate_rules",
            return_value=[_triggered_reduce_speed()],
        ),
        patch(
            "app.services.fup_usage.build_usage_by_period",
            return_value={"monthly": 500},
        ),
        patch(
            "app.services.settings_spec.resolve_value",
            side_effect=_settings_side_effect(throttle_profile=throttle_profile),
        ),
        patch("app.services.events.emit_event", emit_mock),
        patch("app.tasks.usage._emit_fup_notifications", notif_mock),
        patch("app.tasks.usage._fup_should_enforce", return_value=should_enforce),
    ):
        from app.tasks.usage import evaluate_fup_rules

        result = evaluate_fup_rules()
    return result, emit_mock, notif_mock


def test_reduce_speed_without_profile_skips_notification_and_counts():
    result, emit_mock, notif_mock = _run_evaluate(throttle_profile=None)

    assert result["throttle_unconfigured"] == 1
    assert result["enforced"] == 0
    # No usage_exhausted event (the handler would only no-op) ...
    emit_mock.assert_not_called()
    # ... and no "throttled" customer notification queued.
    notif_mock.assert_called_once()
    pending_arg = notif_mock.call_args[0][1]
    assert pending_arg == []


def test_reduce_speed_with_profile_still_notifies():
    """Positive control: when a throttle profile IS set, behaviour is unchanged."""
    result, emit_mock, notif_mock = _run_evaluate(throttle_profile="profile-123")

    assert result["throttle_unconfigured"] == 0
    assert result["enforced"] == 1
    emit_mock.assert_called_once()
    pending_arg = notif_mock.call_args[0][1]
    assert len(pending_arg) == 1
    assert pending_arg[0]["kind"] == "throttled"


def _run_safety_net(*, lift_results):
    session = MagicMock()
    states = [MagicMock(subscription_id=uuid4()) for _ in lift_results]
    fup_state_mock = MagicMock()
    fup_state_mock.list_pending_reset.return_value = states
    lift_mock = MagicMock(side_effect=lift_results)

    with (
        patch("app.tasks.usage.SessionLocal", return_value=session),
        patch("app.services.fup_state.fup_state", fup_state_mock),
        patch("app.services.enforcement.lift_fup_enforcement", lift_mock),
    ):
        from app.tasks.usage import lift_expired_fup_enforcement

        result = lift_expired_fup_enforcement()
    return result, session, lift_mock


def test_safety_net_lifts_each_pending_state():
    result, session, lift_mock = _run_safety_net(
        lift_results=[{"lifted": True}, {"lifted": True}]
    )

    assert result == {"pending": 2, "lifted": 2, "errors": 0}
    assert lift_mock.call_count == 2
    assert session.commit.call_count == 2
    session.close.assert_called_once()


def test_safety_net_isolates_failures():
    """One failing lift is counted as an error and does not abort the sweep."""
    result, session, lift_mock = _run_safety_net(
        lift_results=[{"lifted": True}, RuntimeError("boom"), {"lifted": True}]
    )

    assert result == {"pending": 3, "lifted": 2, "errors": 1}
    assert lift_mock.call_count == 3
    session.rollback.assert_called_once()


def test_safety_net_no_pending_is_clean_noop():
    result, session, lift_mock = _run_safety_net(lift_results=[])

    assert result == {"pending": 0, "lifted": 0, "errors": 0}
    lift_mock.assert_not_called()
    session.close.assert_called_once()


def test_fup_copy_does_not_falsely_claim_next_cycle():
    """The reset wording must reflect the actual data-allowance reset, not the
    billing 'cycle' (the monthly FUP window is the calendar month, not the
    subscriber's billing anchor)."""
    from app.tasks.usage import _build_fup_notification

    # With a known reset time, the copy names the actual date.
    _subj, body = _build_fup_notification(
        "throttled", "Fiber 50", 100, 150, "2026-07-01T00:00:00+00:00"
    )
    assert "next cycle" not in body.lower()
    assert "data allowance resets" in body.lower()
    assert "2026-07-01" in body

    # With no reset time, no false claim and no crash.
    _subj2, body2 = _build_fup_notification("blocked", "Fiber 50", 100, 150, None)
    assert "next cycle" not in body2.lower()
    assert "data allowance resets" in body2.lower()
