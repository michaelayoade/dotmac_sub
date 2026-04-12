"""Shared audit helpers for OLT web services."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.audit_helpers import log_audit_event


def current_user_from_request(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    from app.services import web_admin as web_admin_service

    return web_admin_service.get_current_user(request)


def actor_name_from_request(request: Request | None) -> str:
    current_user = current_user_from_request(request)
    return str(current_user.get("name", "unknown")) if current_user else "system"


def actor_id_from_request(request: Request | None) -> str | None:
    current_user = current_user_from_request(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def log_olt_audit_event(
    db: Session,
    *,
    request: Request | None,
    action: str,
    entity_id: object,
    metadata: dict[str, object] | None = None,
    entity_type: str = "olt",
    status_code: int | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_id=actor_id_from_request(request),
        metadata=metadata,
        status_code=status_code or 200,
        is_success=is_success,
    )
