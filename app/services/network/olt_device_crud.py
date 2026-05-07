"""CRUD manager for OLT devices."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import DeviceStatus, OLTDevice, OntAssignment, OntUnit, PonPort
from app.schemas.network import OLTDeviceUpdate
from app.services.crud import CRUDManager
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network._common import _apply_ordering, _apply_pagination
from app.services.query_builders import apply_active_state

logger = logging.getLogger(__name__)


def _payload_fields_set(payload: Any) -> set[str]:
    fields_set = getattr(payload, "model_fields_set", None)
    return set(fields_set or [])


def _payload_data(payload: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=exclude_unset)
    if isinstance(payload, dict):
        return dict(payload)
    return dict(payload)


def _infer_olt_capabilities(data: dict[str, Any], explicit_fields: set[str]) -> None:
    """Fill firmware-derived OLT capabilities unless the caller overrides them."""
    source = str(data.get("capabilities_source") or "auto")
    if source == "manual":
        return

    from app.services.adapters.olt_types import olt_type_registry

    capabilities = olt_type_registry.get_capabilities(
        model=data.get("model"),
        firmware=data.get("firmware_version") or data.get("software_version"),
    )
    if "capabilities_source" not in explicit_fields:
        data["capabilities_source"] = "auto"
    if "supports_ont_internet_config" not in explicit_fields:
        data["supports_ont_internet_config"] = capabilities.supports_ont_internet_config
    if "supports_ont_wan_config" not in explicit_fields:
        data["supports_ont_wan_config"] = capabilities.supports_ont_wan_config
    if "supports_ont_home_gateway_config" not in explicit_fields:
        data["supports_ont_home_gateway_config"] = (
            capabilities.supports_ont_home_gateway_config
        )
    if "wan_provisioning_mode" not in explicit_fields:
        data["wan_provisioning_mode"] = capabilities.wan_provisioning_mode


class OLTDevices(CRUDManager[OLTDevice]):
    model = OLTDevice
    not_found_detail = "OLT device not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def _is_retired(device: OLTDevice) -> bool:
        status = getattr(device, "status", None)
        return getattr(status, "value", status) == DeviceStatus.retired.value

    @staticmethod
    def _require_authorization_ready(db: Session, device: OLTDevice) -> None:
        """Enforce the active-OLT data contract at the service boundary."""
        if not bool(getattr(device, "is_active", False)):
            return
        from app.services.network.olt_config_pack import (
            get_validation_summary,
            validate_config_pack_comprehensive,
        )

        validation = validate_config_pack_comprehensive(db, device.id)
        if validation.is_valid:
            return
        summary = get_validation_summary(validation)
        errors = "; ".join(validation.errors)
        detail = (
            f"Active OLTs must be authorization-ready: {summary}."
            if not errors
            else f"Active OLTs must be authorization-ready: {summary}. {errors}"
        )
        raise HTTPException(status_code=400, detail=detail)

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[OLTDevice]:
        stmt = select(OLTDevice)
        stmt = apply_active_state(stmt, OLTDevice.is_active, is_active)
        stmt = _apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": OLTDevice.created_at, "name": OLTDevice.name},
        )
        return list(db.scalars(_apply_pagination(stmt, limit, offset)).all())

    @classmethod
    def create(cls, db: Session, payload) -> OLTDevice:
        data = _payload_data(payload, exclude_unset=False)
        _infer_olt_capabilities(data, _payload_fields_set(payload))
        device = super().create(db, data, commit=False)
        try:
            cls._require_authorization_ready(db, device)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(device)
        emit_event(
            db,
            EventType.olt_created,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        return device

    @classmethod
    def get(cls, db: Session, device_id: str) -> OLTDevice:
        device = db.get(OLTDevice, device_id)
        if not device or cls._is_retired(device):
            raise HTTPException(status_code=404, detail=cls.not_found_detail)
        return device

    @classmethod
    def update(cls, db: Session, device_id: str, payload: OLTDeviceUpdate) -> OLTDevice:
        existing = cls.get(db, device_id)
        before_ssh_identity = (
            existing.mgmt_ip,
            existing.ssh_port,
            existing.ssh_username,
            existing.ssh_password,
        )
        explicit_fields = _payload_fields_set(payload)
        data = _payload_data(payload, exclude_unset=True)
        if {
            "model",
            "firmware_version",
            "software_version",
            "capabilities_source",
        } & explicit_fields:
            capability_context = {
                "model": data.get("model", existing.model),
                "firmware_version": data.get(
                    "firmware_version", existing.firmware_version
                ),
                "software_version": data.get(
                    "software_version", existing.software_version
                ),
                "capabilities_source": data.get(
                    "capabilities_source",
                    getattr(existing, "capabilities_source", "auto"),
                ),
            }
            capability_context.update(
                {
                    key: value
                    for key, value in data.items()
                    if key
                    in {
                        "supports_ont_internet_config",
                        "supports_ont_wan_config",
                        "supports_ont_home_gateway_config",
                        "wan_provisioning_mode",
                        "capabilities_source",
                    }
                }
            )
            _infer_olt_capabilities(capability_context, explicit_fields)
            data.update(
                {
                    key: capability_context[key]
                    for key in (
                        "supports_ont_internet_config",
                        "supports_ont_wan_config",
                        "supports_ont_home_gateway_config",
                        "wan_provisioning_mode",
                        "capabilities_source",
                    )
                    if key in capability_context and key not in explicit_fields
                }
            )
        device = existing
        for key, value in data.items():
            setattr(device, key, value)
        db.flush()
        try:
            cls._require_authorization_ready(db, device)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(device)
        after_ssh_identity = (
            device.mgmt_ip,
            device.ssh_port,
            device.ssh_username,
            device.ssh_password,
        )
        if before_ssh_identity != after_ssh_identity:
            try:
                from app.services.network.olt_ssh_pool import ssh_pool

                ssh_pool.invalidate(str(device.id))
            except Exception:
                logger.exception(
                    "Failed to invalidate SSH pool for OLT %s after credential rotation",
                    device.id,
                )
        emit_event(
            db,
            EventType.olt_updated,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        return device

    @classmethod
    def delete(cls, db: Session, device_id: str) -> None:
        device = db.get(OLTDevice, device_id)
        if not device or cls._is_retired(device):
            raise HTTPException(status_code=404, detail=cls.not_found_detail)
        linked_onts = (
            db.scalar(
                select(func.count(OntUnit.id))
                .where(OntUnit.olt_device_id == device.id)
                .where(OntUnit.is_active.is_(True))
            )
            or 0
        )
        active_assignments = (
            db.scalar(
                select(func.count(OntAssignment.id))
                .join(PonPort, OntAssignment.pon_port_id == PonPort.id)
                .where(PonPort.olt_id == device.id)
                .where(OntAssignment.active.is_(True))
            )
            or 0
        )
        if linked_onts or active_assignments:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete OLT while active ONTs or assignments exist. "
                    "Return ONTs to inventory or deactivate assignments first."
                ),
            )
        emit_event(
            db,
            EventType.olt_deleted,
            {"olt_id": str(device.id), "name": device.name},
            actor="system",
        )
        device.status = DeviceStatus.retired
        device.is_active = False
        db.commit()

    @staticmethod
    def propagate_acs_to_onts(db: Session, olt_id: str) -> dict[str, int]:
        """Align active TR-069 device rows to the OLT ACS server.

        ONTs inherit the OLT ACS by resolution; this does not write inherited
        values back onto ``OntUnit.tr069_acs_server_id``.
        """
        olt = db.get(OLTDevice, olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")

        acs_id = getattr(olt, "tr069_acs_server_id", None)
        if not acs_id:
            raise HTTPException(
                status_code=400,
                detail="OLT has no ACS server configured",
            )

        onts = list(
            db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
        )
        total = len(onts)
        updated = 0
        already_bound = 0
        skipped = 0
        from app.services import tr069 as tr069_service

        for ont in onts:
            if getattr(ont, "tr069_acs_server_id", None) == acs_id:
                already_bound += 1
            else:
                changed = tr069_service.sync_ont_acs_server(db, ont, acs_id)
                if changed:
                    updated += changed
                else:
                    skipped += 1
        if updated:
            db.commit()
        return {
            "updated": updated,
            "already_bound": already_bound,
            "skipped": skipped,
            "total": total,
        }

    @staticmethod
    def backfill_pon_ports(db: Session, olt_id: str) -> dict[str, int]:
        """Create missing PON ports from ONT board/port data and link assignments.

        Returns stats dict with ports_created, assignments_linked, total_onts.
        """
        olt = db.get(OLTDevice, olt_id)
        if not olt:
            raise HTTPException(status_code=404, detail="OLT device not found")

        onts = list(
            db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
        )
        total_onts = len(onts)
        ports_created = 0
        assignments_linked = 0

        existing_ports: dict[str, PonPort] = {}
        for port in db.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).all():
            key = f"{getattr(port, 'board', '')}/{getattr(port, 'port', '')}"
            existing_ports[key] = port

        for ont in onts:
            board = getattr(ont, "board", None)
            port_str = getattr(ont, "port", None)
            if not board or not port_str:
                continue
            key = f"{board}/{port_str}"
            if key not in existing_ports:
                import uuid as _uuid

                new_port = PonPort(
                    id=str(_uuid.uuid4()),
                    olt_id=str(olt.id),
                    board=board,
                    port=port_str,
                    label=f"{board}/{port_str}",
                    is_active=True,
                )
                db.add(new_port)
                db.flush()
                existing_ports[key] = new_port
                ports_created += 1

        if ports_created:
            db.commit()
        return {
            "ports_created": ports_created,
            "assignments_linked": assignments_linked,
            "total_onts": total_onts,
        }
