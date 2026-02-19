"""WireGuard background tasks.

Provides Celery tasks for:
- Connection log cleanup with retention policy
- Peer status synchronization
- Expired provision token cleanup
"""

from datetime import datetime, timedelta, timezone

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.models.wireguard import WireGuardConnectionLog, WireGuardPeer
from app.services import wireguard as wg_service
from app.services.settings_spec import resolve_value

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
        now = datetime.now(timezone.utc)

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


@celery_app.task(name="app.tasks.wireguard.sync_peer_stats")
def sync_peer_stats(peer_id: str | None = None) -> dict[str, int]:
    """Sync peer connection statistics from WireGuard interface.

    This is a placeholder task that would normally connect to the
    WireGuard server and pull handshake times and traffic stats.

    In a full implementation, this would use the WireGuard API or
    parse `wg show` output to update peer stats.

    Args:
        peer_id: Optional specific peer to sync, or all if None

    Returns:
        Dict with count of synced peers
    """
    session = SessionLocal()
    try:
        # This is a placeholder - actual implementation would:
        # 1. Connect to WireGuard server (via SSH or API)
        # 2. Run `wg show <interface>` or use wgctrl library
        # 3. Parse output for each peer's:
        #    - latest handshake time
        #    - transfer rx/tx bytes
        #    - endpoint IP
        # 4. Update the database

        synced_count = 0

        if peer_id:
            peers = [wg_service.wg_peers.get(session, peer_id)]
        else:
            peers = wg_service.wg_peers.list(session, limit=1000)

        # Placeholder: In production, fetch actual stats here
        # For now, just count peers that could be synced
        synced_count = len(peers)

        return {"synced_peers": synced_count}
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
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

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
    finally:
        session.close()
