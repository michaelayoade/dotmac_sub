"""Regression test: the mikrotik-live endpoint must release its pooled DB
session BEFORE the blocking RouterOS network call, so an unreachable/slow router
cannot pin a connection idle-in-transaction and starve the pool."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Session:
    def __init__(self, get_return: object) -> None:
        self._get_return = get_return
        self.rolled_back = False
        self.closed = False
        self.expunged: list[object] = []

    def get(self, _model, _pk):
        return self._get_return

    def expunge(self, obj) -> None:
        self.expunged.append(obj)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_mikrotik_live_releases_db_before_router_call(monkeypatch):
    from app.api import bandwidth as bandwidth_api

    nas_device = SimpleNamespace(id=uuid4(), name="nas-1")
    subscription = SimpleNamespace(provisioning_nas_device=nas_device, login="pppoe1")
    db = _Session(get_return=subscription)

    monkeypatch.setattr(
        bandwidth_api.bandwidth_samples,
        "check_subscription_access",
        lambda *a, **k: None,
    )

    observed: dict[str, object] = {}

    def fake_router(device, *, login):
        # Captured at the moment the blocking call would run.
        observed["closed_at_call"] = db.closed
        observed["rolled_back_at_call"] = db.rolled_back
        observed["device"] = device
        observed["login"] = login
        return {"online": True}

    monkeypatch.setattr(bandwidth_api, "get_mikrotik_pppoe_live_bandwidth", fake_router)

    result = bandwidth_api.get_mikrotik_live_bandwidth(
        subscription_id=uuid4(), db=db, current_user={"role": "admin"}
    )

    assert result == {"online": True}
    # Session released, and released BEFORE the router call (the hot part).
    assert db.rolled_back is True and db.closed is True
    assert observed["closed_at_call"] is True
    assert observed["rolled_back_at_call"] is True
    # The NAS device was detached so its loaded columns stay readable post-close.
    assert nas_device in db.expunged
    assert observed["device"] is nas_device
    assert observed["login"] == "pppoe1"


def test_mikrotik_live_missing_nas_device_still_400(monkeypatch):
    from fastapi import HTTPException

    from app.api import bandwidth as bandwidth_api

    subscription = SimpleNamespace(provisioning_nas_device=None, login="x")
    db = _Session(get_return=subscription)
    monkeypatch.setattr(
        bandwidth_api.bandwidth_samples,
        "check_subscription_access",
        lambda *a, **k: None,
    )

    with pytest.raises(HTTPException) as exc:
        bandwidth_api.get_mikrotik_live_bandwidth(
            subscription_id=uuid4(), db=db, current_user={"role": "admin"}
        )
    assert exc.value.status_code == 400
