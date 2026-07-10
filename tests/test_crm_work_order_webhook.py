"""Inbound CRM work-order webhook endpoint: HMAC gate + delegation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import HTTPException

from app.api.crm_webhooks import receive_crm_work_order_event
from app.config import settings
from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror

SECRET = "test-webhook-secret"


@contextmanager
def _with_secret(value: str):
    original = settings.crm_webhook_secret
    object.__setattr__(settings, "crm_webhook_secret", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_webhook_secret", original)


class _FakeRequest:
    def __init__(self, raw: bytes, headers: dict[str, str]):
        self._raw = raw
        self.headers = headers

    async def body(self) -> bytes:
        return self._raw


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _run(coro):
    box: dict[str, object] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


def _post(db_session, body: dict, event: str = "work_order.created", signature=...):
    raw = json.dumps(body).encode()
    headers = {"X-Webhook-Event": event, "Content-Type": "application/json"}
    sig = _sign(raw) if signature is ... else signature
    if sig is not None:
        headers["X-Webhook-Signature-256"] = sig
    try:
        payload = _run(
            receive_crm_work_order_event(_FakeRequest(raw, headers), db_session)
        )
    except HTTPException as exc:
        return exc.status_code, {"detail": exc.detail}
    return 200, payload


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="One",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_valid_event_reaches_service(db_session):
    sub = _subscriber(db_session)
    body = {
        "subscriber_id": str(sub.id),
        "work_order_id": "wo-1",
        "title": "Repair",
        "status": "scheduled",
    }
    with _with_secret(SECRET), patch("app.services.push.send_push"):
        code, resp = _post(db_session, body)
    assert code == 200, resp
    assert resp["status"] == "ok"
    assert (
        db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo-1").count()
        == 1
    )


def test_event_noop_when_pull_disabled(monkeypatch, db_session):
    """Flip kill switch: crm.work_order_pull off -> 200 ack, mirror untouched."""
    monkeypatch.setenv("CRM_WORK_ORDER_PULL_ENABLED", "false")
    sub = _subscriber(db_session)
    body = {
        "subscriber_id": str(sub.id),
        "work_order_id": "wo-killed",
        "title": "Repair",
        "status": "scheduled",
    }
    with _with_secret(SECRET), patch("app.services.push.send_push") as push:
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp == {
        "status": "ignored",
        "reason": "work_order_pull_disabled",
        "event": "work_order.created",
    }
    push.assert_not_called()
    assert (
        db_session.query(WorkOrderMirror)
        .filter_by(crm_work_order_id="wo-killed")
        .count()
        == 0
    )


def test_event_processes_when_setting_missing(monkeypatch, db_session):
    """No env, no DB row -> the control's on_missing default (ON) applies:
    the switch is inert until the Phase 2 flip deliberately turns it off."""
    monkeypatch.delenv("CRM_WORK_ORDER_PULL_ENABLED", raising=False)
    sub = _subscriber(db_session)
    body = {
        "subscriber_id": str(sub.id),
        "work_order_id": "wo-default-on",
        "title": "Repair",
        "status": "scheduled",
    }
    with _with_secret(SECRET), patch("app.services.push.send_push"):
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["status"] == "ok"


def test_branch_gated_by_scheduler_db_row(monkeypatch, db_session):
    """The exact flip lever: the legacy scheduler.crm_work_order_pull_enabled
    DB row turns the branch on and off (no env, no deploy)."""
    from app.models.domain_settings import DomainSetting, SettingDomain

    monkeypatch.delenv("CRM_WORK_ORDER_PULL_ENABLED", raising=False)
    row = DomainSetting(
        domain=SettingDomain.scheduler,
        key="crm_work_order_pull_enabled",
        value_text="false",
    )
    db_session.add(row)
    db_session.commit()

    sub = _subscriber(db_session)
    body = {
        "subscriber_id": str(sub.id),
        "work_order_id": "wo-row-gated",
        "title": "Repair",
        "status": "scheduled",
    }
    with _with_secret(SECRET), patch("app.services.push.send_push"):
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["reason"] == "work_order_pull_disabled"

    row.value_text = "true"
    db_session.commit()
    with _with_secret(SECRET), patch("app.services.push.send_push"):
        code, resp = _post(db_session, body)
    assert code == 200
    assert resp["status"] == "ok"


def test_bad_signature_rejected(db_session):
    body = {"subscriber_id": str(uuid.uuid4()), "work_order_id": "x"}
    with _with_secret(SECRET):
        code, _ = _post(db_session, body, signature="sha256=deadbeef")
    assert code == 401


def test_unknown_event_ignored(db_session):
    body = {"subscriber_id": str(uuid.uuid4()), "work_order_id": "x"}
    with _with_secret(SECRET):
        code, resp = _post(db_session, body, event="work_order.archived")
    assert code == 200
    assert resp["status"] == "ignored"
