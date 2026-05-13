"""Common helpers for ONT web action services."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit
from app.services import network as network_service
from app.services.audit_helpers import log_audit_event
from app.services.network.ont_actions import ActionResult

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "key",
)


def _sanitize_audit_value(value: object, *, depth: int = 0) -> object:
    """Return a JSON-safe, password-safe value for action audit metadata."""
    if depth > 5:
        return str(value)
    if isinstance(value, dict):
        clean: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                clean[key_text] = "***"
            else:
                clean[key_text] = _sanitize_audit_value(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_audit_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def action_result_audit_metadata(result: object) -> dict[str, object]:
    """Build reusable audit metadata for an ActionResult-like object."""
    data = getattr(result, "data", None)
    return {
        "success": bool(getattr(result, "success", False)),
        "waiting": bool(getattr(result, "waiting", False)),
        "message": str(getattr(result, "message", "") or ""),
        "data": _sanitize_audit_value(data or {}),
    }


_CACHED_USER_CONTEXT_ATTR = "_dotmac_cached_user_context"


def cache_current_user_context(request: Request | None) -> dict[str, Any] | None:
    """Capture request actor data before long-running device operations.

    The authenticated user object on request.state can be a SQLAlchemy model.
    After a commit and a long TR-069 wait, reading its attributes can trigger a
    database reload on an expired/closed transaction. Store only plain values.
    """
    if request is None:
        return None
    state = getattr(request, "state", None)
    cached = (
        getattr(state, _CACHED_USER_CONTEXT_ATTR, None)
        if state is not None
        else None
    )
    if cached is not None:
        return cached
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    cached = dict(current_user) if current_user else None
    if state is not None:
        setattr(state, _CACHED_USER_CONTEXT_ATTR, cached)
    return cached


def _current_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    state = getattr(request, "state", None)
    cached = (
        getattr(state, _CACHED_USER_CONTEXT_ATTR, None)
        if state is not None
        else None
    )
    if cached is not None:
        return cached
    try:
        return cache_current_user_context(request)
    except Exception:
        logger.exception("Failed to resolve current user context")
        return None


def actor_name_from_request(request: Request | None) -> str:
    current_user = _current_user(request)
    return str(current_user.get("name", "unknown")) if current_user else "system"


def _actor_id_from_request(request: Request | None) -> str | None:
    current_user = _current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _log_action_audit(
    db: Session,
    *,
    request: Request | None,
    action: str,
    ont_id: object,
    metadata: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    try:
        actor_id = _actor_id_from_request(request)
        cached_user = _current_user(request)
        audit_metadata = dict(metadata or {})
        if cached_user:
            audit_metadata.setdefault("actor_name", cached_user.get("name"))
            audit_metadata.setdefault("actor_email", cached_user.get("email"))
        log_audit_event(
            db=db,
            request=None,
            action=action,
            entity_type="ont",
            entity_id=str(ont_id),
            actor_id=actor_id,
            metadata=audit_metadata,
            status_code=status_code or 200,
            is_success=is_success,
        )
    except Exception:
        logger.exception("Failed to log ONT action audit event for %s", ont_id)
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback after ONT action audit failure")


def _persist_ont_plan_step(
    db: Session,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
) -> None:
    """Persist desired ONT intent even when the immediate apply path is unavailable."""
    if not any(value not in (None, "", []) for value in values.values()):
        return
    try:
        from app.services import (
            web_network_onts_provisioning as provisioning_web_service,
        )

        provisioning_web_service.update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name=step_name,
            values=values,
        )
    except Exception:
        logger.exception("Failed to persist %s intent for ONT %s", step_name, ont_id)


def _is_input_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(
        phrase in text
        for phrase in [
            "required",
            "invalid",
            "must be",
            "out of range",
            "at least one",
            "no wan parameters",
            "no ppp wan service",
            "ppp wan service exists",
            "missing_ppp_wan_service",
        ]
    )


def _intent_saved_result(result: ActionResult) -> ActionResult:
    if result.success or _is_input_error(result.message):
        return result
    return ActionResult(
        success=True,
        message=f"Intent saved. Immediate apply did not complete: {result.message}",
        data=getattr(result, "data", None),
        waiting=getattr(result, "waiting", False),
    )


def _normalize_fsp(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    if ":" in ext:
        suffix = ext.rsplit(":", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _display_olt_value(value: object | None) -> object | str:
    text = str(value or "").strip()
    return "—" if not text or text.lower() == "unknown" else value


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)

    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


def _config_snapshot_service():
    try:
        from app.services.network.ont_config_snapshots import ont_config_snapshots
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="Config snapshots not available",
        ) from exc
    return ont_config_snapshots
