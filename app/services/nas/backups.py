"""NAS configuration backup service."""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.catalog import NasConfigBackup
from app.schemas.catalog import NasConfigBackupCreate
from app.services.common import apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class NasConfigBackups(ListResponseMixin):
    """Service class for NAS configuration backup operations."""

    @staticmethod
    def create(db: Session, payload: NasConfigBackupCreate) -> NasConfigBackup:
        """Create a new config backup."""
        from app.services.nas import NasDevices

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
        device.last_backup_at = datetime.now(UTC)

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
        now = now or datetime.now(UTC)
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
        backup = cast(NasConfigBackup | None, db.get(NasConfigBackup, backup_id))
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
        return cast(
            NasConfigBackup | None,
            db.execute(
                select(NasConfigBackup)
                .where(NasConfigBackup.nas_device_id == nas_device_id)
                .where(NasConfigBackup.is_current == True)
            ).scalar_one_or_none(),
        )

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
