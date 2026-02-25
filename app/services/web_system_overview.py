"""Service helpers for admin system overview dashboard."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_dashboard_stats(db: Session) -> dict[str, object]:
    """Return system overview KPIs and recent audit events.

    Aggregates: user counts, role count, active API keys, system
    health summary, and the most recent audit events.
    """
    from app.models.audit import AuditEvent
    from app.models.auth import ApiKey
    from app.models.rbac import Role
    from app.services import system_health as system_health_service
    from app.services.web_system_users import get_user_stats

    # User stats (reuse existing helper)
    user_stats = get_user_stats(db)

    # Role count
    role_count = db.scalar(
        select(func.count()).select_from(Role).where(Role.is_active.is_(True))
    ) or 0

    # Active API keys
    api_key_count = db.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.is_active.is_(True))
        .where(ApiKey.revoked_at.is_(None))
    ) or 0

    # System health summary (quick check, no thresholds)
    try:
        health = system_health_service.get_system_health()
        health_status = "ok"
        # Quick heuristic: if disk > 90% or memory > 90%, flag as warning
        disk_pct = health.get("disk", {}).get("used_pct_value")
        mem_pct = health.get("memory", {}).get("used_pct_value")
        if disk_pct is not None and disk_pct >= 90:
            health_status = "critical"
        elif mem_pct is not None and mem_pct >= 90:
            health_status = "critical"
        elif (disk_pct is not None and disk_pct >= 75) or (
            mem_pct is not None and mem_pct >= 75
        ):
            health_status = "warning"
    except Exception:
        logger.warning("Failed to get system health for dashboard")
        health = {}
        health_status = "unknown"

    # Recent audit events (last 10)
    recent_audits = (
        db.query(AuditEvent)
        .order_by(AuditEvent.occurred_at.desc())
        .limit(10)
        .all()
    )

    return {
        "user_stats": user_stats,
        "role_count": role_count,
        "api_key_count": api_key_count,
        "health_status": health_status,
        "health_summary": {
            "uptime": health.get("uptime_display", "--"),
            "cpu_count": health.get("cpu_count", 0),
            "memory_pct": health.get("memory", {}).get("used_pct", "--"),
            "disk_pct": health.get("disk", {}).get("used_pct", "--"),
        },
        "recent_audits": recent_audits,
    }
