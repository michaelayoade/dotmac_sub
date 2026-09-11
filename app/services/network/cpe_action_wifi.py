"""WiFi and LAN-related CPE device actions.

WiFi SSID/password changes for the CPE-detail admin page do NOT write to
GenieACS directly. They resolve the exact, unambiguous ONT behind the CPE
(``resolve_cpe_wifi_admission_scope``) and delegate to
``configure_ont_service`` -- the ``network.ont_service_configuration`` owner's
admin entry point, the same one used by the ONT Configure tab -- so the
change is staged as a durable ``OntServiceConfigurationRevision`` and applied
through the normal dispatch/reconcile lifecycle. This is deliberate: a direct
GenieACS write here, with no desired-state trace, is exactly the two-writer
condition a later ONT reconcile pass can silently revert. See
``resolve_cpe_wifi_admission_scope`` for the exact refusal vocabulary and
``app/web/admin/network_cpes.py`` for the web-layer command context and
permission check this delegation requires (``network:ont:write``, in
addition to the ``network:cpe:write`` already gating these routes).

LAN port toggling is unaffected by this change and still writes to GenieACS
directly via ``set_and_verify``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.models.network import OntUnit
from app.services.domain_errors import DomainError
from app.services.genieacs_client import GenieACSError
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_cpe_client_or_error,
    set_and_verify,
)
from app.services.network.ont_service_configuration import (
    ConfigureOntServiceCommand,
    OntConfigurationSection,
    WifiConfigurationChange,
    configure_ont_service,
    resolve_cpe_wifi_admission_scope,
)
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

_LAN_PORT_PATHS = {
    "Device": "Ethernet.Interface.{port}.Enable",
    "InternetGatewayDevice": "LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable",
}


@dataclass(frozen=True, slots=True)
class _CurrentWifiFields:
    enabled: bool
    ssid: str | None
    channel: str | None
    security_mode: str | None


def _current_wifi_change(db: Session, ont: OntUnit) -> _CurrentWifiFields:
    """Read the ONT's current effective WiFi values (for fields left unchanged).

    The owner's ``configure_ont_service`` treats a submitted
    ``WifiConfigurationChange`` as the complete desired WiFi section, not a
    sparse patch (mirrors how the admin Configure tab always submits a
    fully-populated form). A quick CPE-detail SSID/password action changes
    exactly one field, so the others must be carried forward as-is rather
    than defaulted to ``None`` -- which would clear them.
    """
    values = resolve_effective_ont_config(db, ont)["values"]
    channel = values.get("wifi_channel")
    security_mode = values.get("wifi_security_mode")
    enabled = values.get("wifi_enabled")
    return _CurrentWifiFields(
        enabled=True if enabled is None else bool(enabled),
        ssid=str(values.get("wifi_ssid") or "") or None,
        channel=str(channel) if channel not in (None, "") else None,
        security_mode=(str(security_mode) if security_mode not in (None, "") else None),
    )


def _parse_cpe_uuid(cpe_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(cpe_id))
    except (TypeError, ValueError, AttributeError):
        return None


def set_wifi_ssid(
    db: Session,
    cpe_id: str,
    ssid: str,
    *,
    context: CommandContext,
    permission_granted: bool,
) -> ActionResult:
    """Queue a WiFi SSID change on the ONT behind this CPE via its owner."""
    if not ssid or len(ssid) > 32:
        return ActionResult(success=False, message="SSID must be 1-32 characters.")

    cpe_uuid = _parse_cpe_uuid(cpe_id)
    if cpe_uuid is None:
        return ActionResult(
            success=False,
            message="Invalid CPE identifier.",
            error_code="cpe_device_not_found",
        )
    try:
        scope = resolve_cpe_wifi_admission_scope(db, cpe_uuid)
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    ont = db.get(OntUnit, scope.ont_unit_id)
    if ont is None:
        return ActionResult(
            success=False,
            message="The ONT linked to this CPE could not be found.",
            error_code="cpe_ont_missing",
        )
    current = _current_wifi_change(db, ont)
    finish_read_transaction(db)

    try:
        outcome = configure_ont_service(
            db,
            ConfigureOntServiceCommand(
                context=context,
                ont_unit_id=scope.ont_unit_id,
                permission_granted=permission_granted,
                section=OntConfigurationSection.wifi,
                change=WifiConfigurationChange(
                    enabled=current.enabled,
                    ssid=ssid,
                    channel=current.channel,
                    security_mode=current.security_mode,
                    password=None,
                ),
            ),
        )
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    logger.info(
        "WiFi SSID change queued for CPE %s via ONT %s (operation %s)",
        cpe_id,
        scope.ont_unit_id,
        outcome.operation_id,
    )
    return ActionResult(
        success=True,
        message=f"WiFi SSID update queued: {outcome.message}",
        data={
            "operation_id": str(outcome.operation_id),
            "ont_unit_id": str(scope.ont_unit_id),
        },
    )


def set_wifi_password(
    db: Session,
    cpe_id: str,
    password: str,
    *,
    context: CommandContext,
    permission_granted: bool,
) -> ActionResult:
    """Queue a WiFi password change on the ONT behind this CPE via its owner."""
    if not password or len(password) < 8:
        return ActionResult(
            success=False, message="WiFi password must be at least 8 characters."
        )

    cpe_uuid = _parse_cpe_uuid(cpe_id)
    if cpe_uuid is None:
        return ActionResult(
            success=False,
            message="Invalid CPE identifier.",
            error_code="cpe_device_not_found",
        )
    try:
        scope = resolve_cpe_wifi_admission_scope(db, cpe_uuid)
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    ont = db.get(OntUnit, scope.ont_unit_id)
    if ont is None:
        return ActionResult(
            success=False,
            message="The ONT linked to this CPE could not be found.",
            error_code="cpe_ont_missing",
        )
    current = _current_wifi_change(db, ont)
    finish_read_transaction(db)

    try:
        outcome = configure_ont_service(
            db,
            ConfigureOntServiceCommand(
                context=context,
                ont_unit_id=scope.ont_unit_id,
                permission_granted=permission_granted,
                section=OntConfigurationSection.wifi,
                change=WifiConfigurationChange(
                    enabled=current.enabled,
                    ssid=current.ssid,
                    channel=current.channel,
                    security_mode=current.security_mode,
                    password=password,
                ),
            ),
        )
    except DomainError as exc:
        return ActionResult(success=False, message=exc.message, error_code=exc.code)

    logger.info(
        "WiFi password change queued for CPE %s via ONT %s (operation %s)",
        cpe_id,
        scope.ont_unit_id,
        outcome.operation_id,
    )
    return ActionResult(
        success=True,
        message=f"WiFi password update queued: {outcome.message}",
        data={
            "operation_id": str(outcome.operation_id),
            "ont_unit_id": str(scope.ont_unit_id),
        },
    )


def toggle_lan_port(db: Session, cpe_id: str, port: int, enabled: bool) -> ActionResult:
    """Enable or disable a CPE LAN port via TR-069."""
    if port < 1 or port > 4:
        return ActionResult(
            success=False, message="Port number must be between 1 and 4."
        )

    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    value = "true" if enabled else "false"
    path = _LAN_PORT_PATHS[root].format(port=port)
    params = build_tr069_params(root, {path: value})
    try:
        result = set_and_verify(client, device_id, params)
        action_word = "enabled" if enabled else "disabled"
        logger.info("LAN port %d %s on CPE %s", port, action_word, cpe.serial_number)
        return ActionResult(
            success=True,
            message=f"LAN port {port} {action_word} on {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Toggle LAN port %d failed for CPE %s: %s", port, cpe.serial_number, exc
        )
        return ActionResult(success=False, message=f"Failed to toggle LAN port: {exc}")
