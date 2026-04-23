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


def test_set_pppoe_credentials_legacy_endpoint_is_disabled(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(config_setters, "_log_action_audit", lambda *_args, **_kwargs: None)

    result = config_setters.set_pppoe_credentials(
        db,
        "ont-1",
        "subscriber-user",
        "secret",
    )

    assert result.success is False
    assert "Legacy PPPoE TR-069 pushes are disabled" in result.message
