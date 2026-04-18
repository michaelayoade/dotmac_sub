from __future__ import annotations

from starlette.requests import Request

from app import observability as observability_module


def _build_request(path: str) -> Request:
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


def test_should_skip_observability_for_health_and_metrics():
    assert observability_module._should_skip_observability("/health") is True
    assert observability_module._should_skip_observability("/metrics") is True
    assert observability_module._should_skip_observability("/admin/billing") is False


def test_request_path_prefers_route_template():
    request = _build_request("/admin/network/olts/123")
    request.scope["route"] = type(
        "Route", (), {"path": "/admin/network/olts/{olt_id}"}
    )()

    assert observability_module._request_path(request) == "/admin/network/olts/{olt_id}"
