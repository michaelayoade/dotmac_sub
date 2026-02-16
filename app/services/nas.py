"""
NAS Device Management Service Layer

Provides CRUD operations and business logic for:
- NAS Device inventory management
- Configuration backup and restore
- Provisioning templates
- Device provisioning execution
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasConfigBackup,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningTemplate,
    RadiusProfile,
    Subscription,
)
from app.models.network_monitoring import PopSite
from app.schemas.catalog import (
    NasConfigBackupCreate,
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningLogCreate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.credential_crypto import decrypt_credential, encrypt_nas_credentials
from app.services.response import ListResponseMixin


_REDACT_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "ssh_key",
    "shared_secret",
}


def _redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    def redact_value(value: Any) -> Any:
        if isinstance(value, dict):
            return _redact_sensitive(value)
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        return value

    redacted: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key.lower() in _REDACT_KEYS:
            redacted[key] = "***redacted***"
        else:
            redacted[key] = redact_value(value)
    return redacted


# =============================================================================
# NAS DEVICE SERVICE
# =============================================================================

class NasDevices(ListResponseMixin):
    """Service class for NAS device CRUD operations."""

    ALLOWED_ORDER_COLUMNS = {
        "name": NasDevice.name,
        "vendor": NasDevice.vendor,
        "status": NasDevice.status,
        "created_at": NasDevice.created_at,
        "updated_at": NasDevice.updated_at,
    }

    @staticmethod
    def create(db: Session, payload: NasDeviceCreate) -> NasDevice:
        """Create a new NAS device."""
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if provided
        if data.get("pop_site_id"):
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        device = NasDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str | UUID) -> NasDevice:
        """Get a NAS device by ID."""
        device_id = coerce_uuid(device_id)
        device = db.get(NasDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        return device

    @staticmethod
    def get_by_code(db: Session, code: str) -> NasDevice | None:
        """Get a NAS device by its code."""
        return db.execute(
            select(NasDevice).where(NasDevice.code == code)
        ).scalar_one_or_none()

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "name",
        order_dir: str = "asc",
        vendor: NasVendor | None = None,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[NasDevice]:
        """List NAS devices with filtering and pagination."""
        query = select(NasDevice)

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        query = apply_ordering(query, order_by, order_dir, NasDevices.ALLOWED_ORDER_COLUMNS)
        query = apply_pagination(query, limit, offset)

        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(db: Session, device_id: str | UUID, payload: NasDeviceUpdate) -> NasDevice:
        """Update a NAS device."""
        device = NasDevices.get(db, device_id)
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if being changed
        if "pop_site_id" in data and data["pop_site_id"]:
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        for key, value in data.items():
            setattr(device, key, value)

        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str | UUID) -> None:
        """Delete a NAS device."""
        device = NasDevices.get(db, device_id)
        device.is_active = False
        device.status = NasDeviceStatus.decommissioned
        db.commit()

    @staticmethod
    def update_last_seen(db: Session, device_id: str | UUID) -> NasDevice:
        """Update the last_seen_at timestamp for a device."""
        device = NasDevices.get(db, device_id)
        device.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count NAS devices with filtering (same filters as list)."""
        query = select(func.count(NasDevice.id))

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        return db.execute(query).scalar() or 0

    @staticmethod
    def count_by_vendor(db: Session) -> dict[str, int]:
        """Get count of devices grouped by vendor."""
        result = db.execute(
            select(NasDevice.vendor, func.count(NasDevice.id))
            .group_by(NasDevice.vendor)
        ).all()
        return {str(vendor.value): count for vendor, count in result}

    @staticmethod
    def count_by_status(db: Session) -> dict[str, int]:
        """Get count of devices grouped by status."""
        result = db.execute(
            select(NasDevice.status, func.count(NasDevice.id))
            .group_by(NasDevice.status)
        ).all()
        return {str(status.value): count for status, count in result}


# =============================================================================
# NAS CONFIG BACKUP SERVICE
# =============================================================================

