"""ONT config snapshot capture and retrieval service.

Captures point-in-time TR-069 running configuration from an ONT and
stores it as a snapshot for historical tracking, change detection,
and audit purposes.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.network import OntConfigSnapshot
from app.services.common import coerce_uuid
from app.services.network.ont_action_device import get_running_config

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _safe_uuid(value: str, label: str = "ID") -> uuid.UUID:
    """Validate and coerce a string to UUID, raising 400 on failure."""
    try:
        result = coerce_uuid(value)
        if result is None:
            raise ValueError("None result")
        return result
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")


class OntConfigSnapshots:
    """Manager for ONT config snapshots."""

    @staticmethod
    def capture(
        db: Session,
        ont_id: str,
        *,
        source: str = "tr069",
        label: str | None = None,
    ) -> OntConfigSnapshot:
        """Fetch running config from TR-069 and save as snapshot.

        Args:
            db: Database session.
            ont_id: ONT unit ID.
            source: Snapshot source identifier (tr069, olt_ssh, provision).
            label: Optional operator note for the snapshot.

        Returns:
            Created OntConfigSnapshot.

        Raises:
            HTTPException: If config retrieval or storage fails.
        """
        ont_uuid = _safe_uuid(ont_id, "ONT ID")

        result = get_running_config(db, ont_id)
        if not result.success or not result.data:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to capture config: {result.message}",
            )

        snapshot = OntConfigSnapshot(
            ont_unit_id=ont_uuid,
            source=source,
            label=label,
            device_info=result.data.get("device_info"),
            wan=result.data.get("wan"),
            optical=result.data.get("optical"),
            wifi=result.data.get("wifi"),
        )
        db.add(snapshot)
        try:
            db.commit()
            db.refresh(snapshot)
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("Failed to save config snapshot for ONT %s: %s", ont_id, exc)
            raise HTTPException(
                status_code=500,
                detail="Config was retrieved but could not be saved to database.",
            )
        logger.info(
            "Config snapshot captured for ONT %s (source=%s)", ont_id, source
        )
        return snapshot

    @staticmethod
    def list_for_ont(
        db: Session, ont_id: str, *, limit: int = 20
    ) -> list[OntConfigSnapshot]:
        """List snapshots for an ONT, newest first."""
        ont_uuid = _safe_uuid(ont_id, "ONT ID")
        stmt = (
            select(OntConfigSnapshot)
            .where(OntConfigSnapshot.ont_unit_id == ont_uuid)
            .order_by(OntConfigSnapshot.created_at.desc())
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(
        db: Session, snapshot_id: str, *, ont_id: str | None = None
    ) -> OntConfigSnapshot:
        """Get a single snapshot by ID, optionally verifying ONT ownership."""
        snap_uuid = _safe_uuid(snapshot_id, "Snapshot ID")
        snapshot = db.get(OntConfigSnapshot, snap_uuid)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if ont_id and str(snapshot.ont_unit_id) != str(_safe_uuid(ont_id, "ONT ID")):
            raise HTTPException(status_code=404, detail="Snapshot not found for this ONT")
        return snapshot

    @staticmethod
    def delete(
        db: Session, snapshot_id: str, *, ont_id: str | None = None
    ) -> bool:
        """Delete a snapshot, optionally verifying ONT ownership."""
        snap_uuid = _safe_uuid(snapshot_id, "Snapshot ID")
        snapshot = db.get(OntConfigSnapshot, snap_uuid)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if ont_id and str(snapshot.ont_unit_id) != str(_safe_uuid(ont_id, "ONT ID")):
            raise HTTPException(status_code=404, detail="Snapshot not found for this ONT")
        db.delete(snapshot)
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("Failed to delete config snapshot %s: %s", snapshot_id, exc)
            raise HTTPException(status_code=500, detail="Failed to delete snapshot.")
        logger.info("Config snapshot %s deleted", snapshot_id)
        return True


ont_config_snapshots = OntConfigSnapshots()
