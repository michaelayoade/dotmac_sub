import asyncio
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from starlette.responses import Response

from app import main


def _run_async(coro):
    # Run coroutine in a dedicated thread to avoid nested event loops from anyio/pytest-asyncio.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


def _request(path: str = "/", host: str = "example.com") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


class _DummySession:
    def close(self) -> None:
        pass


async def _downstream_response(_request: Request) -> Response:
    return Response("ok", status_code=200)


async def test_domain_routing_uses_stale_cache_when_refresh_fails(monkeypatch):
    monkeypatch.setattr(
        main,
        "_domain_routing_cache",
        {
            "ts": monotonic() - 31,
            "selfcare": "portal.example.com",
            "redirect": "/portal/home",
        },
    )
    monkeypatch.setattr(main, "SessionLocal", lambda: _DummySession())

    def _raise_operational_error(_db):
        raise OperationalError("select 1", {}, RuntimeError("too many clients"))

    monkeypatch.setattr(main, "_load_domain_routing", _raise_operational_error)

    response = await main.domain_routing_middleware(
        _request(host="portal.example.com"),
        _downstream_response,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/portal/home"


async def test_domain_routing_allows_request_when_refresh_fails_without_cache(
    monkeypatch,
):
    monkeypatch.setattr(
        main,
        "_domain_routing_cache",
        {
            "ts": 0.0,
            "selfcare": "",
            "redirect": "/portal/",
        },
    )
    monkeypatch.setattr(main, "SessionLocal", lambda: _DummySession())

    def _raise_operational_error(_db):
        raise OperationalError("select 1", {}, RuntimeError("too many clients"))

    monkeypatch.setattr(main, "_load_domain_routing", _raise_operational_error)

    response = await main.domain_routing_middleware(
        _request(path="/admin/dashboard", host="admin.example.com"),
        _downstream_response,
    )

    assert response.status_code == 200
    assert response.body == b"ok"


def test_grafana_webhook_sink_accepts_alert_posts():
    async def _receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/alerts/grafana-webhook",
            "raw_path": b"/api/v1/alerts/grafana-webhook",
            "query_string": b"",
            "headers": [(b"host", b"example.com")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive=_receive,
    )

    response = _run_async(main.grafana_webhook_sink(request))

    assert response.status_code == 204
