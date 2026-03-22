"""Shared branding context for customer portal templates."""

import logging

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db import SessionLocal

logger = logging.getLogger(__name__)


def customer_branding_context(request: Request) -> dict[str, object]:
    """Build branding context (favicon, sidebar stats, portal name) for customer portal templates."""
    db = SessionLocal()
    try:
        from app.services import web_admin as web_admin_service

        stats = web_admin_service.get_sidebar_stats(db)
    except Exception:
        logger.debug("Failed to load sidebar stats for branding context")
        stats = {}

    portal_name = ""
    try:
        from app.services.web_system_company_info import get_company_info

        info = get_company_info(db)
        portal_name = info.get("company_name") or ""
    except Exception:
        logger.debug("Failed to load company name for portal branding")

    # Check restricted status for global banner
    restricted = False
    try:
        from app.services.customer_portal_context import is_subscriber_restricted
        from app.web.customer.auth import get_current_customer_from_request

        customer = get_current_customer_from_request(request, db)
        if customer:
            subscriber_id = customer.get("subscriber_id")
            if subscriber_id and is_subscriber_restricted(db, subscriber_id):
                restricted = True
    except Exception:
        logger.debug("Failed to check restricted status for branding context")
    finally:
        db.close()

    favicon = str(stats.get("favicon_url") or "").strip()
    return {
        "sidebar_stats": stats,
        "branding_favicon_url": favicon,
        "portal_name": portal_name,
        "restricted": restricted,
    }


def get_customer_templates() -> Jinja2Templates:
    """Return Jinja2Templates configured with customer branding context."""
    return Jinja2Templates(
        directory="templates",
        context_processors=[customer_branding_context],
    )
