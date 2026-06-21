"""Top-up intent status guard (#40, lighter hygiene approach).

Validates the value set + records (does NOT block) a terminal->completed
recovery, since a real gateway payment can legitimately arrive after expiry.
"""

import pytest

from app.models.billing import TopupIntent
from app.services.topup_intents import TopupIntentStatus, set_topup_intent_status


def test_valid_transition_sets_status():
    intent = TopupIntent(status="pending")
    assert set_topup_intent_status(intent, "completed", source="test") is True
    assert intent.status == "completed"


def test_rejects_unknown_value():
    intent = TopupIntent(status="pending")
    with pytest.raises(ValueError):
        set_topup_intent_status(intent, "bogus", source="test")
    assert intent.status == "pending"


def test_submitted_is_a_valid_value():
    intent = TopupIntent(status="pending")
    assert set_topup_intent_status(intent, TopupIntentStatus.submitted, source="t")
    assert intent.status == "submitted"


def test_same_status_is_noop():
    intent = TopupIntent(status="completed")
    assert set_topup_intent_status(intent, "completed", source="test") is False


def test_terminal_recovery_is_allowed_not_blocked():
    # A late real payment must still complete an expired/canceled intent —
    # blocking would drop money; this is allowed (and logged elsewhere).
    for terminal in ("expired", "canceled"):
        intent = TopupIntent(status=terminal)
        assert set_topup_intent_status(intent, "completed", source="webhook") is True
        assert intent.status == "completed"
