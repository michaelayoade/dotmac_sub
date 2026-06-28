import uuid

import pytest

from app.models.rbac import Permission, Role, SubscriberRole, SystemUserRole
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.services.web_system_role_forms import (
    get_permissions_for_form,
    sync_role_permissions,
)
from app.services.web_system_roles import get_roles_page_data


def test_roles_page_counts_system_users_not_subscribers(db_session):
    role = Role(name=f"ops-{uuid.uuid4().hex}", is_active=True)
    legacy_role = Role(name=f"legacy-{uuid.uuid4().hex}", is_active=True)
    user = SystemUser(
        first_name="Ada",
        last_name="Admin",
        email=f"ada-{uuid.uuid4().hex}@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    subscriber = Subscriber(
        first_name="Sam",
        last_name="Subscriber",
        email=f"sam-{uuid.uuid4().hex}@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    db_session.add_all([role, legacy_role, user, subscriber])
    db_session.flush()

    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.add(SubscriberRole(subscriber_id=subscriber.id, role_id=legacy_role.id))
    db_session.commit()

    page_data = get_roles_page_data(db_session, page=1, per_page=25)

    assert page_data["user_counts"][str(role.id)] == 1
    assert page_data["user_counts"].get(str(legacy_role.id), 0) == 0


def test_role_form_hides_admin_only_permissions(db_session):
    visible = Permission(
        key="network:olt:read",
        description="View OLTs",
        is_active=True,
        is_ui_assignable=True,
    )
    hidden = Permission(
        key="network:write",
        description="Broad network write",
        is_active=True,
        is_ui_assignable=False,
    )
    db_session.add_all([visible, hidden])
    db_session.commit()

    permission_keys = {
        permission.key for permission in get_permissions_for_form(db_session)
    }

    assert "network:olt:read" in permission_keys
    assert "network:write" not in permission_keys


def test_role_sync_rejects_hidden_permission_ids(db_session):
    role = Role(name=f"noc-{uuid.uuid4().hex}", is_active=True)
    hidden = Permission(
        key="network:write",
        description="Broad network write",
        is_active=True,
        is_ui_assignable=False,
    )
    db_session.add_all([role, hidden])
    db_session.commit()

    with pytest.raises(ValueError, match="not assignable"):
        sync_role_permissions(
            db_session,
            role_id=role.id,
            permission_ids=[str(hidden.id)],
        )
