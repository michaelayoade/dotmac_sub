from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.fup_state import FupActionStatus
from app.services import fup_state as state_service
from app.services.fup_state import (
    ApplyFupRuntimeState,
    ClearFupRuntimeState,
    FupRuntimeStateError,
    fup_state,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_runtime_state_owner_is_locked_typed_and_idempotent(
    db_session, subscription, monkeypatch
):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        state_service,
        "emit_event",
        lambda _db, _event_type, payload, **_context: emitted.append(payload),
    )
    command = ApplyFupRuntimeState(
        subscription_id=subscription.id,
        offer_id=subscription.offer_id,
        action_status=FupActionStatus.throttled,
        evaluated_at=NOW,
        speed_reduction_percent=75.0,
        cap_resets_at=NOW + timedelta(days=1),
        notes="threshold crossed",
    )

    state = fup_state.apply_action(db_session, command)
    replay = fup_state.apply_action(db_session, command)

    assert replay.id == state.id
    assert state.action_status is FupActionStatus.throttled
    assert state.last_evaluated_at == NOW
    assert state.cap_resets_at == NOW + timedelta(days=1)
    assert [event["transition"] for event in emitted] == ["action_applied"]

    cleared = fup_state.clear(
        db_session,
        ClearFupRuntimeState(
            subscription_id=subscription.id,
            evaluated_at=NOW + timedelta(days=1),
        ),
    )
    replayed_clear = fup_state.clear(
        db_session,
        ClearFupRuntimeState(
            subscription_id=subscription.id,
            evaluated_at=NOW + timedelta(days=1),
        ),
    )

    assert cleared is not None
    assert replayed_clear is not None
    assert cleared.action_status is FupActionStatus.none
    assert cleared.cap_resets_at is None
    assert cleared.original_profile_id is None
    assert cleared.throttle_profile_id is None
    assert [event["transition"] for event in emitted] == [
        "action_applied",
        "state_cleared",
    ]


def test_runtime_state_owner_rejects_offer_drift(db_session, subscription):
    with pytest.raises(FupRuntimeStateError) as exc_info:
        fup_state.apply_action(
            db_session,
            ApplyFupRuntimeState(
                subscription_id=subscription.id,
                offer_id=uuid4(),
                action_status=FupActionStatus.notified,
                evaluated_at=NOW,
            ),
        )

    assert exc_info.value.code.endswith(".offer_mismatch")


def test_runtime_state_commands_require_authoritative_time(subscription):
    with pytest.raises(FupRuntimeStateError) as exc_info:
        ApplyFupRuntimeState(
            subscription_id=subscription.id,
            offer_id=subscription.offer_id,
            action_status=FupActionStatus.notified,
            evaluated_at=datetime(2026, 7, 20, 12, 0),
        )

    assert exc_info.value.code.endswith(".invalid_evaluated_at")
