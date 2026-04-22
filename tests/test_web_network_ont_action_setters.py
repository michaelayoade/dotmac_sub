from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.network.ont_config_overrides import is_bundle_managed_ont
from app.services.web_network_ont_actions import config_setters


def test_is_bundle_managed_ont_requires_active_assignment(monkeypatch):
    db = MagicMock()
    ont = SimpleNamespace(provisioning_profile_id="legacy-profile-id")

    monkeypatch.setattr(
        "app.services.network.ont_config_overrides.get_active_bundle_assignment",
        lambda _db, _ont: None,
    )

    assert is_bundle_managed_ont(db, ont) is False


def test_set_pppoe_credentials_bundle_managed_persists_override(monkeypatch):
    db = MagicMock()
    ont = SimpleNamespace(
        serial_number="ONT-SETTER-001",
        pppoe_username=None,
        wan_vlan=None,
    )

    monkeypatch.setattr(
        config_setters,
        "_acs_config_writer",
        lambda: SimpleNamespace(
            set_pppoe_credentials=lambda *args, **kwargs: SimpleNamespace(
                success=True, message="ok", waiting=False
            )
        ),
    )
    monkeypatch.setattr(
        config_setters.network_service.ont_units,
        "get_including_inactive",
        lambda **kwargs: ont,
    )
    monkeypatch.setattr(
        config_setters,
        "is_bundle_managed_ont",
        lambda _db, _ont: True,
    )

    override_calls: list[dict[str, object]] = []

    def fake_override(*_args, **kwargs):
        override_calls.append(kwargs)

    monkeypatch.setattr(
        config_setters,
        "upsert_ont_config_override",
        fake_override,
    )
    monkeypatch.setattr(
        config_setters,
        "run_tracked_action",
        lambda _db, _op_type, _target_type, _target_id, fn, **_kwargs: fn(),
    )
    monkeypatch.setattr(config_setters, "_persist_ont_plan_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config_setters, "_log_action_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config_setters, "_intent_saved_result", lambda result: result)

    class FakeEventType:
        ont_pppoe_credentials_set = "ont_pppoe_credentials_set"

    monkeypatch.setitem(__import__("sys").modules, "app.services.events", SimpleNamespace(emit_event=lambda *args, **kwargs: None))
    monkeypatch.setitem(__import__("sys").modules, "app.services.events.types", SimpleNamespace(EventType=FakeEventType))

    result = config_setters.set_pppoe_credentials(
        db,
        "ont-1",
        "subscriber-user",
        "secret",
    )

    assert result.success is True
    assert ont.pppoe_username is None
    assert override_calls == [
        {
            "ont": ont,
            "field_name": "wan.pppoe_username",
            "value": "subscriber-user",
            "reason": "config_setters.set_pppoe_credentials",
        }
    ]
    assert db.flush.called
