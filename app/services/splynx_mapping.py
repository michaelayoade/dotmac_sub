"""Splynx ID mapping service — bidirectional lookup for migration."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

logger = logging.getLogger(__name__)


class SplynxMappingManager:
    """Manages Splynx integer ID ↔ DotMac UUID mappings."""

    @staticmethod
    def register(
        db: Session,
        entity_type: SplynxEntityType,
        splynx_id: int,
        dotmac_id: str | uuid.UUID,
        *,
        metadata: dict | None = None,
    ) -> SplynxIdMapping:
        """Register a new Splynx→DotMac mapping."""
        mapping = SplynxIdMapping(
            entity_type=entity_type,
            splynx_id=splynx_id,
            dotmac_id=uuid.UUID(str(dotmac_id)),
            metadata_=metadata,
        )
        db.add(mapping)
        db.flush()
        return mapping

    @staticmethod
    def register_batch(
        db: Session,
        entity_type: SplynxEntityType,
        pairs: list[tuple[int, str | uuid.UUID]],
        *,
        metadata: dict | None = None,
    ) -> int:
        """Register multiple mappings at once. Returns count created."""
        count = 0
        for splynx_id, dotmac_id in pairs:
            mapping = SplynxIdMapping(
                entity_type=entity_type,
                splynx_id=splynx_id,
                dotmac_id=uuid.UUID(str(dotmac_id)),
                metadata_=metadata,
            )
            db.add(mapping)
            count += 1
        db.flush()
        logger.info("Registered %d %s mappings", count, entity_type.value)
        return count

    @staticmethod
    def lookup_by_splynx(
        db: Session,
        entity_type: SplynxEntityType,
        splynx_id: int,
    ) -> uuid.UUID | None:
        """Look up a DotMac UUID by Splynx integer ID."""
        stmt = select(SplynxIdMapping.dotmac_id).where(
            SplynxIdMapping.entity_type == entity_type,
            SplynxIdMapping.splynx_id == splynx_id,
        )
        return db.scalar(stmt)

    @staticmethod
    def lookup_by_dotmac(
        db: Session,
        entity_type: SplynxEntityType,
        dotmac_id: str | uuid.UUID,
    ) -> int | None:
        """Look up a Splynx integer ID by DotMac UUID."""
        stmt = select(SplynxIdMapping.splynx_id).where(
            SplynxIdMapping.entity_type == entity_type,
            SplynxIdMapping.dotmac_id == uuid.UUID(str(dotmac_id)),
        )
        return db.scalar(stmt)

    @staticmethod
    def exists(
        db: Session,
        entity_type: SplynxEntityType,
        splynx_id: int,
    ) -> bool:
        """Check if a mapping already exists."""
        stmt = select(SplynxIdMapping.id).where(
            SplynxIdMapping.entity_type == entity_type,
            SplynxIdMapping.splynx_id == splynx_id,
        )
        return db.scalar(stmt) is not None

    @staticmethod
    def delete_by_dotmac(
        db: Session,
        entity_type: SplynxEntityType,
        dotmac_id: str | uuid.UUID,
        *,
        flush: bool = True,
    ) -> bool:
        """Delete a mapping by DotMac UUID. Returns True if deleted."""
        stmt = select(SplynxIdMapping).where(
            SplynxIdMapping.entity_type == entity_type,
            SplynxIdMapping.dotmac_id == uuid.UUID(str(dotmac_id)),
        )
        mapping = db.scalar(stmt)
        if mapping:
            db.delete(mapping)
            if flush:
                db.flush()
            return True
        return False

    @staticmethod
    def register_or_update(
        db: Session,
        entity_type: SplynxEntityType,
        splynx_id: int,
        dotmac_id: str | uuid.UUID,
        *,
        metadata: dict | None = None,
        flush: bool = True,
    ) -> SplynxIdMapping:
        """Register or update a Splynx→DotMac mapping."""
        dotmac_uuid = uuid.UUID(str(dotmac_id))
        stmt = select(SplynxIdMapping).where(
            SplynxIdMapping.entity_type == entity_type,
            SplynxIdMapping.dotmac_id == dotmac_uuid,
        )
        mapping = db.scalar(stmt)
        if mapping:
            mapping.splynx_id = splynx_id
            if metadata is not None:
                mapping.metadata_ = metadata
        else:
            mapping = SplynxIdMapping(
                entity_type=entity_type,
                splynx_id=splynx_id,
                dotmac_id=dotmac_uuid,
                metadata_=metadata,
            )
            db.add(mapping)
        if flush:
            db.flush()
        return mapping


splynx_mapping = SplynxMappingManager()
