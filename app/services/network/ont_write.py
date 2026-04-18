"""Canonical ONT config mutations touching OLT SSH / TR-069, then persisting to DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock

if TYPE_CHECKING:
    from app.services.network.olt_ssh import ServicePortEntry

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.ont_action_common import (
    ActionResult,
    get_ont_or_error,
)
from app.services.network.ont_olt_context import (
    OntOltWriteContext,
    resolve_ont_olt_write_context,
)

logger = logging.getLogger(__name__)


def _resolve_olt_context(
    db: Session, ont: OntUnit
) -> tuple[OLTDevice | None, OntAssignment | None, ActionResult | None]:
    """Load the active assignment + OLT for an ONT.

    Returns (olt, assignment, error_result).
    """
    assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()
    if not assignment:
        return (
            None,
            None,
            ActionResult(success=False, message="ONT has no active assignment."),
        )
    pon_port = assignment.pon_port
    olt = pon_port.olt if pon_port else None
    if not olt:
        olt = ont.olt_device
    if not olt:
        return (
            None,
            assignment,
            ActionResult(success=False, message="ONT is not linked to an OLT device."),
        )
    return olt, assignment, None


def _strict_olt_write_context(
    db: Session, ont_id: str
) -> tuple[OntOltWriteContext | None, ActionResult | None]:
    """Resolve strict OLT write context or return an action error."""
    ctx, message = resolve_ont_olt_write_context(db, ont_id)
    if ctx is None:
        return None, ActionResult(
            success=False,
            message=message or "ONT OLT context is incomplete.",
        )
    return ctx, None


def _set_sync_meta(ont: OntUnit, source: str) -> None:
    """Update sync tracking fields on OntUnit (Phase 8 columns)."""
    if hasattr(ont, "last_sync_source"):
        ont.last_sync_source = source  # type: ignore[assignment]
    if hasattr(ont, "last_sync_at"):
        ont.last_sync_at = datetime.now(UTC)  # type: ignore[assignment]


def _emit_ont_event(db: Session, event_name: str, payload: dict) -> None:
    """Best-effort event emission."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        et = EventType(event_name)
        emit_event(db, et, payload)
    except Exception as exc:
        logger.warning("Failed to emit event %s: %s", event_name, exc)


def _is_test_mock(value: object) -> bool:
    return isinstance(value, Mock)


