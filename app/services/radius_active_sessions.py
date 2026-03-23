"""Live RADIUS session management."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.radius_active_session import RadiusActiveSession

logger = logging.getLogger(__name__)


class RadiusActiveSessionManager:
    """Manages the radius_active_sessions table (live session view)."""

    @staticmethod
    def on_acct_start(
        db: Session,
        *,
        username: str,
        acct_session_id: str,
        nas_device_id: str | None = None,
        subscriber_id: str | None = None,
        subscription_id: str | None = None,
        access_credential_id: str | None = None,
        nas_ip_address: str | None = None,
        framed_ip_address: str | None = None,
        framed_ipv6_prefix: str | None = None,
        calling_station_id: str | None = None,
        nas_port_id: str | None = None,
        session_start: datetime | None = None,
    ) -> RadiusActiveSession:
        """Insert a new active session on Acct-Start."""
        session = RadiusActiveSession(
            username=username,
            acct_session_id=acct_session_id,
            nas_device_id=uuid.UUID(nas_device_id) if nas_device_id else None,
            subscriber_id=uuid.UUID(subscriber_id) if subscriber_id else None,
            subscription_id=uuid.UUID(subscription_id) if subscription_id else None,
            access_credential_id=(
                uuid.UUID(access_credential_id) if access_credential_id else None
            ),
            nas_ip_address=nas_ip_address,
            framed_ip_address=framed_ip_address,
            framed_ipv6_prefix=framed_ipv6_prefix,
            calling_station_id=calling_station_id,
            nas_port_id=nas_port_id,
            session_start=session_start or datetime.now(UTC),
        )
        db.add(session)
        db.flush()
        logger.debug(
            "Active session started: user=%s sid=%s", username, acct_session_id
        )
        return session

    @staticmethod
    def on_acct_interim(
        db: Session,
        *,
        acct_session_id: str,
        nas_device_id: str | None = None,
        session_time: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
        packets_in: int = 0,
        packets_out: int = 0,
        framed_ip_address: str | None = None,
    ) -> int:
        """Update counters on Acct-Interim-Update. Returns rows updated."""
        values: dict = {
            "session_time": session_time,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "packets_in": packets_in,
            "packets_out": packets_out,
            "last_update": datetime.now(UTC),
        }
        if framed_ip_address:
            values["framed_ip_address"] = framed_ip_address
        stmt = update(RadiusActiveSession).where(
            RadiusActiveSession.acct_session_id == acct_session_id
        )
        if nas_device_id:
            stmt = stmt.where(RadiusActiveSession.nas_device_id == nas_device_id)
        stmt = stmt.values(**values)
        result = db.execute(stmt)
        db.flush()
        return result.rowcount  # type: ignore[return-value]

    @staticmethod
    def on_acct_stop(
        db: Session,
        *,
        acct_session_id: str,
        nas_device_id: str | None = None,
    ) -> int:
        """Delete session on Acct-Stop. Returns rows deleted."""
        stmt = delete(RadiusActiveSession).where(
            RadiusActiveSession.acct_session_id == acct_session_id
        )
        if nas_device_id:
            stmt = stmt.where(RadiusActiveSession.nas_device_id == nas_device_id)
        result = db.execute(stmt)
        db.flush()
        return result.rowcount  # type: ignore[return-value]

    @staticmethod
    def list_online(
        db: Session,
        *,
        subscriber_id: str | None = None,
        nas_device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RadiusActiveSession]:
        """List currently active sessions with optional filters."""
        stmt = select(RadiusActiveSession)
        if subscriber_id:
            stmt = stmt.where(RadiusActiveSession.subscriber_id == subscriber_id)
        if nas_device_id:
            stmt = stmt.where(RadiusActiveSession.nas_device_id == nas_device_id)
        stmt = stmt.order_by(RadiusActiveSession.session_start.desc())
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def count_online(
        db: Session,
        *,
        subscriber_id: str | None = None,
        nas_device_id: str | None = None,
    ) -> int:
        """Count active sessions."""
        from sqlalchemy import func

        stmt = select(func.count(RadiusActiveSession.id))
        if subscriber_id:
            stmt = stmt.where(RadiusActiveSession.subscriber_id == subscriber_id)
        if nas_device_id:
            stmt = stmt.where(RadiusActiveSession.nas_device_id == nas_device_id)
        return db.scalar(stmt) or 0

    @staticmethod
    def purge_stale(db: Session, older_than: datetime) -> int:
        """Remove sessions that haven't updated since the given time."""
        stmt = delete(RadiusActiveSession).where(
            RadiusActiveSession.last_update < older_than,
        )
        result = db.execute(stmt)
        db.flush()
        count = result.rowcount or 0
        if count:
            logger.info("Purged %d stale active sessions", count)
        return count


radius_active_sessions = RadiusActiveSessionManager()
