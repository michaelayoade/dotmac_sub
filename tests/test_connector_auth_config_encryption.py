"""Connector ``auth_config`` is encrypted at rest (EncryptedJSON)."""

from __future__ import annotations

from sqlalchemy import text

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.types import EncryptedJSON


# --- unit tests on the type (no DB) -----------------------------------------


def test_encrypted_json_round_trip():
    t = EncryptedJSON()
    payload = {"password": "s3cret", "host": "h", "port": 22}
    stored = t.process_bind_param(payload, None)
    assert isinstance(stored, str)
    assert stored.startswith(("enc:", "plain:"))
    assert t.process_result_value(stored, None) == payload


def test_encrypted_json_none_and_empty():
    t = EncryptedJSON()
    assert t.process_bind_param(None, None) is None
    assert t.process_bind_param({}, None) == {}
    assert t.process_result_value(None, None) is None


def test_encrypted_json_reads_legacy_plaintext_dict():
    # Rows written before the change are stored as a JSON object (dict). The type
    # must return them unchanged so existing data keeps working.
    t = EncryptedJSON()
    legacy = {"token": "abc", "host": "h"}
    assert t.process_result_value(legacy, None) == legacy


# --- DB integration ----------------------------------------------------------


def test_connector_auth_config_persisted_encrypted(db_session):
    secret = "top-secret-bearer-token"
    config = ConnectorConfig(
        name="enc-test-connector",
        connector_type=ConnectorType.http,
        auth_type=ConnectorAuthType.bearer,
        auth_config={"bearer_token": secret, "host": "example.test"},
    )
    db_session.add(config)
    db_session.commit()
    db_session.expire_all()

    # ORM read is transparent — consumers see the plaintext dict.
    reloaded = db_session.get(ConnectorConfig, config.id)
    assert reloaded.auth_config == {"bearer_token": secret, "host": "example.test"}

    # The raw stored value is an at-rest blob, not a plaintext JSON object.
    raw = db_session.execute(
        text("SELECT auth_config FROM connector_configs WHERE id = :id"),
        {"id": str(config.id)},
    ).scalar()
    raw_str = raw if isinstance(raw, str) else str(raw)
    assert "enc:" in raw_str or "plain:" in raw_str
    # When a real key is configured the secret must not appear in ciphertext.
    if "enc:" in raw_str:
        assert secret not in raw_str
