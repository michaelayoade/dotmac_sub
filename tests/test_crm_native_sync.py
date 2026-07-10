"""Phase 3 sync-window adapter (PR 9): CRM webhook events → native rows, and
the delta beat task gating. Transitional glue deleted at the Phase 3 contract
(PR 15) — these tests pin the flag gating (default OFF), the thin-delta
semantics (existing rows only; full shapes come from the delta beat), and the
never-raise contract of the webhook path."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app.api.crm_webhooks import receive_crm_project_event
from app.config import settings
from app.models.project import Project
from app.models.referral_native import Referral
from app.models.sales import Quote
from app.models.subscriber import Subscriber
from app.services import crm_native_sync

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Native",
        last_name="Sync",
        email=f"ns-{uuid.uuid4().hex[:10]}@example.com",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _project(db, sub, **kw) -> Project:
    row = Project(id=uuid.uuid4(), name="Install", status="open", subscriber_id=sub.id)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _quote(db, sub, **kw) -> Quote:
    row = Quote(id=uuid.uuid4(), subscriber_id=sub.id, status="sent")
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _referral(db, sub, **kw) -> Referral:
    row = Referral(id=uuid.uuid4(), referrer_subscriber_id=sub.id, status="pending")
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setattr(crm_native_sync, "is_enabled", lambda db: True)


# ---------------------------------------------------------------------------
# flag gating
# ---------------------------------------------------------------------------


def test_flag_defaults_off(db_session):
    # Spec default False — CRM stays the writer; native rows track only once
    # the sync window opens.
    assert crm_native_sync.is_enabled(db_session) is False


def test_apply_webhook_delta_noop_when_flag_off(db_session):
    sub = _subscriber(db_session)
    row = _project(db_session, sub)
    out = crm_native_sync.apply_webhook_delta(
        db_session,
        "project",
        "project.completed",
        {"project_id": str(row.id), "status": "completed"},
    )
    assert out == {"status": "skipped", "reason": "native_sync_disabled"}
    db_session.refresh(row)
    assert row.status == "open"
    assert row.completed_at is None


def test_unknown_vertical_is_skipped(db_session, flag_on):
    out = crm_native_sync.apply_webhook_delta(
        db_session, "work_order", "work_order.completed", {"id": str(uuid.uuid4())}
    )
    assert out == {"status": "skipped", "reason": "unknown_vertical"}


def test_adapter_never_raises(db_session, flag_on, monkeypatch):
    def _boom(db, event_type, body):
        raise RuntimeError("adapter exploded")

    monkeypatch.setitem(crm_native_sync._HANDLERS, "project", _boom)
    out = crm_native_sync.apply_webhook_delta(
        db_session, "project", "project.updated", {"project_id": str(uuid.uuid4())}
    )
    assert out == {"status": "error", "reason": "adapter_failed"}


# ---------------------------------------------------------------------------
# project deltas
# ---------------------------------------------------------------------------


def test_project_completed_updates_native_row(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _project(db_session, sub, status="active")
    out = crm_native_sync.apply_webhook_delta(
        db_session,
        "project",
        "project.completed",
        {
            "project_id": str(row.id),
            "status": "completed",
            "completed_at": "2026-07-08T10:00:00Z",
        },
    )
    assert out["status"] == "ok"
    db_session.refresh(row)
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.completed_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 8, 10, 0, tzinfo=UTC
    )


def test_project_completed_infers_status_when_payload_omits_it(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _project(db_session, sub, status="active")
    crm_native_sync.apply_webhook_delta(
        db_session, "project", "project.completed", {"project_id": str(row.id)}
    )
    db_session.refresh(row)
    assert row.status == "completed"
    assert row.completed_at is not None


def test_project_unknown_status_not_applied(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _project(db_session, sub, status="active")
    out = crm_native_sync.apply_webhook_delta(
        db_session,
        "project",
        "project.updated",
        {"project_id": str(row.id), "status": "definitely_not_a_status"},
    )
    assert out["status"] == "ok"
    db_session.refresh(row)
    assert row.status == "active"


def test_project_missing_native_row_defers_to_beat(db_session, flag_on):
    # project.created for a row the delta beat hasn't imported yet: the thin
    # payload can't synthesize the full CRM shape, so the adapter defers.
    out = crm_native_sync.apply_webhook_delta(
        db_session,
        "project",
        "project.created",
        {"project_id": str(uuid.uuid4()), "status": "open"},
    )
    assert out == {"status": "skipped", "reason": "native_row_missing"}


def test_project_task_events_defer_to_beat(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _project(db_session, sub)
    out = crm_native_sync.apply_webhook_delta(
        db_session,
        "project",
        "project_task.completed",
        {"project_id": str(row.id)},
    )
    assert out == {"status": "skipped", "reason": "task_events_via_delta_beat"}


# ---------------------------------------------------------------------------
# quote deltas
# ---------------------------------------------------------------------------


def test_quote_accepted_updates_native_status(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, status="sent")
    out = crm_native_sync.apply_webhook_delta(
        db_session, "quote", "quote.accepted", {"quote_id": str(row.id)}
    )
    assert out["status"] == "ok"
    db_session.refresh(row)
    assert row.status == "accepted"


def test_quote_status_from_payload_wins(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _quote(db_session, sub, status="draft")
    crm_native_sync.apply_webhook_delta(
        db_session, "quote", "quote.updated", {"id": str(row.id), "status": "sent"}
    )
    db_session.refresh(row)
    assert row.status == "sent"


def test_quote_missing_native_row_skipped(db_session, flag_on):
    out = crm_native_sync.apply_webhook_delta(
        db_session, "quote", "quote.created", {"quote_id": str(uuid.uuid4())}
    )
    assert out == {"status": "skipped", "reason": "native_row_missing"}


# ---------------------------------------------------------------------------
# referral deltas
# ---------------------------------------------------------------------------


def test_referral_qualified_sets_status_and_timestamp(db_session, flag_on):
    sub = _subscriber(db_session)
    row = _referral(db_session, sub)
    out = crm_native_sync.apply_webhook_delta(
        db_session, "referral", "referral.qualified", {"referral_id": str(row.id)}
    )
    assert out["status"] == "ok"
    db_session.refresh(row)
    assert row.status == "qualified"
    assert row.qualified_at is not None


def test_referral_rewarded_uses_native_reward_vocabulary(db_session, flag_on):
    # The CRM webhook path historically wrote reward_status="paid" into the
    # mirror; native vocabulary is "issued" (§1.7) — pin that mapping.
    sub = _subscriber(db_session)
    row = _referral(db_session, sub, status="qualified")
    crm_native_sync.apply_webhook_delta(
        db_session,
        "referral",
        "referral.rewarded",
        {"referral_id": str(row.id), "amount": "2500", "currency": "NGN"},
    )
    db_session.refresh(row)
    assert row.status == "rewarded"
    assert row.reward_status == "issued"
    assert str(row.reward_amount) in {"2500", "2500.00"}
    assert row.reward_currency == "NGN"
    assert row.reward_issued_at is not None


def test_referral_missing_native_row_skipped(db_session, flag_on):
    out = crm_native_sync.apply_webhook_delta(
        db_session, "referral", "referral.captured", {"referral_id": str(uuid.uuid4())}
    )
    assert out == {"status": "skipped", "reason": "native_row_missing"}


# ---------------------------------------------------------------------------
# webhook branch integration (flag on/off through the endpoint)
# ---------------------------------------------------------------------------

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


def _post_project_event(db, body: dict, event: str):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {
        "X-Webhook-Event": event,
        "X-Webhook-Signature-256": sig,
        "X-Webhook-Delivery-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    with _with_secret(SECRET):
        return _run(receive_crm_project_event(_FakeRequest(raw, headers), db))


def test_webhook_branch_updates_native_row_when_flag_on(db_session, flag_on):
    sub = _subscriber(db_session)
    native = _project(db_session, sub, status="active")
    resp = _post_project_event(
        db_session,
        {
            "subscriber_id": str(sub.id),
            "project_id": str(native.id),
            "name": "Install",
            "status": "completed",
        },
        "project.completed",
    )
    assert resp["status"] == "ok"
    db_session.refresh(native)
    assert native.status == "completed"
    assert native.completed_at is not None


def test_webhook_branch_leaves_native_row_alone_when_flag_off(db_session):
    sub = _subscriber(db_session)
    native = _project(db_session, sub, status="active")
    resp = _post_project_event(
        db_session,
        {
            "subscriber_id": str(sub.id),
            "project_id": str(native.id),
            "name": "Install",
            "status": "completed",
        },
        "project.completed",
    )
    # Mirror path still applies (response ok); the native row must not move.
    assert resp["status"] == "ok"
    db_session.refresh(native)
    assert native.status == "active"
    assert native.completed_at is None


# ---------------------------------------------------------------------------
# delta beat task gating
# ---------------------------------------------------------------------------


def _patch_task_db(monkeypatch, db):
    from app.tasks import crm_native_sync as task_module

    @contextmanager
    def _fake_session():
        yield db

    monkeypatch.setattr(task_module, "task_session", _fake_session)
    return task_module


def test_delta_task_skips_when_flag_off(db_session, monkeypatch):
    task_module = _patch_task_db(monkeypatch, db_session)
    called = []
    monkeypatch.setattr(
        task_module, "_run_delta", lambda *a, **kw: called.append(a) or {}
    )
    out = task_module.pull_crm_phase3_native_delta()
    assert out == {"status": "skipped", "reason": "native_sync_disabled"}
    assert called == []


def test_delta_task_errors_without_crm_dsn(db_session, monkeypatch, flag_on):
    task_module = _patch_task_db(monkeypatch, db_session)
    monkeypatch.delenv("CRM_DATABASE_URL", raising=False)
    out = task_module.pull_crm_phase3_native_delta()
    assert out == {"status": "error", "reason": "crm_database_url_not_configured"}


def test_delta_task_runs_importer_when_enabled(
    db_session, monkeypatch, flag_on, tmp_path
):
    task_module = _patch_task_db(monkeypatch, db_session)
    state_file = str(tmp_path / "p3-state.json")
    monkeypatch.setenv("CRM_DATABASE_URL", "postgresql://crm.example/crm")
    monkeypatch.setenv("CRM_PHASE3_SYNC_STATE_FILE", state_file)
    monkeypatch.setenv("CRM_PHASE3_SYNC_OVERLAP_SECONDS", "120")
    seen: dict = {}

    def _fake_run(crm_dsn, state_file, overlap_seconds):
        seen.update(dsn=crm_dsn, state=state_file, overlap=overlap_seconds)
        return {"status": "ok", "created": 1, "updated": 2, "blockers": 0}

    monkeypatch.setattr(task_module, "_run_delta", _fake_run)
    out = task_module.pull_crm_phase3_native_delta()
    assert out == {"status": "ok", "created": 1, "updated": 2, "blockers": 0}
    assert seen == {
        "dsn": "postgresql://crm.example/crm",
        "state": state_file,
        "overlap": 120,
    }


def test_delta_task_overlap_default(monkeypatch):
    from app.tasks import crm_native_sync as task_module

    monkeypatch.delenv("CRM_PHASE3_SYNC_OVERLAP_SECONDS", raising=False)
    assert task_module._overlap_seconds() == 600
    monkeypatch.setenv("CRM_PHASE3_SYNC_OVERLAP_SECONDS", "not-a-number")
    assert task_module._overlap_seconds() == 600


# ---------------------------------------------------------------------------
# beat entry (scheduler_config) gating
# ---------------------------------------------------------------------------


def test_beat_entry_declared_and_flag_gated():
    """The schedule entry exists in scheduler_config, names the registered
    task, and is emitted only inside the flag conditional (static check — the
    dynamic build needs the app DB)."""
    import ast
    import pathlib

    import app.services.scheduler_config as scheduler_config

    source = pathlib.Path(scheduler_config.__file__).read_text()
    assert "crm_phase3_native_sync_enabled" in source
    tree = ast.parse(source)
    found_gated = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Constant)
                    and sub.value
                    == "app.tasks.crm_native_sync.pull_crm_phase3_native_delta"
                ):
                    found_gated = True
    assert found_gated, "crm_phase3_native_delta beat entry must be flag-gated"
