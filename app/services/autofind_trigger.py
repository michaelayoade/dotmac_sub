"""Autofind trigger service with deduplication.

Handles triggering ONT autofind scans from syslog events and webhooks
with Redis-based cooldown to prevent redundant scans.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services.queue_adapter import enqueue_task
from app.services.redis_client import safe_get, safe_set

logger = logging.getLogger(__name__)

# Configuration
AUTOFIND_COOLDOWN_SECONDS = int(os.getenv("AUTOFIND_COOLDOWN_SECONDS", "30"))
AUTOFIND_REDIS_PREFIX = "autofind:cooldown:"


@dataclass
class AutofindTriggerResult:
    """Result of an autofind trigger attempt."""

    triggered: bool
    olt_id: str | None = None
    olt_name: str | None = None
    task_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "task_id": self.task_id,
            "reason": self.reason,
        }


def _cooldown_key(olt_id: str) -> str:
    """Generate Redis key for OLT autofind cooldown."""
    return f"{AUTOFIND_REDIS_PREFIX}{olt_id}"


def is_in_cooldown(olt_id: str) -> bool:
    """Check if an OLT is in autofind cooldown period.

    Args:
        olt_id: UUID string of the OLT

    Returns:
        True if OLT is in cooldown, False otherwise
    """
    key = _cooldown_key(olt_id)
    return safe_get(key) is not None


def set_cooldown(olt_id: str, seconds: int | None = None) -> bool:
    """Set autofind cooldown for an OLT.

    Args:
        olt_id: UUID string of the OLT
        seconds: Cooldown duration (defaults to AUTOFIND_COOLDOWN_SECONDS)

    Returns:
        True if set successfully, False otherwise
    """
    key = _cooldown_key(olt_id)
    ttl = seconds or AUTOFIND_COOLDOWN_SECONDS
    return safe_set(key, "1", ttl=ttl)


def find_olt_by_ip(db: Session, ip_address: str) -> OLTDevice | None:
    """Find an OLT device by management IP address.

    Args:
        db: Database session
        ip_address: Management IP address

    Returns:
        OLTDevice if found, None otherwise
    """
    stmt = select(OLTDevice).where(
        OLTDevice.mgmt_ip == ip_address,
        OLTDevice.is_active.is_(True),
    )
    return db.scalars(stmt).first()


def find_olt_by_name(db: Session, name: str) -> OLTDevice | None:
    """Find an OLT device by name (case-insensitive).

    Args:
        db: Database session
        name: OLT name

    Returns:
        OLTDevice if found, None otherwise
    """
    stmt = select(OLTDevice).where(
        OLTDevice.name.ilike(name),
        OLTDevice.is_active.is_(True),
    )
    return db.scalars(stmt).first()


def find_olt_by_id(db: Session, olt_id: str | UUID) -> OLTDevice | None:
    """Find an OLT device by ID.

    Args:
        db: Database session
        olt_id: OLT UUID (string or UUID object)

    Returns:
        OLTDevice if found, None otherwise
    """
    if isinstance(olt_id, str):
        try:
            olt_id = UUID(olt_id)
        except ValueError:
            return None
    return db.get(OLTDevice, olt_id)


def trigger_autofind(
    olt_id: str,
    olt_name: str | None = None,
    source: str = "unknown",
    force: bool = False,
) -> AutofindTriggerResult:
    """Trigger an autofind scan for an OLT.

    Args:
        olt_id: UUID string of the OLT
        olt_name: OLT name for logging (optional)
        source: Source of the trigger (e.g., "syslog", "webhook")
        force: If True, bypass cooldown check

    Returns:
        AutofindTriggerResult with trigger status
    """
    display_name = olt_name or olt_id[:8]

    # Check cooldown unless forced
    if not force and is_in_cooldown(olt_id):
        logger.debug(
            "autofind_trigger_cooldown",
            extra={
                "olt_id": olt_id,
                "olt_name": olt_name,
                "source": source,
            },
        )
        return AutofindTriggerResult(
            triggered=False,
            olt_id=olt_id,
            olt_name=olt_name,
            reason=f"OLT {display_name} is in cooldown period",
        )

    # Set cooldown immediately to prevent concurrent triggers
    set_cooldown(olt_id)

    # Queue the autofind task
    dispatch = enqueue_task(
        "app.tasks.ont_autofind.autofind_single_olt",
        args=[olt_id],
        correlation_id=f"autofind:{source}:{olt_id}",
        source=source,
    )

    if not dispatch.queued:
        logger.warning(
            "autofind_trigger_queue_failed",
            extra={
                "olt_id": olt_id,
                "olt_name": olt_name,
                "source": source,
                "error": dispatch.error,
            },
        )
        return AutofindTriggerResult(
            triggered=False,
            olt_id=olt_id,
            olt_name=olt_name,
            reason=f"Failed to queue task: {dispatch.error}",
        )

    logger.info(
        "autofind_trigger_queued",
        extra={
            "olt_id": olt_id,
            "olt_name": olt_name,
            "source": source,
            "task_id": dispatch.task_id,
        },
    )

    return AutofindTriggerResult(
        triggered=True,
        olt_id=olt_id,
        olt_name=olt_name,
        task_id=dispatch.task_id,
    )


def trigger_autofind_by_ip(
    db: Session,
    ip_address: str,
    source: str = "syslog",
    force: bool = False,
) -> AutofindTriggerResult:
    """Trigger autofind for an OLT identified by IP address.

    Args:
        db: Database session
        ip_address: Management IP of the OLT
        source: Source of the trigger
        force: If True, bypass cooldown check

    Returns:
        AutofindTriggerResult with trigger status
    """
    olt = find_olt_by_ip(db, ip_address)
    if not olt:
        logger.debug(
            "autofind_trigger_olt_not_found",
            extra={
                "ip_address": ip_address,
                "source": source,
            },
        )
        return AutofindTriggerResult(
            triggered=False,
            reason=f"No active OLT found with IP {ip_address}",
        )

    return trigger_autofind(
        olt_id=str(olt.id),
        olt_name=olt.name,
        source=source,
        force=force,
    )


def trigger_autofind_by_identifier(
    db: Session,
    identifier: str,
    source: str = "webhook",
    force: bool = False,
) -> AutofindTriggerResult:
    """Trigger autofind for an OLT identified by ID, IP, or name.

    Tries to match in order: UUID, IP address, name.

    Args:
        db: Database session
        identifier: OLT identifier (UUID, IP, or name)
        source: Source of the trigger
        force: If True, bypass cooldown check

    Returns:
        AutofindTriggerResult with trigger status
    """
    # Try as UUID first
    olt = find_olt_by_id(db, identifier)
    if olt:
        return trigger_autofind(
            olt_id=str(olt.id),
            olt_name=olt.name,
            source=source,
            force=force,
        )

    # Try as IP address
    olt = find_olt_by_ip(db, identifier)
    if olt:
        return trigger_autofind(
            olt_id=str(olt.id),
            olt_name=olt.name,
            source=source,
            force=force,
        )

    # Try as name
    olt = find_olt_by_name(db, identifier)
    if olt:
        return trigger_autofind(
            olt_id=str(olt.id),
            olt_name=olt.name,
            source=source,
            force=force,
        )

    return AutofindTriggerResult(
        triggered=False,
        reason=f"No active OLT found matching '{identifier}'",
    )
