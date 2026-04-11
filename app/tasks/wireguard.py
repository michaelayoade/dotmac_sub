"""WireGuard background tasks.

Provides Celery tasks for:
- Connection log cleanup with retention policy
- Peer status synchronization
- Expired provision token cleanup
"""

import logging
from datetime import UTC, datetime, timedelta

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.models.wireguard import WireGuardConnectionLog, WireGuardPeer
from app.services import wireguard as wg_service
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 90


def _get_wireguard_log_retention_days(db=None) -> int:
    """Get WireGuard log retention days from settings."""
    days = (
        resolve_value(db, SettingDomain.network, "wireguard_log_retention_days")
        if db
        else None
    )
    if days is None:
        return _DEFAULT_RETENTION_DAYS
    try:
        return int(str(days))
    except (TypeError, ValueError):
        return _DEFAULT_RETENTION_DAYS


@celery_app.task(name="app.tasks.wireguard.cleanup_connection_logs")
def cleanup_connection_logs(retention_days: int | None = None) -> dict[str, int]:
    """Delete connection logs older than the retention period.

    Args:
        retention_days: Number of days to retain logs (uses configurable setting if not provided)

    Returns:
        Dict with count of deleted records
    """
    session = SessionLocal()
    try:
        # Use configurable setting if retention_days not explicitly provided
        if retention_days is None:
            retention_days = _get_wireguard_log_retention_days(session)
        deleted_count = wg_service.wg_connection_logs.cleanup_old_logs(
            session, days=retention_days
        )
        return {"deleted_logs": deleted_count}
    except Exception as e:
        logger.error("Error in cleanup_connection_logs: %s", e)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.wireguard.cleanup_expired_tokens")
def cleanup_expired_tokens() -> dict[str, int]:
    """Clear expired provisioning tokens from peers.

    This task removes token hashes for tokens that have expired,
    preventing unnecessary database bloat.

    Returns:
        Dict with count of cleaned tokens
    """
    session = SessionLocal()
    try:
        now = datetime.now(UTC)

        # Find all peers with expired tokens
        expired_peers = (
            session.query(WireGuardPeer)
            .filter(WireGuardPeer.provision_token_hash.isnot(None))
            .filter(WireGuardPeer.provision_token_expires_at < now)
            .all()
        )

        cleaned_count = 0
        for peer in expired_peers:
            peer.provision_token_hash = None
            peer.provision_token_expires_at = None
            cleaned_count += 1

        session.commit()
        return {"cleaned_tokens": cleaned_count}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.wireguard.generate_connection_log_report")
def generate_connection_log_report(
    server_id: str | None = None,
    days: int = 30,
) -> dict:
    """Generate a summary report of connection activity.

    Args:
        server_id: Optional server to report on, or all if None
        days: Number of days to include in report

    Returns:
        Dict with connection statistics
    """
    session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=days)

        query = session.query(WireGuardConnectionLog).filter(
            WireGuardConnectionLog.connected_at >= cutoff
        )

        if server_id:
            # Filter by peers belonging to this server
            peer_ids = (
                session.query(WireGuardPeer.id)
                .filter(WireGuardPeer.server_id == server_id)
                .all()
            )
            peer_id_list = [p[0] for p in peer_ids]
            query = query.filter(WireGuardConnectionLog.peer_id.in_(peer_id_list))

        logs = query.all()

        total_connections = len(logs)
        total_rx = sum(log.rx_bytes for log in logs)
        total_tx = sum(log.tx_bytes for log in logs)

        # Count unique peers
        unique_peers = len(set(log.peer_id for log in logs))

        # Calculate average session duration
        durations = []
        for log in logs:
            if log.disconnected_at:
                duration = (log.disconnected_at - log.connected_at).total_seconds()
                durations.append(duration)

        avg_duration = sum(durations) / len(durations) if durations else 0

        return {
            "period_days": days,
            "total_connections": total_connections,
            "unique_peers": unique_peers,
            "total_rx_bytes": total_rx,
            "total_tx_bytes": total_tx,
            "avg_session_duration_seconds": round(avg_duration, 2),
        }
    except Exception as e:
        logger.error("Error in generate_connection_log_report: %s", e)
        session.rollback()
        raise
    finally:
        session.close()
