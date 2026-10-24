"""Admit operator ONT edits through the service-configuration lifecycle.

The ONT service-configuration lifecycle (``configure_ont_service``) is the only
writer of ONT/OLT/ACS desired state. Admin web routes that used to call
``reconcile_ont`` or edit desired config directly build a typed section change
here and let the lifecycle plan and apply it. Nothing in this module talks to an
OLT or ACS.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import finish_read_transaction
from app.services import web_admin as web_admin_service
from app.services.auth_dependencies import has_permission
from app.services.domain_errors import DomainError
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_service_configuration import (
    ConfigureOntServiceCommand,
    ManagementConfigurationChange,
    OntConfigurationChange,
    OntConfigurationSection,
    WifiConfigurationChange,
    configure_ont_service,
)
from app.services.owner_commands import CommandContext


def lifecycle_command_context(
    request: Request, *, reason: str, idempotency_key: str
) -> CommandContext:
    command_id = uuid.uuid4()
    request_id = str(getattr(request.state, "request_id", "") or "").strip()
    try:
        correlation_id = uuid.UUID(request_id)
    except ValueError:
        correlation_id = command_id
    auth = getattr(request.state, "auth", {}) or {}
    principal_type = str(auth.get("principal_type") or "user")
    actor_id = web_admin_service.get_actor_id(request) or "unknown"
    return CommandContext(
        command_id=command_id,
        correlation_id=correlation_id,
        actor=f"{principal_type}:{actor_id}",
        scope="network:ont:write",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def submit_section_change(
    db: Session,
    request: Request,
    ont_id: str,
    *,
    section: OntConfigurationSection,
    change: OntConfigurationChange,
    reason: str,
) -> ActionResult:
    """Admit one section change; the lifecycle plans and applies it."""
    try:
        ont_uuid = uuid.UUID(str(ont_id))
    except ValueError:
        return ActionResult(success=False, message="Invalid ONT identifier.")
    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    finish_read_transaction(db)
    try:
        outcome = configure_ont_service(
            db,
            ConfigureOntServiceCommand(
                context=lifecycle_command_context(
                    request,
                    reason=reason,
                    idempotency_key=f"ont-config:{ont_id}:{section.value}:{uuid.uuid4()}",
                ),
                ont_unit_id=ont_uuid,
                permission_granted=permission_granted,
                section=section,
                change=change,
            ),
        )
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)
    return ActionResult(
        success=True,
        message=outcome.message,
        waiting=True,
        data={
            "operation_id": str(outcome.operation_id),
            "revision": outcome.revision,
        },
    )


def submit_wifi_change(
    db: Session,
    request: Request,
    ont_id: str,
    *,
    ssid: str | None = None,
    password: str | None = None,
) -> ActionResult:
    """Change SSID and/or password; unchanged WiFi fields carry forward."""
    ssid_text = (ssid or "").strip() or None
    password_text = (password or "").strip() or None
    if ssid_text is None and password_text is None:
        return ActionResult(
            success=False, message="A WiFi name or password is required."
        )
    try:
        ont = _load_ont(db, ont_id)
    except ValueError:
        return ActionResult(success=False, message="ONT not found.")
    values = resolve_effective_ont_config(db, ont)["values"]
    enabled = values.get("wifi_enabled")
    change = WifiConfigurationChange(
        enabled=True if enabled is None else bool(enabled),
        ssid=ssid_text or _text(values.get("wifi_ssid")),
        channel=_text(values.get("wifi_channel")),
        security_mode=_text(values.get("wifi_security_mode")),
        password=password_text,
    )
    what = "WiFi name" if password_text is None else "WiFi password"
    return submit_section_change(
        db,
        request,
        ont_id,
        section=OntConfigurationSection.wifi,
        change=change,
        reason=f"Change ONT {what}",
    )


def submit_management_change(
    db: Session,
    request: Request,
    ont_id: str,
    *,
    ip_mode: str | None,
    ip_address: str | None,
    remote_access: bool,
    wcd_index: int | None = None,
) -> ActionResult:
    return submit_section_change(
        db,
        request,
        ont_id,
        section=OntConfigurationSection.management,
        change=ManagementConfigurationChange(
            ip_mode=ip_mode,
            ip_address=ip_address,
            remote_access=remote_access,
            wcd_index=wcd_index,
        ),
        reason="Change ONT management settings",
    )


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _load_ont(db: Session, ont_id: str):
    from app.models.network import OntUnit

    ont = db.get(OntUnit, uuid.UUID(str(ont_id)))
    if ont is None:
        raise ValueError("ONT not found")
    return ont
