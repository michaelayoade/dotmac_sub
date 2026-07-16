"""Tests for the device-projection read model + network-device list contract.

See app/services/device_projection_views.py (SQL read of device_projections) and
the ui.network_device_list_projection contract in
web_network_core_devices_inventory.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.network_monitoring import DeviceProjection
from app.services import device_projection_views
from app.services import web_network_core_devices_inventory as inventory


def _proj(db, source_id, *, name, device_type="core", status="up", vendor="acme", **kw):
    row = DeviceProjection(
        device_type=device_type,
        source_id=source_id,
        name=name,
        operational_status=status,
        vendor=vendor,
        refreshed_at=kw.pop("refreshed_at", datetime.now(UTC)),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


# --- Contract ---


def test_network_device_list_definition_capabilities():
    d = inventory.NETWORK_DEVICE_LIST_DEFINITION
    assert d.filterable_keys == ("type", "status", "vendor")
    assert d.sortable_keys == ("name", "last_seen")
    assert d.default_sort == "name"
    assert d.default_per_page == 25


def test_build_network_device_list_query_normalizes_and_rejects():
    q = inventory.build_network_device_list_query(
        device_type="olt", status=" ", search="edge", page=2
    )
    assert q.filter_value("type") == "olt"
    assert q.filter_value("status") is None  # blank dropped
    assert q.search == "edge"
    assert q.sort_by == "name"
    with pytest.raises(ValueError):
        inventory.build_network_device_list_query(sort_by="vendor")  # not sortable
    with pytest.raises(ValueError):
        inventory.build_network_device_list_query(per_page=30)  # not allowed


# --- Read owner ---


def test_query_filters_sorts_and_paginates(db_session):
    _proj(db_session, "1", name="alpha", device_type="core", status="up")
    _proj(db_session, "2", name="bravo", device_type="olt", status="down")
    _proj(db_session, "3", name="charlie", device_type="core", status="up")

    # type filter
    rows, total = device_projection_views.query_device_projections(
        db_session, device_type="core"
    )
    assert total == 2
    assert [r["name"] for r in rows] == ["alpha", "charlie"]

    # sort desc
    rows, _ = device_projection_views.query_device_projections(
        db_session, sort_dir="desc"
    )
    assert [r["name"] for r in rows] == ["charlie", "bravo", "alpha"]

    # pagination
    rows, total = device_projection_views.query_device_projections(
        db_session, limit=2, offset=0
    )
    assert total == 3 and len(rows) == 2

    # status filter + search
    rows, total = device_projection_views.query_device_projections(
        db_session, status="down"
    )
    assert total == 1 and rows[0]["name"] == "bravo"
    rows, total = device_projection_views.query_device_projections(
        db_session, search="charl"
    )
    assert total == 1 and rows[0]["name"] == "charlie"

    # each row carries a projected status presentation (tone from the owner)
    assert rows[0]["status_presentation"].value == "up"


def test_stats_and_freshness(db_session):
    early = datetime.now(UTC) - timedelta(minutes=5)
    _proj(db_session, "1", name="a", device_type="core", status="up")
    _proj(db_session, "2", name="b", device_type="olt", status="down", refreshed_at=early)
    _proj(db_session, "3", name="c", device_type="ont", status="up")

    stats = device_projection_views.device_projection_stats(db_session)
    assert stats["total"] == 3
    assert stats["core"] == 1 and stats["olt"] == 1 and stats["ont"] == 1
    assert stats["up"] == 2 and stats["down"] == 1

    # freshness = most recent reconcile stamp
    latest = device_projection_views.latest_refreshed_at(db_session)
    assert latest is not None


def test_devices_list_page_data_reads_projection_with_pagination(db_session):
    for i in range(30):
        _proj(db_session, str(i), name=f"dev-{i:02d}", device_type="core")

    query = inventory.build_network_device_list_query(per_page=25)
    payload = inventory.devices_list_page_data(db_session, query)

    assert payload["total"] == 30
    assert len(payload["devices"]) == 25  # one page
    assert payload["pagination"] is True  # >1 page → controls render
    assert payload["stats"]["total"] == 30
    assert payload["devices_refreshed_at"] is not None
    assert payload["htmx_url"] == "/admin/network/devices/filter"


# --- Boundary: the list read owner must not derive live status per request ---


def test_list_read_does_not_call_collect_devices(db_session, monkeypatch):
    _proj(db_session, "1", name="alpha")

    def _boom(*_a, **_k):
        raise AssertionError("devices_list_page_data must read the projection, "
                             "not derive via collect_devices")

    monkeypatch.setattr(inventory, "collect_devices", _boom)
    query = inventory.build_network_device_list_query()
    payload = inventory.devices_list_page_data(db_session, query)
    assert payload["stats"]["total"] == 1
