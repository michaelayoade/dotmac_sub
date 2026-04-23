from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.network.ont_config_overrides import is_bundle_managed_ont


def test_is_bundle_managed_ont_requires_active_assignment(monkeypatch):
    db = MagicMock()
    ont = SimpleNamespace(provisioning_profile_id="legacy-profile-id")

    monkeypatch.setattr(
        "app.services.network.ont_config_overrides.get_active_bundle_assignment",
        lambda _db, _ont: None,
    )

    assert is_bundle_managed_ont(db, ont) is False
