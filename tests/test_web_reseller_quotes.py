"""Reseller portal Quotes web page: auth gate + registration."""

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.web.reseller.routes import router


def _client(db_session):
    app = FastAPI()
    # Bypass the router-level reseller auth guard; the page function does its own
    # context check (patched below) and redirects when unauthenticated.
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_get_redirects_to_login_when_anonymous(db_session):
    client = _client(db_session)
    with patch(
        "app.services.web_reseller_routes._require_reseller_context",
        return_value=None,
    ):
        r = client.get("/reseller/quotes", follow_redirects=False)
    assert r.status_code == 303
    assert "/reseller/auth/login" in r.headers["location"]


def test_route_is_registered():
    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/reseller/quotes" in paths
