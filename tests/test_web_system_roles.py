import uuid

from app.models.rbac import Role, SubscriberRole, SystemUserRole
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
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
