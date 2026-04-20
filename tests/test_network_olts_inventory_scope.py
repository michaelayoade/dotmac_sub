from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request


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
        raise AssertionError("mismatched direct ONT authorization must not queue")

    monkeypatch.setattr(
        network_olts_inventory.olt_operations_service,
        "queue_authorize_autofind_ont",
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
