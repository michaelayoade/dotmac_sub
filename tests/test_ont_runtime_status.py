from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.network import (
    OLTDevice,
    OntStatusSource,
    OntUnit,
    OnuOnlineStatus,
    PollStatus,
)
from app.services.network.olt_ssh_ont._common import RegisteredOntEntry
from app.services.network.ont_runtime_status import (
    record_olt_poll_failure,
    refresh_huawei_olt_status,
)
from app.services.network.ont_status import resolve_effective_ont_status

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_effective_status_retains_last_online_while_retrying():
    ont = SimpleNamespace(
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=2),
        acs_last_inform_at=None,
        last_seen_at=NOW - timedelta(hours=2),
    )

    status = resolve_effective_ont_status(ont, now=NOW)

    assert status.status == OnuOnlineStatus.online
    assert status.source == OntStatusSource.olt
    assert status.retry_pending is True


def test_effective_status_recent_acs_overrides_olt_offline():
    ont = SimpleNamespace(
        olt_status=OnuOnlineStatus.offline,
        olt_status_seen_at=NOW,
        acs_last_inform_at=NOW - timedelta(minutes=5),
        last_seen_at=None,
    )

    status = resolve_effective_ont_status(ont, now=NOW)

    assert status.status == OnuOnlineStatus.online
    assert status.source == OntStatusSource.acs
    assert status.retry_pending is False


def test_bulk_huawei_refresh_persists_only_matched_observations(
    db_session, monkeypatch
):
    olt = OLTDevice(name="Huawei status test", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    online = OntUnit(
        serial_number="HWTCABEF7A70",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.offline,
    )
    absent = OntUnit(
        serial_number="HWTC00000001",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=1),
    )
    db_session.add_all([online, absent])
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        lambda _olt: (
            True,
            "ok",
            [RegisteredOntEntry("0/1/0", 1, "48575443ABEF7A70", "online")],
        ),
    )

    stats = refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert stats.observed == 1
    assert stats.online == 1
    assert online.olt_status == OnuOnlineStatus.online
    assert online.olt_status_seen_at == NOW
    assert absent.olt_status == OnuOnlineStatus.online
    assert absent.olt_status_seen_at == NOW - timedelta(hours=1)
    assert olt.last_poll_at == NOW
    assert olt.last_poll_status == PollStatus.success
    assert olt.last_poll_error is None
    assert olt.consecutive_poll_failures == 0
    assert olt.last_successful_ssh_at == NOW


def test_bulk_huawei_refresh_retries_empty_parse_without_mass_offline(
    db_session, monkeypatch
):
    olt = OLTDevice(name="Huawei empty test", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTC00000002",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW,
    )
    db_session.add(ont)
    db_session.flush()
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        lambda _olt: (True, "Found 0 registered ONTs", []),
    )

    with pytest.raises(RuntimeError, match="no parseable rows"):
        refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_status_seen_at == NOW


def test_olt_poll_failure_telemetry_is_retry_safe(db_session):
    olt = OLTDevice(
        name="Huawei failed poll",
        vendor="Huawei",
        consecutive_poll_failures=2,
    )
    db_session.add(olt)
    db_session.flush()

    record_olt_poll_failure(olt, RuntimeError("summary parse failed"), now=NOW)

    assert olt.last_poll_at == NOW
    assert olt.last_poll_status == PollStatus.failed
    assert olt.last_poll_error == "summary parse failed"
    assert olt.consecutive_poll_failures == 3
