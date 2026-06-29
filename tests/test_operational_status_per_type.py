"""Tests for per-type operational-status derivers (Phase 2b): OLT + ONT.

See docs/designs/DEVICE_OPERATIONAL_STATUS.md.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.device_operational_status import (
    DEGRADED,
    DOWN,
    UNMONITORED,
    UP,
    derive_olt_operational_status,
    derive_ont_operational_status,
)

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _enum(v):
    return SimpleNamespace(value=v)


# ── OLT (ping + poll + freshness) ────────────────────────────────────────────


def _olt(ping_ok, poll=None, ping_at=NOW):
    return SimpleNamespace(
        last_ping_ok=ping_ok,
        last_ping_at=ping_at,
        last_poll_status=_enum(poll) if poll else None,
    )


def test_olt_ping_ok_poll_success_is_up():
    op = derive_olt_operational_status(_olt(True, "success"), now=NOW)
    assert op.status == UP


def test_olt_ping_ok_poll_failing_is_degraded():
    op = derive_olt_operational_status(_olt(True, "timeout"), now=NOW)
    assert op.status == DEGRADED
    assert op.reason == "poll_timeout"


def test_olt_ping_failed_is_down():
    op = derive_olt_operational_status(_olt(False, "success"), now=NOW)
    assert op.status == DOWN


def test_olt_never_pinged_is_unmonitored():
    op = derive_olt_operational_status(_olt(None, None, ping_at=None), now=NOW)
    assert op.status == UNMONITORED
    assert op.reason == "not_warmed"


def test_olt_stale_ping_is_unmonitored():
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(hours=3)), now=NOW
    )
    assert op.status == UNMONITORED
    assert op.reason == "stale"


def test_olt_stale_direct_falls_back_to_linked_zabbix():
    # OLT poller dead (stale ping), but the linked Zabbix device says up
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(days=60)),
        linked_live_status="up",
        warm_stale=False,
        now=NOW,
    )
    assert op.status == UP
    assert op.reason == "zabbix_up_linked"


def test_olt_fresh_direct_beats_linked_zabbix():
    # fresh direct ping failure wins over a linked 'up' (direct is more specific)
    op = derive_olt_operational_status(
        _olt(False, "success"), linked_live_status="up", now=NOW
    )
    assert op.status == DOWN


def test_olt_stale_with_stale_warmer_stays_unmonitored():
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(days=60)),
        linked_live_status="up",
        warm_stale=True,
        now=NOW,
    )
    assert op.status == UNMONITORED


# ── ONT (multi-source reconciliation) ────────────────────────────────────────


def _ont(olt_status=None, acs=None, seen=None, offline_reason=None):
    return SimpleNamespace(
        olt_status=_enum(olt_status) if olt_status else None,
        acs_last_inform_at=acs,
        last_seen_at=seen,
        offline_reason=_enum(offline_reason) if offline_reason else None,
    )


def test_ont_olt_online_is_up():
    op = derive_ont_operational_status(_ont("online"), now=NOW)
    assert op.status == UP
    assert op.reason == "olt_online"


def test_ont_offline_but_recent_acs_is_up():
    # the key accuracy win: OLT says offline, but ACS informed 5 min ago
    op = derive_ont_operational_status(
        _ont("offline", acs=NOW - timedelta(minutes=5)), now=NOW
    )
    assert op.status == UP
    assert op.reason == "acs_inform_recent"


def test_ont_offline_with_history_is_down():
    op = derive_ont_operational_status(
        _ont("offline", seen=NOW - timedelta(days=2), offline_reason="no_signal"),
        now=NOW,
    )
    assert op.status == DOWN
    assert op.reason == "no_signal"


def test_ont_never_seen_is_unmonitored_not_down():
    op = derive_ont_operational_status(_ont("offline"), now=NOW)
    assert op.status == UNMONITORED
    assert op.reason == "never_seen"


def test_ont_stale_acs_does_not_count_as_up():
    op = derive_ont_operational_status(
        _ont("offline", acs=NOW - timedelta(hours=2), seen=NOW - timedelta(hours=2)),
        now=NOW,
    )
    assert op.status == DOWN
