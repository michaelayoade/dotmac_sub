"""Service helpers for ONT TR-069 detail web routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.genieacs_service import genieacs_service

logger = logging.getLogger(__name__)


def _get_linked_tr069_device(db: Session, ont_id: str):
    """Get the linked Tr069CpeDevice for an ONT."""
    from app.models.tr069 import Tr069CpeDevice

    return db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont_id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .order_by(Tr069CpeDevice.updated_at.desc())
        .limit(1)
    ).first()


def _determine_tr069_status(linked_device, last_inform_at: datetime | None) -> str:
    """Determine TR-069 online/offline status based on last inform time."""
    if not linked_device or not last_inform_at:
        return "offline"
    # Consider online if last inform was within 24 hours
    threshold = datetime.now(UTC) - timedelta(hours=24)
    # Ensure comparison works with timezone-aware datetimes
    if last_inform_at.tzinfo is None:
        last_inform_at = last_inform_at.replace(tzinfo=UTC)
    return "online" if last_inform_at > threshold else "offline"


def tr069_tab_data(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the TR-069 tab partial template.

    Args:
        db: Database session.
        ont_id: OntUnit ID.

    Returns:
        Template context dict with TR-069 summary data.
    """
    summary = genieacs_service.get_device_summary(
        db,
        ont_id,
        persist_observed_runtime=True,
    )

    # Get linked TR-069 device for connection request URL
    linked_device = _get_linked_tr069_device(db, ont_id)

    # Extract last inform time from recent_informs or linked device
    last_inform_at = None
    if summary.recent_informs:
        # Get the most recent inform session's started_at
        first_session = summary.recent_informs[0]
        last_inform_at = getattr(first_session, "started_at", None)
    if not last_inform_at and linked_device:
        last_inform_at = getattr(linked_device, "last_inform_at", None)

    # Determine online/offline status
    tr069_status = _determine_tr069_status(linked_device, last_inform_at)

    # Extract device info from summary.system
    system_info = summary.system or {}
    acs_device_oui = system_info.get("OUI") or system_info.get("Manufacturer OUI")
    acs_product_class = system_info.get("Product Class") or system_info.get("Model")
    acs_serial_number = system_info.get("Serial Number")
    acs_software_version = system_info.get("Software Version") or system_info.get(
        "Firmware Version"
    )

    # Get connection request URL from linked device
    connection_request_url = None
    if linked_device:
        connection_request_url = getattr(linked_device, "connection_request_url", None)

    # Get ACS server info
    acs_server_name = None
    acs_server_url = None
    if linked_device and hasattr(linked_device, "acs_server"):
        acs_server = linked_device.acs_server
        if acs_server:
            acs_server_name = getattr(acs_server, "name", None)
            acs_server_url = getattr(acs_server, "url", None)

    return {
        "tr069": summary,
        "tr069_available": summary.available,
        "tr069_status": tr069_status,
        "last_inform_at": last_inform_at,
        "acs_device_oui": acs_device_oui,
        "acs_product_class": acs_product_class,
        "acs_serial_number": acs_serial_number,
        "acs_software_version": acs_software_version,
        "connection_request_url": connection_request_url,
        "ont_id": ont_id,
        "acs_server_name": acs_server_name,
        "acs_server_url": acs_server_url,
    }
