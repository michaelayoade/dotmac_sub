import uuid

import pytest
from fastapi import HTTPException

from app.models.router_management import (
    RouterStatus,
)
from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterCreate,
    RouterUpdate,
)
from app.services.router_management.inventory import (
    JumpHostInventory,
    RouterInventory,
)


def test_create_router(db_session):
    payload = RouterCreate(
        name="test-router-1",
        hostname="tr1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret123",
    )
    router = RouterInventory.create(db_session, payload)
    assert router.name == "test-router-1"
    assert router.status == RouterStatus.offline
    assert router.is_active is True


def test_create_router_duplicate_name(db_session):
    payload = RouterCreate(
        name="dup-router",
        hostname="dr1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    RouterInventory.create(db_session, payload)
    with pytest.raises(HTTPException, match="409"):
        RouterInventory.create(db_session, payload)


def test_get_router(db_session):
    payload = RouterCreate(
        name="get-router",
        hostname="gr1",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    created = RouterInventory.create(db_session, payload)
    fetched = RouterInventory.get(db_session, created.id)
    assert fetched.id == created.id
    assert fetched.name == "get-router"


def test_get_router_not_found(db_session):
    with pytest.raises(HTTPException, match="404"):
        RouterInventory.get(db_session, uuid.uuid4())


def test_list_routers(db_session):
    for i in range(3):
        RouterInventory.create(
            db_session,
            RouterCreate(
                name=f"list-router-{i}",
                hostname=f"lr{i}",
                management_ip=f"10.0.{i}.1",
                rest_api_username="admin",
                rest_api_password="secret",
            ),
        )
    routers = RouterInventory.list(db_session)
    assert len(routers) >= 3


def test_list_routers_filter_status(db_session):
    r = RouterInventory.create(
        db_session,
        RouterCreate(
            name="online-router",
            hostname="or1",
            management_ip="10.1.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    r.status = RouterStatus.online
    db_session.commit()

    online = RouterInventory.list(db_session, status="online")
    names = [x.name for x in online]
    assert "online-router" in names


def test_update_router(db_session):
    created = RouterInventory.create(
        db_session,
        RouterCreate(
            name="update-router",
            hostname="ur1",
            management_ip="10.2.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    updated = RouterInventory.update(
        db_session, created.id, RouterUpdate(location="Server Room A")
    )
    assert updated.location == "Server Room A"


def test_delete_router(db_session):
    created = RouterInventory.create(
        db_session,
        RouterCreate(
            name="delete-router",
            hostname="dr1",
            management_ip="10.3.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    RouterInventory.delete(db_session, created.id)
    with pytest.raises(HTTPException, match="404"):
        RouterInventory.get(db_session, created.id)


def test_create_jump_host(db_session):
    payload = JumpHostCreate(
        name="test-jh-1",
        hostname="jump.example.com",
        username="tunnel",
    )
    jh = JumpHostInventory.create(db_session, payload)
    assert jh.name == "test-jh-1"
    assert jh.port == 22


def test_list_jump_hosts(db_session):
    JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-list-1", hostname="j1.example.com", username="t"),
    )
    hosts = JumpHostInventory.list(db_session)
    assert len(hosts) >= 1


def test_update_jump_host(db_session):
    jh = JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-update", hostname="j2.example.com", username="t"),
    )
    updated = JumpHostInventory.update(
        db_session, jh.id, JumpHostUpdate(port=2222)
    )
    assert updated.port == 2222


def test_delete_jump_host(db_session):
    jh = JumpHostInventory.create(
        db_session,
        JumpHostCreate(name="jh-delete", hostname="j3.example.com", username="t"),
    )
    JumpHostInventory.delete(db_session, jh.id)
    with pytest.raises(HTTPException, match="404"):
        JumpHostInventory.get(db_session, jh.id)
