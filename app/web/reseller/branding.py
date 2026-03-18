"""Shared branding context for reseller portal templates."""

import logging

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal

logger = logging.getLogger(__name__)


def reseller_branding_context(_request: Request) -> dict[str, object]:
    """Build branding context (favicon, portal name) for reseller portal templates."""
    db = SessionLocal()
    try:
        from app.services import web_admin as web_admin_service

        stats = web_admin_service.get_sidebar_stats(db)
    except Exception:
        logger.debug("Failed to load sidebar stats for reseller branding context")
        stats = {}

    portal_name = ""
    try:
        from app.services.web_system_company_info import get_company_info

        info = get_company_info(db)
        portal_name = info.get("company_name") or ""
    except Exception:
        logger.debug("Failed to load company name for reseller branding")
    finally:
        db.close()

    favicon = str(stats.get("favicon_url") or "").strip()
    return {
        "sidebar_stats": stats,
        "branding_favicon_url": favicon,
        "portal_name": portal_name,
    }


def get_reseller_templates() -> Jinja2Templates:
    """Return Jinja2Templates configured with reseller branding context."""
    return Jinja2Templates(
        directory="templates",
        context_processors=[reseller_branding_context],
    )
