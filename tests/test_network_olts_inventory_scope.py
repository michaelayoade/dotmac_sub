from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.models.subscriber import UserType


def _request(*, htmx: bool = False) -> Request:
    headers = [(b"hx-request", b"true")] if htmx else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/olts/authorize-ont",
            "headers": headers,
            "query_string": b"",
        }
    )


def test_direct_ont_authorize_rejects_mismatched_olt(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    olt_id = uuid4()
    other_olt_id = uuid4()
    ont_id = uuid4()

    class FakeDb:
        def get(self, *_args):
            return SimpleNamespace(
                olt_device_id=other_olt_id,
                serial_number="HWTC12345678",
            )

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("mismatched direct ONT authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "authorize_ont",
        _unexpected_queue,
    )

    response = network_olts_inventory.olt_authorize_ont(
        _request(),
        str(olt_id),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id=str(ont_id),
        db=FakeDb(),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_reseller_authorize_without_scoped_ont_is_rejected(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    actor_id = uuid4()

    class FakeDb:
        def get(self, *_args):
            return SimpleNamespace(
                id=actor_id,
                user_type=UserType.reseller,
                reseller_id=uuid4(),
            )

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("unscoped reseller authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "authorize_ont",
        _unexpected_queue,
    )

    request = _request()
    request.state.auth = {
        "principal_id": str(actor_id),
        "principal_type": "subscriber",
        "roles": [],
        "scopes": ["network:write"],
    }
    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(uuid4()),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id="",
        db=FakeDb(),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_authorize_without_request_auth_is_rejected(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("missing-auth authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "authorize_ont",
        _unexpected_queue,
    )

    response = network_olts_inventory.olt_authorize_ont(
        _request(),
        str(uuid4()),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id=str(uuid4()),
        db=SimpleNamespace(get=lambda *_args, **_kwargs: None),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_filter_manageable_ont_ids_requires_request_auth() -> None:
    from app.services.network.ont_scope import filter_manageable_ont_ids_from_request

    request = _request()

    scoped = filter_manageable_ont_ids_from_request(
        request,
        db=SimpleNamespace(),
        ont_ids=[str(uuid4()), str(uuid4())],
    )

    assert scoped == []
