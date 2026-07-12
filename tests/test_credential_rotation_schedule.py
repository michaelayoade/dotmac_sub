from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import credential_rotation_schedule as rotation


def _managed_setting(db_session) -> None:
    db_session.add(
        DomainSetting(
            domain=SettingDomain.auth,
            key="credential_encryption_key",
            value_type=SettingValueType.string,
            value_text=("bao://secret/settings/auth#credential_encryption_key"),
            is_secret=True,
            is_active=True,
        )
    )
    db_session.commit()


def _patch_settings(monkeypatch, *, auto_apply: bool = True) -> None:
    values = {
        "credential_rotation_enabled": True,
        "credential_rotation_auto_apply": auto_apply,
        "credential_rotation_interval_days": 90,
        "credential_rotation_grace_days": 7,
    }
    monkeypatch.setattr(
        rotation,
        "resolve_value",
        lambda _db, _domain, key: values[key],
    )


def test_static_environment_key_blocks_scheduled_rotation(db_session, monkeypatch):
    _patch_settings(monkeypatch)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "literal-key")

    result = rotation.evaluate_scheduled_rotation(db_session)

    assert result == {
        "status": "blocked",
        "reason": "static_environment_key",
        "rotated": False,
    }


def test_first_managed_run_initializes_rotation_clock(db_session, monkeypatch):
    _managed_setting(db_session)
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {rotation._CURRENT_FIELD: "current-key"},
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"current-key")
    writes: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_write_keyring",
        lambda payload: writes.append(payload) or True,
    )

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert result["status"] == "initialized"
    assert writes[0][rotation._ROTATED_AT_FIELD] == now.isoformat()


def test_due_rotation_stages_dual_key_before_reencrypting(db_session, monkeypatch):
    _managed_setting(db_session)
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {
            rotation._CURRENT_FIELD: "current-key",
            rotation._ROTATED_AT_FIELD: (now - timedelta(days=91)).isoformat(),
        },
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"current-key")
    monkeypatch.setattr(rotation, "generate_encryption_key", lambda: "new-key")
    calls: list[str] = []
    staged: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_write_keyring",
        lambda payload: calls.append("stage") or staged.append(payload) or True,
    )

    class _Result:
        updated_records = 4
        updated_values = 6

    def _rotate(*_args, **kwargs):
        calls.append("rotate")
        assert kwargs["old_key"] == "current-key"
        assert kwargs["new_key"] == "new-key"
        return _Result()

    monkeypatch.setattr(rotation, "rotate_credential_encryption_material", _rotate)
    monkeypatch.setattr(rotation, "clear_cache", lambda: None)

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert calls == ["stage", "rotate"]
    assert result["status"] == "rotated"
    assert staged[0][rotation._PREVIOUS_FIELD] == "current-key"
    assert staged[0][rotation._CURRENT_FIELD] == "new-key"


def test_grace_period_converges_before_previous_key_retirement(db_session, monkeypatch):
    _managed_setting(db_session)
    _patch_settings(monkeypatch)
    now = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(rotation, "is_openbao_available", lambda: True)
    monkeypatch.setattr(
        rotation,
        "_keyring_payload",
        lambda: {
            rotation._CURRENT_FIELD: "new-key",
            rotation._PREVIOUS_FIELD: "old-key",
            rotation._RETIRE_AFTER_FIELD: (now - timedelta(days=1)).isoformat(),
        },
    )
    monkeypatch.setattr(rotation, "get_encryption_key", lambda: b"new-key")

    class _Result:
        updated_records = 1
        updated_values = 1

    monkeypatch.setattr(
        rotation,
        "rotate_credential_encryption_material",
        lambda *_args, **_kwargs: _Result(),
    )
    retired: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotation,
        "_retire_previous_key",
        lambda payload: retired.append(payload) or True,
    )
    monkeypatch.setattr(rotation, "clear_cache", lambda: None)

    result = rotation.evaluate_scheduled_rotation(db_session, now=now)

    assert result["status"] == "previous_key_retired"
    assert retired
