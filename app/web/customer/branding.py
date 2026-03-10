"""Shared branding context for customer portal templates."""

import logging

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal

logger = logging.getLogger(__name__)


def customer_branding_context(_request: Request) -> dict[str, object]:
    """Build branding context (favicon, sidebar stats) for customer portal templates."""
    db = SessionLocal()
    try:
        from app.services import web_admin as web_admin_service

        stats = web_admin_service.get_sidebar_stats(db)
    except Exception:
        logger.debug("Failed to load sidebar stats for branding context")
        stats = {}
    finally:
        db.close()

    favicon = str(stats.get("favicon_url") or "").strip()
    return {
        "sidebar_stats": stats,
        "branding_favicon_url": favicon,
    }


def get_customer_templates() -> Jinja2Templates:
    """Return Jinja2Templates configured with customer branding context."""
    return Jinja2Templates(
        directory="templates",
        context_processors=[customer_branding_context],
    )
