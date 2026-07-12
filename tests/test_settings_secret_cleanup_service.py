from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import settings_secret_cleanup as cleanup_service


def test_find_plaintext_secret_settings_skips_refs_and_encrypted_values(db_session):
    rows = [
        DomainSetting(
            domain=SettingDomain.auth,
            key="jwt_secret",
            value_type=SettingValueType.string,
            value_text="plain-secret",
            is_secret=True,
            is_active=True,
        ),
        DomainSetting(
            domain=SettingDomain.auth,
            key="totp_encryption_key",
            value_type=SettingValueType.string,
            value_text="bao://secret/settings/auth#totp_encryption_key",
            is_secret=True,
            is_active=True,
        ),
        DomainSetting(
            domain=SettingDomain.comms,
            key="whatsapp_api_secret",
            value_type=SettingValueType.string,
            value_text="enc:encrypted-value",
            is_secret=True,
            is_active=True,
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()

    found = cleanup_service.find_plaintext_secret_settings(db_session)
    assert [f"{row.domain.value}.{row.key}" for row in found] == ["auth.jwt_secret"]

    noncanonical = cleanup_service.find_noncanonical_secret_settings(db_session)
    assert [f"{row.domain.value}.{row.key}" for row in noncanonical] == [
        "auth.jwt_secret",
        "comms.whatsapp_api_secret",
    ]


def test_migrate_plaintext_secret_settings_dry_run_does_not_mutate(db_session):
    row = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_secret",
        value_type=SettingValueType.string,
        value_text="plain-secret",
        is_secret=True,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    result = cleanup_service.migrate_plaintext_secret_settings(db_session, dry_run=True)

    db_session.refresh(row)
    assert result.migrated == 1
    assert result.errors == []
    assert row.value_text == "plain-secret"


def test_migrate_plaintext_secret_settings_rewrites_to_openbao_ref(
    db_session, monkeypatch
):
    row = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_secret",
        value_type=SettingValueType.string,
        value_text="plain-secret",
        is_secret=True,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    writes = []
    monkeypatch.setattr(cleanup_service, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        cleanup_service, "read_secret_fields", lambda path, masked=False: {}
    )

    def _write_secret(path, data):
        writes.append((path, data))
        return True

    monkeypatch.setattr(cleanup_service, "write_secret", _write_secret)

    result = cleanup_service.migrate_plaintext_secret_settings(
        db_session, dry_run=False
    )

    db_session.refresh(row)
    assert result.migrated == 1
    assert result.errors == []
    assert writes == [("settings/auth", {"jwt_secret": "plain-secret"})]
    assert row.value_text == "bao://secret/settings/auth#jwt_secret"


def test_migrate_encrypted_secret_setting_decrypts_before_openbao_write(
    db_session, monkeypatch
):
    row = DomainSetting(
        domain=SettingDomain.comms,
        key="whatsapp_api_secret",
        value_type=SettingValueType.string,
        value_text="enc:ciphertext",
        is_secret=True,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    writes = []
    monkeypatch.setattr(cleanup_service, "is_openbao_available", lambda: True)
    monkeypatch.setattr(cleanup_service, "read_secret_fields", lambda _path: {})
    monkeypatch.setattr(
        cleanup_service, "decrypt_credential", lambda _value: "clear-secret"
    )
    monkeypatch.setattr(
        cleanup_service,
        "write_secret",
        lambda path, data: writes.append((path, data)) or True,
    )

    result = cleanup_service.migrate_plaintext_secret_settings(
        db_session, dry_run=False
    )

    db_session.refresh(row)
    assert result.migrated == 1
    assert writes == [("settings/comms", {"whatsapp_api_secret": "clear-secret"})]
    assert row.value_text == "bao://secret/settings/comms#whatsapp_api_secret"


def test_migrate_plaintext_secret_settings_reports_openbao_unavailable(
    db_session, monkeypatch
):
    row = DomainSetting(
        domain=SettingDomain.auth,
        key="jwt_secret",
        value_type=SettingValueType.string,
        value_text="plain-secret",
        is_secret=True,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(cleanup_service, "is_openbao_available", lambda: False)

    result = cleanup_service.migrate_plaintext_secret_settings(
        db_session, dry_run=False
    )

    db_session.refresh(row)
    assert result.migrated == 0
    assert result.errors == ["OpenBao is not configured or reachable."]
    assert row.value_text == "plain-secret"
