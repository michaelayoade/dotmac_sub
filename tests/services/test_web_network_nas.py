from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import web_network_nas as web_nas_service


def _request_stub() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(),
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"user-agent": "pytest", "x-request-id": "req-1"},
    )


def test_create_device_returns_error_context(monkeypatch):
    request = _request_stub()
    db = MagicMock()

    monkeypatch.setattr(
        web_nas_service, "_base_context", lambda *_args, **_kwargs: {"request": request}
    )
    monkeypatch.setattr(web_nas_service, "_form_options", lambda _db: {"pop_sites": []})
    monkeypatch.setattr(
        web_nas_service, "parse_form_data_sync", lambda _req: {"name": "Router"}
    )

    def _fake_build_payload(_db, form, existing_tags, for_update):
        assert form["name"] == "Router"
        return None, ["Device name is required"]

    monkeypatch.setattr(
        web_nas_service.nas_service, "build_nas_device_payload", _fake_build_payload
    )

    result = web_nas_service.create_device(
        request,
        db,
        form_data={
            "name": "Router",
            "vendor": "mikrotik",
            "ip_address": "192.0.2.1",
            "status": "active",
            "radius_pool_ids": [],
            "partner_org_ids": [],
        },
    )

    assert result.redirect_url is None
    assert result.context is not None
    assert result.context["errors"] == ["Device name is required"]


@pytest.mark.parametrize("is_error", [False, True])
def test_connection_rule_redirect_encodes_message(monkeypatch, is_error):
    db = MagicMock()
    device_id = "dev-1"

    if is_error:

        def _raiser(*_args, **_kwargs):
            raise RuntimeError("bad rule")

        monkeypatch.setattr(
            web_nas_service.nas_service,
            "create_connection_rule_for_device",
            _raiser,
        )
    else:
        monkeypatch.setattr(
            web_nas_service.nas_service,
            "create_connection_rule_for_device",
            lambda *_args, **_kwargs: "Rule created",
        )

    url = web_nas_service.create_connection_rule(
        db,
        device_id=device_id,
        name="rule-1",
        connection_type=None,
        ip_assignment_mode=None,
        rate_limit_profile=None,
        match_expression=None,
        priority=10,
        notes=None,
    )

    assert url.startswith(
        f"/admin/network/nas/devices/{device_id}?tab=connection-rules"
    )
    if is_error:
        assert "rule_status=error" in url
        assert "bad+rule" in url
    else:
        assert "rule_status=success" in url
        assert "Rule+created" in url
