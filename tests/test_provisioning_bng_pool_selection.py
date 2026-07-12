"""BNG-scoped IP pool selection contracts for subscriber provisioning."""

import pytest
from fastapi import HTTPException

from app.models.catalog import NasDevice
from app.models.network import IpPool, IPVersion
from app.services import web_network_ip
from app.services.provisioning_helpers import _resolve_pool_for_version


def _pool(db, name: str, cidr: str, *, nas_id=None) -> IpPool:
    pool = IpPool(
        name=name,
        cidr=cidr,
        ip_version=IPVersion.ipv4,
        nas_device_id=nas_id,
        is_active=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _nas(db, name: str, *, pool_ids=()) -> NasDevice:
    nas = NasDevice(
        name=name,
        nas_ip="160.119.127.85",
        tags=[f"radius_pool:{pool_id}" for pool_id in pool_ids],
        is_active=True,
    )
    db.add(nas)
    db.flush()
    return nas


def test_uses_pool_selected_on_bng(db_session):
    selected = _pool(db_session, "Garki private", "172.16.99.0/24")
    _pool(db_session, "Jabi private", "172.16.107.0/24")
    nas = _nas(db_session, "Garki Access", pool_ids=[selected.id])

    resolved = _resolve_pool_for_version(
        db_session, IPVersion.ipv4, None, nas_device_id=str(nas.id)
    )

    assert resolved is not None
    assert resolved.id == selected.id


def test_legacy_direct_bng_pool_link_remains_supported(db_session):
    nas = _nas(db_session, "Legacy BNG")
    selected = _pool(db_session, "Legacy private", "172.16.98.0/24", nas_id=nas.id)

    resolved = _resolve_pool_for_version(
        db_session, IPVersion.ipv4, None, nas_device_id=str(nas.id)
    )

    assert resolved is not None
    assert resolved.id == selected.id


def test_bng_without_assigned_pool_fails_closed(db_session):
    _pool(db_session, "Unrelated global pool", "172.16.109.0/24")
    nas = _nas(db_session, "Unconfigured BNG")

    assert (
        _resolve_pool_for_version(
            db_session, IPVersion.ipv4, None, nas_device_id=str(nas.id)
        )
        is None
    )


def test_cross_bng_pool_override_is_rejected(db_session):
    selected = _pool(db_session, "Garki private", "172.16.99.0/24")
    other = _pool(db_session, "Jabi private", "172.16.107.0/24")
    nas = _nas(db_session, "Garki Access", pool_ids=[selected.id])

    with pytest.raises(HTTPException, match="not assigned") as exc:
        _resolve_pool_for_version(
            db_session,
            IPVersion.ipv4,
            str(other.id),
            nas_device_id=str(nas.id),
        )

    assert exc.value.status_code == 400


def test_pool_ui_create_and_edit_snapshot_include_bng_scope(db_session):
    nas = _nas(db_session, "UI BNG")
    values = web_network_ip.parse_ip_pool_form(
        {
            "name": "UI private pool",
            "ip_version": "ipv4",
            "cidr": "172.16.150.0/24",
            "nas_device_id": str(nas.id),
            "is_active": "true",
        }
    )

    pool, error = web_network_ip.create_ip_pool(db_session, values)

    assert error is None
    assert pool is not None
    assert pool.nas_device_id == nas.id
    snapshot = web_network_ip.pool_form_snapshot_from_model(pool)
    assert snapshot["nas_device_id"] == str(nas.id)
    assert web_network_ip._nas_devices_using_pool(db_session, str(pool.id)) == [nas]


def test_pool_ui_rejects_inactive_bng(db_session):
    nas = _nas(db_session, "Inactive BNG")
    nas.is_active = False
    db_session.flush()
    values = {
        "name": "Invalid private pool",
        "ip_version": "ipv4",
        "cidr": "172.16.151.0/24",
        "nas_device_id": str(nas.id),
        "is_active": True,
    }

    pool, error = web_network_ip.create_ip_pool(db_session, values)

    assert pool is None
    assert error == "Selected BNG was not found or is inactive."