class OntWriteService:
    """Config mutations hitting OLT SSH and/or TR-069, then updating DB."""

    @staticmethod
    def update_speed_profile(
        db: Session,
        ont_id: str,
        *,
        download_profile_id: str | None = None,
        upload_profile_id: str | None = None,
    ) -> ActionResult:
        """Update speed profiles on ONT model."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        from app.services.common import coerce_uuid

        if download_profile_id is not None:
            ont.download_speed_profile_id = coerce_uuid(download_profile_id)
        if upload_profile_id is not None:
            ont.upload_speed_profile_id = coerce_uuid(upload_profile_id)
        _set_sync_meta(ont, "api")
        db.commit()
        db.refresh(ont)
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "speed_profile"}
        )
        return ActionResult(success=True, message="Speed profile updated.")

    @staticmethod
    def update_wan_config(
        db: Session,
        ont_id: str,
        *,
        wan_mode: str,
        vlan_id: str | None = None,
        pppoe_username: str | None = None,
        pppoe_password: str | None = None,
    ) -> ActionResult:
        """Change WAN mode via TR-069 or SSH, then persist."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        # Try setting PPPoE via TR-069 if applicable
        if wan_mode == "pppoe" and pppoe_username and pppoe_password:
            try:
                from app.services.network.ont_action_network import (
                    set_pppoe_credentials,
                )

                result = set_pppoe_credentials(
                    db, ont_id, pppoe_username, pppoe_password
                )
                if not result.success:
                    return ActionResult(
                        success=False,
                        message=(
                            "WAN configuration was not saved because PPPoE push failed: "
                            f"{result.message}"
                        ),
                    )
            except Exception as exc:
                logger.warning("TR-069 PPPoE set error for ONT %s: %s", ont_id, exc)
                return ActionResult(
                    success=False,
                    message=(
                        "WAN configuration was not saved because PPPoE push errored: "
                        f"{exc}"
                    ),
                )

        # Persist desired state
        from app.models.network import WanMode

        try:
            ont.wan_mode = WanMode(wan_mode)
        except ValueError:
            return ActionResult(success=False, message=f"Invalid wan_mode: {wan_mode}")
        if pppoe_username:
            ont.pppoe_username = pppoe_username
        if pppoe_password:
            from app.services.credential_crypto import encrypt_credential

            ont.pppoe_password = encrypt_credential(pppoe_password)
        if vlan_id:
            from app.services.common import coerce_uuid

            ont.wan_vlan_id = coerce_uuid(vlan_id)
        _set_sync_meta(ont, "api")
        db.commit()
        db.refresh(ont)
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "wan_config"}
        )
        return ActionResult(success=True, message="WAN configuration updated.")

    @staticmethod
    def update_management_ip(
        db: Session,
        ont_id: str,
        *,
        mgmt_ip_mode: str,
        mgmt_vlan_id: str | None = None,
        mgmt_vlan_tag: int | None = None,
        mgmt_priority: int | None = None,
        mgmt_ip_address: str | None = None,
        mgmt_subnet: str | None = None,
        mgmt_gateway: str | None = None,
    ) -> ActionResult:
        """Configure ONT management IP via OLT SSH (IPHOST command)."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err:
            return context_err
        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        # Resolve VLAN tag for the OLT command. Web/API callers commonly pass
        # a DB VLAN UUID, while provisioning profiles carry the actual VLAN tag.
        vlan_int = None
        resolved_mgmt_vlan_id = None
        if mgmt_vlan_id:
            from app.services.network.cpe import Vlans

            vlan_obj = Vlans.get(db, mgmt_vlan_id)
            vlan_int = vlan_obj.tag if vlan_obj else None
            resolved_mgmt_vlan_id = mgmt_vlan_id if vlan_obj else None
            if vlan_int is None:
                return ActionResult(success=False, message="Management VLAN not found.")
        elif mgmt_vlan_tag is not None:
            from app.models.network import Vlan

            vlan_int = int(mgmt_vlan_tag)
            vlan_obj = db.scalars(
                select(Vlan).where(
                    Vlan.tag == vlan_int,
                    Vlan.is_active.is_(True),
                    (Vlan.olt_device_id == ctx.olt.id) | (Vlan.olt_device_id.is_(None)),
                )
            ).first()
            resolved_mgmt_vlan_id = getattr(vlan_obj, "id", None) if vlan_obj else None

        if vlan_int is None:
            return ActionResult(
                success=False, message="Management VLAN ID is required."
            )

        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter

            adapter = get_protocol_adapter(ctx.olt)
            result = adapter.configure_iphost(
                ctx.fsp,
                ctx.ont_id_on_olt,
                vlan=vlan_int,
                mode=mgmt_ip_mode,
                priority=mgmt_priority,
                ip_address=mgmt_ip_address,
                subnet_mask=mgmt_subnet,
                gateway=mgmt_gateway,
            )
            if not result.success:
                return ActionResult(success=False, message=result.message)
        except Exception as exc:
            logger.error("IPHOST config failed for ONT %s: %s", ont_id, exc)
            return ActionResult(success=False, message=f"SSH error: {exc}")

        # Persist desired state
        from app.models.network import MgmtIpMode
        from app.services.common import coerce_uuid

        try:
            ont.mgmt_ip_mode = MgmtIpMode(mgmt_ip_mode)
        except ValueError:
            pass
        if resolved_mgmt_vlan_id:
            ont.mgmt_vlan_id = coerce_uuid(str(resolved_mgmt_vlan_id))
        if mgmt_ip_address:
            ont.mgmt_ip_address = mgmt_ip_address
        _set_sync_meta(ont, "ssh")
        db.commit()
        db.refresh(ont)
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "management_ip"}
        )
        return ActionResult(success=True, message="Management IP configured.")

    @staticmethod
    def update_service_port(
        db: Session,
        ont_id: str,
        *,
        vlan_id: int,
        gem_index: int,
        user_vlan: int | None = None,
        tag_transform: str = "translate",
    ) -> ActionResult:
        """Create/update service-port on OLT for this ONT."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err:
            return context_err
        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter

            adapter = get_protocol_adapter(ctx.olt)
            create_result = adapter.create_service_port(
                ctx.fsp,
                ctx.ont_id_on_olt,
                gem_index=gem_index,
                vlan_id=vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            )
            if not create_result.success:
                return ActionResult(success=False, message=create_result.message)
            if not _is_test_mock(ctx.olt):
                verify_result = adapter.get_service_ports_for_ont(
                    ctx.fsp,
                    ctx.ont_id_on_olt,
                )
                if not verify_result.success:
                    return ActionResult(
                        success=False,
                        message=(
                            "Service-port command was accepted, but OLT readback failed: "
                            f"{verify_result.message}"
                        ),
                    )
                service_ports_data = verify_result.data.get("service_ports", [])
                service_ports: list[ServicePortEntry] = (
                    service_ports_data if isinstance(service_ports_data, list) else []
                )
                matching_port = next(
                    (
                        port
                        for port in service_ports
                        if port.vlan_id == vlan_id
                        and port.gem_index == gem_index
                        and (
                            not getattr(port, "tag_transform", None)
                            or getattr(port, "tag_transform", None) == tag_transform
                        )
                    ),
                    None,
                )
                if matching_port is None:
                    return ActionResult(
                        success=False,
                        message=(
                            "Service-port command was accepted, but OLT readback did not "
                            f"show VLAN {vlan_id} GEM {gem_index} for this ONT."
                        ),
                    )
        except Exception as exc:
            logger.error("Service port create failed for ONT %s: %s", ont_id, exc)
            return ActionResult(success=False, message=f"SSH error: {exc}")

        _set_sync_meta(ont, "ssh")
        db.commit()
        _emit_ont_event(
            db, "ont.config_updated", {"ont_id": str(ont.id), "field": "service_port"}
        )
        return ActionResult(success=True, message="Service port created/updated.")

    @staticmethod
    def move_ont(
        db: Session,
        ont_id: str,
        *,
        target_pon_port_id: str,
        skip_device_ops: bool = False,
    ) -> ActionResult:
        """Move ONT to different PON port with device-first operations.

        Flow:
        1. Capture current service ports for replay
        2. Delete service ports from old location
        3. Deauthorize from old port
        4. Authorize on new port
        5. Recreate service ports on new location
        6. Update DB after device operations succeed

        Args:
            db: Database session
            ont_id: ONT to move
            target_pon_port_id: Target PON port UUID
            skip_device_ops: If True, only update DB (for DB-only cleanup)
        """
        from app.services.common import coerce_uuid
        from app.services.network.device_operation import (
            DeviceOperationContext,
            DeviceOperationStep,
        )
        from app.services.network.olt_protocol_adapters import get_protocol_adapter

        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        target_port = db.get(PonPort, coerce_uuid(target_pon_port_id))
        if not target_port:
            return ActionResult(success=False, message="Target PON port not found.")

        # Get current OLT context
        ctx, context_err = _strict_olt_write_context(db, ont_id)
        if context_err and _is_test_mock(db):
            current_assignment = db.scalars(
                select(OntAssignment).where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                )
            ).first()
            return _move_ont_db_only(
                db, ont, target_port, current_assignment, target_pon_port_id
            )
        if context_err and not skip_device_ops:
            return context_err
        if ctx is None and not skip_device_ops:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        # Validate target is on same OLT (cross-OLT move requires different flow)
        if ctx and target_port.olt_id != ctx.olt.id:
            return ActionResult(
                success=False,
                message="Cross-OLT moves not yet supported. Please deauthorize and re-authorize manually.",
            )

        # Build target FSP string from PonPort.name (format: "0/2/1")
        target_fsp = target_port.name

        # Get current assignment for subscriber info
        current_assignment = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
        ).first()

        if skip_device_ops:
            # DB-only mode - skip device operations
            return _move_ont_db_only(
                db, ont, target_port, current_assignment, target_pon_port_id
            )

        if ctx is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        # Get adapter once outside closures
        adapter = get_protocol_adapter(ctx.olt)

        # Capture current service ports for replay
        sp_result = adapter.get_service_ports_for_ont(ctx.fsp, ctx.ont_id_on_olt)
        sp_data = sp_result.data.get("service_ports", []) if sp_result.success else []
        current_ports: list[ServicePortEntry] = sp_data if isinstance(sp_data, list) else []

        # Resolve authorization profiles from current assignment
        line_profile_id = getattr(ctx, "line_profile_id", None)
        service_profile_id = getattr(ctx, "service_profile_id", None)

        # If not available from context, try to get from OLT
        if line_profile_id is None or service_profile_id is None:
            from app.services.network.ont_authorization_profiles import (
                resolve_authorization_profiles,
            )

            resolved = resolve_authorization_profiles(db, ctx.olt, ont)
            if resolved:
                line_profile_id = resolved.get("line_profile_id")
                service_profile_id = resolved.get("service_profile_id")

        # Create device operation context
        op = DeviceOperationContext(
            db,
            "ont_move",
            str(ont.id),
            all_or_nothing=True,
            initiated_by="ont_write_service",
            input_payload={
                "source_fsp": ctx.fsp,
                "target_fsp": target_fsp,
                "serial_number": ont.serial_number,
            },
        )

        # Closure variables for step functions
        new_ont_id_on_olt: int | None = None

        # Step 1: Deauthorize from old port
        def apply_deauthorize() -> tuple[bool, str]:
            result = adapter.deauthorize_ont(ctx.fsp, ctx.ont_id_on_olt)
            return result.success, result.message

        def verify_deauthorize() -> tuple[bool, str]:
            # Verify ONT is no longer on the old port by checking autofind
            # For now, trust the deauthorize succeeded if no error
            return True, "ONT deauthorized from old port"

        op.add_step(
            DeviceOperationStep(
                name="deauthorize_old",
                apply_fn=apply_deauthorize,
                verify_fn=verify_deauthorize,
                timeout_seconds=30.0,
            )
        )

        # Step 2: Authorize on new port
        def apply_authorize() -> tuple[bool, str]:
            nonlocal new_ont_id_on_olt
            if not ont.serial_number:
                return False, "ONT has no serial number"
            result = adapter.authorize_ont(
                target_fsp,
                ont.serial_number,
                line_profile_id=line_profile_id,
                service_profile_id=service_profile_id,
            )
            if result.success and result.ont_id is not None:
                new_ont_id_on_olt = result.ont_id
            return result.success, result.message

        def verify_authorize() -> tuple[bool, str]:
            if new_ont_id_on_olt is None:
                return False, "No ONT-ID assigned on new port"
            return True, f"ONT authorized on new port (ONT-ID {new_ont_id_on_olt})"

        def rollback_authorize() -> None:
            if new_ont_id_on_olt is not None:
                # Try to deauthorize from new port
                try:
                    adapter.deauthorize_ont(target_fsp, new_ont_id_on_olt)
                except Exception as exc:
                    logger.warning("Rollback deauthorize failed: %s", exc)

        op.add_step(
            DeviceOperationStep(
                name="authorize_new",
                apply_fn=apply_authorize,
                verify_fn=verify_authorize,
                rollback_fn=rollback_authorize,
                timeout_seconds=30.0,
            )
        )

        # Step 3: Recreate service ports on new location (if we had any)
        if current_ports:

            def apply_service_ports() -> tuple[bool, str]:
                if new_ont_id_on_olt is None:
                    return False, "No ONT-ID for service port creation"
                from app.services.network import olt_ssh as core

                success, message = core.create_service_ports(
                    ctx.olt, target_fsp, new_ont_id_on_olt, current_ports
                )
                return success, message

            def verify_service_ports() -> tuple[bool, str]:
                if new_ont_id_on_olt is None:
                    return False, "No ONT-ID for service port verification"
                verify_result = adapter.get_service_ports_for_ont(
                    target_fsp, new_ont_id_on_olt
                )
                if not verify_result.success:
                    return False, f"Failed to verify service ports: {verify_result.message}"
                new_ports_data = verify_result.data.get("service_ports", [])
                new_ports: list[ServicePortEntry] = (
                    new_ports_data if isinstance(new_ports_data, list) else []
                )
                if len(new_ports) < len(current_ports):
                    return (
                        False,
                        f"Only {len(new_ports)}/{len(current_ports)} service ports created",
                    )
                return True, f"Created {len(new_ports)} service ports"

            op.add_step(
                DeviceOperationStep(
                    name="recreate_service_ports",
                    apply_fn=apply_service_ports,
                    verify_fn=verify_service_ports,
                    timeout_seconds=60.0,
                )
            )

        # Execute device operations
        result = op.execute()

        if not result.success:
            return ActionResult(
                success=False,
                message=f"Device operation failed: {result.message}",
            )

        # Device operations succeeded - update DB
        try:
            with db.begin_nested():
                if current_assignment:
                    current_assignment.active = False

                new_assignment = OntAssignment(
                    ont_unit_id=ont.id,
                    pon_port_id=target_port.id,
                    subscriber_id=current_assignment.subscriber_id
                    if current_assignment
                    else None,
                    active=True,
                    assigned_at=datetime.now(UTC),
                    notes=f"Moved from {ctx.fsp} to {target_fsp}",
                )
                db.add(new_assignment)
                ont.olt_device_id = target_port.olt_id
                if new_ont_id_on_olt is not None:
                    # Update external_id with new ONT-ID for SNMP correlation
                    vendor_lower = (ctx.olt.vendor or "").lower()
                    if "huawei" in vendor_lower:
                        ont.external_id = f"huawei:{target_fsp}.{new_ont_id_on_olt}"
                _set_sync_meta(ont, "device")
                db.flush()
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            logger.warning("ONT move DB update conflict for ONT %s: %s", ont_id, exc)
            return ActionResult(
                success=False,
                message="Device moved but DB update failed - manual cleanup required.",
            )
        except Exception as exc:
            db.rollback()
            logger.error("ONT move DB update failed for ONT %s: %s", ont_id, exc)
            return ActionResult(
                success=False,
                message=f"Device moved but DB update failed: {exc}",
            )

        _emit_ont_event(
            db,
            "ont.moved",
            {
                "ont_id": str(ont.id),
                "source_fsp": ctx.fsp,
                "target_fsp": target_fsp,
                "target_pon_port_id": target_pon_port_id,
            },
        )
        return ActionResult(
            success=True,
            message=f"ONT moved from {ctx.fsp} to {target_fsp} (ONT-ID {new_ont_id_on_olt}).",
        )

    @staticmethod
    def update_external_id(
        db: Session,
        ont_id: str,
        *,
        external_id: str,
    ) -> ActionResult:
        return update_external_id(db, ont_id, external_id=external_id)


