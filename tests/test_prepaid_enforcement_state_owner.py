from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services import prepaid_enforcement_state
from app.services.prepaid_enforcement_state import PrepaidEnforcementStateError

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_timer_owner_preserves_first_observation_and_stages_transition_events(
    db_session, subscriber_account, monkeypatch
):
    emitted: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        prepaid_enforcement_state,
        "emit_event",
        lambda _db, event_type, payload, **_context: emitted.append(
            (event_type, payload)
        ),
    )

    assert prepaid_enforcement_state.arm_prepaid_low_balance_timer(
        db_session, subscriber_account.id, armed_at=NOW
    )
    assert not prepaid_enforcement_state.arm_prepaid_low_balance_timer(
        db_session,
        subscriber_account.id,
        armed_at=NOW + timedelta(minutes=5),
    )
    assert prepaid_enforcement_state.mark_prepaid_deactivated(
        db_session,
        subscriber_account.id,
        deactivated_at=NOW + timedelta(days=1),
    )

    assert subscriber_account.prepaid_low_balance_at == NOW
    assert subscriber_account.prepaid_deactivation_at == NOW + timedelta(days=1)
    assert [payload["transition"] for _, payload in emitted] == [
        "low_balance_armed",
        "deactivation_recorded",
    ]

    assert prepaid_enforcement_state.clear_prepaid_enforcement_timers(
        db_session, subscriber_account.id
    )
    assert not prepaid_enforcement_state.clear_prepaid_enforcement_timers(
        db_session, subscriber_account.id
    )
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert emitted[-1][1]["transition"] == "timers_cleared"


@pytest.mark.parametrize("account_id", [None, "not-a-uuid"])
def test_timer_owner_rejects_invalid_account_identity(db_session, account_id):
    with pytest.raises(PrepaidEnforcementStateError) as exc_info:
        prepaid_enforcement_state.clear_prepaid_enforcement_timers(
            db_session, account_id
        )

    assert exc_info.value.code.endswith(".invalid_account_id")


def test_timer_owner_fails_closed_when_account_is_missing(db_session):
    with pytest.raises(PrepaidEnforcementStateError) as exc_info:
        prepaid_enforcement_state.clear_prepaid_enforcement_timers(db_session, uuid4())

    assert exc_info.value.code.endswith(".account_not_found")
