"""Canonical ONT config mutations touching OLT SSH / TR-069, then persisting to DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.ont_action_common import (
    ActionResult,
    get_ont_or_error,
)
from app.services.web_network_service_ports import _normalize_fsp, _parse_ont_id_on_olt

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


def _fsp_from_assignment(assignment: OntAssignment) -> str | None:
    """Derive FSP string from assignment → pon_port relationship."""
    pp = assignment.pon_port
    if not pp:
        return None
    # PonPort typically has frame/slot/port or a name like "0/1/0"
    return _normalize_fsp(pp.name)


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
                    logger.warning(
                        "TR-069 PPPoE set failed for ONT %s: %s", ont_id, result.message
                    )
            except Exception as exc:
                logger.warning("TR-069 PPPoE set error for ONT %s: %s", ont_id, exc)

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

        olt, assignment, olt_err = _resolve_olt_context(db, ont)
        if olt_err:
            return olt_err
        if olt is None or assignment is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        fsp = _fsp_from_assignment(assignment)
        if not fsp:
            return ActionResult(
                success=False, message="Cannot determine FSP for this ONT."
            )

        # We need the ONT number within the PON port; parse from external_id or assignment
        ont_number = _parse_ont_id_on_olt(ont.external_id)
        if ont_number is None:
            return ActionResult(
                success=False,
                message="ONT external_id does not contain a usable ONT number.",
            )

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
                    (Vlan.olt_device_id == olt.id) | (Vlan.olt_device_id.is_(None)),
                )
            ).first()
            resolved_mgmt_vlan_id = getattr(vlan_obj, "id", None) if vlan_obj else None

        if vlan_int is None:
            return ActionResult(
                success=False, message="Management VLAN ID is required."
            )

        try:
            from app.services.network.olt_ssh_ont import configure_ont_iphost

            success, message = configure_ont_iphost(
                olt,
                fsp,
                ont_number,
                vlan_id=vlan_int,
                ip_mode=mgmt_ip_mode,
                priority=mgmt_priority,
                ip_address=mgmt_ip_address,
                subnet=mgmt_subnet,
                gateway=mgmt_gateway,
            )
            if not success:
                return ActionResult(success=False, message=message)
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

        olt, assignment, olt_err = _resolve_olt_context(db, ont)
        if olt_err:
            return olt_err
        if olt is None or assignment is None:
            return ActionResult(success=False, message="ONT OLT context is incomplete.")

        fsp = _fsp_from_assignment(assignment)
        if not fsp:
            return ActionResult(success=False, message="Cannot determine FSP.")

        ont_number = _parse_ont_id_on_olt(ont.external_id)
        if ont_number is None:
            return ActionResult(
                success=False,
                message="ONT external_id does not contain a usable ONT number.",
            )

        try:
            from app.services.network.olt_ssh_service_ports import (
                create_single_service_port,
            )

            success, message = create_single_service_port(
                olt,
                fsp,
                ont_number,
                gem_index,
                vlan_id,
                user_vlan=user_vlan,
                tag_transform=tag_transform,
            )
            if not success:
                return ActionResult(success=False, message=message)
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
    ) -> ActionResult:
        """Move ONT to different PON port — deactivate old assignment, create new."""
        ont, err = get_ont_or_error(db, ont_id)
        if err:
            return err
        if ont is None:
            return ActionResult(success=False, message="ONT not found.")

        from app.services.common import coerce_uuid

        target_port = db.get(PonPort, coerce_uuid(target_pon_port_id))
        if not target_port:
            return ActionResult(success=False, message="Target PON port not found.")

        # Deactivate current assignment
        current = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
        ).first()
        if current:
            current.active = False

        # Create new assignment
        new_assignment = OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=target_port.id,
            subscriber_id=current.subscriber_id if current else None,
            active=True,
            assigned_at=datetime.now(UTC),
            notes="Moved from previous assignment",
        )
        db.add(new_assignment)
        ont.olt_device_id = target_port.olt_id
        _set_sync_meta(ont, "manual")
        db.commit()
        _emit_ont_event(
            db,
            "ont.moved",
            {"ont_id": str(ont.id), "target_pon_port_id": target_pon_port_id},
        )
        return ActionResult(success=True, message="ONT moved to new PON port.")

    @staticmethod
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