class NasConfigBackups(ListResponseMixin):
    """Service class for NAS configuration backup operations."""

    @staticmethod
    def create(db: Session, payload: NasConfigBackupCreate) -> NasConfigBackup:
        """Create a new config backup."""
        # Verify device exists
        device = NasDevices.get(db, payload.nas_device_id)

        # Mark previous backups as not current (single atomic UPDATE).
        db.execute(
            update(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .where(NasConfigBackup.is_current.is_(True))
            .values(is_current=False)
        )

        # Create new backup
        data = payload.model_dump(exclude_unset=True)
        config_content = data["config_content"]

        # Calculate hash and size
        config_hash = hashlib.sha256(config_content.encode()).hexdigest()
        config_size = len(config_content.encode())

        # Check if content changed from previous backup
        previous = db.execute(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .order_by(NasConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        has_changes = previous is None or previous.config_hash != config_hash

        backup = NasConfigBackup(
            **data,
            config_hash=config_hash,
            config_size_bytes=config_size,
            has_changes=has_changes,
            is_current=True,
        )
        db.add(backup)

        # Update device last_backup_at
        device.last_backup_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(backup)
        return backup

    @staticmethod
    def cleanup_retention(
        db: Session,
        *,
        keep_last: int = 10,
        keep_all_days: int = 7,
        keep_daily_days: int = 30,
        keep_weekly_days: int = 365,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Apply retention policy to NAS config backups."""
        now = now or datetime.now(timezone.utc)
        keep_all_cutoff = now - timedelta(days=keep_all_days)
        keep_daily_cutoff = now - timedelta(days=keep_daily_days)
        keep_weekly_cutoff = now - timedelta(days=keep_weekly_days)

        device_ids = db.execute(select(NasConfigBackup.nas_device_id).distinct()).scalars().all()
        deleted = 0
        kept = 0

        for device_id in device_ids:
            backups = db.execute(
                select(NasConfigBackup)
                .where(NasConfigBackup.nas_device_id == device_id)
                .order_by(NasConfigBackup.created_at.desc())
            ).scalars().all()

            keep_ids: set[UUID] = set()
            daily_kept: set[str] = set()
            weekly_kept: set[str] = set()

            for backup in backups:
                if backup.keep_forever:
                    keep_ids.add(backup.id)

            for backup in backups[:keep_last]:
                keep_ids.add(backup.id)

            for backup in backups:
                if backup.id in keep_ids:
                    continue
                created_at = backup.created_at or now
                if created_at >= keep_all_cutoff:
                    keep_ids.add(backup.id)
                    continue
                if created_at >= keep_daily_cutoff:
                    day_key = created_at.date().isoformat()
                    if day_key not in daily_kept:
                        daily_kept.add(day_key)
                        keep_ids.add(backup.id)
                    continue
                if created_at >= keep_weekly_cutoff:
                    week_key = f"{created_at.isocalendar().year}-W{created_at.isocalendar().week}"
                    if week_key not in weekly_kept:
                        weekly_kept.add(week_key)
                        keep_ids.add(backup.id)

            for backup in backups:
                if backup.id in keep_ids:
                    kept += 1
                    continue
                db.delete(backup)
                deleted += 1

        db.commit()
        return {"deleted": deleted, "kept": kept}

    @staticmethod
    def get(db: Session, backup_id: str | UUID) -> NasConfigBackup:
        """Get a config backup by ID."""
        backup_id = coerce_uuid(backup_id)
        backup = db.get(NasConfigBackup, backup_id)
        if not backup:
            raise HTTPException(status_code=404, detail="Config backup not found")
        return backup

    @staticmethod
    def list(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        is_current: bool | None = None,
        has_changes: bool | None = None,
    ) -> list[NasConfigBackup]:
        """List config backups with filtering."""
        query = select(NasConfigBackup).order_by(NasConfigBackup.created_at.desc())

        if nas_device_id:
            query = query.where(NasConfigBackup.nas_device_id == nas_device_id)
        if is_current is not None:
            query = query.where(NasConfigBackup.is_current == is_current)
        if has_changes is not None:
            query = query.where(NasConfigBackup.has_changes == has_changes)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        is_current: bool | None = None,
        has_changes: bool | None = None,
    ) -> int:
        """Count config backups with filtering (same filters as list)."""
        query = select(func.count(NasConfigBackup.id))

        if nas_device_id:
            query = query.where(NasConfigBackup.nas_device_id == nas_device_id)
        if is_current is not None:
            query = query.where(NasConfigBackup.is_current == is_current)
        if has_changes is not None:
            query = query.where(NasConfigBackup.has_changes == has_changes)

        return db.execute(query).scalar() or 0

    @staticmethod
    def get_current(db: Session, nas_device_id: UUID) -> NasConfigBackup | None:
        """Get the current (latest) backup for a device."""
        return db.execute(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == nas_device_id)
            .where(NasConfigBackup.is_current == True)
        ).scalar_one_or_none()

    @staticmethod
    def compare(db: Session, backup_id_1: UUID, backup_id_2: UUID) -> dict:
        """Compare two config backups and return diff info."""
        backup1 = NasConfigBackups.get(db, backup_id_1)
        backup2 = NasConfigBackups.get(db, backup_id_2)

        lines1 = backup1.config_content.splitlines()
        lines2 = backup2.config_content.splitlines()

        # Simple line-by-line comparison
        added = []
        removed = []
        set1 = set(lines1)
        set2 = set(lines2)

        for line in lines2:
            if line not in set1 and line.strip():
                added.append(line)
        for line in lines1:
            if line not in set2 and line.strip():
                removed.append(line)

        return {
            "backup_1": {"id": str(backup1.id), "created_at": backup1.created_at},
            "backup_2": {"id": str(backup2.id), "created_at": backup2.created_at},
            "lines_added": len(added),
            "lines_removed": len(removed),
            "added": added[:100],  # Limit to first 100
            "removed": removed[:100],
        }


# =============================================================================
# PROVISIONING TEMPLATE SERVICE
# =============================================================================

class ProvisioningTemplates(ListResponseMixin):
    """Service class for provisioning template operations."""

    @staticmethod
    def create(db: Session, payload: ProvisioningTemplateCreate) -> ProvisioningTemplate:
        """Create a new provisioning template."""
        data = payload.model_dump(exclude_unset=True)

        # Extract placeholders from template content if not provided
        if not data.get("placeholders"):
            content = data.get("template_content", "")
            # Find all {{placeholder}} patterns
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        template = ProvisioningTemplate(**data)
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def get(db: Session, template_id: str | UUID) -> ProvisioningTemplate:
        """Get a provisioning template by ID."""
        template_id = coerce_uuid(template_id)
        template = db.get(ProvisioningTemplate, template_id)
        if not template:
            raise HTTPException(status_code=404, detail="Provisioning template not found")
        return template

    @staticmethod
    def get_by_code(db: Session, code: str) -> ProvisioningTemplate | None:
        """Get a template by its code."""
        return db.execute(
            select(ProvisioningTemplate).where(ProvisioningTemplate.code == code)
        ).scalar_one_or_none()

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> list[ProvisioningTemplate]:
        """List provisioning templates with filtering."""
        query = select(ProvisioningTemplate).order_by(ProvisioningTemplate.name)

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        action: ProvisioningAction | None = None,
        is_active: bool | None = None,
    ) -> int:
        """Count provisioning templates with filtering (same filters as list)."""
        query = select(func.count(ProvisioningTemplate.id))

        if vendor:
            query = query.where(ProvisioningTemplate.vendor == vendor)
        if connection_type:
            query = query.where(ProvisioningTemplate.connection_type == connection_type)
        if action:
            query = query.where(ProvisioningTemplate.action == action)
        if is_active is not None:
            query = query.where(ProvisioningTemplate.is_active == is_active)

        return db.execute(query).scalar() or 0

    @staticmethod
    def find_template(
        db: Session,
        vendor: NasVendor,
        connection_type: ConnectionType,
        action: ProvisioningAction,
    ) -> ProvisioningTemplate | None:
        """Find the best matching template for given criteria."""
        # First try exact match
        template = db.execute(
            select(ProvisioningTemplate)
            .where(ProvisioningTemplate.vendor == vendor)
            .where(ProvisioningTemplate.connection_type == connection_type)
            .where(ProvisioningTemplate.action == action)
            .where(ProvisioningTemplate.is_active == True)
            .order_by(ProvisioningTemplate.is_default.desc())
            .limit(1)
        ).scalar_one_or_none()

        if template:
            return template

        # Fall back to "other" vendor with same connection type and action
        return db.execute(
            select(ProvisioningTemplate)
            .where(ProvisioningTemplate.vendor == NasVendor.other)
            .where(ProvisioningTemplate.connection_type == connection_type)
            .where(ProvisioningTemplate.action == action)
            .where(ProvisioningTemplate.is_active == True)
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def update(
        db: Session, template_id: str | UUID, payload: ProvisioningTemplateUpdate
    ) -> ProvisioningTemplate:
        """Update a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        data = payload.model_dump(exclude_unset=True)

        # Re-extract placeholders if content changed
        if "template_content" in data and not data.get("placeholders"):
            content = data["template_content"]
            placeholders = list(set(re.findall(r"\{\{(\w+)\}\}", content)))
            data["placeholders"] = placeholders

        for key, value in data.items():
            setattr(template, key, value)

        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def delete(db: Session, template_id: str | UUID) -> None:
        """Delete a provisioning template."""
        template = ProvisioningTemplates.get(db, template_id)
        db.delete(template)
        db.commit()

    @staticmethod
    def render(template: ProvisioningTemplate, variables: dict[str, Any]) -> str:
        """Render a template with given variables."""
        content = template.template_content
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))
        return content


# =============================================================================
# PROVISIONING LOG SERVICE
# =============================================================================

class ProvisioningLogs(ListResponseMixin):
    """Service class for provisioning log operations."""

    @staticmethod
    def create(db: Session, payload: ProvisioningLogCreate) -> ProvisioningLog:
        """Create a new provisioning log entry."""
        data = payload.model_dump(exclude_unset=True)
        log = ProvisioningLog(**data)
        db.add(log)
        db.commit()
        db.refresh(log)
        return log

    @staticmethod
    def get(db: Session, log_id: str | UUID) -> ProvisioningLog:
        """Get a provisioning log by ID."""
        log_id = coerce_uuid(log_id)
        log = db.get(ProvisioningLog, log_id)
        if not log:
            raise HTTPException(status_code=404, detail="Provisioning log not found")
        return log

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: str | None = None,
    ) -> list[ProvisioningLog]:
        """List provisioning logs with filtering."""
        query = select(ProvisioningLog).order_by(ProvisioningLog.created_at.desc())

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: str | None = None,
    ) -> int:
        """Count provisioning logs with filtering (same filters as list)."""
        query = select(func.count(ProvisioningLog.id))

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        return db.execute(query).scalar() or 0

    @staticmethod
    def update_status(
        db: Session,
        log_id: UUID,
        status: str,
        response: str | None = None,
        error: str | None = None,
        execution_time_ms: int | None = None,
    ) -> ProvisioningLog:
        """Update the status of a provisioning log."""
        log = ProvisioningLogs.get(db, log_id)
        log.status = status
        if response:
            log.response_received = response
        if error:
            log.error_message = error
        if execution_time_ms:
            log.execution_time_ms = execution_time_ms
        db.commit()
        db.refresh(log)
        return log


