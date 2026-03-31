from __future__ import annotations

import json
import sys

import pytest
from cryptography.fernet import Fernet

from app.models.billing import BankAccount, PaymentMethod
from app.models.catalog import AccessCredential, NasDevice
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.integration_hook import IntegrationHook
from app.models.network import (
    OLTDevice,
    OntProfileWanService,
    OntProvisioningProfile,
    OntUnit,
)
from app.models.subscriber import Subscriber
from app.models.tr069 import Tr069AcsServer
from app.models.webhook import WebhookEndpoint
from app.services.credential_crypto import (
    _coerce_encryption_key,
    decrypt_credential_with_key,
    encrypt_credential_with_key,
)
from app.services.credential_key_rotation import rotate_credential_encryption_material


def test_encrypt_decrypt_with_key_round_trip():
    key = Fernet.generate_key().decode("ascii")

    encrypted = encrypt_credential_with_key("secret-value", key)

    assert encrypted is not None
    assert encrypted.startswith("enc:")
    assert decrypt_credential_with_key(encrypted, key) == "secret-value"


def test_encrypt_decrypt_with_key_handles_plain_and_legacy_values():
    assert (
        encrypt_credential_with_key("plain:value", Fernet.generate_key())
        == "plain:value"
    )
    assert decrypt_credential_with_key("plain:value", Fernet.generate_key()) == "value"
    assert (
        decrypt_credential_with_key("legacy-value", Fernet.generate_key())
        == "legacy-value"
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, None),
        ("", None),
        ("abc", b"abc"),
        (b"abc", b"abc"),
    ],
)
def test_coerce_encryption_key_supported_inputs(raw_value, expected):
    assert _coerce_encryption_key(raw_value) == expected


def test_coerce_encryption_key_rejects_non_ascii_text():
    with pytest.raises(ValueError, match="ASCII-safe Fernet text"):
        _coerce_encryption_key("abcé")


def test_rotate_credential_encryption_material_updates_known_storage_targets(
    db_session,
):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")

    subscriber = Subscriber(
        first_name="Rotate",
        last_name="Me",
        email="rotate@example.com",
        user_type="customer",
    )
    profile = OntProvisioningProfile(name="Residential")
    db_session.add_all([subscriber, profile])
    db_session.flush()

    nas = NasDevice(
        name="NAS-1",
        shared_secret=encrypt_credential_with_key("radius-secret", old_key),
        ssh_password="plain:ssh-pass",
    )
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="rotate-user",
        secret_hash=encrypt_credential_with_key("pppoe-pass", old_key),
    )
    payment_method = PaymentMethod(
        account_id=subscriber.id,
        token=encrypt_credential_with_key("pm-token", old_key),
    )
    bank_account = BankAccount(
        account_id=subscriber.id,
        token="plain:bank-token",
    )
    webhook = WebhookEndpoint(
        name="Main",
        url="https://example.com/hook",
        secret=encrypt_credential_with_key("hook-secret", old_key),
    )
    acs = Tr069AcsServer(
        name="ACS",
        base_url="https://acs.example.com",
        cwmp_password=encrypt_credential_with_key("cwmp-pass", old_key),
        connection_request_password="plain:cr-pass",
    )
    olt = OLTDevice(
        name="OLT-1",
        ssh_password=encrypt_credential_with_key("olt-ssh", old_key),
        snmp_ro_community="plain:public-ro",
    )
    ont = OntUnit(
        serial_number="ONT123456",
        pppoe_password=encrypt_credential_with_key("ont-pass", old_key),
    )
    wan = OntProfileWanService(
        profile_id=profile.id,
        service_type="internet",
        connection_type="pppoe",
        pppoe_static_password="plain:wan-pass",
    )
    hook = IntegrationHook(
        title="Hook",
        auth_config={
            "token": encrypt_credential_with_key("api-token", old_key),
            "username": "plain-user",
        },
    )
    setting = DomainSetting(
        domain=SettingDomain.comms,
        key="whatsapp_api_key",
        value_type=SettingValueType.string,
        value_text=encrypt_credential_with_key("wa-key", old_key),
        is_secret=True,
        is_active=True,
    )

    db_session.add_all(
        [
            nas,
            credential,
            payment_method,
            bank_account,
            webhook,
            acs,
            olt,
            ont,
            wan,
            hook,
            setting,
        ]
    )
    db_session.commit()

    result = rotate_credential_encryption_material(
        db_session, old_key=old_key, new_key=new_key
    )
    db_session.expire_all()

    assert result.updated_records >= 10
    assert result.updated_values >= 11

    assert (
        decrypt_credential_with_key(
            db_session.get(NasDevice, nas.id).shared_secret, new_key
        )
        == "radius-secret"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(NasDevice, nas.id).ssh_password, new_key
        )
        == "ssh-pass"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(AccessCredential, credential.id).secret_hash, new_key
        )
        == "pppoe-pass"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(PaymentMethod, payment_method.id).token, new_key
        )
        == "pm-token"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(BankAccount, bank_account.id).token, new_key
        )
        == "bank-token"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(WebhookEndpoint, webhook.id).secret, new_key
        )
        == "hook-secret"
    )
    refreshed_acs = db_session.get(Tr069AcsServer, acs.id)
    assert (
        decrypt_credential_with_key(refreshed_acs.cwmp_password, new_key) == "cwmp-pass"
    )
    assert (
        decrypt_credential_with_key(refreshed_acs.connection_request_password, new_key)
        == "cr-pass"
    )
    refreshed_olt = db_session.get(OLTDevice, olt.id)
    assert decrypt_credential_with_key(refreshed_olt.ssh_password, new_key) == "olt-ssh"
    assert (
        decrypt_credential_with_key(refreshed_olt.snmp_ro_community, new_key)
        == "public-ro"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(OntUnit, ont.id).pppoe_password, new_key
        )
        == "ont-pass"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(OntProfileWanService, wan.id).pppoe_static_password, new_key
        )
        == "wan-pass"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(DomainSetting, setting.id).value_text, new_key
        )
        == "wa-key"
    )
    assert (
        decrypt_credential_with_key(
            db_session.get(IntegrationHook, hook.id).auth_config["token"], new_key
        )
        == "api-token"
    )


