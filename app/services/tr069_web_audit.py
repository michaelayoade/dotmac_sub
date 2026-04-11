"""Shared audit helpers for TR-069 web services."""

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


def log_tr069_audit_event(
    db: Session,
    *,
    request: Request | None,
    action: str,
    entity_type: str,
    entity_id: object,
    metadata: dict[str, Any] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    kwargs: dict[str, object] = {}
    if status_code is not None:
        kwargs["status_code"] = status_code
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_id=actor_id_from_request(request),
        metadata=metadata,
        is_success=is_success,
        **kwargs,
    )
