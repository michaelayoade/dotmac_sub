"""Regression tests for OLT TR-069 web route result adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.web.admin import network_olts, network_olts_profiles


class _JsonRequest:
    headers = {"Accept": "application/json"}


class _FormData(dict):
    def getlist(self, key: str) -> list[str]:
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _AsyncFormRequest(_JsonRequest):
    def __init__(self, form: _FormData):
        self._form = form

    async def form(self) -> _FormData:
        return self._form


def _json_body(response) -> dict:
    return json.loads(response.body.decode())


@pytest.mark.parametrize("module", [network_olts, network_olts_profiles])
def test_tr069_profile_create_uses_operation_result_json(module, monkeypatch) -> None:
    monkeypatch.setattr(
        module.olt_tr069_admin_service,
        "handle_create_tr069_profile_audited",
        lambda *args, **kwargs: (True, "Profile created"),
    )

    response = module.olt_tr069_profile_create(
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


@pytest.mark.parametrize("module", [network_olts, network_olts_profiles])
@pytest.mark.asyncio
async def test_tr069_rebind_uses_operation_result_json(module, monkeypatch) -> None:
    monkeypatch.setattr(
        module.olt_tr069_admin_service,
        "handle_rebind_tr069_profiles_audited",
        lambda *args, **kwargs: {"rebound": 2, "failed": 0, "errors": []},
    )

    response = await module.olt_tr069_rebind(
        _AsyncFormRequest(_FormData(target_profile_id="7", ont_ids=["ont-1", "ont-2"])),
        "olt-1",
        db=SimpleNamespace(),
    )

    body = _json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["status"] == "success"
    assert body["message"] == "Rebound 2 ONT(s) to profile 7"
    assert body["data"] == {"rebound": 2, "failed": 0, "errors": []}


@pytest.mark.parametrize("module", [network_olts, network_olts_profiles])
@pytest.mark.asyncio
async def test_tr069_rebind_invalid_profile_returns_adapter_error(module) -> None:
    response = await module.olt_tr069_rebind(
        _AsyncFormRequest(_FormData(target_profile_id="not-an-int", ont_ids=["ont-1"])),
        "olt-1",
        db=SimpleNamespace(),
    )

    assert response.status_code == 400
    assert _json_body(response) == {
        "success": False,
        "status": "error",
        "message": "Missing ONT selection or target profile",
    }
