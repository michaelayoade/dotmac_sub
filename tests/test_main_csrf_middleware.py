from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from starlette.requests import Request
from starlette.responses import Response

from app.main import csrf_middleware


def _build_request(
    *,
    path: str = "/admin/billing",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _run_async(awaitable):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, awaitable)
        return future.result()


def test_csrf_middleware_returns_204_when_no_response_and_request_still_connected(
    monkeypatch, caplog
):
    request = _build_request()

    async def _connected() -> bool:
        return False

    monkeypatch.setattr(request, "is_disconnected", _connected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 204
    assert "reload_or_shutdown" in caplog.text
    assert any(record.levelname == "INFO" for record in caplog.records)


def test_csrf_middleware_returns_204_for_actual_disconnect(monkeypatch):
    request = _build_request()

    async def _disconnected() -> bool:
        return True

    monkeypatch.setattr(request, "is_disconnected", _disconnected)

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("No response returned.")

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 204


def test_csrf_middleware_exempts_customer_logout_without_token():
    request = _build_request(path="/portal/auth/logout", method="POST")

    async def call_next(_request: Request) -> Response:
        return Response(status_code=303, headers={"location": "/portal/auth/login"})

    response = _run_async(csrf_middleware(request, call_next))

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/auth/login"
