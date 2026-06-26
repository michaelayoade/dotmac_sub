"""Tests for the device status mismatch worklist (Phase 2a).

See docs/designs/DEVICE_OPERATIONAL_STATUS.md.
"""

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.device_operational_status import mismatch_worklist


def _device(db, name, *, status, live):
    d = NetworkDevice(name=name, status=status, live_status=live)
    db.add(d)
    db.flush()
    return d


def test_worklist_groups_mismatches_by_reason_with_owner(db_session):
    # admin online but observed down -> field ops
    _device(db_session, "R1", status=DeviceStatus.online, live="down")
    # admin offline but observed up -> inventory hygiene
    _device(db_session, "R2", status=DeviceStatus.offline, live="up")
    # active but no live coverage -> net-eng/VPN
    _device(db_session, "R3", status=DeviceStatus.online, live=None)
    # agreement -> NOT in worklist
    _device(db_session, "R4", status=DeviceStatus.online, live="up")
    db_session.commit()

    wl = mismatch_worklist(db_session)
    assert wl["total"] == 3
    by_reason = {g["reason"]: g for g in wl["groups"]}
    assert set(by_reason) == {
        "admin_online_observed_down",
        "admin_offline_observed_up",
        "active_but_unmonitored",
    }
    assert by_reason["admin_online_observed_down"]["owner"] == "Field ops"
    assert by_reason["admin_offline_observed_up"]["owner"] == "Inventory hygiene"
    assert by_reason["active_but_unmonitored"]["owner"] == "Net-eng / VPN"
    # the agreeing device is absent
    names = {r["name"] for g in wl["groups"] for r in g["rows"]}
    assert "R4" not in names


def test_worklist_reason_filter(db_session):
    _device(db_session, "D1", status=DeviceStatus.online, live="down")
    _device(db_session, "D2", status=DeviceStatus.offline, live="up")
    db_session.commit()

    wl = mismatch_worklist(db_session, reason="admin_online_observed_down")
    assert wl["total"] == 1
    assert wl["groups"][0]["reason"] == "admin_online_observed_down"
    assert wl["reason_filter"] == "admin_online_observed_down"


def test_worklist_empty_when_all_agree(db_session):
    _device(db_session, "OK1", status=DeviceStatus.online, live="up")
    _device(
        db_session, "OK2", status=DeviceStatus.maintenance, live="down"
    )  # intentional
    db_session.commit()

    wl = mismatch_worklist(db_session)
    assert wl["total"] == 0
    assert wl["groups"] == []
