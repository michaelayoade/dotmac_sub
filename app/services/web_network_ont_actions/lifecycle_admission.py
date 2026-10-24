"""Admit operator ONT edits through the service-configuration lifecycle.

The ONT service-configuration lifecycle (``configure_ont_service``) is the only
writer of ONT/OLT/ACS desired state. Admin web routes that used to call
``reconcile_ont`` or edit desired config directly build a typed section change
here and let the lifecycle plan and apply it. Nothing in this module talks to an
OLT or ACS.
"""

from __future__ import annotations

import uuid
from dataclasses import astuple

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import finish_read_transaction
from app.services import web_admin as web_admin_service
from app.services.auth_dependencies import has_permission
from app.services.domain_errors import DomainError
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_service_configuration import (
    ConfigureOntServiceCommand,
    ManagementConfigurationChange,
    OntConfigurationChange,
    OntConfigurationSection,
    WifiPatchConfigurationChange,
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


def admit_section_change(
    db: Session,
    ont_id: str,
    *,
    context: CommandContext,
    permission_granted: bool,
    section: OntConfigurationSection,
    change: OntConfigurationChange,
) -> ActionResult:
    """Admit one section change with an explicit command context."""
    try:
        ont_uuid = uuid.UUID(str(ont_id))
    except ValueError:
        return ActionResult(success=False, message="Invalid ONT identifier.")
    finish_read_transaction(db)
    try:
        outcome = configure_ont_service(
            db,
            ConfigureOntServiceCommand(
                context=context,
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
    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    return admit_section_change(
        db,
        ont_id,
        context=lifecycle_command_context(
            request,
            reason=reason,
            idempotency_key=f"ont-config:{ont_id}:{section.value}:{uuid.uuid4()}",
        ),
        permission_granted=permission_granted,
        section=section,
        change=change,
    )


def wifi_patch(
    *,
    ssid: str | None = None,
    password: str | None = None,
    enabled: bool | None = None,
    channel: str | None = None,
    security_mode: str | None = None,
) -> WifiPatchConfigurationChange | None:
    """Build a sparse patch; blank strings mean unchanged. None if empty."""
    patch = WifiPatchConfigurationChange(
        ssid=(ssid or "").strip() or None,
        password=(password or "").strip() or None,
        enabled=enabled,
        channel=_text(channel),
        security_mode=_text(security_mode),
    )
    if all(value is None for value in astuple(patch)):
        return None
    return patch


def submit_wifi_change(
    db: Session,
    request: Request,
    ont_id: str,
    *,
    ssid: str | None = None,
    password: str | None = None,
    enabled: bool | None = None,
    channel: str | None = None,
    security_mode: str | None = None,
) -> ActionResult:
    """Change WiFi fields; unset fields stay as the owner holds them."""
    patch = wifi_patch(
        ssid=ssid,
        password=password,
        enabled=enabled,
        channel=channel,
        security_mode=security_mode,
    )
    if patch is None:
        return ActionResult(success=False, message="A WiFi setting is required.")
    return submit_section_change(
        db,
        request,
        ont_id,
        section=OntConfigurationSection.wifi,
        change=patch,
        reason="Change ONT WiFi settings",
    )


def submit_wifi_change_for_bulk(
    db: Session,
    ont_id: str,
    *,
    actor: str,
    params: dict[str, object],
) -> ActionResult:
    """One lifecycle admission per ONT for a bulk wifi_update.

    The bulk route already checked ``network:ont:write`` for the initiator.
    """
    raw_enabled = params.get("enabled")
    patch = wifi_patch(
        ssid=cast_text(params.get("ssid")),
        password=cast_text(params.get("password")),
        enabled=raw_enabled if isinstance(raw_enabled, bool) else None,
        channel=cast_text(params.get("channel")),
        security_mode=cast_text(params.get("security_mode")),
    )
    if patch is None:
        return ActionResult(success=False, message="A WiFi setting is required.")
    return admit_section_change(
        db,
        ont_id,
        context=CommandContext.system(
            actor=actor or "bulk:unknown",
            scope="network:ont:write",
            reason="Bulk ONT WiFi update",
            idempotency_key=f"ont-config:{ont_id}:wifi:{uuid.uuid4()}",
        ),
        permission_granted=True,
        section=OntConfigurationSection.wifi,
        change=patch,
    )


def cast_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
