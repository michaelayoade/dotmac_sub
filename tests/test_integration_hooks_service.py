from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.models.integration_hook import (
    IntegrationHookExecution,
    IntegrationHookExecutionStatus,
)
from app.services import integration_hooks as hooks_service


def test_create_cli_hook_requires_command(db_session):
    with pytest.raises(HTTPException) as exc:
        hooks_service.create_hook(
            db_session,
            title="CLI hook",
            hook_type="cli",
            command="",
            url=None,
            http_method="POST",
            auth_type="none",
            auth_config=None,
            retry_max=1,
            retry_backoff_ms=100,
            event_filters=["invoice.created"],
            is_enabled=True,
            notes=None,
        )
    assert exc.value.status_code == 400


def test_create_and_duplicate_hook(db_session):
    hook = hooks_service.create_hook(
        db_session,
        title="n8n sync",
        hook_type="web",
        command=None,
        url="https://example.test/hook",
        http_method="POST",
        auth_type="bearer",
        auth_config={"token": "secret"},
        retry_max=5,
        retry_backoff_ms=200,
        event_filters=["invoice.created", "payment.received"],
        is_enabled=True,
        notes="Primary automation",
    )
    assert hook.title == "n8n sync"
    assert hook.url == "https://example.test/hook"

    copy = hooks_service.duplicate_hook(db_session, hook_id=str(hook.id))
    assert copy.id != hook.id
    assert copy.title.endswith("(Copy)")
    assert copy.is_enabled is False


def test_build_hooks_page_state_includes_success_rate(db_session):
    hook = hooks_service.create_hook(
        db_session,
        title="ERP sync",
        hook_type="web",
        command=None,
        url="https://erp.example/hook",
        http_method="POST",
        auth_type="none",
        auth_config=None,
        retry_max=3,
        retry_backoff_ms=500,
        event_filters=["invoice.paid"],
        is_enabled=True,
        notes=None,
    )
    db_session.add_all(
        [
            IntegrationHookExecution(
                hook_id=hook.id,
                event_type="invoice.paid",
                status=IntegrationHookExecutionStatus.success,
                created_at=datetime.now(UTC),
            ),
            IntegrationHookExecution(
                hook_id=hook.id,
                event_type="invoice.paid",
                status=IntegrationHookExecutionStatus.failed,
                created_at=datetime.now(UTC),
            ),
            IntegrationHookExecution(
                hook_id=hook.id,
                event_type="invoice.paid",
                status=IntegrationHookExecutionStatus.success,
                created_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    state = hooks_service.build_hooks_page_state(db_session)
    assert state["stats"]["total"] >= 1
    metrics = state["hook_metrics"][str(hook.id)]
    assert metrics["total"] == 3
    assert metrics["success_total"] == 2
    assert metrics["success_rate"] == pytest.approx(66.7, rel=1e-2)


def test_dispatch_for_event_respects_filters(db_session, monkeypatch):
    hook_any = hooks_service.create_hook(
        db_session,
        title="Any event",
        hook_type="web",
        command=None,
        url="https://example.test/any",
        http_method="POST",
        auth_type="none",
        auth_config=None,
        retry_max=1,
        retry_backoff_ms=10,
        event_filters=[],
        is_enabled=True,
        notes=None,
    )
    hook_filtered = hooks_service.create_hook(
        db_session,
        title="Only invoice",
        hook_type="web",
        command=None,
        url="https://example.test/invoice",
        http_method="POST",
        auth_type="none",
        auth_config=None,
        retry_max=1,
        retry_backoff_ms=10,
        event_filters=["invoice.created"],
        is_enabled=True,
        notes=None,
    )
    called: list[str] = []

    def _fake_execute(_db, *, hook, event_type, payload):
        called.append(str(hook.id))
        return None

    monkeypatch.setattr(hooks_service, "execute_hook", _fake_execute)

    count = hooks_service.dispatch_for_event(
        db_session,
        event_type="payment.received",
        payload={"event_type": "payment.received"},
    )
    assert count == 1
    assert called == [str(hook_any.id)]
    assert str(hook_filtered.id) not in called


def test_trigger_test_uses_execute_hook(db_session, monkeypatch):
    hook = hooks_service.create_hook(
        db_session,
        title="Test hook",
        hook_type="web",
        command=None,
        url="https://example.test/test",
        http_method="POST",
        auth_type="none",
        auth_config=None,
        retry_max=1,
        retry_backoff_ms=10,
        event_filters=[],
        is_enabled=True,
        notes=None,
    )
    captured = {}

    def _fake_execute(_db, *, hook, event_type, payload):
        captured["hook_id"] = str(hook.id)
        captured["event_type"] = event_type
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(hooks_service, "execute_hook", _fake_execute)

    result = hooks_service.trigger_test(db_session, hook_id=str(hook.id))
    assert result == "ok"
    assert captured["hook_id"] == str(hook.id)
    assert captured["event_type"] == "custom.test"
    assert captured["payload"]["event_type"] == "custom.test"
