"""Per-type behavior tests for the binary device operational owner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.device_operational_status import (
    NOT_WORKING,
    WORKING,
    derive_nas_operational_status,
    derive_olt_operational_status,
    derive_ont_operational_status,
    derive_router_operational_status,
)

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _enum(value):
    return SimpleNamespace(value=value)


def _olt(ping_ok, poll=None, ping_at=NOW):
    return SimpleNamespace(
        last_ping_ok=ping_ok,
        last_ping_at=ping_at,
        last_poll_status=_enum(poll) if poll else None,
    )


def test_olt_positive_ping_and_poll_is_working():
    op = derive_olt_operational_status(_olt(True, "success"), now=NOW)

    assert op.status == WORKING
    assert op.reason == "observed_working"


def test_olt_positive_ping_with_poll_failure_is_working_and_impaired():
    op = derive_olt_operational_status(_olt(True, "timeout"), now=NOW)

    assert op.status == WORKING
    assert op.reason == "poll_timeout"
    assert op.impaired is True


def test_olt_negative_ping_is_not_working():
    op = derive_olt_operational_status(_olt(False, "success"), now=NOW)

    assert op.status == NOT_WORKING
    assert op.reason == "ping_failed"
    assert op.alarming is True


def test_olt_without_observation_is_not_working_until_verified():
    op = derive_olt_operational_status(_olt(None, None, ping_at=None), now=NOW)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_not_started"


def test_olt_expired_direct_observation_is_not_working():
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(hours=3)), now=NOW
    )

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"


def test_olt_expired_direct_observation_uses_current_linked_evidence():
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(days=60)),
        linked_live_status="up",
        warm_stale=False,
        now=NOW,
    )

    assert op.status == WORKING
    assert op.reason == "observed_working_linked"


def test_olt_current_direct_negative_beats_linked_positive():
    op = derive_olt_operational_status(
        _olt(False, "success"), linked_live_status="up", now=NOW
    )

    assert op.status == NOT_WORKING


def test_olt_stale_linked_evidence_does_not_confirm_operation():
    op = derive_olt_operational_status(
        _olt(True, "success", ping_at=NOW - timedelta(days=60)),
        linked_live_status="up",
        warm_stale=True,
        now=NOW,
    )

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"


def _ont(olt_status=None, acs=None, seen=None, offline_reason=None, olt_seen=True):
    return SimpleNamespace(
        olt_status=_enum(olt_status) if olt_status else None,
        olt_status_seen_at=NOW if olt_status and olt_seen else None,
        acs_last_inform_at=acs,
        last_seen_at=seen,
        offline_reason=_enum(offline_reason) if offline_reason else None,
    )


def test_ont_current_olt_positive_is_working():
    op = derive_ont_operational_status(_ont("online"), now=NOW)

    assert op.status == WORKING
    assert op.reason == "olt_online"


def test_ont_recent_acs_positive_is_working():
    op = derive_ont_operational_status(
        _ont("offline", acs=NOW - timedelta(minutes=5)), now=NOW
    )

    assert op.status == WORKING
    assert op.reason == "acs_inform_recent"


def test_ont_current_negative_is_not_working():
    op = derive_ont_operational_status(
        _ont("offline", seen=NOW - timedelta(days=2), offline_reason="no_signal"),
        now=NOW,
    )

    assert op.status == NOT_WORKING
    assert op.reason == "no_signal"
    assert op.alarming is True


def test_ont_never_verified_is_not_working():
    op = derive_ont_operational_status(_ont("offline", olt_seen=False), now=NOW)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_not_started"
    assert op.alarming is False


def test_ont_expired_positive_is_not_working():
    op = derive_ont_operational_status(
        _ont("online", acs=NOW - timedelta(hours=2), olt_seen=False), now=NOW
    )

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"


def test_unlinked_active_nas_is_not_working_without_verifier():
    op = derive_nas_operational_status(
        SimpleNamespace(status=_enum("active"), health_status=None)
    )

    assert op.status == NOT_WORKING
    assert op.reason == "verification_not_configured"


def test_degraded_nas_is_working_with_impairment():
    op = derive_nas_operational_status(
        SimpleNamespace(status=_enum("active"), health_status=_enum("degraded"))
    )

    assert op.status == WORKING
    assert op.impaired is True


def test_router_requires_current_last_seen_confirmation():
    router = SimpleNamespace(status=_enum("online"), last_seen_at=None)

    op = derive_router_operational_status(router, now=NOW)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"


def test_current_router_is_working():
    router = SimpleNamespace(
        status=_enum("online"), last_seen_at=NOW - timedelta(minutes=2)
    )

    op = derive_router_operational_status(router, now=NOW)

    assert op.status == WORKING
