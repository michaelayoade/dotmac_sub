"""UISP mgmt-IP backfill for uisp-<uuid> named devices."""

from __future__ import annotations

import uuid

from app.models.network_monitoring import NetworkDevice
from app.services.topology.uisp_ip_backfill import backfill_uisp_mgmt_ips


class _FakeClient:
    def __init__(self, devices):
        self._devices = devices

    def list_devices(self):
        return self._devices


def _uisp_payload(uisp_id: str, ip: str | None):
    return {
        "identification": {"id": uisp_id, "name": f"radio-{uisp_id[:6]}"},
        "ipAddress": ip,
    }


def _device(db_session, name, **kw):
    kw.setdefault("is_active", True)
    kw.setdefault("source", "zabbix_reconcile")
    device = NetworkDevice(name=name, **kw)
    db_session.add(device)
    db_session.flush()
    return device


def test_backfill_stamps_ip_and_uisp_id(db_session):
    uid = str(uuid.uuid4())
    device = _device(db_session, f"uisp-{uid}")
    client = _FakeClient([_uisp_payload(uid, "172.16.40.10/24")])

    result = backfill_uisp_mgmt_ips(db_session, client)

    assert result["stamped_ip"] == 1
    assert result["stamped_uisp_id"] == 1
    assert device.mgmt_ip == "172.16.40.10"
    assert device.uisp_device_id == uid


def test_backfill_skips_claimed_ip_and_reports_conflict(db_session):
    uid = str(uuid.uuid4())
    _device(db_session, "existing", mgmt_ip="172.16.40.11")
    orphan = _device(db_session, f"uisp-{uid}")
    client = _FakeClient([_uisp_payload(uid, "172.16.40.11")])

    result = backfill_uisp_mgmt_ips(db_session, client)

    assert result["ip_conflicts"] == 1
    assert result["stamped_ip"] == 0
    assert orphan.mgmt_ip is None


def test_backfill_dry_run_writes_nothing(db_session):
    uid = str(uuid.uuid4())
    device = _device(db_session, f"uisp-{uid}")
    client = _FakeClient([_uisp_payload(uid, "172.16.40.12")])

    result = backfill_uisp_mgmt_ips(db_session, client, dry_run=True)

    assert result["stamped_ip"] == 1  # would-stamp count
    assert device.mgmt_ip is None
    assert device.uisp_device_id is None


def test_backfill_counts_unmatched_and_ipless(db_session):
    known = str(uuid.uuid4())
    _device(db_session, f"uisp-{known}")
    _device(db_session, f"uisp-{uuid.uuid4()}")  # not in UISP
    _device(db_session, "not-a-uisp-name", uisp_device_id=None)  # not a candidate
    client = _FakeClient([_uisp_payload(known, None)])  # matched, but no IP

    result = backfill_uisp_mgmt_ips(db_session, client)

    assert result["candidates"] == 2
    assert result["matched_in_uisp"] == 1
    assert result["no_ip_in_uisp"] == 1
    assert result["not_in_uisp"] == 1
    assert result["stamped_ip"] == 0


def test_backfill_never_touches_devices_with_ip(db_session):
    uid = str(uuid.uuid4())
    device = _device(db_session, f"uisp-{uid}", mgmt_ip="10.99.99.1")
    client = _FakeClient([_uisp_payload(uid, "172.16.40.13")])

    result = backfill_uisp_mgmt_ips(db_session, client)

    assert result["candidates"] == 0
    assert device.mgmt_ip == "10.99.99.1"


def test_two_devices_same_uisp_ip_only_first_wins(db_session):
    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    _device(db_session, f"uisp-{uid_a}")
    _device(db_session, f"uisp-{uid_b}")
    client = _FakeClient(
        [_uisp_payload(uid_a, "172.16.40.14"), _uisp_payload(uid_b, "172.16.40.14")]
    )

    result = backfill_uisp_mgmt_ips(db_session, client)

    assert result["stamped_ip"] == 1
    assert result["ip_conflicts"] == 1
