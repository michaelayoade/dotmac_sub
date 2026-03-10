from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.main import csrf_middleware


def _build_request(*, path: str = "/admin/billing") -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_csrf_middleware_reraises_no_response_when_request_is_still_connected(monkeypatch):
    request = _build_request()

    async def _connected() -> bool:
        return False

    monkeypatch.setattr(request, "is_disconnected", _connected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    with pytest.raises(RuntimeError, match="No response returned."):
        await csrf_middleware(request, call_next)


@pytest.mark.asyncio
async def test_csrf_middleware_returns_204_for_actual_disconnect(monkeypatch):
    request = _build_request()

    async def _disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", _disconnected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = await csrf_middleware(request, call_next)

    assert response.status_code == 204