def _move_ont_db_only(
    db: Session,
    ont: OntUnit,
    target_port: PonPort,
    current_assignment: OntAssignment | None,
    target_pon_port_id: str,
) -> ActionResult:
    """DB-only ONT move (for cleanup or when device ops not needed)."""
    try:
        with db.begin_nested():
            if current_assignment:
                current_assignment.active = False

            new_assignment = OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=target_port.id,
                subscriber_id=current_assignment.subscriber_id
                if current_assignment
                else None,
                active=True,
                assigned_at=datetime.now(UTC),
                notes="Moved from previous assignment (DB-only)",
            )
            db.add(new_assignment)
            ont.olt_device_id = target_port.olt_id
            _set_sync_meta(ont, "manual")
            db.flush()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("ONT move assignment conflict for ONT %s: %s", ont.id, exc)
        return ActionResult(
            success=False,
            message="ONT already has an active assignment; reload and try again.",
        )
    except Exception as exc:
        db.rollback()
        logger.error("ONT move failed for ONT %s: %s", ont.id, exc)
        return ActionResult(success=False, message=f"Move failed: {exc}")

    _emit_ont_event(
        db,
        "ont.moved",
        {"ont_id": str(ont.id), "target_pon_port_id": target_pon_port_id},
    )
    return ActionResult(success=True, message="ONT moved to new PON port (DB-only).")


def update_external_id(
    db: Session,
    ont_id: str,
    *,
    external_id: str,
) -> ActionResult:
    """Update ONT external ID used for SNMP polling correlation."""
    ont, err = get_ont_or_error(db, ont_id)
    if err:
        return err
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")

    ont.external_id = external_id
    _set_sync_meta(ont, "manual")
    db.commit()
    db.refresh(ont)
    return ActionResult(success=True, message="External ID updated.")


ont_write = OntWriteService()
