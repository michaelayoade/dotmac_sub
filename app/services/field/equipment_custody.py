from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.dispatch import TechnicianProfile
from app.models.field_asset import (
    FIELD_ASSET_CUSTODY_SOURCES,
    FieldAsset,
    FieldAssetCustody,
)
from app.models.field_material import FieldInventoryItem
from app.models.network import CPEDevice, OLTDevice, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import Router
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import _profile_from_principal


class FieldEquipmentCustodyService:
    @staticmethod
    def list_mine(
        db: Session,
        principal: dict,
        *,
        status: str = "issued",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        return _serialize_many(
            db,
            apply_pagination(
                _base_query(db)
                .filter(FieldAssetCustody.technician_id == profile.id)
                .filter(FieldAssetCustody.status == _status(status))
                .order_by(FieldAssetCustody.issued_at.desc()),
                limit,
                offset,
            ).all(),
        )

    @staticmethod
    def list_all(
        db: Session,
        *,
        technician_id: str | None = None,
        asset_source: str | None = None,
        status: str = "issued",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        query = _base_query(db).filter(FieldAssetCustody.status == _status(status))
        if technician_id:
            query = query.filter(
                FieldAssetCustody.technician_id == coerce_uuid(technician_id)
            )
        if asset_source:
            query = query.filter(
                FieldAssetCustody.asset_source == _source(asset_source)
            )
        query = query.order_by(FieldAssetCustody.issued_at.desc())
        return _serialize_many(db, apply_pagination(query, limit, offset).all())

    @staticmethod
    def issue(
        db: Session,
        *,
        asset_source: str,
        asset_id: str,
        technician_id: str,
        condition_on_issue: str | None = None,
        notes: str | None = None,
    ) -> dict:
        source = _source(asset_source)
        asset_uuid = coerce_uuid(asset_id)
        technician_uuid = coerce_uuid(technician_id)
        technician = db.get(TechnicianProfile, technician_uuid)
        if technician is None or not technician.is_active:
            raise HTTPException(status_code=404, detail="Technician not found")
        _asset_or_404(db, source, str(asset_uuid))
        existing = (
            _base_query(db)
            .filter(FieldAssetCustody.asset_source == source)
            .filter(FieldAssetCustody.asset_id == asset_uuid)
            .filter(FieldAssetCustody.status == "issued")
            .one_or_none()
        )
        if existing is not None:
            if existing.technician_id == technician.id:
                return _serialize(db, existing)
            raise HTTPException(status_code=409, detail="Asset is already issued")
        custody = FieldAssetCustody(
            asset_source=source,
            asset_id=asset_uuid,
            field_asset_id=asset_uuid if source == "field_asset" else None,
            technician_id=technician.id,
            system_user_id=technician.system_user_id,
            status="issued",
            condition_on_issue=(condition_on_issue or "").strip() or None,
            notes=(notes or "").strip() or None,
        )
        db.add(custody)
        db.commit()
        db.refresh(custody)
        return _serialize(db, custody)

    @staticmethod
    def return_asset(
        db: Session,
        custody_id: str,
        *,
        status: str = "returned",
        condition_on_return: str | None = None,
        notes: str | None = None,
    ) -> dict:
        custody = db.get(FieldAssetCustody, coerce_uuid(custody_id))
        if custody is None:
            raise HTTPException(status_code=404, detail="Asset custody not found")
        if custody.status != "issued":
            raise HTTPException(status_code=409, detail="Asset is not currently issued")
        custody.status = _return_status(status)
        custody.returned_at = datetime.now(UTC)
        custody.condition_on_return = (condition_on_return or "").strip() or None
        if notes:
            custody.notes = notes.strip()
        db.commit()
        db.refresh(custody)
        return _serialize(db, custody)


def _base_query(db: Session):
    return db.query(FieldAssetCustody).options(
        selectinload(FieldAssetCustody.technician),
        selectinload(FieldAssetCustody.system_user),
    )


def _serialize_many(db: Session, rows: list[FieldAssetCustody]) -> list[dict]:
    return [_serialize(db, row) for row in rows]


def _serialize(db: Session, row: FieldAssetCustody) -> dict:
    asset = _asset_item(db, row.asset_source, str(row.asset_id))
    user = row.system_user
    assigned_to = None
    if user is not None:
        assigned_to = user.display_name or f"{user.first_name} {user.last_name}".strip()
    if not assigned_to and row.technician is not None:
        assigned_to = row.technician.title or str(row.technician.id)
    return {
        "id": row.id,
        "asset_source": row.asset_source,
        "asset_id": row.asset_id,
        "technician_id": row.technician_id,
        "system_user_id": row.system_user_id,
        "status": row.status,
        "issued_at": row.issued_at,
        "returned_at": row.returned_at,
        "condition_on_issue": row.condition_on_issue,
        "condition_on_return": row.condition_on_return,
        "notes": row.notes,
        "asset_label": asset.get("label") if asset else None,
        "asset_identifier": asset.get("identifier") if asset else None,
        "assigned_to": assigned_to,
    }


def _asset_or_404(db: Session, source: str, asset_id: str) -> dict:
    asset = _asset_item(db, source, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def _asset_item(db: Session, source: str, asset_id: str) -> dict | None:
    asset_uuid = coerce_uuid(asset_id)
    if source == "field_inventory":
        inventory_item = db.get(FieldInventoryItem, asset_uuid)
        if inventory_item is None or not inventory_item.is_active:
            return None
        return {
            "label": inventory_item.name,
            "identifier": inventory_item.sku or inventory_item.crm_item_id,
        }
    if source == "field_asset":
        field_asset = db.get(FieldAsset, asset_uuid)
        if field_asset is None or not field_asset.is_active:
            return None
        return {"label": field_asset.name, "identifier": field_asset.asset_tag}
    if source == "ont":
        ont = db.get(OntUnit, asset_uuid)
        if ont is None or not ont.is_active:
            return None
        return {
            "label": ont.name or ont.serial_number,
            "identifier": ont.serial_number,
        }
    if source == "cpe":
        cpe = db.get(CPEDevice, asset_uuid)
        if cpe is None:
            return None
        return {
            "label": cpe.serial_number or cpe.mac_address or str(cpe.id),
            "identifier": cpe.mac_address or cpe.serial_number,
        }
    if source == "olt":
        olt = db.get(OLTDevice, asset_uuid)
        if olt is None or not olt.is_active:
            return None
        return {"label": olt.name, "identifier": olt.hostname or olt.mgmt_ip}
    if source == "network_device":
        network_device = db.get(NetworkDevice, asset_uuid)
        if network_device is None or not network_device.is_active:
            return None
        return {
            "label": network_device.name,
            "identifier": network_device.hostname or network_device.mgmt_ip,
        }
    if source == "router":
        router = db.get(Router, asset_uuid)
        if router is None or not router.is_active:
            return None
        return {
            "label": router.name,
            "identifier": router.hostname or router.management_ip,
        }
    return None


def _source(value: str) -> str:
    source = (value or "").strip().lower()
    if source not in FIELD_ASSET_CUSTODY_SOURCES:
        raise HTTPException(
            status_code=422, detail=f"Unsupported asset source: {value}"
        )
    return source


def _status(value: str) -> str:
    status = (value or "issued").strip().lower()
    if status not in {"issued", "returned", "lost", "damaged"}:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {value}")
    return status


def _return_status(value: str) -> str:
    status = _status(value)
    if status == "issued":
        raise HTTPException(status_code=422, detail="Return status cannot be issued")
    return status


field_equipment_custody = FieldEquipmentCustodyService()
