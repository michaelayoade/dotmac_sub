"""Event handlers for syslog messages.

Routes parsed syslog events to appropriate actions like triggering autofind.
"""

from __future__ import annotations

import logging

from app.services.autofind_trigger import trigger_autofind_by_ip
from app.services.db_session_adapter import db_session_adapter
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


def _handle_autofind_event(event: OntEvent) -> None:
    """Handle an ONTAUTOFIND syslog event.

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

    logger.info(
        "syslog_autofind_received",
        extra={
            "source_ip": event.source_ip,
            "fsp": event.fsp,
            "serial_number": event.serial_number,
        },
    )

    # Trigger autofind with cooldown check
    with db_session_adapter.read_session() as db:
        result = trigger_autofind_by_ip(
            db=db,
            ip_address=event.source_ip,
            source="syslog",
        )

    if result.triggered:
        logger.info(
            "syslog_autofind_triggered",
            extra={
                "olt_id": result.olt_id,
                "olt_name": result.olt_name,
                "task_id": result.task_id,
                "serial_number": event.serial_number,
            },
        )
    else:
        logger.debug(
            "syslog_autofind_skipped",
            extra={
                "source_ip": event.source_ip,
                "reason": result.reason,
            },
        )
