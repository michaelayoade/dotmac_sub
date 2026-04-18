import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.rbac import Permission, Role, RolePermission, SubscriberRole
from app.services import auth_dependencies


def _auth(subscriber) -> dict:
    return {
        "subscriber_id": str(subscriber.id),
        "person_id": str(subscriber.id),
        "principal_id": str(subscriber.id),
        "principal_type": "subscriber",
        "session_id": str(uuid.uuid4()),
        "roles": [],
        "scopes": [],
    }


def _request(method: str) -> Request:
    return Request(
        {"type": "http", "method": method, "path": "/admin/network", "headers": []}
    )


def _ensure_permission(db_session, key: str) -> Permission:
    permission = db_session.query(Permission).filter(Permission.key == key).first()
    if permission is None:
        permission = Permission(key=key, is_active=True)
        db_session.add(permission)
        db_session.commit()
        db_session.refresh(permission)
    return permission


def _grant_permission(db_session, subscriber, key: str) -> None:
    permission = _ensure_permission(db_session, key)
    role = Role(name=f"role-{uuid.uuid4().hex}", is_active=True)
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)

    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.add(SubscriberRole(subscriber_id=subscriber.id, role_id=role.id))
    db_session.commit()


def test_network_hub_permission_requires_real_role_assignment(db_session, subscriber):
    checker = auth_dependencies.require_permission("network:hub:read")
    auth = _auth(subscriber)
    _ensure_permission(db_session, "network:hub:read")

    with pytest.raises(HTTPException) as exc:
        checker(auth=auth, db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Forbidden"

    _grant_permission(db_session, subscriber, "network:hub:read")

    assert checker(auth=auth, db=db_session) == auth


def test_dns_threat_method_permissions_enforce_read_vs_write(db_session, subscriber):
    checker = auth_dependencies.require_method_permission(
        "network:dns_threat:read",
        "network:dns_threat:write",
    )
    auth = _auth(subscriber)
    _ensure_permission(db_session, "network:dns_threat:read")
    _ensure_permission(db_session, "network:dns_threat:write")

    _grant_permission(db_session, subscriber, "network:dns_threat:read")

    assert checker(request=_request("GET"), auth=auth, db=db_session) == auth

    with pytest.raises(HTTPException) as exc:
        checker(request=_request("POST"), auth=auth, db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Forbidden"

    _grant_permission(db_session, subscriber, "network:dns_threat:write")

    assert checker(request=_request("POST"), auth=auth, db=db_session) == auth


def test_existing_monitoring_permission_is_enforced_via_roles(db_session, subscriber):
    checker = auth_dependencies.require_permission("monitoring:read")
    auth = _auth(subscriber)
    _ensure_permission(db_session, "monitoring:read")

    with pytest.raises(HTTPException) as exc:
        checker(auth=auth, db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Forbidden"

    _grant_permission(db_session, subscriber, "monitoring:read")

    assert checker(auth=auth, db=db_session) == auth
