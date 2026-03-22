"""Tests for PPPoE credential auto-generation."""

import uuid

from app.models.catalog import AccessCredential
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.sequence import DocumentSequence
from app.models.subscription_engine import SettingValueType
from app.services.pppoe_credentials import (
    SEQUENCE_KEY,
    _generate_random_password,
    auto_generate_pppoe_credential,
)


def _seed_pppoe_settings(
    db,
    *,
    enabled: bool = True,
    prefix: str = "1050",
    padding: int = 5,
    start: int = 1,
    password_length: int = 12,
) -> None:
    """Insert PPPoE domain settings for tests."""
    specs = [
        ("pppoe_auto_generate_enabled", SettingValueType.boolean, str(enabled), enabled),
        ("pppoe_username_prefix", SettingValueType.string, prefix, None),
        ("pppoe_username_padding", SettingValueType.integer, str(padding), None),
        ("pppoe_username_start", SettingValueType.integer, str(start), None),
        ("pppoe_default_password_length", SettingValueType.integer, str(password_length), None),
    ]
    for key, vtype, text, json_val in specs:
        db.add(
            DomainSetting(
                domain=SettingDomain.radius,
                key=key,
                value_type=vtype,
                value_text=text,
                value_json=json_val,
                is_active=True,
            )
        )
    db.commit()


class TestAutoGeneratePppoeCredential:
    """Tests for auto_generate_pppoe_credential."""

    def test_disabled_by_default_returns_none(self, db_session, subscriber):
        """When pppoe_auto_generate_enabled is False, returns None."""
        _seed_pppoe_settings(db_session, enabled=False)
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is None

    def test_no_setting_returns_none(self, db_session, subscriber):
        """When no settings exist at all, returns None (defaults to disabled)."""
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is None

    def test_generates_credential_when_enabled(self, db_session, subscriber):
        """When enabled, creates AccessCredential with correct username."""
        _seed_pppoe_settings(db_session, enabled=True, start=25915)
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        assert result.username == "105025915"
        assert str(result.subscriber_id) == str(subscriber.id)
        assert result.is_active is True
        assert result.secret_hash is not None

    def test_skips_when_credential_exists(self, db_session, subscriber):
        """When subscriber already has active credential, skips."""
        _seed_pppoe_settings(db_session, enabled=True, start=1000)

        # Create existing credential
        existing = AccessCredential(
            subscriber_id=subscriber.id,
            username="existing_user",
            secret_hash="plain:test123",
            is_active=True,
        )
        db_session.add(existing)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is None

    def test_generates_when_only_inactive_credential_exists(self, db_session, subscriber):
        """When subscriber only has inactive credentials, generates new one."""
        _seed_pppoe_settings(db_session, enabled=True, start=5000)

        inactive = AccessCredential(
            subscriber_id=subscriber.id,
            username="old_user",
            secret_hash="plain:old",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is not None
        assert result.username == "105005000"

    def test_sequential_usernames(self, db_session):
        """Two consecutive calls produce sequential usernames."""
        from app.models.subscriber import Subscriber

        _seed_pppoe_settings(db_session, enabled=True, start=100)

        sub1 = Subscriber(
            first_name="A",
            last_name="One",
            email=f"a-{uuid.uuid4().hex[:8]}@test.com",
        )
        sub2 = Subscriber(
            first_name="B",
            last_name="Two",
            email=f"b-{uuid.uuid4().hex[:8]}@test.com",
        )
        db_session.add_all([sub1, sub2])
        db_session.commit()

        cred1 = auto_generate_pppoe_credential(db_session, str(sub1.id))
        cred2 = auto_generate_pppoe_credential(db_session, str(sub2.id))

        assert cred1 is not None
        assert cred2 is not None
        assert cred1.username == "105000100"
        assert cred2.username == "105000101"

    def test_custom_prefix_and_padding(self, db_session, subscriber):
        """Respects custom prefix and padding settings."""
        _seed_pppoe_settings(
            db_session, enabled=True, prefix="PPP", padding=3, start=1,
        )
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        assert result.username == "PPP001"

    def test_password_encrypted(self, db_session, subscriber):
        """Generated password is stored encrypted or with plain: prefix."""
        _seed_pppoe_settings(db_session, enabled=True, start=1)
        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))

        assert result is not None
        # Without CREDENTIAL_ENCRYPTION_KEY, falls back to plain: prefix
        assert result.secret_hash.startswith(("enc:", "plain:"))

    def test_radius_profile_id_assigned(self, db_session, subscriber):
        """Passes through the radius_profile_id when provided."""
        from app.models.catalog import RadiusProfile

        _seed_pppoe_settings(db_session, enabled=True, start=1)

        profile = RadiusProfile(name="Test Profile", is_active=True)
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        result = auto_generate_pppoe_credential(
            db_session,
            str(subscriber.id),
            radius_profile_id=str(profile.id),
        )

        assert result is not None
        assert str(result.radius_profile_id) == str(profile.id)


class TestGenerateRandomPassword:
    """Tests for _generate_random_password."""

    def test_correct_length(self):
        pw = _generate_random_password(16)
        assert len(pw) == 16

    def test_alphanumeric_only(self):
        pw = _generate_random_password(100)
        assert pw.isalnum()

    def test_different_each_time(self):
        passwords = {_generate_random_password(12) for _ in range(10)}
        assert len(passwords) == 10


class TestSeedPppoeSequence:
    """Tests for the seed script's core function."""

    def test_creates_sequence(self, db_session):
        """Creates DocumentSequence with specified start value."""
        seq = DocumentSequence(key=SEQUENCE_KEY, next_value=25915)
        db_session.add(seq)
        db_session.commit()

        existing = (
            db_session.query(DocumentSequence)
            .filter(DocumentSequence.key == SEQUENCE_KEY)
            .first()
        )
        assert existing is not None
        assert existing.next_value == 25915

    def test_sequence_used_by_auto_gen(self, db_session, subscriber):
        """DocumentSequence feeds into auto-generation."""
        _seed_pppoe_settings(db_session, enabled=True)

        # Pre-seed sequence at 25915
        seq = DocumentSequence(key=SEQUENCE_KEY, next_value=25915)
        db_session.add(seq)
        db_session.commit()

        result = auto_generate_pppoe_credential(db_session, str(subscriber.id))
        assert result is not None
        assert result.username == "105025915"

        # Verify sequence incremented
        db_session.refresh(seq)
        assert seq.next_value == 25916
