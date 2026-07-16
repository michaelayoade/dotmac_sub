"""Tests for the unified device projection reconciler.

See app/services/device_projection_reconcile.py. The reconciler is the sole
canonical writer of the device_projections table; it delegates device
derivation to collect_devices and must materialise the result idempotently,
stamp freshness, and prune orphans. These tests drive it through a controlled
collect_devices so the contract is exercised without heavy device fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.network_monitoring import DeviceProjection
from app.services import device_projection_reconcile as reconcile_mod
from app.services.device_projection_reconcile import reconcile_device_projections


def _device(device_id, device_type, *, status="up", **overrides):
    device = {
        "id": device_id,
        "name": f"{device_type}-{device_id}",
        "type": device_type,
        "serial_number": f"SN{device_id}",
        "ip_address": "10.0.0.1",
        "vendor": "acme",
        "model": "x1",
        "status": status,
        "operational_reason": None,
        "last_seen": None,
        "subscriber": None,
    }
    device.update(overrides)
    return device


def _patch_collect(monkeypatch, devices):
    monkeypatch.setattr(reconcile_mod, "collect_devices", lambda db: list(devices))


def _rows(db):
    return {
        (r.device_type, r.source_id): r
        for r in db.execute(select(DeviceProjection)).scalars()
    }


def test_reconcile_inserts_one_row_per_derived_device(db_session, monkeypatch):
    _patch_collect(
        monkeypatch,
        [
            _device("1", "olt"),
            _device("2", "core"),
            _device("3", "ont"),
            _device("4", "cpe", status="unknown"),
        ],
    )

    result = reconcile_device_projections(db_session)

    assert result.inserted == 4
    assert result.updated == 0
    assert result.pruned == 0
    rows = _rows(db_session)
    assert set(rows) == {("olt", "1"), ("core", "2"), ("ont", "3"), ("cpe", "4")}
    assert rows[("cpe", "4")].operational_status == "unknown"
    assert rows[("olt", "1")].vendor == "acme"


def test_reconcile_is_idempotent(db_session, monkeypatch):
    devices = [_device("1", "olt"), _device("2", "core")]
    _patch_collect(monkeypatch, devices)

    first = reconcile_device_projections(db_session)
    assert first.inserted == 2

    second = reconcile_device_projections(db_session)
    assert second.inserted == 0
    assert second.updated == 2
    assert second.pruned == 0
    assert len(_rows(db_session)) == 2


def test_reconcile_updates_changed_status_and_freshness(db_session, monkeypatch):
    early = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    _patch_collect(monkeypatch, [_device("1", "olt", status="up")])
    reconcile_device_projections(db_session, now=early)
    assert _rows(db_session)[("olt", "1")].refreshed_at == early

    later = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    _patch_collect(
        monkeypatch,
        [_device("1", "olt", status="down", operational_reason="link_down")],
    )
    reconcile_device_projections(db_session, now=later)

    row = _rows(db_session)[("olt", "1")]
    assert row.operational_status == "down"
    assert row.operational_reason == "link_down"
    assert row.refreshed_at == later


def test_reconcile_prunes_orphaned_devices(db_session, monkeypatch):
    _patch_collect(monkeypatch, [_device("1", "olt"), _device("2", "core")])
    reconcile_device_projections(db_session)

    # Device 2 disappears from the authoritative source.
    _patch_collect(monkeypatch, [_device("1", "olt")])
    result = reconcile_device_projections(db_session)

    assert result.pruned == 1
    assert set(_rows(db_session)) == {("olt", "1")}


def test_same_source_id_across_types_are_distinct_rows(db_session, monkeypatch):
    # (device_type, source_id) is the natural key: an OLT and an ONT may share
    # a numeric id without colliding.
    _patch_collect(monkeypatch, [_device("1", "olt"), _device("1", "ont")])
    result = reconcile_device_projections(db_session)

    assert result.inserted == 2
    assert set(_rows(db_session)) == {("olt", "1"), ("ont", "1")}
