"""Vendor model capability and TR-069 parameter map services."""

from __future__ import annotations

import builtins
import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import Tr069ParameterMap, VendorModelCapability
from app.services.common import apply_ordering, coerce_uuid

logger = logging.getLogger(__name__)


class VendorCapabilities:
    """CRUD operations for vendor model capability catalog entries."""

    @staticmethod
    def list(
        db: Session,
        *,
        vendor: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        order_by: str = "vendor",
        order_dir: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[VendorModelCapability]:
        """List vendor capabilities with optional filtering."""
        stmt = select(VendorModelCapability)
        if is_active is not None:
            stmt = stmt.where(VendorModelCapability.is_active.is_(is_active))
        if vendor:
            stmt = stmt.where(VendorModelCapability.vendor.ilike(vendor))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                VendorModelCapability.vendor.ilike(pattern)
                | VendorModelCapability.model.ilike(pattern)
            )

        allowed_columns = {
            "vendor": VendorModelCapability.vendor,
            "model": VendorModelCapability.model,
            "created_at": VendorModelCapability.created_at,
        }
        stmt = apply_ordering(stmt, order_by, order_dir, allowed_columns)
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, capability_id: str) -> VendorModelCapability:
        """Get a vendor capability by ID with parameter maps loaded."""
        stmt = (
            select(VendorModelCapability)
            .options(selectinload(VendorModelCapability.parameter_maps))
            .where(VendorModelCapability.id == coerce_uuid(capability_id))
        )
        item = db.scalars(stmt).first()
        if not item:
            raise HTTPException(status_code=404, detail="Vendor capability not found")
        return item

    @staticmethod
    def create(
        db: Session,
        *,
        vendor: str,
        model: str,
        firmware_pattern: str | None = None,
        tr069_root: str | None = None,
        supported_features: dict | None = None,
        max_wan_services: int = 1,
        max_lan_ports: int = 4,
        max_ssids: int = 2,
        supports_vlan_tagging: bool = True,
        supports_qinq: bool = False,
        supports_ipv6: bool = False,
        notes: str | None = None,
    ) -> VendorModelCapability:
        """Create a new vendor model capability entry."""
        item = VendorModelCapability(
            vendor=vendor,
            model=model,
            firmware_pattern=firmware_pattern,
            tr069_root=tr069_root,
            supported_features=supported_features or {},
            max_wan_services=max_wan_services,
            max_lan_ports=max_lan_ports,
            max_ssids=max_ssids,
            supports_vlan_tagging=supports_vlan_tagging,
            supports_qinq=supports_qinq,
            supports_ipv6=supports_ipv6,
            notes=notes,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        logger.info(
            "Created vendor capability %s: %s %s", item.id, item.vendor, item.model
        )
        return item

    @staticmethod
    def update(
        db: Session, capability_id: str, **kwargs: object
    ) -> VendorModelCapability:
        """Update an existing vendor model capability."""
        item = db.get(VendorModelCapability, coerce_uuid(capability_id))
        if not item:
            raise HTTPException(status_code=404, detail="Vendor capability not found")
        for key, value in kwargs.items():
            if hasattr(item, key):
                setattr(item, key, value)
        db.commit()
        db.refresh(item)
        logger.info(
            "Updated vendor capability %s: %s %s", item.id, item.vendor, item.model
        )
        return item

    @staticmethod
    def delete(db: Session, capability_id: str) -> None:
        """Soft-delete a vendor capability by setting is_active=False."""
        item = db.get(VendorModelCapability, coerce_uuid(capability_id))
        if not item:
            raise HTTPException(status_code=404, detail="Vendor capability not found")
        item.is_active = False
        db.commit()
        logger.info("Soft-deleted vendor capability %s", capability_id)

    @staticmethod
    def count(db: Session, *, is_active: bool | None = None) -> int:
        """Count vendor capabilities."""
        stmt = select(func.count()).select_from(VendorModelCapability)
        if is_active is not None:
            stmt = stmt.where(VendorModelCapability.is_active.is_(is_active))
        return db.scalar(stmt) or 0

    @staticmethod
    def resolve_capability(
        db: Session,
        *,
        vendor: str,
        model: str,
        firmware: str | None = None,
    ) -> VendorModelCapability | None:
        """Resolve the best-matching capability for a vendor+model+firmware.

        Matching priority:
        1. Exact vendor + model + firmware pattern match
        2. Exact vendor + model (no firmware filter)
        3. None if no match
        """
        stmt = (
            select(VendorModelCapability)
            .options(selectinload(VendorModelCapability.parameter_maps))
            .where(
                VendorModelCapability.vendor.ilike(vendor),
                VendorModelCapability.model.ilike(model),
                VendorModelCapability.is_active.is_(True),
            )
        )
        candidates = list(db.scalars(stmt).all())
        if not candidates:
            return None

        # If firmware provided, try to match firmware_pattern first
        if firmware:
            for cap in candidates:
                if cap.firmware_pattern and firmware.startswith(cap.firmware_pattern):
                    return cap

        # Fall back to first match (no firmware filter or no firmware_pattern set)
        return candidates[0]

    @staticmethod
    def list_vendors(db: Session) -> builtins.list[str]:
        """Return distinct vendor names for filter dropdowns."""
        stmt = (
            select(VendorModelCapability.vendor)
            .where(VendorModelCapability.is_active.is_(True))
            .distinct()
            .order_by(VendorModelCapability.vendor)
        )
        return list(db.scalars(stmt).all())


class Tr069ParameterMaps:
    """CRUD operations for TR-069 parameter map entries."""

    @staticmethod
    def list_for_capability(db: Session, capability_id: str) -> list[Tr069ParameterMap]:
        """List all parameter maps for a given vendor capability."""
        stmt = (
            select(Tr069ParameterMap)
            .where(Tr069ParameterMap.capability_id == coerce_uuid(capability_id))
            .order_by(Tr069ParameterMap.canonical_name)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, param_map_id: str) -> Tr069ParameterMap:
        """Get a parameter map by ID."""
        item = db.get(Tr069ParameterMap, coerce_uuid(param_map_id))
        if not item:
            raise HTTPException(
                status_code=404, detail="TR-069 parameter map not found"
            )
        return item

    @staticmethod
    def create(
        db: Session,
        *,
        capability_id: str,
        canonical_name: str,
        tr069_path: str,
        writable: bool = True,
        value_type: str | None = None,
        notes: str | None = None,
    ) -> Tr069ParameterMap:
        """Create a new TR-069 parameter map entry."""
        item = Tr069ParameterMap(
            capability_id=coerce_uuid(capability_id),
            canonical_name=canonical_name,
            tr069_path=tr069_path,
            writable=writable,
            value_type=value_type,
            notes=notes,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        logger.info(
            "Created TR-069 param map %s: %s → %s",
            item.id,
            item.canonical_name,
            item.tr069_path,
        )
        return item

    @staticmethod
    def update(db: Session, param_map_id: str, **kwargs: object) -> Tr069ParameterMap:
        """Update a TR-069 parameter map entry."""
        item = db.get(Tr069ParameterMap, coerce_uuid(param_map_id))
        if not item:
            raise HTTPException(
                status_code=404, detail="TR-069 parameter map not found"
            )
        for key, value in kwargs.items():
            if hasattr(item, key):
                setattr(item, key, value)
        db.commit()
        db.refresh(item)
        logger.info("Updated TR-069 param map %s", param_map_id)
        return item

    @staticmethod
    def delete(db: Session, param_map_id: str) -> None:
        """Hard-delete a TR-069 parameter map entry."""
        item = db.get(Tr069ParameterMap, coerce_uuid(param_map_id))
        if not item:
            raise HTTPException(
                status_code=404, detail="TR-069 parameter map not found"
            )
        db.delete(item)
        db.commit()
        logger.info("Deleted TR-069 param map %s", param_map_id)

    @staticmethod
    def resolve_path(
        db: Session,
        *,
        capability_id: str,
        canonical_name: str,
    ) -> str | None:
        """Resolve a canonical parameter name to the device-specific TR-069 path."""
        stmt = select(Tr069ParameterMap.tr069_path).where(
            Tr069ParameterMap.capability_id == coerce_uuid(capability_id),
            Tr069ParameterMap.canonical_name == canonical_name,
        )
        return db.scalar(stmt)


vendor_capabilities = VendorCapabilities()
tr069_parameter_maps = Tr069ParameterMaps()
