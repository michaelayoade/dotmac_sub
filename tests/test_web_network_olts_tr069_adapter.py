"""Regression tests for OLT TR-069 web route result adapters."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

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


class _SyncFormRequest(_JsonRequest):
    """Mock request for sync routes that use parse_form_data_sync."""

    def __init__(self, form: _FormData, *, auth: dict | None = None):
        self._form = form
        self.state = SimpleNamespace(auth=auth)
        self.url = SimpleNamespace(path="/admin/network/olts/olt-1/tr069/rebind")

    def get_form(self) -> _FormData:
        return self._form


def _json_body(response) -> dict:
    return json.loads(response.body.decode())


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


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


def test_tr069_rebind_uses_operation_result_json(monkeypatch) -> None:
    ont_ids = [str(uuid4()), str(uuid4())]
    form_data = _FormData(target_profile_id="7", ont_ids=ont_ids)
    request = _SyncFormRequest(form_data, auth={"principal_type": "system_user"})

    monkeypatch.setattr(
        network_olts_profiles.olt_tr069_admin_service,
        "handle_rebind_tr069_profiles_audited",
        lambda *args, **kwargs: {"rebound": 2, "failed": 0, "errors": []},
    )
    monkeypatch.setattr(
        network_olts_profiles,
        "parse_form_data_sync",
        lambda req: req.get_form(),
    )

    response = network_olts_profiles.olt_tr069_rebind(
        request,
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


def test_tr069_rebind_invalid_profile_returns_adapter_error(monkeypatch) -> None:
    form_data = _FormData(target_profile_id="not-an-int", ont_ids=[str(uuid4())])
    request = _SyncFormRequest(form_data, auth={"principal_type": "system_user"})

    monkeypatch.setattr(
        network_olts_profiles,
        "parse_form_data_sync",
        lambda req: req.get_form(),
    )

    response = network_olts_profiles.olt_tr069_rebind(
        request,
        "olt-1",
        db=SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(id="ignored")),
    )

    assert response.status_code == 400
    assert _json_body(response) == {
        "success": False,
        "status": "error",
        "message": "Missing ONT selection or target profile",
    }
