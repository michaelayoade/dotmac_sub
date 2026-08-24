"""Support-safe RADIUS observation projection owned by the network domain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.network import radius_sessions, subscriber_ont_adapter


class SupportMonitoringStatus(StrEnum):
    available = "available"
    no_data = "no_data"
    unavailable = "unavailable"
    unauthorized = "unauthorized"
    ambiguous = "ambiguous"


@dataclass(frozen=True, slots=True)
class SupportMonitoringQuery:
    subscriber_id: UUID
    authorized: bool


@dataclass(frozen=True, slots=True)
class RadiusObservation:
    state: str
    active_session_count: int
    framed_ip_addresses: tuple[str, ...]
    observed_at: datetime | None
    source: str = "network.radius_sessions"


@dataclass(frozen=True, slots=True)
class OntObservation:
    """Effective ONT state, kept separate from RADIUS evidence."""

    reference: str
    serial_number: str | None
    effective_state: str
    source: str = "network.ont_status"


@dataclass(frozen=True, slots=True)
class SupportMonitoringProjection:
    status: SupportMonitoringStatus
    radius: RadiusObservation | None = None
    onts: tuple[OntObservation, ...] = ()


def project_support_monitoring(
    db: Session, query: SupportMonitoringQuery
) -> SupportMonitoringProjection:
    """Return facts, never an outage/LOS/CPE diagnosis or an ORM object."""
    if not query.authorized:
        return SupportMonitoringProjection(SupportMonitoringStatus.unauthorized)
    try:
        resolution = radius_sessions.resolve_subscriber_radius_sessions(
            db, query.subscriber_id
        )
    except Exception:
        return SupportMonitoringProjection(SupportMonitoringStatus.unavailable)
    sessions = resolution.sessions
    try:
        linked_onts = subscriber_ont_adapter.get_subscriber_onts(
            db, str(query.subscriber_id)
        )
    except Exception:
        # The RADIUS evidence remains usable; ONT is unavailable rather than
        # being invented as an offline observation.
        linked_onts = []
    onts = tuple(
        OntObservation(
            reference=row.ont_id,
            serial_number=row.serial_number,
            effective_state=row.olt_status,
        )
        for row in linked_onts
    )
    if not sessions and not onts:
        # Absence of an active-session row is no live observation.  It must
        # never be promoted to a customer/network offline diagnosis.
        return SupportMonitoringProjection(SupportMonitoringStatus.no_data)
    if not sessions:
        return SupportMonitoringProjection(
            SupportMonitoringStatus.available,
            radius=None,
            onts=onts,
        )
    return SupportMonitoringProjection(
        SupportMonitoringStatus.available,
        RadiusObservation(
            "online" if resolution.is_online else "offline",
            len(sessions),
            tuple(
                str(value)
                for row in sessions
                if (value := getattr(row, "framed_ip_address", None))
            ),
            getattr(resolution.primary_session, "last_update", None),
        ),
        onts,
    )
