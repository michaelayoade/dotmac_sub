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
    PonPort,
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
        board="0/1",
        port="0",
    )
    absent = OntUnit(
        serial_number="HWTC00000001",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=1),
        board="0/1",
        port="0",
    )
    db_session.add_all([online, absent])
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        lambda _olt, _fsps, **_kwargs: (
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


def test_bulk_huawei_refresh_falls_back_to_pon_port_when_port_is_full_fsp(
    db_session, monkeypatch
):
    olt = OLTDevice(name="Huawei dirty board test", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/0/3")
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTCAABBCCDD",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        olt_status=OnuOnlineStatus.offline,
        board="0/0",
        port="0/0/3",
    )
    db_session.add(ont)
    db_session.flush()
    requested_fsps: list[list[str]] = []

    def fake_registered(_olt, fsps, **_kwargs):
        requested_fsps.append(list(fsps))
        return (
            True,
            "ok",
            [RegisteredOntEntry("0/0/3", 1, "48575443AABBCCDD", "online")],
        )

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        fake_registered,
    )

    stats = refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert requested_fsps == [["0/0/3"]]
    assert stats.invalid == 0


def test_bulk_huawei_refresh_skips_invalid_ont_location_and_polls_valid_ports(
    db_session, monkeypatch
):
    olt = OLTDevice(name="Huawei mixed location test", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    valid = OntUnit(
        serial_number="HWTCDDEEFF00",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.offline,
        board="0/1",
        port="0",
    )
    invalid = OntUnit(
        serial_number="HWTCINVALID01",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=1),
        board="0/0",
        port="0/0/3",
    )
    db_session.add_all([valid, invalid])
    db_session.flush()
    requested_fsps: list[list[str]] = []

    def fake_registered(_olt, fsps, **_kwargs):
        requested_fsps.append(list(fsps))
        return (
            True,
            "ok",
            [RegisteredOntEntry("0/1/0", 1, "48575443DDEEFF00", "online")],
        )

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        fake_registered,
    )

    stats = refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert requested_fsps == [["0/1/0"]]
    assert stats.observed == 1
    assert stats.invalid == 1
    assert valid.olt_status == OnuOnlineStatus.online
    assert invalid.olt_status == OnuOnlineStatus.online
    assert invalid.olt_status_seen_at == NOW - timedelta(hours=1)


def test_bulk_huawei_refresh_all_invalid_locations_does_not_retry_transport(
    db_session, monkeypatch
):
    olt = OLTDevice(
        name="Huawei invalid-only location test",
        vendor="Huawei",
        consecutive_poll_failures=2,
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTCINVALID02",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW - timedelta(hours=1),
        board="0/0",
        port="0/0/3",
    )
    db_session.add(ont)
    db_session.flush()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("invalid-only inventory must not call the OLT")

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        fail_if_called,
    )

    stats = refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert stats.observed == 0
    assert stats.invalid == 1
    assert olt.last_poll_at == NOW
    assert olt.last_poll_status == PollStatus.failed
    assert olt.last_poll_error == (
        "Huawei ONT inventory has no canonical pollable F/S/P locations"
    )
    assert olt.consecutive_poll_failures == 3
    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_status_seen_at == NOW - timedelta(hours=1)


def _huawei_olt_with_online_ont(db_session):
    olt = OLTDevice(name="Huawei empty test", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTC00000002",
        olt_device_id=olt.id,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=NOW,
        board="0/1",
        port="0",
    )
    db_session.add(ont)
    db_session.flush()
    return olt, ont


def test_bulk_huawei_refresh_recognized_empty_is_not_mass_offline(
    db_session, monkeypatch
):
    """A recognized empty read is authoritative: no raise, no mass-offline.

    The port genuinely holds no ONTs (an ONT was removed on the device but is
    still active in Sub). Absent inventory rows retain their last confirmed
    state instead of being flipped offline or aborting the whole OLT poll.
    """
    olt, ont = _huawei_olt_with_online_ont(db_session)
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        lambda _olt, _fsps, **_kwargs: (True, "Found 0 registered ONTs", []),
    )

    refresh_huawei_olt_status(db_session, olt, now=NOW)

    assert ont.olt_status == OnuOnlineStatus.online
    assert ont.olt_status_seen_at == NOW


def test_bulk_huawei_refresh_unrecognized_read_fails_closed_without_mass_offline(
    db_session, monkeypatch
):
    """An unrecognized read is a poll failure: raise and retry, retain state."""
    olt, ont = _huawei_olt_with_online_ont(db_session)
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.status.get_registered_ont_serials",
        lambda _olt, _fsps, **_kwargs: (
            False,
            "Huawei inventory response for 0/1/0 was not recognized",
            [],
        ),
    )

    with pytest.raises(RuntimeError, match="not recognized"):
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
