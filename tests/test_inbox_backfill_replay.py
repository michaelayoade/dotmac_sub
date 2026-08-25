"""Exact-replay canary for the temporary inbox history bridge."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.inbox_backfill import BackfillDrift, _require_exact


def test_existing_history_must_match_every_derived_fact() -> None:
    occurred_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
    row = SimpleNamespace(status="open", occurred_at=occurred_at)

    _require_exact(
        row,
        entity="mod_inbox.conversations",
        entity_id="conversation-1",
        expected={"status": "open", "occurred_at": occurred_at},
    )

    with pytest.raises(BackfillDrift, match="status"):
        _require_exact(
            row,
            entity="mod_inbox.conversations",
            entity_id="conversation-1",
            expected={
                "status": "resolved",
                "occurred_at": occurred_at + timedelta(minutes=1),
            },
        )
