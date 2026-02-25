"""Services for integration hooks configuration and metrics."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.integration_hook import (
    IntegrationHook,
    IntegrationHookAuthType,
    IntegrationHookExecution,
    IntegrationHookExecutionStatus,
    IntegrationHookType,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum

HOOK_TEMPLATES: dict[str, dict[str, Any]] = {
    "n8n": {
        "template_id": "n8n",
        "label": "n8n Webhook",
        "description": "Send DotMac events to an n8n workflow webhook trigger.",
        "title": "n8n Automation Hook",
        "hook_type": "web",
        "url": "https://n8n.example.com/webhook/dotmac-events",
        "http_method": "POST",
        "auth_type": "none",
        "event_filters_csv": "invoice.created,payment.received,subscription.activated",
        "retry_max": 3,
        "retry_backoff_ms": 500,
    },
    "zapier": {
        "template_id": "zapier",
        "label": "Zapier Catch Hook",
        "description": "Forward selected events to Zapier Catch Hook.",
        "title": "Zapier Event Hook",
        "hook_type": "web",
        "url": "https://hooks.zapier.com/hooks/catch/123456/abcdef/",
        "http_method": "POST",
        "auth_type": "none",
        "event_filters_csv": "invoice.paid,payment.failed,subscription.suspended",
        "retry_max": 3,
        "retry_backoff_ms": 500,
    },
    "make": {
        "template_id": "make",
        "label": "Make Custom Webhook",
        "description": "Trigger a Make scenario through custom webhook URL.",
        "title": "Make Scenario Hook",
        "hook_type": "web",
        "url": "https://hook.make.com/your-scenario-token",
        "http_method": "POST",
        "auth_type": "none",
        "event_filters_csv": "subscriber.created,invoice.overdue,service_order.completed",
        "retry_max": 3,
        "retry_backoff_ms": 500,
    },
}


def list_hook_templates() -> list[dict[str, Any]]:
    return list(HOOK_TEMPLATES.values())


def get_hook_template(template_id: str | None) -> dict[str, Any] | None:
    if not template_id:
        return None
    return HOOK_TEMPLATES.get(template_id)


def list_hooks(
    db: Session,
    *,
    hook_type: str | None = None,
    is_enabled: bool | None = None,
    order_by: str = "created_at",
    order_dir: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> list[IntegrationHook]:
    query = db.query(IntegrationHook)
    if hook_type:
        query = query.filter(
            IntegrationHook.hook_type
            == validate_enum(hook_type, IntegrationHookType, "hook_type")
        )
    if is_enabled is not None:
        query = query.filter(IntegrationHook.is_enabled == is_enabled)
    query = apply_ordering(
        query,
        order_by,
        order_dir,
        {"created_at": IntegrationHook.created_at, "title": IntegrationHook.title},
    )
    return apply_pagination(query, limit, offset).all()


def get_hook(db: Session, hook_id: str) -> IntegrationHook:
    hook = db.get(IntegrationHook, coerce_uuid(hook_id))
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    return hook


def create_hook(
    db: Session,
    *,
    title: str,
    hook_type: str,
    command: str | None,
    url: str | None,
    http_method: str | None,
    auth_type: str | None,
    auth_config: dict[str, Any] | None,
    retry_max: int,
    retry_backoff_ms: int,
    event_filters: list[str] | None,
    is_enabled: bool,
    notes: str | None,
) -> IntegrationHook:
    _validate_hook_fields(hook_type=hook_type, command=command, url=url)
    hook = IntegrationHook(
        title=title.strip(),
        hook_type=validate_enum(hook_type, IntegrationHookType, "hook_type"),
        command=command.strip() if command else None,
        url=url.strip() if url else None,
        http_method=(http_method or "POST").upper(),
        auth_type=validate_enum(
            auth_type or IntegrationHookAuthType.none.value,
            IntegrationHookAuthType,
            "auth_type",
        ),
        auth_config=auth_config,
        retry_max=max(0, retry_max),
        retry_backoff_ms=max(0, retry_backoff_ms),
        event_filters=event_filters or [],
        is_enabled=is_enabled,
        notes=notes.strip() if notes else None,
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    return hook


def update_hook(
    db: Session,
    *,
    hook_id: str,
    title: str,
    hook_type: str,
    command: str | None,
    url: str | None,
    http_method: str | None,
    auth_type: str | None,
    auth_config: dict[str, Any] | None,
    retry_max: int,
    retry_backoff_ms: int,
    event_filters: list[str] | None,
    is_enabled: bool,
    notes: str | None,
) -> IntegrationHook:
    hook = get_hook(db, hook_id)
    _validate_hook_fields(hook_type=hook_type, command=command, url=url)
    hook.title = title.strip()
    hook.hook_type = validate_enum(hook_type, IntegrationHookType, "hook_type")
    hook.command = command.strip() if command else None
    hook.url = url.strip() if url else None
    hook.http_method = (http_method or "POST").upper()
    hook.auth_type = validate_enum(
        auth_type or IntegrationHookAuthType.none.value,
        IntegrationHookAuthType,
        "auth_type",
    )
    hook.auth_config = auth_config
    hook.retry_max = max(0, retry_max)
    hook.retry_backoff_ms = max(0, retry_backoff_ms)
    hook.event_filters = event_filters or []
    hook.is_enabled = is_enabled
    hook.notes = notes.strip() if notes else None
    hook.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(hook)
    return hook


def duplicate_hook(db: Session, *, hook_id: str) -> IntegrationHook:
    source = get_hook(db, hook_id)
    copy = IntegrationHook(
        title=f"{source.title} (Copy)",
        hook_type=source.hook_type,
        command=source.command,
        url=source.url,
        http_method=source.http_method,
        auth_type=source.auth_type,
        auth_config=source.auth_config,
        retry_max=source.retry_max,
        retry_backoff_ms=source.retry_backoff_ms,
        event_filters=source.event_filters or [],
        is_enabled=False,
        notes=source.notes,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return copy


def set_enabled(db: Session, *, hook_id: str, is_enabled: bool) -> IntegrationHook:
    hook = get_hook(db, hook_id)
    hook.is_enabled = is_enabled
    hook.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(hook)
    return hook


def build_hooks_page_state(db: Session) -> dict[str, Any]:
    hooks = list_hooks(db, limit=500, offset=0, order_by="created_at", order_dir="desc")
    hook_ids = [hook.id for hook in hooks]
    metrics: dict[str, dict[str, Any]] = {}
    if hook_ids:
        counts = (
            db.query(
                IntegrationHookExecution.hook_id.label("hook_id"),
                func.count(IntegrationHookExecution.id).label("total"),
                func.sum(
                    case(
                        (IntegrationHookExecution.status == IntegrationHookExecutionStatus.success, 1),
                        else_=0,
                    )
                ).label("success_total"),
                func.max(IntegrationHookExecution.created_at).label("last_triggered_at"),
            )
            .filter(IntegrationHookExecution.hook_id.in_(hook_ids))
            .group_by(IntegrationHookExecution.hook_id)
            .all()
        )
        for row in counts:
            total = int(row.total or 0)
            success_total = int(row.success_total or 0)
            success_rate = round((success_total / total) * 100, 1) if total > 0 else 0.0
            metrics[str(row.hook_id)] = {
                "total": total,
                "success_total": success_total,
                "success_rate": success_rate,
                "last_triggered_at": row.last_triggered_at,
            }
    stats = {
        "total": len(hooks),
        "enabled": sum(1 for h in hooks if h.is_enabled),
        "web": sum(1 for h in hooks if h.hook_type == IntegrationHookType.web),
        "cli": sum(1 for h in hooks if h.hook_type == IntegrationHookType.cli),
        "internal": sum(1 for h in hooks if h.hook_type == IntegrationHookType.internal),
    }
    return {"hooks": hooks, "hook_metrics": metrics, "stats": stats}


def _validate_hook_fields(*, hook_type: str, command: str | None, url: str | None) -> None:
    resolved = validate_enum(hook_type, IntegrationHookType, "hook_type")
    if resolved == IntegrationHookType.cli and not (command and command.strip()):
        raise HTTPException(status_code=400, detail="command is required for CLI hooks")
    if resolved in {IntegrationHookType.web, IntegrationHookType.internal} and not (
        url and url.strip()
    ):
        raise HTTPException(status_code=400, detail="url is required for web/internal hooks")


def list_executions(
    db: Session,
    *,
    hook_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[IntegrationHookExecution]:
    query = (
        db.query(IntegrationHookExecution)
        .filter(IntegrationHookExecution.hook_id == coerce_uuid(hook_id))
    )
    query = apply_ordering(
        query,
        "created_at",
        "desc",
        {"created_at": IntegrationHookExecution.created_at},
    )
    return apply_pagination(query, limit, offset).all()


def execute_hook(
    db: Session,
    *,
    hook: IntegrationHook,
    event_type: str,
    payload: dict[str, Any],
) -> IntegrationHookExecution:
    started = time.perf_counter()
    status = IntegrationHookExecutionStatus.success
    response_status: int | None = None
    response_body: str | None = None
    try:
        if hook.hook_type in {IntegrationHookType.web, IntegrationHookType.internal}:
            response_status, response_body = _execute_http_hook(hook=hook, payload=payload)
            if response_status < 200 or response_status >= 300:
                status = IntegrationHookExecutionStatus.failed
        elif hook.hook_type == IntegrationHookType.cli:
            response_status, response_body = _execute_cli_hook(hook=hook, payload=payload)
            if response_status != 0:
                status = IntegrationHookExecutionStatus.failed
    except Exception as exc:
        status = IntegrationHookExecutionStatus.failed
        response_body = str(exc)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    execution = IntegrationHookExecution(
        hook_id=hook.id,
        event_type=event_type,
        status=status,
        latency_ms=elapsed_ms,
        response_status=response_status,
        payload=payload,
        response_body=response_body,
    )
    hook.last_triggered_at = datetime.now(UTC)
    hook.updated_at = datetime.now(UTC)
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


def dispatch_for_event(
    db: Session,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    hooks = list_hooks(
        db,
        hook_type=None,
        is_enabled=True,
        order_by="created_at",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    executed = 0
    for hook in hooks:
        filters = hook.event_filters or []
        if filters and event_type not in filters:
            continue
        execute_hook(db, hook=hook, event_type=event_type, payload=payload)
        executed += 1
    return executed


def trigger_test(
    db: Session,
    *,
    hook_id: str,
    event_type: str = "custom.test",
    payload: dict[str, Any] | None = None,
) -> IntegrationHookExecution:
    hook = get_hook(db, hook_id)
    test_payload = payload or {
        "event_id": "test-event",
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {"source": "manual-test"},
        "context": {},
    }
    return execute_hook(db, hook=hook, event_type=event_type, payload=test_payload)


def _execute_http_hook(*, hook: IntegrationHook, payload: dict[str, Any]) -> tuple[int, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth_config = hook.auth_config if isinstance(hook.auth_config, dict) else {}
    if hook.auth_type == IntegrationHookAuthType.bearer:
        token = str(auth_config.get("token") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif hook.auth_type == IntegrationHookAuthType.basic:
        username = str(auth_config.get("username") or "")
        password = str(auth_config.get("password") or "")
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
    elif hook.auth_type == IntegrationHookAuthType.hmac:
        secret = str(auth_config.get("secret") or "")
        if secret:
            headers["X-Hook-Secret"] = secret
    response = httpx.request(
        method=(hook.http_method or "POST").upper(),
        url=str(hook.url or ""),
        headers=headers,
        json=payload,
        timeout=10.0,
    )
    body = response.text
    if len(body) > 2000:
        body = body[:2000]
    return response.status_code, body


def _execute_cli_hook(*, hook: IntegrationHook, payload: dict[str, Any]) -> tuple[int, str]:
    command = str(hook.command or "").strip()
    if not command:
        raise ValueError("CLI hook command is empty")
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=20,
        input=json.dumps(payload),
    )
    body = (result.stdout or "").strip()
    if not body:
        body = (result.stderr or "").strip()
    if len(body) > 2000:
        body = body[:2000]
    return int(result.returncode), body
