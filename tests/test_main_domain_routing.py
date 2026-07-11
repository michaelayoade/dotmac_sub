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


def _request(
    path: str = "/", host: str = "example.com", method: str = "GET"
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
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


def test_domain_routing_uses_stale_cache_when_refresh_fails(monkeypatch):
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

    response = _run_async(
        main.domain_routing_middleware(
            _request(host="portal.example.com"),
            _downstream_response,
        )
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/portal/home"


def test_domain_routing_non_root_paths_do_not_touch_db(monkeypatch):
    def _fail_session():
        raise AssertionError("domain routing should not open a DB session")

    monkeypatch.setattr(main, "SessionLocal", _fail_session)

    response = _run_async(
        main.domain_routing_middleware(
            _request(path="/admin/dashboard", host="portal.example.com"),
            _downstream_response,
        )
    )

    assert response.status_code == 200
    assert response.body == b"ok"


def test_domain_routing_allows_request_when_refresh_fails_without_cache(
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

    response = _run_async(
        main.domain_routing_middleware(
            _request(path="/admin/dashboard", host="admin.example.com"),
            _downstream_response,
        )
    )

    assert response.status_code == 200
    assert response.body == b"ok"


def test_audit_middleware_skips_webhooks_without_db(monkeypatch):
    def _fail_session():
        raise AssertionError("audit middleware should not open a DB session")

    monkeypatch.setattr(main, "SessionLocal", _fail_session)

    response = _run_async(
        main.audit_middleware(
            _request(path="/api/v1/webhooks/crm/chat", method="POST"),
            _downstream_response,
        )
    )

    assert response.status_code == 200
    assert response.body == b"ok"


def test_audit_middleware_fails_open_when_settings_refresh_fails(monkeypatch):
    monkeypatch.setattr(main, "_AUDIT_SETTINGS_CACHE", None)
    monkeypatch.setattr(main, "_AUDIT_SETTINGS_CACHE_AT", None)
    monkeypatch.setattr(main, "SessionLocal", lambda: _DummySession())

    def _raise_operational_error(_db):
        raise OperationalError("select 1", {}, RuntimeError("too many clients"))

    monkeypatch.setattr(main, "_load_audit_settings", _raise_operational_error)

    response = _run_async(
        main.audit_middleware(
            _request(path="/portal/payments", method="POST"),
            _downstream_response,
        )
    )

    assert response.status_code == 200
    assert response.body == b"ok"


def test_audit_middleware_uses_stale_settings_without_db_refresh(monkeypatch):
    monkeypatch.setattr(
        main,
        "_AUDIT_SETTINGS_CACHE",
        {
            "enabled": True,
            "methods": {"POST"},
            "skip_paths": [],
            "read_trigger_header": "x-audit-read",
            "read_trigger_query": "audit",
        },
    )
    monkeypatch.setattr(
        main,
        "_AUDIT_SETTINGS_CACHE_AT",
        monotonic() - main._AUDIT_SETTINGS_CACHE_TTL_SECONDS - 1,
    )
    monkeypatch.setattr(
        main,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(
            AssertionError("stale audit settings should not refresh through DB")
        ),
    )

    audit_calls = []

    monkeypatch.setattr(
        main,
        "_try_log_audit_request",
        lambda request, response: audit_calls.append(
            (request.url.path, response.status_code)
        ),
    )

    response = _run_async(
        main.audit_middleware(
            _request(path="/portal/payments", method="POST"),
            _downstream_response,
        )
    )

    assert response.status_code == 200
    assert response.body == b"ok"
    assert audit_calls == [("/portal/payments", 200)]


class _PostgresBind:
    class dialect:
        name = "postgresql"


class _AuditSettingsQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _AuditSettingsSession:
    def __init__(self):
        self.statements = []

    def get_bind(self):
        return _PostgresBind()

    def execute(self, statement):
        self.statements.append(str(statement))

    def query(self, *_args, **_kwargs):
        return _AuditSettingsQuery()


def test_load_audit_settings_sets_short_postgres_statement_timeout(monkeypatch):
    monkeypatch.setattr(main, "_AUDIT_SETTINGS_CACHE", None)
    monkeypatch.setattr(main, "_AUDIT_SETTINGS_CACHE_AT", None)
    db = _AuditSettingsSession()

    settings = main._load_audit_settings(db)

    assert settings["enabled"] is True
    assert any(
        f"statement_timeout = '{main._AUDIT_SETTINGS_REFRESH_TIMEOUT_MS}ms'" in item
        for item in db.statements
    )


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
