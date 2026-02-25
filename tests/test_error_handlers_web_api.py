from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.errors import register_error_handlers
from app.observability import ObservabilityMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    register_error_handlers(app)

    api_router = APIRouter(prefix="/api/v1")

    @app.get("/web-http-403")
    def web_http_403():
        raise HTTPException(status_code=403, detail="Forbidden area")

    @app.get("/web-http-409")
    def web_http_409():
        raise HTTPException(status_code=409, detail="Record already exists")

    @app.get("/web-crash")
    def web_crash():
        raise RuntimeError("boom")

    @app.get("/redirect-error-known")
    def redirect_error_known():
        return {"ok": True}

    @app.get("/redirect-error-unknown")
    def redirect_error_unknown():
        return {"ok": True}

    @api_router.get("/http-403")
    def api_http_403():
        raise HTTPException(status_code=403, detail="Forbidden api")

    @api_router.get("/needs-int")
    def api_needs_int(value: int):
        return {"value": value}

    app.include_router(api_router)
    return app


def test_web_404_renders_html_template() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/missing-page", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Page not found" in resp.text
    assert "Reference ID:" in resp.text


def test_web_http_exception_renders_html_template() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/web-http-403", headers={"accept": "text/html"})
    assert resp.status_code == 403
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Forbidden area" in resp.text


def test_web_500_renders_html_template() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/web-crash", headers={"accept": "text/html"})
    assert resp.status_code == 500
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Server error" in resp.text


def test_api_http_exception_returns_json() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/api/v1/http-403", headers={"accept": "text/html"})
    assert resp.status_code == 403
    assert "application/json" in resp.headers.get("content-type", "")
    body = resp.json()
    assert body["code"] == "http_403"
    assert body["message"] == "Forbidden api"
    assert "request_id" in body


def test_api_validation_error_returns_json() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/api/v1/needs-int", params={"value": "abc"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Validation error"
    assert isinstance(body["details"], list)


def test_htmx_request_stays_json_not_template() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get(
        "/web-http-409",
        headers={"accept": "text/html", "HX-Request": "true"},
    )
    assert resp.status_code == 409
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "http_409"


def test_redirect_error_known_token_converts_to_template() -> None:
    app = _build_app()

    @app.get("/redirect-known")
    def redirect_known():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/somewhere?error=not_found", status_code=303)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/redirect-known", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "could not be found" in resp.text


def test_redirect_error_unknown_token_keeps_redirect() -> None:
    app = _build_app()

    @app.get("/redirect-unknown")
    def redirect_unknown():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/somewhere?error=post_failed", status_code=303)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/redirect-unknown", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/somewhere?error=post_failed"
