"""Shared audit helpers for CPE web services."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.audit_helpers import log_audit_event


def actor_id_from_request(request: Request | None) -> str | None:
    if request is None:
        return None
    from app.services import web_admin as web_admin_service

    current_user = web_admin_service.get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def actor_name_from_request(request: Request | None) -> str:
    if request is None:
        return "system"
    from app.services import web_admin as web_admin_service

    current_user = web_admin_service.get_current_user(request)
    if not current_user:
        return "system"
    return str(current_user.get("name") or current_user.get("email") or "unknown")


def log_cpe_audit_event(
    db: Session,
    *,
    request: Request | None,
    action: str,
    entity_id: object,
    metadata: dict[str, Any] | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="cpe",
        entity_id=str(entity_id),
        actor_id=actor_id_from_request(request),
        metadata=metadata,
        is_success=is_success,
    )
