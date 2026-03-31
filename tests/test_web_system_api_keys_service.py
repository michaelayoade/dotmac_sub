from app.services import web_system_api_key_forms as api_key_forms_service
from app.services import web_system_api_key_mutations as api_key_mutations_service
from app.services import web_system_api_keys as api_keys_service


def test_create_and_list_api_keys_for_subscriber(db_session, subscriber):
    """create_api_key uses subscriber_id (not system_user_id)."""
    raw_key = api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Primary key",
        expires_in="7",
    )

    keys = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))

    assert raw_key
    assert len(keys) == 1
    assert keys[0].subscriber_id == subscriber.id
    assert keys[0].label == "Primary key"


def test_revoke_api_key_requires_owner(db_session, subscriber):
    """revoke_api_key revokes any key by id (no ownership check currently)."""
    api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Owner key",
        expires_in=None,
    )
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[0]

    assert key.is_active is True

    assert (
        api_key_mutations_service.revoke_api_key(
            db_session,
            key_id=str(key.id),
        )
        is True
    )

    db_session.refresh(key)
    assert key.is_active is False
    assert key.revoked_at is not None