# =============================================================================
# RADIUS PROFILE SERVICE (Enhanced)
# =============================================================================

class RadiusProfiles(ListResponseMixin):
    """Service class for RADIUS profile operations."""

    @staticmethod
    def get(db: Session, profile_id: str | UUID) -> RadiusProfile:
        """Get a RADIUS profile by ID."""
        profile_id = coerce_uuid(profile_id)
        profile = db.get(RadiusProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        return profile

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        vendor: NasVendor | None = None,
        connection_type: ConnectionType | None = None,
        is_active: bool | None = None,
    ) -> list[RadiusProfile]:
        """List RADIUS profiles with filtering."""
        query = select(RadiusProfile).order_by(RadiusProfile.name)

        if vendor:
            query = query.where(RadiusProfile.vendor == vendor)
        if connection_type:
            query = query.where(RadiusProfile.connection_type == connection_type)
        if is_active is not None:
            query = query.where(RadiusProfile.is_active == is_active)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def generate_mikrotik_rate_limit(profile: RadiusProfile) -> str:
        """Generate MikroTik rate-limit string from profile settings."""
        if profile.mikrotik_rate_limit:
            return profile.mikrotik_rate_limit

        if not profile.download_speed or not profile.upload_speed:
            return ""

        # Convert Kbps to format: rx/tx (download/upload in MikroTik terms)
        # MikroTik format: rx-rate[/tx-rate] [rx-burst-rate[/tx-burst-rate] [rx-burst-threshold[/tx-burst-threshold] [rx-burst-time[/tx-burst-time]]]]
        download_k = f"{profile.download_speed}k"
        upload_k = f"{profile.upload_speed}k"

        rate_limit = f"{download_k}/{upload_k}"

        if profile.burst_download and profile.burst_upload:
            burst_down = f"{profile.burst_download}k"
            burst_up = f"{profile.burst_upload}k"
            rate_limit += f" {burst_down}/{burst_up}"

            if profile.burst_threshold:
                threshold = f"{profile.burst_threshold}k"
                rate_limit += f" {threshold}/{threshold}"

                if profile.burst_time:
                    rate_limit += f" {profile.burst_time}s/{profile.burst_time}s"

        return rate_limit


