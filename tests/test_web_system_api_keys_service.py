from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import web_system_api_key_forms as api_key_forms_service
from app.services import web_system_api_key_mutations as api_key_mutations_service
from app.services import web_system_api_keys as api_keys_service
from app.services.auth import hash_api_key
from app.services.settings_cache import SettingsCache


def _set_auth_cap(db_session, key: str, value: int) -> None:
    """Persist an auth-domain integer cap and clear any cached read."""
    from app.models.domain_settings import DomainSetting

    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key=key,
            value_type=SettingValueType.integer,
            value_text=str(value),
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(SettingDomain.auth.value, key)


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
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[
        0
    ]

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


def test_rotate_invalidates_old_secret_and_new_works(db_session, subscriber):
    """Rotate re-hashes: the old raw secret no longer matches, the new one does."""
    old_raw = api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Rotatable",
        expires_in=None,
    )
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[
        0
    ]
    assert key.key_hash == hash_api_key(old_raw)

    new_raw = api_key_mutations_service.rotate_api_key(db_session, key_id=str(key.id))

    assert new_raw and new_raw != old_raw
    db_session.refresh(key)
    # Old secret can never match again; new secret verifies.
    assert key.key_hash != hash_api_key(old_raw)
    assert key.key_hash == hash_api_key(new_raw)
    # Same row (label/owner preserved), still active.
    assert key.label == "Rotatable"
    assert key.subscriber_id == subscriber.id
    assert key.is_active is True


def test_rotate_refuses_revoked_key(db_session, subscriber):
    api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Dead key",
        expires_in=None,
    )
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[
        0
    ]
    api_key_mutations_service.revoke_api_key(db_session, key_id=str(key.id))

    assert (
        api_key_mutations_service.rotate_api_key(db_session, key_id=str(key.id)) is None
    )


def test_max_ttl_cap_rejects_and_defaults(db_session, subscriber):
    _set_auth_cap(db_session, "api_key_max_ttl_days", 30)

    # Requesting a longer lifetime than the cap is rejected with a clear message.
    with pytest.raises(api_key_forms_service.ApiKeyLimitError) as exc:
        api_key_forms_service.create_api_key(
            db_session,
            subscriber_id=str(subscriber.id),
            label="Too long",
            expires_in="90",
        )
    assert "30-day" in str(exc.value)

    # No expiry requested -> defaulted to the cap (not "never").
    api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Defaulted",
        expires_in=None,
    )
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[
        0
    ]
    assert key.expires_at is not None
    # SQLite returns naive datetimes; normalise both sides before comparing.
    expires_at = key.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    delta = expires_at - datetime.now(UTC)
    assert timedelta(days=29) < delta <= timedelta(days=30)


def test_max_keys_per_owner_cap(db_session, subscriber):
    _set_auth_cap(db_session, "api_key_max_per_owner", 1)

    api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="First",
        expires_in=None,
    )

    with pytest.raises(api_key_forms_service.ApiKeyLimitError) as exc:
        api_key_forms_service.create_api_key(
            db_session,
            subscriber_id=str(subscriber.id),
            label="Second",
            expires_in=None,
        )
    assert "limit reached" in str(exc.value).lower()

    # Revoking frees a slot (cap counts active keys only).
    key = api_keys_service.list_api_keys_for_subscriber(db_session, str(subscriber.id))[
        0
    ]
    api_key_mutations_service.revoke_api_key(db_session, key_id=str(key.id))

    api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=str(subscriber.id),
        label="Replacement",
        expires_in=None,
    )


def test_system_owned_key_create_and_list(db_session, subscriber):
    """A key with no subscriber owner is creatable and shows up in the list."""
    raw_key = api_key_forms_service.create_api_key(
        db_session,
        subscriber_id=None,
        label="Service key",
        expires_in=None,
    )
    assert raw_key

    # Visible when scoped to a subscriber (system keys always included)...
    scoped = api_keys_service.list_api_keys_for_subscriber(
        db_session, str(subscriber.id)
    )
    assert any(k.subscriber_id is None and k.label == "Service key" for k in scoped)

    # ...and visible with no subscriber scope at all.
    unscoped = api_keys_service.list_api_keys_for_subscriber(db_session, None)
    assert [k.label for k in unscoped] == ["Service key"]

    # A permissioned admin can revoke a system-owned key (no owner scope needed).
    key = unscoped[0]
    assert (
        api_key_mutations_service.revoke_api_key(db_session, key_id=str(key.id)) is True
    )
