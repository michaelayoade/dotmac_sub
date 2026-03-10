"""Service helpers for the system about / version page."""

from __future__ import annotations

import logging
import platform
import sys

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_system_info(db: Session) -> dict:
    """Gather application and environment information."""
    from app.models.subscriber import Subscriber
    from app.services import system_health as system_health_service

    # Python / framework versions
    try:
        import fastapi

        fastapi_version = fastapi.__version__
    except Exception as exc:
        logger.warning("Failed to get FastAPI version: %s", exc)
        fastapi_version = "unknown"

    try:
        import sqlalchemy

        sqla_version = sqlalchemy.__version__
    except Exception as exc:
        logger.warning("Failed to get SQLAlchemy version: %s", exc)
        sqla_version = "unknown"

    try:
        import uvicorn

        uvicorn_version = uvicorn.__version__
    except Exception as exc:
        logger.warning("Failed to get uvicorn version: %s", exc)
        uvicorn_version = "unknown"

    # PostgreSQL version
    try:
        pg_version = db.scalar(text("SHOW server_version")) or "unknown"
    except Exception as exc:
        logger.warning("Failed to get PostgreSQL version: %s", exc)
        pg_version = "unknown"

    # Database size
    try:
        db_size = db.scalar(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        ) or "unknown"
    except Exception as exc:
        logger.warning("Failed to get database size: %s", exc)
        db_size = "unknown"

    # Active connections
    try:
        active_connections = db.scalar(
            text("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
        ) or 0
    except Exception as exc:
        logger.warning("Failed to get active connections: %s", exc)
        active_connections = 0

    # Subscriber count
    try:
        subscriber_count = db.scalar(
            select(func.count()).select_from(Subscriber)
        ) or 0
    except Exception as exc:
        logger.warning("Failed to get subscriber count: %s", exc)
        subscriber_count = 0

    # Redis check
    try:
        from app.config import settings as app_settings

        redis_url = getattr(app_settings, "REDIS_URL", None) or "not configured"
        redis_status = "configured"
    except Exception as exc:
        logger.warning("Failed to get Redis config: %s", exc)
        redis_url = "unknown"
        redis_status = "unknown"

    # System health
    try:
        health = system_health_service.get_system_health()
    except Exception as exc:
        logger.warning("Failed to get system health: %s", exc)
        health = {}

    return {
        "app_version": "1.0.0",
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "fastapi_version": fastapi_version,
        "sqlalchemy_version": sqla_version,
        "uvicorn_version": uvicorn_version,
        "pg_version": pg_version,
        "db_size": db_size,
        "active_connections": active_connections,
        "subscriber_count": subscriber_count,
        "redis_url": redis_url,
        "redis_status": redis_status,
        "uptime": health.get("uptime_display", "--"),
        "cpu_count": health.get("cpu_count", 0),
        "memory_pct": health.get("memory", {}).get("used_pct", "--"),
        "disk_pct": health.get("disk", {}).get("used_pct", "--"),
    }
