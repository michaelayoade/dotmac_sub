"""Event handlers for syslog messages.

Routes parsed syslog events to appropriate actions like autofind persistence.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.models.network import OLTDevice
from app.services.db_session_adapter import db_session_adapter
from app.services.web_network_ont_autofind import upsert_autofind_from_syslog
from app.syslog.parsers import OntEvent, OntEventType

logger = logging.getLogger(__name__)


def handle_ont_event(event: OntEvent) -> None:
    """Handle an ONT-related syslog event.

    Args:
        event: Parsed ONT event from syslog
    """
    if event.event_type == OntEventType.autofind:
        _handle_autofind_event(event)
    elif event.event_type in (
        OntEventType.online,
        OntEventType.offline,
        OntEventType.dying_gasp,
        OntEventType.los,
    ):
        # Log other events but don't take action yet
        logger.debug(
            "syslog_ont_event",
            extra={
                "event_type": event.event_type.value,
                "fsp": event.fsp,
                "ont_id": event.ont_id,
                "serial_number": event.serial_number,
                "source_ip": event.source_ip,
            },
        )


def _find_olt_by_ip(ip_address: str) -> str | None:
    """Find OLT ID by management IP address."""
    with db_session_adapter.read_session() as db:
        olt = db.scalars(
            select(OLTDevice).where(
                OLTDevice.mgmt_ip == ip_address,
                OLTDevice.is_active.is_(True),
            )
        ).first()
        return str(olt.id) if olt else None


def _handle_autofind_event(event: OntEvent) -> None:
    """Handle an ONTAUTOFIND syslog event.

    Directly persists the autofind candidate to the database.
    No SSH polling, no Celery tasks, no cooldown - just immediate persistence.

    Args:
        event: Autofind event with F/S/P and serial number
    """
    if not event.source_ip:
        logger.warning(
            "syslog_autofind_no_source_ip",
            extra={
                "fsp": event.fsp,
                "serial_number": event.serial_number,
            },
        )
        return

    if not event.serial_number:
        logger.warning(
            "syslog_autofind_no_serial",
            extra={
                "fsp": event.fsp,
                "source_ip": event.source_ip,
            },
        )
        return

    # Resolve OLT by source IP
    olt_id = _find_olt_by_ip(event.source_ip)
    if not olt_id:
        logger.debug(
            "syslog_autofind_olt_not_found",
            extra={
                "source_ip": event.source_ip,
                "fsp": event.fsp,
                "serial_number": event.serial_number,
            },
        )
        return

    logger.info(
        "syslog_autofind_received",
        extra={
            "source_ip": event.source_ip,
            "olt_id": olt_id,
            "fsp": event.fsp,
            "serial_number": event.serial_number,
        },
    )

    # Persist directly to database
    with db_session_adapter.session() as db:
        ok = upsert_autofind_from_syslog(
            db,
            olt_id=olt_id,
            fsp=event.fsp,
            serial_number=event.serial_number,
        )

    if ok:
        logger.info(
            "syslog_autofind_persisted",
            extra={
                "olt_id": olt_id,
                "fsp": event.fsp,
                "serial_number": event.serial_number,
            },
        )
    else:
        logger.warning(
            "syslog_autofind_persist_failed",
            extra={
                "olt_id": olt_id,
                "fsp": event.fsp,
                "serial_number": event.serial_number,
            },
        )
