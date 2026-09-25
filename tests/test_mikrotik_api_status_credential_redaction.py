"""get_mikrotik_api_status must never persist or return a cleartext RouterOS
API password, even when both the REST and RouterOS-API status helpers raise
routeros_api-shaped exceptions carrying it.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.services.nas._mikrotik as mikrotik_mod
from app.models.catalog import NasDevice


class _FakeProvisioningLogSession:
    """Captures what would have been persisted, without touching a real DB."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:  # pragma: no cover - not expected on this path
        pass

    def close(self) -> None:
        pass


def test_get_mikrotik_api_status_never_persists_or_returns_the_password(
    monkeypatch,
):
    secret = "hunter2CLEARTEXT"
    rest_exc = RuntimeError(
        f"Error executing command b'/login =name=x =password={secret} .tag=1'"
    )
    api_exc = RuntimeError(
        f"Error executing command b'/login =name=x =password={secret} .tag=2'"
    )
    # Sensitivity proof: the fixtures genuinely carry the secret.
    assert secret in str(rest_exc)
    assert secret in str(api_exc)

    monkeypatch.setattr(
        mikrotik_mod, "_mikrotik_status_from_rest", MagicMock(side_effect=rest_exc)
    )
    monkeypatch.setattr(
        mikrotik_mod,
        "_mikrotik_status_from_routeros_api",
        MagicMock(side_effect=api_exc),
    )

    fake_session = _FakeProvisioningLogSession()
    monkeypatch.setattr(
        mikrotik_mod.db_session_adapter,
        "create_session",
        lambda: fake_session,
    )

    device = MagicMock(spec=NasDevice)
    device.id = uuid4()

    with pytest.raises(HTTPException) as excinfo:
        mikrotik_mod.get_mikrotik_api_status(device, db=MagicMock())

    # The returned HTTPException detail must not carry the secret.
    assert secret not in excinfo.value.detail
    assert "=password=<redacted>" in excinfo.value.detail

    # Neither auth-attempt ProvisioningLog persisted along the way may carry
    # the secret either (both the REST-failure record and the
    # RouterOS-API-failure record).
    persisted_errors = [log.error_message for log in fake_session.added]
    assert len(persisted_errors) == 2
    assert all(secret not in (msg or "") for msg in persisted_errors)
    assert any("=password=<redacted>" in (msg or "") for msg in persisted_errors)
