"""Regression tests for live-bandwidth SSE DB session boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


class _Request:
    async def is_disconnected(self) -> bool:
        return True


class _Session:
    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_admin_live_bandwidth_releases_request_db_before_stream(monkeypatch):
    from app.api import bandwidth as bandwidth_api

    db = _Session()
    subscription_id = uuid4()
    checked: dict[str, object] = {}

    def check_access(session, sub_id, user):
        checked["session"] = session
        checked["subscription_id"] = sub_id
        checked["user"] = user

    monkeypatch.setattr(
        bandwidth_api.bandwidth_samples, "check_subscription_access", check_access
    )

    response = bandwidth_api.get_live_bandwidth(
        subscription_id=subscription_id,
        request=_Request(),
        db=db,
        current_user={"role": "admin"},
    )

    assert response is not None
    assert checked["session"] is db
    assert checked["subscription_id"] == subscription_id
    assert db.rolled_back is True
    assert db.closed is True


def test_customer_live_bandwidth_releases_request_db_before_stream(monkeypatch):
    from app.web.customer import routes

    db = _Session()
    subscription_id = uuid4()
    request = _Request()

    monkeypatch.setattr(
        routes,
        "get_current_customer_from_request",
        lambda req, session: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(
        routes,
        "resolve_customer_subscription",
        lambda session, customer: SimpleNamespace(id=subscription_id),
    )

    response = routes.customer_bandwidth_live(request=request, db=db)

    assert response is not None
    assert db.rolled_back is True
    assert db.closed is True
