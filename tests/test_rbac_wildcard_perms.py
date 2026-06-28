"""Wildcard permission grants: a role granted ``network:*`` (or ``*``) passes
any matching requirement, without enumerating every leaf permission."""

import uuid

from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.services import auth_dependencies as ad


def _grant(db_session, held_key: str):
    """A system user whose role holds exactly ``held_key``."""
    from app.models.system_user import SystemUser

    user = SystemUser(
        email=f"wc-{uuid.uuid4().hex[:8]}@example.com",
        first_name="Wild",
        last_name="Card",
        is_active=True,
    )
    perm = Permission(key=held_key, description="x", is_active=True)
    role = Role(name=f"wc-{uuid.uuid4().hex[:6]}", is_active=True)
    db_session.add_all([user, perm, role])
    db_session.commit()
    db_session.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    return {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [role.name],
        "scopes": [],
    }


def test_wildcard_ancestors():
    assert ad._wildcard_ancestors("network:nas:write") == [
        "*",
        "network:*",
        "network:nas:*",
    ]
    assert ad._wildcard_ancestors("billing:read") == ["*", "billing:*"]


def test_domain_wildcard_satisfies_granular_requirement(db_session):
    auth = _grant(db_session, "network:*")
    assert ad.has_permission(auth, db_session, "network:nas:write") is True
    assert ad.has_permission(auth, db_session, "network:vpn:read") is True


def test_broad_permission_does_not_satisfy_granular_requirement(db_session):
    auth = _grant(db_session, "network:write")
    permission = Permission(
        key="network:olt:write",
        description="Manage OLTs",
        is_active=True,
    )
    db_session.add(permission)
    db_session.commit()

    assert ad.has_permission(auth, db_session, "network:olt:write") is False


def test_domain_wildcard_does_not_cross_domains(db_session):
    auth = _grant(db_session, "network:*")
    assert ad.has_permission(auth, db_session, "billing:read") is False


def test_global_wildcard_satisfies_anything(db_session):
    auth = _grant(db_session, "*")
    assert ad.has_permission(auth, db_session, "billing:invoice:create") is True
    assert ad.has_permission(auth, db_session, "network:nas:write") is True


def test_subdomain_wildcard_scoped(db_session):
    auth = _grant(db_session, "network:nas:*")
    assert ad.has_permission(auth, db_session, "network:nas:write") is True
    # Does not reach a sibling network resource.
    assert ad.has_permission(auth, db_session, "network:vpn:write") is False


def test_wildcard_flows_through_scoped_grants(db_session):
    """grant_scopes_for_permission also honours wildcards (shares the key
    expansion), so a global network:* grant is 'global' for a scoped check."""
    auth = _grant(db_session, "network:*")
    assert (
        ad.grant_scopes_for_permission(auth, db_session, "network:nas:write")
        == "global"
    )
