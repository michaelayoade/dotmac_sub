"""Speed profile catalog management services."""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    OntUnit,
    SpeedProfile,
    SpeedProfileDirection,
    SpeedProfileType,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def format_speed(speed_kbps: int) -> str:
    """Format speed in kbps to a human-readable string (Kbps/Mbps/Gbps)."""
    if speed_kbps >= 1_000_000:
        value = speed_kbps / 1_000_000
        return f"{value:g} Gbps"
    if speed_kbps >= 1_000:
        value = speed_kbps / 1_000
        return f"{value:g} Mbps"
    return f"{speed_kbps} Kbps"


class SpeedProfiles:
    """CRUD operations for speed profile catalog entries."""

    @staticmethod
    def list(
        db: Session,
        *,
        direction: str | None = None,
        speed_type: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[SpeedProfile]:
        """List speed profiles with optional filtering."""
        stmt = select(SpeedProfile)
        if is_active is not None:
            stmt = stmt.where(SpeedProfile.is_active.is_(is_active))
        if direction:
            try:
                d = SpeedProfileDirection(direction)
                stmt = stmt.where(SpeedProfile.direction == d)
            except ValueError:
                logger.warning("Invalid direction filter value: %s", direction)
        if speed_type:
            try:
                st = SpeedProfileType(speed_type)
                stmt = stmt.where(SpeedProfile.speed_type == st)
            except ValueError:
                logger.warning("Invalid speed_type filter value: %s", speed_type)
        if search:
            stmt = stmt.where(SpeedProfile.name.ilike(f"%{search}%"))

        col = getattr(SpeedProfile, order_by, SpeedProfile.name)
        stmt = stmt.order_by(col.desc() if order_dir == "desc" else col.asc())
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, profile_id: str) -> SpeedProfile:
        """Get a speed profile by ID or raise 404."""
        profile = db.get(SpeedProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(status_code=404, detail="Speed profile not found")
        return profile

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        direction: SpeedProfileDirection,
        speed_kbps: int,
        speed_type: SpeedProfileType = SpeedProfileType.internet,
        use_prefix_suffix: bool = False,
        is_default: bool = False,
        notes: str | None = None,
    ) -> SpeedProfile:
        """Create a new speed profile catalog entry."""
        profile = SpeedProfile(
            name=name,
            direction=direction,
            speed_kbps=speed_kbps,
            speed_type=speed_type,
            use_prefix_suffix=use_prefix_suffix,
            is_default=is_default,
            notes=notes,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        logger.info("Created speed profile %s: %s", profile.id, profile.name)
        return profile

    @staticmethod
    def update(db: Session, profile_id: str, **kwargs: object) -> SpeedProfile:
        """Update an existing speed profile catalog entry."""
        profile = db.get(SpeedProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(status_code=404, detail="Speed profile not found")
        for key, value in kwargs.items():
            if value is not None and hasattr(profile, key):
                setattr(profile, key, value)
        db.commit()
        db.refresh(profile)
        logger.info("Updated speed profile %s: %s", profile.id, profile.name)
        return profile

    @staticmethod
    def delete(db: Session, profile_id: str) -> None:
        """Soft-delete a speed profile by setting is_active=False."""
        profile = db.get(SpeedProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(status_code=404, detail="Speed profile not found")
        profile.is_active = False
        db.commit()
        logger.info("Soft-deleted speed profile %s", profile_id)

    @staticmethod
    def count(
        db: Session,
        *,
        is_active: bool | None = None,
        direction: str | None = None,
    ) -> int:
        """Count speed profiles with optional filtering."""
        stmt = select(func.count()).select_from(SpeedProfile)
        if is_active is not None:
            stmt = stmt.where(SpeedProfile.is_active.is_(is_active))
        if direction:
            try:
                d = SpeedProfileDirection(direction)
                stmt = stmt.where(SpeedProfile.direction == d)
            except ValueError:
                pass
        return db.scalar(stmt) or 0

    @staticmethod
    def count_by_profile(db: Session) -> dict[str, int]:
        """Return dict of {profile_id: count_of_ont_units_using_it}.

        Counts references from both OntUnit.download_speed_profile_id and
        OntUnit.upload_speed_profile_id columns.
        """
        counts: dict[str, int] = defaultdict(int)

        # Count download profile references
        dl_stmt = (
            select(
                OntUnit.download_speed_profile_id,
                func.count().label("cnt"),
            )
            .where(OntUnit.download_speed_profile_id.isnot(None))
            .group_by(OntUnit.download_speed_profile_id)
        )
        for row in db.execute(dl_stmt).all():
            counts[str(row[0])] += row[1]

        # Count upload profile references
        ul_stmt = (
            select(
                OntUnit.upload_speed_profile_id,
                func.count().label("cnt"),
            )
            .where(OntUnit.upload_speed_profile_id.isnot(None))
            .group_by(OntUnit.upload_speed_profile_id)
        )
        for row in db.execute(ul_stmt).all():
            counts[str(row[0])] += row[1]

        return dict(counts)


speed_profiles = SpeedProfiles()
