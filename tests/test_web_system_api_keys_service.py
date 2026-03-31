from app.models.system_user import SystemUser
from app.services import web_system_api_key_forms as api_key_forms_service
from app.services import web_system_api_key_mutations as api_key_mutations_service
from app.services import web_system_api_keys as api_keys_service


def _create_system_user(db_session, email: str) -> SystemUser:
    user = SystemUser(
        first_name="Admin",
        last_name="User",
        email=email,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_create_and_list_api_keys_for_system_user(db_session):
    user = _create_system_user(db_session, "api-owner@example.com")

    raw_key = api_key_forms_service.create_api_key(
        db_session,
        system_user_id=str(user.id),
        label="Primary key",
        expires_in="7",
    )

    keys = api_keys_service.list_api_keys_for_system_user(db_session, str(user.id))

    assert raw_key
    assert len(keys) == 1
    assert keys[0].system_user_id == user.id
    assert keys[0].subscriber_id is None
    assert keys[0].label == "Primary key"


def test_revoke_api_key_requires_owner(db_session):
    owner = _create_system_user(db_session, "api-owner-2@example.com")
    other = _create_system_user(db_session, "api-other@example.com")

    api_key_forms_service.create_api_key(
        db_session,
        system_user_id=str(owner.id),
        label="Owner key",
        expires_in=None,
    )
    key = api_keys_service.list_api_keys_for_system_user(db_session, str(owner.id))[0]

    assert (
        api_key_mutations_service.revoke_api_key(
            db_session,
            key_id=str(key.id),
            system_user_id=str(other.id),
        )
        is False
    )

    db_session.refresh(key)
    assert key.is_active is True

    assert (
        api_key_mutations_service.revoke_api_key(
            db_session,
            key_id=str(key.id),
            system_user_id=str(owner.id),
        )
        is True
    )

    db_session.refresh(key)
    assert key.is_active is False
    assert key.revoked_at is not None
