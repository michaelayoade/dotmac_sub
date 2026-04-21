"""Regression tests for OLT TR-069 web route result adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.web.admin import network_olts_profiles


class _JsonRequest:
    headers = {"Accept": "application/json"}


class _FormData(dict):
    def getlist(self, key: str) -> list[str]:
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _AsyncFormRequest(_JsonRequest):
    def __init__(self, form: _FormData, *, auth: dict | None = None):
        self._form = form
        self.state = SimpleNamespace(auth=auth)
        self.url = SimpleNamespace(path="/admin/network/olts/olt-1/tr069/rebind")

    async def form(self) -> _FormData:
        return self._form


def _json_body(response) -> dict:
    return json.loads(response.body.decode())


def test_tr069_profile_create_uses_operation_result_json(monkeypatch) -> None:
    monkeypatch.setattr(
        network_olts_profiles.olt_tr069_admin_service,
        "handle_create_tr069_profile_audited",
        lambda *args, **kwargs: (True, "Profile created"),
    )

    response = network_olts_profiles.olt_tr069_profile_create(
        _JsonRequest(),
        "olt-1",
        profile_name="ACS",
        acs_url="http://acs.example:7547",
        acs_username="",
        acs_password="",
        inform_interval=300,
        db=SimpleNamespace(),
    )

    assert response.status_code == 200
    assert _json_body(response) == {
        "success": True,
        "status": "success",
        "message": "Profile created",
    }


@pytest.mark.asyncio
async def test_tr069_rebind_uses_operation_result_json(monkeypatch) -> None:
    ont_ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setattr(
        network_olts_profiles.olt_tr069_admin_service,
        "handle_rebind_tr069_profiles_audited",
        lambda *args, **kwargs: {"rebound": 2, "failed": 0, "errors": []},
    )

    response = await network_olts_profiles.olt_tr069_rebind(
        _AsyncFormRequest(
            _FormData(target_profile_id="7", ont_ids=ont_ids),
            auth={"principal_type": "system_user"},
        ),
        "olt-1",
        db=SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(id=ont_ids[0])),
    )

    body = _json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["status"] == "success"
    assert body["message"] == "Rebound 2 ONT(s) to profile 7"
    assert body["data"] == {
        "rebound": 2,
        "failed": 0,
        "errors": [],
        "skipped_out_of_scope": 0,
    }


@pytest.mark.asyncio
async def test_tr069_rebind_invalid_profile_returns_adapter_error() -> None:
    response = await network_olts_profiles.olt_tr069_rebind(
        _AsyncFormRequest(
            _FormData(target_profile_id="not-an-int", ont_ids=[str(uuid4())]),
            auth={"principal_type": "system_user"},
        ),
        "olt-1",
        db=SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(id="ignored")),
    )

    assert response.status_code == 400
    assert _json_body(response) == {
        "success": False,
        "status": "error",
        "message": "Missing ONT selection or target profile",
    }
