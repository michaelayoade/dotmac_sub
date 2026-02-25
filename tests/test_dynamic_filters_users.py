import json
import uuid

import pytest
from fastapi import HTTPException

from app.models.auth import UserCredential
from app.models.rbac import Role, SubscriberRole
from app.models.subscriber import Subscriber, UserType
from app.services.dynamic_filters import FilterValidationError, build_filter_expression, parse_filter_payload
from app.services.web_system_users import USER_DOCTYPE, USER_FILTER_SPECS, list_users


def _subscriber(first_name: str, last_name: str, email: str, *, is_active: bool = True) -> Subscriber:
    return Subscriber(
        first_name=first_name,
        last_name=last_name,
        email=email,
        is_active=is_active,
        user_type=UserType.system_user,
    )


def test_dynamic_filter_parser_supports_and_or_groups():
    payload = {
        "and": [["User", "email", "like", "@example.com"]],
        "or": [["User", "status", "=", "active"], ["User", "status", "=", "pending"]],
    }

    query = parse_filter_payload(payload, default_doctype="User")

    assert len(query.and_filters) == 1
    assert len(query.or_filters) == 2


def test_dynamic_filter_builder_rejects_unknown_field():
    query = parse_filter_payload(
        [["User", "definitely_not_a_field", "=", "x"]],
        default_doctype="User",
    )

    with pytest.raises(FilterValidationError):
        build_filter_expression(query, doctype="User", field_specs=USER_FILTER_SPECS)


def test_users_list_applies_dynamic_status_and_role_filters(db_session):
    admin_role = Role(name=f"Admin-{uuid.uuid4().hex}", is_active=True)
    db_session.add(admin_role)

    pending_user = _subscriber("Pending", "User", f"pending-{uuid.uuid4().hex}@example.com")
    active_user = _subscriber("Active", "User", f"active-{uuid.uuid4().hex}@example.com")
    db_session.add_all([pending_user, active_user])
    db_session.flush()

    db_session.add(
        UserCredential(
            subscriber_id=active_user.id,
            username=f"active-{uuid.uuid4().hex}",
            password_hash="hash",
            is_active=True,
            must_change_password=False,
        )
    )
    db_session.add(SubscriberRole(subscriber_id=pending_user.id, role_id=admin_role.id))
    db_session.commit()

    payload = json.dumps(
        {
            "and": [[USER_DOCTYPE, "status", "=", "pending"]],
            "or": [[USER_DOCTYPE, "role_id", "=", str(admin_role.id)]],
        }
    )

    users, total = list_users(
        db_session,
        search=None,
        role_id=None,
        status=None,
        filters=payload,
        order_by="last_name",
        order_dir="asc",
        offset=0,
        limit=25,
    )

    assert total == 1
    assert len(users) == 1
    assert users[0]["email"] == pending_user.email


def test_users_list_rejects_invalid_dynamic_field(db_session):
    with pytest.raises(HTTPException) as exc:
        list_users(
            db_session,
            search=None,
            role_id=None,
            status=None,
            filters=json.dumps([[USER_DOCTYPE, "bad_field", "=", "x"]]),
            order_by="last_name",
            order_dir="asc",
            offset=0,
            limit=25,
        )

    assert exc.value.status_code == 400