# =============================================================================
# DEVICE PROVISIONER - Execute commands on NAS devices
# =============================================================================

class DeviceProvisioner:
    """
    Execute provisioning commands on NAS devices.

    Supports multiple execution methods:
    - SSH: Direct SSH command execution
    - API: REST API calls (MikroTik REST API, Huawei NCE, etc.)
    - RADIUS CoA: Change of Authorization packets
    """

    @staticmethod
    def provision_user(
        db: Session,
        nas_device_id: UUID,
        action: ProvisioningAction,
        variables: dict[str, Any],
        triggered_by: str = "system",
    ) -> ProvisioningLog:
        """
        Execute a provisioning action on a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            action: The provisioning action to execute
            variables: Variables to substitute in the template
            triggered_by: Who triggered this action

        Returns:
            ProvisioningLog with execution results
        """
        import time

        device = NasDevices.get(db, nas_device_id)

        # Determine connection type
        connection_type = device.default_connection_type or ConnectionType.pppoe

        # Find appropriate template
        template = ProvisioningTemplates.find_template(
            db, device.vendor, connection_type, action
        )

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"No provisioning template found for {device.vendor.value}/{connection_type.value}/{action.value}",
            )

        # Render the command
        command = ProvisioningTemplates.render(template, variables)

        # Create log entry
        log = ProvisioningLogs.create(
            db,
            ProvisioningLogCreate(
                nas_device_id=device.id,
                subscriber_id=variables.get("subscriber_id"),
                template_id=template.id,
                action=action,
                command_sent=command,
                status="running",
                triggered_by=triggered_by,
                request_data=_redact_sensitive(variables),
            ),
        )

        # Execute the command
        start_time = time.time()
        try:
            execution_method = template.execution_method or "ssh"

            if execution_method == "ssh":
                response = DeviceProvisioner._execute_ssh(device, command)
            elif execution_method == "api":
                response = DeviceProvisioner._execute_api(device, command, variables)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported execution method: {execution_method}",
                )

            execution_time = int((time.time() - start_time) * 1000)

            # Update log with success
            ProvisioningLogs.update_status(
                db, log.id, "success", response=response, execution_time_ms=execution_time
            )

            # Handle queue mapping for bandwidth monitoring
            DeviceProvisioner._handle_queue_mapping(
                db, device, action, variables
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db, log.id, "failed", error=str(e), execution_time_ms=execution_time
            )
            raise

        return ProvisioningLogs.get(db, log.id)

    @staticmethod
    def _handle_queue_mapping(
        db: Session,
        device: NasDevice,
        action: ProvisioningAction,
        variables: dict[str, Any],
    ) -> None:
        """
        Handle queue mapping creation/deactivation based on provisioning action.

        This integrates with the bandwidth monitoring system by maintaining
        the mapping between MikroTik queue names and subscriptions.
        """
        from app.services.queue_mapping import queue_mapping

        subscription_id = variables.get("subscription_id")
        if not subscription_id:
            return

        # Convert to UUID if string
        if isinstance(subscription_id, str):
            subscription_id = UUID(subscription_id)

        # Determine queue name from variables or generate from username
        queue_name = variables.get("queue_name")
        if not queue_name:
            username = variables.get("username")
            if username:
                queue_name = f"queue-{username}"
            else:
                queue_name = f"sub-{subscription_id}"

        if action == ProvisioningAction.create_user:
            # Create or update queue mapping for bandwidth monitoring
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

        elif action in (ProvisioningAction.delete_user, ProvisioningAction.suspend_user):
            # Deactivate queue mappings when user is deleted or suspended
            queue_mapping.remove_subscription_mappings(db, subscription_id)

        elif action == ProvisioningAction.unsuspend_user:
            # Re-activate queue mapping when user is unsuspended
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

    @staticmethod
    def _execute_ssh(device: NasDevice, command: str) -> str:
        """Execute command via SSH."""
        import paramiko

        if not device.management_ip and not device.ip_address:
            raise HTTPException(status_code=400, detail="Device has no management IP")

        if not device.ssh_username:
            raise HTTPException(status_code=400, detail="Device has no SSH credentials")

        host = device.management_ip or device.ip_address
        port = device.management_port or 22

        client = paramiko.SSHClient()
        if device.ssh_verify_host_key is False:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            if device.ssh_key:
                # Use SSH key authentication - decrypt key before use
                import io
                decrypted_key = decrypt_credential(device.ssh_key)
                key = paramiko.RSAKey.from_private_key(io.StringIO(decrypted_key))
                client.connect(
                    host, port=port, username=device.ssh_username, pkey=key, timeout=30
                )
            else:
                # Use password authentication - decrypt password before use
                decrypted_password = decrypt_credential(device.ssh_password)
                client.connect(
                    host,
                    port=port,
                    username=device.ssh_username,
                    password=decrypted_password,
                    timeout=30,
                )

            stdin, stdout, stderr = client.exec_command(command, timeout=60)
            output = stdout.read().decode()
            error = stderr.read().decode()

            if error and not output:
                raise Exception(f"SSH error: {error}")

            return output or error

        finally:
            client.close()

    @staticmethod
    def _execute_api(device: NasDevice, command: str, variables: dict) -> str:
        """Execute command via REST API."""
        import requests

        if not device.api_url:
            raise HTTPException(status_code=400, detail="Device has no API URL configured")

        # Build authentication - decrypt credentials before use
        auth = None
        headers = {}

        if device.api_token:
            decrypted_token = decrypt_credential(device.api_token)
            headers["Authorization"] = f"Bearer {decrypted_token}"
        elif device.api_username and device.api_password:
            decrypted_password = decrypt_credential(device.api_password)
            auth = (device.api_username, decrypted_password)

        # For MikroTik REST API, the command is the API path
        url = f"{device.api_url.rstrip('/')}/{command.lstrip('/')}"

        verify_tls = device.api_verify_tls if device.api_verify_tls is not None else False
        response = requests.post(
            url,
            json=variables,
            auth=auth,
            headers=headers,
            timeout=30,
            verify=verify_tls,
        )

        response.raise_for_status()
        return response.text

    @staticmethod
    def backup_config(db: Session, nas_device_id: UUID, triggered_by: str = "system") -> NasConfigBackup:
        """
        Backup configuration from a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            triggered_by: Who triggered this backup

        Returns:
            NasConfigBackup with the configuration content
        """
        device = NasDevices.get(db, nas_device_id)

        # Determine backup method
        backup_method = device.backup_method or ConfigBackupMethod.ssh

        if backup_method == ConfigBackupMethod.ssh:
            config_content = DeviceProvisioner._backup_via_ssh(device)
        elif backup_method == ConfigBackupMethod.api:
            config_content = DeviceProvisioner._backup_via_api(device)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Backup method {backup_method.value} not implemented",
            )

        # Determine config format based on vendor
        config_format = "txt"
        if device.vendor == NasVendor.mikrotik:
            config_format = "rsc"

        # Create backup record
        backup = NasConfigBackups.create(
            db,
            NasConfigBackupCreate(
                nas_device_id=device.id,
                config_content=config_content,
                config_format=config_format,
                backup_method=backup_method,
                is_scheduled=False,
                is_manual=True,
            ),
        )

        return backup

    @staticmethod
    def _backup_via_ssh(device: NasDevice) -> str:
        """Backup configuration via SSH."""
        # Vendor-specific export commands
        if device.vendor == NasVendor.mikrotik:
            command = "/export"
        elif device.vendor == NasVendor.cisco:
            command = "show running-config"
        elif device.vendor == NasVendor.huawei:
            command = "display current-configuration"
        elif device.vendor == NasVendor.juniper:
            command = "show configuration"
        else:
            command = "show running-config"  # Generic fallback

        return DeviceProvisioner._execute_ssh(device, command)

    @staticmethod
    def _backup_via_api(device: NasDevice) -> str:
        """Backup configuration via REST API."""
        if device.vendor == NasVendor.mikrotik:
            # MikroTik REST API export endpoint
            return DeviceProvisioner._execute_api(device, "/rest/export", {})
        else:
            raise HTTPException(
                status_code=400,
                detail=f"API backup not implemented for vendor {device.vendor.value}",
            )


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

# Instantiate service singletons for easy import
nas_devices = NasDevices()
nas_config_backups = NasConfigBackups()
provisioning_templates = ProvisioningTemplates()
provisioning_logs = ProvisioningLogs()
radius_profiles = RadiusProfiles()
device_provisioner = DeviceProvisioner()
