"""Authorization preset management services."""

from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    AuthorizationPreset,
    OLTDevice,
    OntProvisioningProfile,
    Vlan,
)
from app.services.common import apply_ordering, coerce_uuid

logger = logging.getLogger(__name__)


class AuthorizationPresets:
    """CRUD operations for authorization presets."""

    @staticmethod
    def list(
        db: Session,
        *,
        is_active: bool | None = None,
        olt_device_id: str | None = None,
        include_global: bool = True,
        order_by: str = "priority",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuthorizationPreset]:
        """List authorization presets with optional filtering.

        Args:
            db: Database session
            is_active: Filter by active status
            olt_device_id: Filter by OLT scope (also includes global if include_global)
            include_global: Include global presets (olt_device_id is null)
            order_by: Field to order by
            order_dir: Order direction (asc/desc)
            limit: Maximum results
            offset: Skip first N results
        """
        stmt = select(AuthorizationPreset).options(
            selectinload(AuthorizationPreset.provisioning_profile),
            selectinload(AuthorizationPreset.default_vlan),
            selectinload(AuthorizationPreset.olt_device),
        )

        if is_active is not None:
            stmt = stmt.where(AuthorizationPreset.is_active == is_active)

        if olt_device_id:
            olt_uuid = coerce_uuid(olt_device_id)
            if include_global:
                stmt = stmt.where(
                    (AuthorizationPreset.olt_device_id == olt_uuid)
                    | (AuthorizationPreset.olt_device_id.is_(None))
                )
            else:
                stmt = stmt.where(AuthorizationPreset.olt_device_id == olt_uuid)

        allowed_columns = {
            "name": AuthorizationPreset.name,
            "priority": AuthorizationPreset.priority,
            "created_at": AuthorizationPreset.created_at,
        }
        stmt = apply_ordering(stmt, order_by, order_dir, allowed_columns)
        stmt = stmt.offset(offset).limit(limit)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, preset_id: str | UUID) -> AuthorizationPreset | None:
        """Get a single authorization preset by ID."""
        stmt = (
            select(AuthorizationPreset)
            .options(
                selectinload(AuthorizationPreset.provisioning_profile),
                selectinload(AuthorizationPreset.default_vlan),
                selectinload(AuthorizationPreset.olt_device),
            )
            .where(AuthorizationPreset.id == coerce_uuid(preset_id))
        )
        return db.scalars(stmt).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> AuthorizationPreset | None:
        """Get a preset by unique name."""
        stmt = select(AuthorizationPreset).where(AuthorizationPreset.name == name)
        return db.scalars(stmt).first()

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        description: str | None = None,
        provisioning_profile_id: str | None = None,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        default_vlan_id: str | None = None,
        auto_authorize: bool = False,
        serial_pattern: str | None = None,
        olt_device_id: str | None = None,
        priority: int = 0,
        is_active: bool = True,
        is_default: bool = False,
    ) -> AuthorizationPreset:
        """Create a new authorization preset."""
        # Validate serial pattern if provided
        if serial_pattern:
            try:
                re.compile(serial_pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e

        # Validate foreign keys exist
        if provisioning_profile_id:
            profile = db.get(OntProvisioningProfile, coerce_uuid(provisioning_profile_id))
            if not profile:
                raise ValueError("Provisioning profile not found")

        if default_vlan_id:
            vlan = db.get(Vlan, coerce_uuid(default_vlan_id))
            if not vlan:
                raise ValueError("VLAN not found")

        if olt_device_id:
            olt = db.get(OLTDevice, coerce_uuid(olt_device_id))
            if not olt:
                raise ValueError("OLT device not found")

        # If setting as default, unset other defaults (within same scope)
        if is_default:
            stmt = select(AuthorizationPreset).where(
                AuthorizationPreset.is_default.is_(True)
            )
            if olt_device_id:
                stmt = stmt.where(
                    AuthorizationPreset.olt_device_id == coerce_uuid(olt_device_id)
                )
            else:
                stmt = stmt.where(AuthorizationPreset.olt_device_id.is_(None))
            for existing in db.scalars(stmt).all():
                existing.is_default = False

        preset = AuthorizationPreset(
            name=name,
            description=description,
            provisioning_profile_id=coerce_uuid(provisioning_profile_id) if provisioning_profile_id else None,
            line_profile_id=line_profile_id,
            service_profile_id=service_profile_id,
            default_vlan_id=coerce_uuid(default_vlan_id) if default_vlan_id else None,
            auto_authorize=auto_authorize,
            serial_pattern=serial_pattern,
            olt_device_id=coerce_uuid(olt_device_id) if olt_device_id else None,
            priority=priority,
            is_active=is_active,
            is_default=is_default,
        )
        db.add(preset)
        db.flush()
        logger.info("Created authorization preset: %s (id=%s)", name, preset.id)
        return preset

    @staticmethod
    def update(
        db: Session,
        preset_id: str | UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        provisioning_profile_id: str | None = None,
        line_profile_id: int | None = None,
        service_profile_id: int | None = None,
        default_vlan_id: str | None = None,
        auto_authorize: bool | None = None,
        serial_pattern: str | None = None,
        olt_device_id: str | None = None,
        priority: int | None = None,
        is_active: bool | None = None,
        is_default: bool | None = None,
        clear_provisioning_profile: bool = False,
        clear_default_vlan: bool = False,
        clear_olt_device: bool = False,
    ) -> AuthorizationPreset | None:
        """Update an existing authorization preset."""
        preset = db.get(AuthorizationPreset, coerce_uuid(preset_id))
        if not preset:
            return None

        if name is not None:
            preset.name = name
        if description is not None:
            preset.description = description

        # Handle nullable foreign keys with explicit clear flags
        if clear_provisioning_profile:
            preset.provisioning_profile_id = None
        elif provisioning_profile_id is not None:
            profile = db.get(OntProvisioningProfile, coerce_uuid(provisioning_profile_id))
            if not profile:
                raise ValueError("Provisioning profile not found")
            preset.provisioning_profile_id = profile.id

        if clear_default_vlan:
            preset.default_vlan_id = None
        elif default_vlan_id is not None:
            vlan = db.get(Vlan, coerce_uuid(default_vlan_id))
            if not vlan:
                raise ValueError("VLAN not found")
            preset.default_vlan_id = vlan.id

        if clear_olt_device:
            preset.olt_device_id = None
        elif olt_device_id is not None:
            olt = db.get(OLTDevice, coerce_uuid(olt_device_id))
            if not olt:
                raise ValueError("OLT device not found")
            preset.olt_device_id = olt.id

        if line_profile_id is not None:
            preset.line_profile_id = line_profile_id
        if service_profile_id is not None:
            preset.service_profile_id = service_profile_id
        if auto_authorize is not None:
            preset.auto_authorize = auto_authorize
        if serial_pattern is not None:
            if serial_pattern:
                try:
                    re.compile(serial_pattern)
                except re.error as e:
                    raise ValueError(f"Invalid regex pattern: {e}") from e
            preset.serial_pattern = serial_pattern or None
        if priority is not None:
            preset.priority = priority
        if is_active is not None:
            preset.is_active = is_active
        if is_default is not None:
            if is_default:
                # Unset other defaults in same scope
                stmt = select(AuthorizationPreset).where(
                    AuthorizationPreset.is_default.is_(True),
                    AuthorizationPreset.id != preset.id,
                )
                if preset.olt_device_id:
                    stmt = stmt.where(
                        AuthorizationPreset.olt_device_id == preset.olt_device_id
                    )
                else:
                    stmt = stmt.where(AuthorizationPreset.olt_device_id.is_(None))
                for existing in db.scalars(stmt).all():
                    existing.is_default = False
            preset.is_default = is_default

        db.flush()
        logger.info("Updated authorization preset: %s (id=%s)", preset.name, preset.id)
        return preset

    @staticmethod
    def delete(db: Session, preset_id: str | UUID) -> bool:
        """Delete an authorization preset."""
        preset = db.get(AuthorizationPreset, coerce_uuid(preset_id))
        if not preset:
            return False
        name = preset.name
        db.delete(preset)
        db.flush()
        logger.info("Deleted authorization preset: %s (id=%s)", name, preset_id)
        return True

    @staticmethod
    def find_matching_preset(
        db: Session,
        serial_number: str,
        olt_device_id: str | None = None,
    ) -> AuthorizationPreset | None:
        """Find the highest-priority preset matching the serial number.

        Used for auto-authorization when an ONT is discovered.
        """
        stmt = (
            select(AuthorizationPreset)
            .where(
                AuthorizationPreset.is_active.is_(True),
                AuthorizationPreset.auto_authorize.is_(True),
                AuthorizationPreset.serial_pattern.isnot(None),
            )
            .order_by(AuthorizationPreset.priority.desc())
        )

        # Include OLT-specific and global presets
        if olt_device_id:
            olt_uuid = coerce_uuid(olt_device_id)
            stmt = stmt.where(
                (AuthorizationPreset.olt_device_id == olt_uuid)
                | (AuthorizationPreset.olt_device_id.is_(None))
            )

        normalized_serial = serial_number.upper().replace("-", "").strip()

        for preset in db.scalars(stmt).all():
            if preset.serial_pattern:
                try:
                    if re.match(preset.serial_pattern, normalized_serial, re.IGNORECASE):
                        logger.debug(
                            "Serial %s matched preset %s (pattern: %s)",
                            serial_number,
                            preset.name,
                            preset.serial_pattern,
                        )
                        return preset
                except re.error:
                    logger.warning(
                        "Invalid regex in preset %s: %s",
                        preset.name,
                        preset.serial_pattern,
                    )
                    continue

        return None

    @staticmethod
    def count(db: Session, *, is_active: bool | None = None) -> int:
        """Count authorization presets."""
        stmt = select(AuthorizationPreset)
        if is_active is not None:
            stmt = stmt.where(AuthorizationPreset.is_active == is_active)
        return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


# Singleton instance
authorization_presets = AuthorizationPresets()
