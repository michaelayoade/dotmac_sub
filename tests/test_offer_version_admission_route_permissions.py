"""End-to-end proof of the ACTUAL router-plus-route permission graph for the
offer-version admission routes (finding 1's real compound requirement):
``catalog:write AND (catalog:billing_write OR
catalog:offer_version:admission)`` — never a pure OR / standalone-narrower-
permission alternative, because ``app/api/catalog.py``'s router carries its
own pre-existing ``catalog:write`` gate on top of the route-level
``require_any_permission`` dependency.

Exercises the REAL dependency callables (``require_method_permission`` for
the router-level gate, ``require_any_permission`` for the route-level one),
not a mock, mirroring ``tests/test_network_permissions_enforcement.py``'s
established pattern for this kind of test.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import catalog as api_catalog
from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services import auth_dependencies
from app.services.catalog import offer_access_requirement


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Route",
        last_name="Permission",
        email=f"route-perm-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "session_id": str(uuid.uuid4()),
        "roles": [],
        "scopes": [],
    }


def _request(method: str) -> Request:
    return Request(
        {"type": "http", "method": method, "path": "/offer-versions", "headers": []}
    )


def _ensure_permission(db_session, key: str) -> Permission:
    permission = db_session.query(Permission).filter(Permission.key == key).first()
    if permission is None:
        permission = Permission(key=key, is_active=True)
        db_session.add(permission)
        db_session.commit()
        db_session.refresh(permission)
    return permission


def _grant(db_session, user: SystemUser, key: str) -> None:
    permission = _ensure_permission(db_session, key)
    role = Role(name=f"role-{uuid.uuid4().hex}", is_active=True)
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)
    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()


def _passes_router_and_route_gate(db_session, user: SystemUser) -> bool:
    """Simulate the ACTUAL two-dependency chain a POST/PATCH
    /offer-versions request goes through: the router-level ``catalog:write``
    gate, then the route-level ``require_any_permission`` dependency."""

    auth = _auth(user)
    router_gate = auth_dependencies.require_method_permission(
        "catalog:read", "catalog:write"
    )
    try:
        auth = router_gate(request=_request("POST"), auth=auth, db=db_session)
    except HTTPException:
        return False
    try:
        api_catalog._require_offer_version_admission(
            request=_request("POST"), auth=auth, db=db_session
        )
    except HTTPException:
        return False
    return True


def test_the_narrow_admission_permission_alone_is_refused_by_the_router_gate(
    db_session,
):
    """A role holding ONLY catalog:offer_version:admission (no catalog:write)
    is REFUSED — proving finding 1's real compound requirement, not the
    route dependency's own pure-OR appearance in isolation."""

    user = _system_user(db_session)
    _grant(db_session, user, offer_access_requirement.ADMISSION_SCOPE)

    assert _passes_router_and_route_gate(db_session, user) is False


def test_catalog_write_plus_the_narrow_admission_permission_passes(db_session):
    """A role holding BOTH catalog:write and the narrow admission scope
    satisfies the actual compound requirement."""

    user = _system_user(db_session)
    _grant(db_session, user, "catalog:write")
    _grant(db_session, user, offer_access_requirement.ADMISSION_SCOPE)

    assert _passes_router_and_route_gate(db_session, user) is True


def test_catalog_write_plus_billing_write_passes(db_session):
    """The pre-existing pattern (catalog:write + catalog:billing_write)
    keeps working unchanged."""

    user = _system_user(db_session)
    _grant(db_session, user, "catalog:write")
    _grant(db_session, user, "catalog:billing_write")

    assert _passes_router_and_route_gate(db_session, user) is True


def test_catalog_write_alone_without_either_admission_permission_is_refused(
    db_session,
):
    """catalog:write alone does not satisfy the route-level OR — the
    compound requirement needs BOTH halves."""

    user = _system_user(db_session)
    _grant(db_session, user, "catalog:write")

    assert _passes_router_and_route_gate(db_session, user) is False


def test_admission_principal_resolution_is_identical_for_post_and_patch():
    """Symmetry proof for finding 2: both the POST create_offer_version and
    PATCH update_offer_version routes call the SAME ``_admission_principal``
    function — a system_user/api_key/subscriber auth resolves identically,
    and an unrecognized principal type is refused identically, regardless of
    which route reached it.

    Decision 2 (round 11): a ``subscriber`` auth is now resolved to a typed
    ``SubscriberPrincipal`` rather than refused — the seeded
    subscriber-admin path is preserved, not silently 403'd."""

    import inspect

    create_source = inspect.getsource(api_catalog.create_offer_version)
    update_source = inspect.getsource(api_catalog.update_offer_version)
    assert "_admission_principal(auth)" in create_source
    assert "_admission_principal(auth)" in update_source

    system_user_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "system_user",
    }
    api_key_auth = {"principal_id": str(uuid.uuid4()), "principal_type": "api_key"}
    subscriber_auth = {
        "principal_id": str(uuid.uuid4()),
        "principal_type": "subscriber",
    }
    other_auth = {"principal_id": str(uuid.uuid4()), "principal_type": "reseller_user"}

    assert isinstance(
        api_catalog._admission_principal(system_user_auth),
        offer_access_requirement.StaffPrincipal,
    )
    assert isinstance(
        api_catalog._admission_principal(api_key_auth),
        offer_access_requirement.ApiKeyPrincipal,
    )
    assert isinstance(
        api_catalog._admission_principal(subscriber_auth),
        offer_access_requirement.SubscriberPrincipal,
    )
    with pytest.raises(HTTPException) as excinfo:
        api_catalog._admission_principal(other_auth)
    assert excinfo.value.status_code == 403