def test_rotate_credential_encryption_material_dry_run_leaves_db_unchanged(db_session):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    nas = NasDevice(
        name="NAS-dry-run",
        shared_secret=encrypt_credential_with_key("radius-secret", old_key),
    )
    db_session.add(nas)
    db_session.commit()
    nas_id = nas.id
    original_value = db_session.get(NasDevice, nas_id).shared_secret

    savepoint = db_session.begin_nested()
    result = rotate_credential_encryption_material(
        db_session, old_key=old_key, new_key=new_key, commit=False
    )
    savepoint.rollback()
    db_session.expire_all()

    assert result.updated_records == 1
    assert db_session.get(NasDevice, nas_id).shared_secret == original_value


def test_rotate_credential_encryption_material_raises_on_length_overflow(
    db_session, monkeypatch
):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    nas = NasDevice(
        name="NAS-overflow",
        shared_secret=encrypt_credential_with_key("radius-secret", old_key),
    )
    db_session.add(nas)
    db_session.commit()

    import app.services.credential_key_rotation as rotation_service

    def _overflow_encrypt(value, key):
        if value == "radius-secret":
            return "enc:" + ("x" * 512)
        return encrypt_credential_with_key(value, key)

    monkeypatch.setattr(
        rotation_service, "encrypt_credential_with_key", _overflow_encrypt
    )

    with pytest.raises(ValueError, match="exceeds column length"):
        rotate_credential_encryption_material(
            db_session, old_key=old_key, new_key=new_key
        )


def test_rotate_credential_encryption_material_skips_non_dict_hook_config(db_session):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    hook = IntegrationHook(title="Bad Hook", auth_config="opaque-token")
    db_session.add(hook)
    db_session.commit()

    result = rotate_credential_encryption_material(
        db_session, old_key=old_key, new_key=new_key
    )

    assert result.updated_records == 0
    assert db_session.get(IntegrationHook, hook.id).auth_config == "opaque-token"


def test_rotate_credential_encryption_material_raises_on_corrupted_ciphertext(
    db_session,
):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")

    nas = NasDevice(
        name="NAS-bad",
        shared_secret="enc:not-a-valid-token",
    )
    db_session.add(nas)
    db_session.commit()

    with pytest.raises(ValueError, match=r"Failed to rotate NasDevice\.shared_secret"):
        rotate_credential_encryption_material(
            db_session, old_key=old_key, new_key=new_key
        )


def test_rotate_credential_encryption_material_raises_on_corrupted_domain_setting(
    db_session,
):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    setting = DomainSetting(
        domain=SettingDomain.comms,
        key="bad_secret",
        value_type=SettingValueType.string,
        value_text="enc:not-a-valid-token",
        is_secret=True,
        is_active=True,
    )
    db_session.add(setting)
    db_session.commit()

    with pytest.raises(
        ValueError, match=r"Failed to rotate DomainSetting comms\.bad_secret"
    ):
        rotate_credential_encryption_material(
            db_session, old_key=old_key, new_key=new_key
        )


def test_rotate_credential_encryption_material_raises_on_corrupted_hook_secret(
    db_session,
):
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    hook = IntegrationHook(
        title="Corrupt Hook",
        auth_config={"token": "enc:not-a-valid-token"},
    )
    db_session.add(hook)
    db_session.commit()

    with pytest.raises(
        ValueError, match=r"Failed to rotate IntegrationHook auth_config\.token"
    ):
        rotate_credential_encryption_material(
            db_session, old_key=old_key, new_key=new_key
        )


def test_decrypt_credential_with_key_rejects_invalid_key_type():
    with pytest.raises(TypeError, match="encryption_key must be a str, bytes, or None"):
        decrypt_credential_with_key("enc:anything", 123)  # type: ignore[arg-type]


def test_rotation_cli_emits_json_success(monkeypatch, capsys):
    import scripts.rotate_credential_encryption_key as cli

    class _DummySession:
        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        sys, "argv", ["rotate_credential_encryption_key.py", "--generate"]
    )
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(cli, "get_encryption_key", lambda: b"old-key")
    monkeypatch.setattr(cli, "generate_encryption_key", lambda: "new-key")
    monkeypatch.setattr(
        cli,
        "rotate_credential_encryption_material",
        lambda *args, **kwargs: type(
            "Result", (), {"updated_records": 2, "updated_values": 3}
        )(),
    )

    assert cli.main() == 0
    out = capsys.readouterr()
    payload = json.loads(out.out.strip())
    warning = json.loads(out.err.strip())
    assert payload["ok"] is True
    assert payload["updated_records"] == 2
    assert warning["warning"].startswith("Credential encryption key not printed")


def test_rotation_cli_emits_json_error(monkeypatch, capsys):
    import scripts.rotate_credential_encryption_key as cli

    class _DummySession:
        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        sys, "argv", ["rotate_credential_encryption_key.py", "--new-key", "new-key"]
    )
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(cli, "get_encryption_key", lambda: b"old-key")

    def _boom(*args, **kwargs):
        raise ValueError("rotation failed")

    monkeypatch.setattr(cli, "rotate_credential_encryption_material", _boom)

    assert cli.main() == 2
    out = capsys.readouterr()
    payload = json.loads(out.err.strip())
    assert payload["ok"] is False
    assert payload["error"] == "rotation failed"

