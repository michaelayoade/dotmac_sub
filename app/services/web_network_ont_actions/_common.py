"""Common helpers for ONT web action services."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit, OnuMode, Vlan, WanMode
from app.services import network as network_service
from app.services.audit_helpers import log_audit_event
from app.services.network.ont_actions import ActionResult

logger = logging.getLogger(__name__)


def _current_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    from app.web.admin import get_current_user

    return get_current_user(request)


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
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
        status_code=status_code or 200,
        is_success=is_success,
    )


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


def _persist_wan_intent(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    wan_vlan: int | None,
    ip_address: str | None,
    subnet_mask: str | None,
    gateway: str | None,
    dns_servers: str | None,
    instance_index: int,
) -> None:
    mode = (wan_mode or "").strip().lower()
    step_values: dict[str, object] = {
        "wan_mode": mode,
        "wan_vlan": wan_vlan,
        "ip_address": ip_address,
        "subnet_mask": subnet_mask,
        "gateway": gateway,
        "dns_servers": dns_servers,
        "instance_index": instance_index,
    }
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
        if mode == "bridge":
            ont.onu_mode = OnuMode.bridging
            ont.wan_mode = WanMode.setup_via_onu
        elif mode in {"dhcp", "pppoe", "static"}:
            ont.onu_mode = OnuMode.routing
            ont.wan_mode = WanMode.static_ip if mode == "static" else WanMode(mode)
        if wan_vlan is not None:
            vlan = db.scalars(select(Vlan).where(Vlan.tag == wan_vlan).limit(1)).first()
            if vlan:
                ont.wan_vlan_id = vlan.id
        db.add(ont)
        db.flush()
    except Exception:
        logger.exception("Failed to persist WAN model intent for ONT %s", ont_id)
    _persist_ont_plan_step(db, ont_id, "configure_wan_tr069", step_values)


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
