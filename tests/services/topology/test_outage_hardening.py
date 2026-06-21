"""OutageIncident guarded status + stale surfacing (#48b-B).

Manual-only by design (auto-resolve would mis-fire on a flapping link). This
hardens the manual state machine and surfaces lingering open incidents (which
keep showing customers a false outage banner) for operator review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.network_monitoring import OutageIncident
from app.services.topology.outage import (
    is_stale_open,
    list_stale_open_incidents,
    set_outage_status,
)

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _incident(db, *, status="open", started_at=None):
    inc = OutageIncident(
        status=status,
        started_at=started_at or datetime.now(UTC),
        affected_count=0,
    )
    db.add(inc)
    db.flush()
    return inc


def test_set_status_rejects_invalid(db_session):
    inc = _incident(db_session)
    with pytest.raises(ValueError):
        set_outage_status(inc, "bogus")


def test_set_status_resolves_and_is_idempotent(db_session):
    inc = _incident(db_session, status="open")
    assert set_outage_status(inc, "resolved") is True
    assert inc.status == "resolved"
    assert inc.resolved_at is not None
    first = inc.resolved_at
    # second resolve is a no-op and does not re-stamp resolved_at
    assert set_outage_status(inc, "resolved") is False
    assert inc.resolved_at == first


def test_is_stale_open(db_session):
    fresh = _incident(db_session, started_at=NOW - timedelta(hours=2))
    old = _incident(db_session, started_at=NOW - timedelta(hours=48))
    assert is_stale_open(fresh, now=NOW) is False
    assert is_stale_open(old, now=NOW) is True
    # a resolved incident is never "stale-open"
    set_outage_status(old, "resolved")
    assert is_stale_open(old, now=NOW) is False


def test_list_stale_open_incidents(db_session):
    fresh = _incident(db_session, started_at=datetime.now(UTC) - timedelta(hours=1))
    old = _incident(db_session, started_at=datetime.now(UTC) - timedelta(hours=48))
    ids = {i.id for i in list_stale_open_incidents(db_session)}
    assert old.id in ids
    assert fresh.id not in ids
