"""
Queue Mapping Service for bandwidth monitoring.

Manages the mapping between MikroTik queue names and subscriptions,
allowing the poller to associate bandwidth samples with the correct subscriber.
"""
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.bandwidth import QueueMapping

logger = logging.getLogger(__name__)


class QueueMappingService:
    """Service for managing queue-to-subscription mappings."""

    @staticmethod
    def create(
        db: Session,
        nas_device_id: uuid.UUID,
        queue_name: str,
        subscription_id: uuid.UUID,
    ) -> QueueMapping:
        """
        Create a new queue mapping.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device
            queue_name: Queue name on the MikroTik device
            subscription_id: UUID of the associated subscription

        Returns:
            Created QueueMapping instance
        """
        mapping = QueueMapping(
            nas_device_id=nas_device_id,
            queue_name=queue_name,
            subscription_id=subscription_id,
        )
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        logger.info(f"Created queue mapping: {queue_name} -> {subscription_id}")
        return mapping

    @staticmethod
    def get(db: Session, mapping_id: uuid.UUID) -> QueueMapping:
        """Get a queue mapping by ID."""
        mapping = db.get(QueueMapping, mapping_id)
        if not mapping:
            raise HTTPException(status_code=404, detail="Queue mapping not found")
        return mapping

    @staticmethod
    def get_by_queue(
        db: Session,
        nas_device_id: uuid.UUID,
        queue_name: str,
    ) -> QueueMapping | None:
        """
        Get a queue mapping by NAS device and queue name.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device
            queue_name: Queue name to lookup

        Returns:
            QueueMapping if found, None otherwise
        """
        return (
            db.query(QueueMapping)
            .filter(
                QueueMapping.nas_device_id == nas_device_id,
                QueueMapping.queue_name == queue_name,
                QueueMapping.is_active.is_(True),
            )
            .first()
        )

    @staticmethod
    def resolve_subscription(
        db: Session,
        nas_device_id: uuid.UUID,
        queue_name: str,
    ) -> uuid.UUID | None:
        """
        Resolve a queue name to a subscription ID.

        This is the main lookup method used by the poller.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device
            queue_name: Queue name from the MikroTik device

        Returns:
            Subscription UUID if mapping exists, None otherwise
        """
        mapping = QueueMappingService.get_by_queue(db, nas_device_id, queue_name)
        return mapping.subscription_id if mapping else None

    @staticmethod
    def get_device_mappings(
        db: Session,
        nas_device_id: uuid.UUID,
    ) -> list[QueueMapping]:
        """
        Get all active mappings for a NAS device.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device

        Returns:
            List of active QueueMapping instances
        """
        return (
            db.query(QueueMapping)
            .filter(
                QueueMapping.nas_device_id == nas_device_id,
                QueueMapping.is_active.is_(True),
            )
            .all()
        )

    @staticmethod
    def get_device_mapping_dict(
        db: Session,
        nas_device_id: uuid.UUID,
    ) -> dict[str, uuid.UUID]:
        """
        Get all active mappings for a NAS device as a dictionary.

        This is optimized for bulk lookups during polling.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device

        Returns:
            Dict mapping queue_name -> subscription_id
        """
        mappings = QueueMappingService.get_device_mappings(db, nas_device_id)
        return {m.queue_name: m.subscription_id for m in mappings}

    @staticmethod
    def get_subscription_mappings(
        db: Session,
        subscription_id: uuid.UUID,
    ) -> list[QueueMapping]:
        """
        Get all mappings for a subscription.

        Args:
            db: Database session
            subscription_id: UUID of the subscription

        Returns:
            List of QueueMapping instances
        """
        return (
            db.query(QueueMapping)
            .filter(
                QueueMapping.subscription_id == subscription_id,
                QueueMapping.is_active.is_(True),
            )
            .all()
        )

    @staticmethod
    def update(
        db: Session,
        mapping_id: uuid.UUID,
        queue_name: str | None = None,
        subscription_id: uuid.UUID | None = None,
        is_active: bool | None = None,
    ) -> QueueMapping:
        """
        Update a queue mapping.

        Args:
            db: Database session
            mapping_id: UUID of the mapping to update
            queue_name: New queue name (optional)
            subscription_id: New subscription ID (optional)
            is_active: New active status (optional)

        Returns:
            Updated QueueMapping instance
        """
        mapping = QueueMappingService.get(db, mapping_id)

        if queue_name is not None:
            mapping.queue_name = queue_name
        if subscription_id is not None:
            mapping.subscription_id = subscription_id
        if is_active is not None:
            mapping.is_active = is_active

        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def delete(db: Session, mapping_id: uuid.UUID) -> None:
        """
        Delete a queue mapping.

        Args:
            db: Database session
            mapping_id: UUID of the mapping to delete
        """
        mapping = QueueMappingService.get(db, mapping_id)
        db.delete(mapping)
        db.commit()
        logger.info(f"Deleted queue mapping: {mapping_id}")

    @staticmethod
    def deactivate(db: Session, mapping_id: uuid.UUID) -> QueueMapping:
        """
        Deactivate a queue mapping (soft delete).

        Args:
            db: Database session
            mapping_id: UUID of the mapping to deactivate

        Returns:
            Deactivated QueueMapping instance
        """
        return QueueMappingService.update(db, mapping_id, is_active=False)

    @staticmethod
    def sync_from_provisioning(
        db: Session,
        nas_device_id: uuid.UUID,
        queue_name: str,
        subscription_id: uuid.UUID,
    ) -> QueueMapping:
        """
        Create or update a queue mapping during provisioning.

        This method is called when a subscription is provisioned on a NAS device.
        It either creates a new mapping or updates an existing one.

        Args:
            db: Database session
            nas_device_id: UUID of the NAS device
            queue_name: Queue name on the MikroTik device
            subscription_id: UUID of the subscription

        Returns:
            Created or updated QueueMapping instance
        """
        existing = (
            db.query(QueueMapping)
            .filter(
                QueueMapping.nas_device_id == nas_device_id,
                QueueMapping.queue_name == queue_name,
            )
            .first()
        )

        if existing:
            existing.subscription_id = subscription_id
            existing.is_active = True
            db.commit()
            db.refresh(existing)
            logger.info(f"Updated queue mapping: {queue_name} -> {subscription_id}")
            return existing

        return QueueMappingService.create(
            db, nas_device_id, queue_name, subscription_id
        )

    @staticmethod
    def remove_subscription_mappings(
        db: Session,
        subscription_id: uuid.UUID,
    ) -> int:
        """
        Deactivate all mappings for a subscription.

        This method is called when a subscription is suspended or terminated.

        Args:
            db: Database session
            subscription_id: UUID of the subscription

        Returns:
            Number of mappings deactivated
        """
        count = (
            db.query(QueueMapping)
            .filter(
                QueueMapping.subscription_id == subscription_id,
                QueueMapping.is_active.is_(True),
            )
            .update({"is_active": False})
        )
        db.commit()
        logger.info(f"Deactivated {count} mappings for subscription {subscription_id}")
        return count


# Service instance
queue_mapping = QueueMappingService()
