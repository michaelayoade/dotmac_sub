from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.web.admin import system as admin_system


def test_require_system_user_principal_accepts_system_user():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "system_user"})
    )

    auth = admin_system._require_system_user_principal(request)

    assert auth["principal_type"] == "system_user"


def test_require_system_user_principal_rejects_subscriber():
    request = SimpleNamespace(
        state=SimpleNamespace(auth={"principal_type": "subscriber"})
    )

    with pytest.raises(HTTPException) as exc:
        admin_system._require_system_user_principal(request)

    assert exc.value.status_code == 403


def test_dbi_principal_id_prefers_stable_actor_id(monkeypatch):
    request = SimpleNamespace()
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"subscriber_id": "system-user-1", "id": "system-user-1"},
    )

    assert admin_system._dbi_principal_id(request) == "system-user-1"
